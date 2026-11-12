"""install / uninstall / doctor against a temp HOME (`--home`) and a stubbed Orca."""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from orche import __version__ as VERSION
from orche import headless, oauth, registry, shell
from orche.paths import PLUGIN_ROOT
from orche.install import doctor, launchd
from test_registry import load_cli

REAL_RUNNER = shell.RUNNER
SCRIPT = PLUGIN_ROOT / "templates" / "statusline.sh"
# One per hooks.json handler: dispatch_relay serves Stop and StopFailure.
HOOK_NAMES = ("dispatch_relay", "dispatch_relay", "report_inject", "env_read_gate", "scope_gate", "session_start", "session_end",
              "inbox_wake")
PLUGIN_CMDS = [f'"{PLUGIN_ROOT}/hooks/run.sh" {name}' for name in HOOK_NAMES]

STATUSLINE = """#!/bin/bash
input=$(cat)
current_dir_path=$(echo "$input" | grep -o '"current_dir":"[^"]*"' | sed 's/"current_dir":"//;s/"$//')
current_dir=$(basename "$current_dir_path")
output="dir:${current_dir}"
printf "%s\\n" "$output\""""  # no trailing newline, like the real script

def tree_digest(root) -> dict:
    """Every path under `root` but the fake claude's folder (it logs each call)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if rel.split(os.sep)[0] == "fake-claude":
                continue
            if p.is_symlink():
                out[rel] = "link:" + os.readlink(p)
            elif p.is_file():
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                out[rel] = "dir"
    return out


def ok_runner(results):
    """A shell.RUNNER stub answering orca calls from `results` ({"status": ..., "repo list": ...})."""
    calls = []

    def run(argv, *, timeout, input, **kw):
        if argv[0] != "orca":
            return REAL_RUNNER(argv, timeout=timeout, input=input, **kw)
        calls.append(argv)
        key = argv[1] if argv[1] == "status" else " ".join(argv[1:3])
        res = results.get(key, {})
        if res is None:
            return shell.Result(1, json.dumps({"ok": False, "error": {"code": "unreachable",
                                                                      "message": "Orca is not running"}}), "", argv)
        return shell.Result(0, json.dumps({"ok": True, "result": res}), "", argv)
    return run, calls


class InstallCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.h = self.home / "h"
        c = self.h / ".claude"
        (c / "agents").mkdir(parents=True)
        self.settings = {
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5", "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": "8",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-5-5"},
            "model": "fable",
            "hooks": {"PreToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "/x/gate.py"}]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "/x/stop", "timeout": 5}]}]},
            "statusLine": {"type": "command", "command": f"bash {c}/statusline-command.sh"},
            "enabledPlugins": {"superpowers@official": True},
            "extraKnownMarketplaces": {"m": {"source": {"source": "github", "repo": "a/b"}}},
            "effortLevel": "high", "tui": {"x": 1},
            "skipDangerousModePermissionPrompt": True, "skipWorkflowUsageWarning": True,
        }
        (c / "settings.json").write_text(json.dumps(self.settings, indent=2, ensure_ascii=False) + "\n")
        (c / "statusline-command.sh").write_text(STATUSLINE)
        (self.h / "mine").mkdir()
        (self.h / "mine" / "deep-reasoner.md").write_text("---\nname: deep-reasoner\n---\n")
        (c / "agents" / "hr.md").write_text("my own hr\n")  # pre-existing regular file
        (c / "agents" / "deep-reasoner.md").symlink_to(self.h / "mine" / "deep-reasoner.md")
        self.c = c
        # doctor probes `claude --version/--help` and `claude plugin list --json`: a fake script, never the
        # real binary (the plugin is not installed until a test says so).
        self.claude = support.write_fake_claude(self.home / "fake-claude")
        (self.home / "config.json").write_text(json.dumps({"claude_bin": str(self.claude)}))
        shell.RUNNER, self.calls = ok_runner({"status": {"running": True}})
        self.addCleanup(setattr, shell, "RUNNER", REAL_RUNNER)

    def plugin_on(self):
        """orche@orche installed and enabled, as `claude plugin list --json` reports it."""
        support.plugin_installed(self.claude.parent)

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def install(self, *extra):
        code, out, err = self.orche("install", "--home", str(self.h), *extra)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def read_settings(self):
        return json.loads((self.c / "settings.json").read_text())

    def commands(self, data, event=None):
        return [h["command"] for ev, entries in data.get("hooks", {}).items() if event in (None, ev)
                for e in entries for h in e["hooks"]]

    def backups(self):
        return sorted(str(p.relative_to(self.h)) for p in self.h.rglob("*.bak-*"))


class InstallTest(InstallCase):
    def test_dry_run_prints_every_change_and_touches_nothing(self):
        before = tree_digest(self.home)
        code, out, err = self.orche("--dry-run", "install", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(tree_digest(self.home), before)
        for expected in (f"settings: would patch {self.c / 'settings.json'}",
                         "settings: would set (cleanupPeriodDays (absent) -> 3650)",
                         "settings: would set (env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (absent) -> 50)",
                         f"settings: would backup {self.c / 'settings.json'}",
                         f"statusline: would write {self.home / 'statusline.sh'}",
                         f"agents: would symlink {self.c / 'agents' / 'exec.md'}",
                         f"agents: skip {self.c / 'agents' / 'hr.md'} (existing regular file; left untouched)",
                         "hooks: settings mode (the orche plugin is not enabled for new sessions, so settings.json "
                         "registers Orche's hooks)",
                         "/plugin install orche@orche"):
            self.assertIn(expected, out)
        self.assertEqual(out.splitlines()[-1], "  /plugin install orche@orche")

    def test_no_plugin_hint_drops_the_two_plugin_lines(self):
        out = self.install("--no-jev")
        self.assertIn(f"  /plugin marketplace add {PLUGIN_ROOT}", out)
        self.assertIn("  /plugin install orche@orche", out)
        out = self.install("--no-jev", "--no-plugin-hint")
        self.assertNotIn("/plugin", out)
        self.assertNotIn("Install the Orche hooks as a Claude Code plugin", out)
        self.assertEqual(out.splitlines()[-1], "jev: skipped (--no-jev); add later with `orche jev setup`")
        code, out, err = self.orche("--json", "install", "--home", str(self.h), "--no-jev", "--no-plugin-hint")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(json.loads(out)["instructions"], [])

    def test_install_backs_up_merges_and_is_idempotent(self):
        self.plugin_on()
        out = self.install()
        self.assertEqual(out.splitlines()[-1], "hooks: ok (on for new sessions: the orche plugin is enabled)")
        data = self.read_settings()
        for key, value in self.settings.items():
            if key not in ("env", "hooks", "statusLine"):
                self.assertEqual(data[key], value, key)
        self.assertEqual(data["cleanupPeriodDays"], 3650)
        self.assertEqual(data["env"], {**self.settings["env"], "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
        self.assertEqual(data["hooks"], self.settings["hooks"])  # plugin mode
        self.assertIn("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL left untouched", out)
        # Status line: the user's script untouched, kept as the inner command under Orche's own script.
        self.assertEqual((self.c / "statusline-command.sh").read_text(), STATUSLINE)
        self.assertEqual(data["statusLine"], {"type": "command", "command": f"bash {self.home / 'statusline.sh'}"})
        self.assertEqual((self.home / "statusline.inner").read_text(), f"bash {self.c}/statusline-command.sh\n")
        self.assertEqual((self.home / "statusline.sh").read_text(), SCRIPT.read_text())
        # Agents: ours linked, foreign files and links untouched and reported.
        self.assertEqual(os.readlink(self.c / "agents" / "exec.md"), str(PLUGIN_ROOT / "agents" / "exec.md"))
        self.assertEqual((self.c / "agents" / "hr.md").read_text(), "my own hr\n")
        self.assertEqual(os.readlink(self.c / "agents" / "deep-reasoner.md"),
                         str(self.h / "mine" / "deep-reasoner.md"))
        self.assertIn("existing symlink to", out)
        # Backups next to each patched original.
        names = {Path(b).name.split(".bak-")[0] for b in self.backups()}
        self.assertEqual(names, {"settings.json"})
        self.assertEqual(json.loads(sorted(self.c.glob("settings.json.bak-*"))[0].read_text()), self.settings)
        self.assertTrue(self.home.joinpath("status").is_dir())  # ORCHE_HOME dirs
        # Second run: byte-identical files, no new backups.
        snap, backups = tree_digest(self.h), self.backups()
        out2 = self.install()
        self.assertEqual(tree_digest(self.h), snap)
        self.assertEqual(self.backups(), backups)
        self.assertNotIn("would", out2)
        self.assertIn(f"settings: ok {self.c / 'settings.json'} (unchanged)", out2)

    def test_settings_mode_registers_eight_hooks_once(self):
        self.install("--mode", "settings")
        out = self.install("--mode", "settings")
        cmds = self.commands(self.read_settings())
        for cmd in PLUGIN_CMDS:
            self.assertEqual(cmds.count(cmd), PLUGIN_CMDS.count(cmd), cmd)
        self.assertEqual(len(cmds), 2 + 8)
        # The wake hook keeps its asyncRewake flag: without it a wake file only adds context, no turn.
        self.assertEqual(self.read_settings()["hooks"]["FileChanged"],
                         [{"hooks": [{"type": "command", "command": f'"{PLUGIN_ROOT}/hooks/run.sh" inbox_wake',
                                      "asyncRewake": True}]}])
        pre = self.read_settings()["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], self.settings["hooks"]["PreToolUse"][0])  # existing entries keep their order
        self.assertEqual([e.get("matcher") for e in pre[1:]], ["Read|Bash|Grep|Edit|Write|Monitor|Agent|Task",
                                                               "Edit|Write|NotebookEdit|MultiEdit"])
        self.assertNotIn("/plugin install", out)
        self.assertNotIn("careful", out)

    # -- R4: switching install modes never leaves two Stop hooks --------------------------------
    def test_plugin_install_after_settings_install_removes_settings_hooks(self):
        self.install("--mode", "settings")
        self.assertEqual(self.state()["mode"], "settings")
        backups = len(self.backups())
        self.plugin_on()
        out = self.install()   # the existing install's mode (settings), but the plugin is on: it wins
        self.assertEqual(self.read_settings()["hooks"], self.settings["hooks"])
        self.assertIn("hooks: plugin mode (the orche plugin is enabled and registers Orche's hooks; settings.json "
                      "entries would run each one twice)", out)
        self.assertIn(f"settings: remove (hook Stop {PLUGIN_CMDS[0]} (plugin mode: the plugin registers "
                      "the hooks))", out)
        self.assertEqual(len(self.backups()), backups + 1)
        self.assertEqual(self.state()["added_hooks"], [])
        self.assertIn(self.state()["mode"], ("plugin", "settings"))
        self.assertIn(f"settings: ok {self.c / 'settings.json'} (unchanged)", self.install())
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.read_settings(), self.settings)

    def test_careful_hook_recorded_by_an_older_install_is_left_alone(self):
        script = self.careful_script()
        cmd = f'bash "{script}"'
        self.settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]})
        (self.c / "settings.json").write_text(json.dumps(self.settings, indent=2) + "\n")
        (self.home / "install-state.json").write_text(json.dumps(
            {"added_hooks": [{"event": "PreToolUse", "command": cmd}], "careful_command": cmd}))
        for mode in ("settings", "plugin", "settings"):
            self.install("--mode", mode)
            self.assertEqual(sum("check-careful" in c for c in self.commands(self.read_settings())), 1, mode)
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.read_settings(), self.settings)

    def test_settings_install_after_plugin_install_adds_hooks_once(self):
        self.plugin_on()
        self.install()
        self.assertEqual(self.read_settings()["hooks"], self.settings["hooks"])
        support.plugin_installed(self.claude.parent, disabled_in=self.c)   # the owner turns the plugin off
        self.install("--mode", "settings")
        self.install("--mode", "settings")
        cmds = self.commands(self.read_settings())
        self.assertEqual([cmds.count(c) for c in PLUGIN_CMDS], [PLUGIN_CMDS.count(c) for c in PLUGIN_CMDS])
        self.assertEqual(len(cmds), 2 + 8)
        self.assertEqual(sorted(h["command"] for h in self.state()["added_hooks"]), sorted(PLUGIN_CMDS))

    def test_careful_script_is_never_registered(self):
        self.careful_script()
        for mode in ("plugin", "settings"):
            out = self.install("--mode", mode)
            self.assertFalse(any("check-careful" in c for c in self.commands(self.read_settings())), mode)
            self.assertNotIn("careful", out)

    def test_gstack_own_careful_registration_is_left_alone(self):
        self.careful_script()
        own = 'bash -c "exec bash \\"$HOME/.claude/skills/gstack/careful/bin/check-careful.sh\\""'
        self.settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": own}]})
        (self.c / "settings.json").write_text(json.dumps(self.settings, indent=2) + "\n")
        self.install("--mode", "settings")
        self.assertEqual(sum("check-careful" in c for c in self.commands(self.read_settings())), 1)
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.read_settings(), self.settings)

    def test_invalid_settings_json_is_left_untouched(self):
        (self.c / "settings.json").write_text("{broken")
        code, out, _ = self.orche("install", "--home", str(self.h))
        self.assertEqual(code, 1)
        self.assertEqual((self.c / "settings.json").read_text(), "{broken")
        self.assertIn("settings: error", out)
        self.assertIn(f"hooks: error {self.c / 'settings.json'} (new sessions would run without Orche: 0 of 8 hooks "
                      "registered and the orche plugin not enabled)", out)

    # -- c108: the existing install's mode is kept; plugin mode only while the plugin is on -----------
    def test_install_without_mode_keeps_the_existing_installs_mode(self):
        self.install("--mode", "settings")
        out = self.install()
        self.assertEqual(self.state()["mode"], "settings")
        self.assertEqual([self.commands(self.read_settings()).count(c) for c in PLUGIN_CMDS],
                         [PLUGIN_CMDS.count(c) for c in PLUGIN_CMDS])
        self.assertNotIn("hooks: settings mode", out)
        self.assertNotIn("/plugin", out)
        self.assertEqual(out.splitlines()[-1], f"hooks: ok (on for new sessions: all 8 in {self.c / 'settings.json'})")

    def test_plugin_mode_with_the_plugin_disabled_registers_the_hooks_in_settings(self):
        # The 2026-11-01 incident: the plugin installed but turned off on purpose; plugin mode asked for.
        support.plugin_installed(self.claude.parent, disabled_in=self.c)
        self.settings["enabledPlugins"]["orche@orche"] = False
        out = self.install("--mode", "plugin", "--no-plugin-hint")
        self.assertEqual(self.state()["mode"], "settings")
        self.assertEqual(sorted(h["command"] for h in self.state()["added_hooks"]), sorted(PLUGIN_CMDS))
        self.assertIn("hooks: settings mode (the orche plugin is not enabled for new sessions, so settings.json "
                      "registers Orche's hooks)", out)
        self.assertIn(f"hooks: ok (on for new sessions: all 8 in {self.c / 'settings.json'})", out)
        calls = (self.claude.parent / "claude-argv.txt").read_text().splitlines()
        self.assertEqual(set(calls), {"plugin list --json"})   # asked, never changed
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.read_settings(), self.settings)

    def session_home(self, settings=None):
        """This test's home as the real one ($HOME) with $CLAUDE_CONFIG_DIR at an Orca-like profile folder."""
        profile = self.home / "profile"
        profile.mkdir()
        if settings is not None:
            (profile / "settings.json").write_text(settings)
        support.no_staff(self)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.h), "CLAUDE_CONFIG_DIR": str(profile)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return profile / "settings.json"

    def test_settings_mode_writes_the_hooks_into_claude_config_dir_too(self):
        other = self.session_home(json.dumps({"model": "fable", "hooks": {}}) + "\n")
        out = self.install("--mode", "settings", "--no-reconcile")
        data = json.loads(other.read_text())
        self.assertEqual(data["model"], "fable")
        self.assertEqual(sorted(self.commands(data)), sorted(PLUGIN_CMDS))
        self.assertNotIn("cleanupPeriodDays", data)   # only the hook entries go there
        self.assertEqual(len(list(other.parent.glob("settings.json.bak-*"))), 2)   # hooks, then statusLine
        self.assertEqual(data["statusLine"]["command"], f"bash {self.home / 'statusline.sh'}")
        self.assertIn(f"hooks: ok (on for new sessions: all 8 in {self.c / 'settings.json'}, {other})", out)
        self.assertIn(f"settings: ok {other} (unchanged)", self.install("--mode", "settings", "--no-reconcile"))
        self.assertEqual(len(list(other.parent.glob("settings.json.bak-*"))), 2)
        self.plugin_on()   # enabled for both folders: plugin mode takes the entries out of both
        self.install("--no-reconcile")
        self.assertEqual(self.commands(json.loads(other.read_text())), [])
        self.assertEqual(self.read_settings()["hooks"], self.settings["hooks"])
        self.assertIn(self.state()["mode"], ("plugin", "settings"))
        calls = (self.claude.parent / "claude-argv.txt").read_text().splitlines()
        self.assertEqual(calls[-2:], ["plugin list --json", "plugin list --json"])   # once per folder

    def test_a_session_file_that_cannot_take_the_hooks_fails_the_install(self):
        other = self.session_home("{broken")
        code, out, _ = self.orche("install", "--home", str(self.h), "--mode", "settings", "--no-reconcile")
        self.assertEqual(code, 1, out)
        self.assertEqual(other.read_text(), "{broken")
        self.assertIn(f"hooks: error {other} (new sessions would run without Orche: 0 of 8 hooks registered and the "
                      "orche plugin not enabled)", out)
        self.assertNotIn("hooks: ok", out)

    def test_uninstall_removes_blocks_links_and_hooks_keeps_backups(self):
        self.install("--mode", "settings")
        installed, backups = tree_digest(self.home), self.backups()
        code, out, err = self.orche("--dry-run", "uninstall", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(tree_digest(self.home), installed)
        self.assertIn("would remove", out)
        code, out, err = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual((self.c / "statusline-command.sh").read_text(), STATUSLINE)  # exactly the block removed
        self.assertEqual(self.read_settings(), self.settings)  # hooks removed, absent keys removed again
        self.assertFalse((self.c / "agents" / "exec.md").is_symlink())
        self.assertEqual((self.c / "agents" / "hr.md").read_text(), "my own hr\n")
        self.assertTrue((self.c / "agents" / "deep-reasoner.md").is_symlink())
        self.assertTrue(set(backups) <= set(self.backups()))
        self.assertTrue((self.home / "install-state.json").is_file() and (self.home / "status").is_dir())
        self.assertIn("kept: backups", out)


    def state(self):
        return json.loads((self.home / "install-state.json").read_text())

    def careful_script(self):
        script = self.c / "skills" / "gstack" / "careful" / "bin" / "check-careful.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/bash\n")
        return script

    def test_preset_values_restored_and_reinstall_keeps_recorded_originals(self):
        self.settings["cleanupPeriodDays"] = 30
        self.settings["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "80"
        (self.c / "settings.json").write_text(json.dumps(self.settings, indent=2) + "\n")
        self.install()
        self.assertEqual(self.read_settings()["cleanupPeriodDays"], 3650)
        self.install()  # our values are now in place; they must not become the "originals"
        originals = self.state()["originals"]
        self.assertEqual(originals["cleanupPeriodDays"], {"present": True, "value": 30})
        self.assertEqual(originals["env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], {"present": True, "value": "80"})
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_settings(), self.settings)
        self.assertIn("settings: restore (cleanupPeriodDays -> 30)", out)
        self.assertNotIn("originals", self.state())

    def test_uninstall_only_removes_hooks_orche_added(self):
        user_cmd = PLUGIN_CMDS[0]  # the user registered dispatch_relay by hand, identical string
        self.settings["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": user_cmd}]})
        (self.c / "settings.json").write_text(json.dumps(self.settings, indent=2) + "\n")
        # Settings mode finds it already registered on Stop and records only the other six.
        # (Plugin mode would remove it: with the plugin enabled it would run twice; see R4 tests.)
        self.install("--mode", "settings")
        self.assertEqual(sorted(h["command"] for h in self.state()["added_hooks"]), sorted(PLUGIN_CMDS[1:]))
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.read_settings(), self.settings)

    def test_step_error_is_reported_other_steps_run_and_state_survives(self):
        from orche.install import statusline
        with mock.patch.object(statusline, "install", side_effect=ValueError(f"bad {self.h}/x")):
            code, out, err = self.orche("install", "--home", str(self.h))
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("install: error (statusline: ValueError: bad ~/x)", out)
        self.assertNotIn(f"{self.h}/x", out)
        self.assertNotIn("Traceback", out)
        self.assertTrue((self.c / "agents" / "exec.md").is_symlink())   # later steps ran
        self.assertIn("originals", self.state())
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_settings(), self.settings, out)
        self.assertFalse((self.c / "agents" / "exec.md").exists())

    def test_a_status_line_target_that_is_not_text_is_skipped(self):
        binary = b"\xcf\xfa\xed\xfe\x07\x00\x00\x01\xff\xfe"   # a compiled program
        (self.c / "statusline-command.sh").write_bytes(binary)
        prog = self.c / "line-bin"
        prog.write_bytes(binary)
        data = dict(self.settings, statusLine={"type": "command", "command": str(prog)})
        (self.c / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        for _ in range(2):   # install, then the install `orche update` runs
            code, out, err = self.orche("install", "--home", str(self.h), "--no-jev")
            self.assertEqual((code, err), (0, ""), out)
            self.assertNotIn("error", out)
        self.assertEqual((self.c / "statusline-command.sh").read_bytes(), binary)
        self.assertEqual(prog.read_bytes(), binary)
        self.assertEqual(self.state()["statusline"]["inner"], str(prog))

    def test_corrupt_state_uninstall_refuses_settings_and_keeps_the_file(self):
        self.install("--mode", "settings")
        installed = self.read_settings()
        state = self.home / "install-state.json"
        state.write_text("{corrupt")
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 1, out)
        self.assertIn("install-state.json unreadable: hooks/values not restored — fix or remove the file "
                      "(backup kept)", out)
        self.assertEqual(self.read_settings(), installed)
        self.assertEqual(state.read_text(), "{corrupt")
        self.assertEqual([p.read_text() for p in self.home.glob("install-state.json.bak-*")], ["{corrupt"])

    def test_corrupt_state_install_refuses_before_settings(self):
        state = self.home / "install-state.json"
        state.write_text("[1, 2]")
        before = (self.c / "settings.json").read_bytes()
        code, out, _ = self.orche("install", "--home", str(self.h))
        self.assertEqual(code, 1, out)
        self.assertIn("install-state.json unreadable: install refused, settings.json not touched", out)
        self.assertEqual((self.c / "settings.json").read_bytes(), before)
        self.assertEqual(state.read_text(), "[1, 2]")
        self.assertFalse((self.c / "agents" / "exec.md").exists())

    def test_values_changed_after_install_are_kept(self):
        self.install()
        data = self.read_settings()
        data["cleanupPeriodDays"] = 99
        data["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "70"
        (self.c / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 0, out)
        after = self.read_settings()
        self.assertEqual((after["cleanupPeriodDays"], after["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"]), (99, "70"))
        self.assertIn("settings: kept cleanupPeriodDays=99 (changed after install)", out)
        self.assertIn("settings: kept env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70 (changed after install)", out)

    def test_hand_edited_added_hooks_does_not_raise(self):
        (self.home / "install-state.json").write_text(json.dumps({"added_hooks": "oops"}))
        code, out, _ = self.orche("install", "--home", str(self.h), "--mode", "settings")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(h["command"] for h in self.state()["added_hooks"]), sorted(PLUGIN_CMDS))
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.read_settings(), self.settings)

    def test_state_save_failure_reported_once(self):
        from orche import paths
        paths.ensure_dirs()
        self.home.chmod(0o555)  # ORCHE_HOME itself read-only: the state file cannot be created
        try:
            code, out, _ = self.orche("install", "--home", str(self.h))
        finally:
            self.home.chmod(0o755)
        self.assertEqual(code, 1)
        errors = [l for l in out.splitlines() if l.startswith("state: error")]
        self.assertEqual(errors, ["state: error (install-state.json: Permission denied)"], out)
        self.assertEqual(self.read_settings()["cleanupPeriodDays"], 3650)  # install itself went ahead


class SkillsTest(InstallCase):
    """Settings mode links each plugin skill into ~/.claude/skills/; plugin mode and uninstall remove ours."""

    NAMES = sorted(p.name for p in (PLUGIN_ROOT / "skills").iterdir() if (p / "SKILL.md").is_file())

    def skills(self):
        return self.c / "skills"

    def test_settings_mode_links_every_skill_and_is_idempotent(self):
        self.assertEqual(self.NAMES, ["orche", "orche-apply", "orche-build", "orche-main"])
        out = self.install("--mode", "settings")
        for name in self.NAMES:
            self.assertEqual(os.readlink(self.skills() / name), str(PLUGIN_ROOT / "skills" / name))
            self.assertIn(f"skills: symlink {self.skills() / name}", out)
        out = self.install("--mode", "settings")
        for name in self.NAMES:
            self.assertIn(f"skills: ok {self.skills() / name} (already linked)", out)

    def test_foreign_entries_with_our_names_are_skipped_and_reported(self):
        (self.skills() / "orche").mkdir(parents=True)
        (self.skills() / "orche" / "SKILL.md").write_text("mine\n")
        (self.skills() / "orche-main").symlink_to(self.home)
        out = self.install("--mode", "settings")
        self.assertIn(f"skills: skip {self.skills() / 'orche'} (existing entry, not Orche's; left untouched)", out)
        self.assertIn(f"skills: skip {self.skills() / 'orche-main'} (existing symlink to", out)
        self.assertEqual((self.skills() / "orche" / "SKILL.md").read_text(), "mine\n")
        self.assertEqual(os.readlink(self.skills() / "orche-main"), str(self.home))
        self.assertTrue((self.skills() / "orche-build").is_symlink())
        code, out, err = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(sorted(p.name for p in self.skills().iterdir()), ["orche", "orche-main"])
        self.assertTrue((self.skills() / "orche").is_dir() and (self.skills() / "orche-main").is_symlink())

    def test_a_skills_dir_without_skill_md_is_not_linked(self):
        from orche.install import skills
        root = self.home / "plugin"
        (root / "skills" / "real").mkdir(parents=True)
        (root / "skills" / "real" / "SKILL.md").write_text("x\n")
        (root / "skills" / "_shared").mkdir()
        (root / "skills" / "_shared" / "note.md").write_text("x\n")
        notes = []
        ctx = types.SimpleNamespace(plugin_root=root, claude=self.c, dry_run=False,
                                    note=lambda *a: notes.append(a))
        skills.install(ctx, "settings")
        self.assertEqual(sorted(p.name for p in self.skills().iterdir()), ["real"])
        self.assertEqual([n[2] for n in notes], [self.skills() / "real"])

    def test_dry_run_writes_nothing(self):
        before = tree_digest(self.home)
        code, out, err = self.orche("--dry-run", "install", "--home", str(self.h), "--mode", "settings")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(tree_digest(self.home), before)
        self.assertIn(f"skills: would symlink {self.skills() / 'orche-apply'}", out)
        self.assertFalse(self.skills().exists())

    def test_plugin_mode_removes_our_links_and_keeps_foreign_ones(self):
        self.install("--mode", "settings")
        (self.skills() / "mine").mkdir()
        self.plugin_on()
        out = self.install("--mode", "plugin")
        for name in self.NAMES:
            self.assertFalse((self.skills() / name).is_symlink(), name)
            self.assertIn(f"skills: remove {self.skills() / name}", out)
        self.assertIn("plugin mode: the plugin ships the skills", out)
        self.assertTrue((self.skills() / "mine").is_dir())

    def test_plain_plugin_mode_says_nothing_about_skills(self):
        self.plugin_on()
        out = self.install()
        self.assertNotIn("skills:", out)
        self.assertFalse(self.skills().exists())

    def test_uninstall_removes_our_links(self):
        self.install("--mode", "settings")
        code, out, err = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(list(self.skills().iterdir()), [])
        self.assertIn(f"skills: remove {self.skills() / 'orche'}", out)


OLD_BLOCK = ("\n# >>> orche >>>\n# Orche project segment (added by `orche install`, removed by `orche uninstall`).\n"
             "{\n  printf 'old\\n'\n} 2>/dev/null || true\n# <<< orche <<<\n")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class StatuslineInstallTest(InstallCase):
    """Orche's own status line script: modes, defaults, migration of the old block, uninstall, doctor and the
    setup report. Temp homes only."""

    def setUp(self):
        super().setUp()
        from orche.install import statusline
        self.sl = statusline
        self.ours = f"bash {self.home / 'statusline.sh'}"
        self.mine = f"bash {self.c}/statusline-command.sh"

    def write_settings(self, path=None, **line):
        data = {k: v for k, v in self.settings.items() if k != "statusLine"}
        if line:
            data["statusLine"] = line
        (path or self.c / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        return data

    def line_of(self, path=None):
        return json.loads((path or self.c / "settings.json").read_text()).get("statusLine")

    def state(self):
        return json.loads((self.home / "install-state.json").read_text())

    def rec(self):
        return {k: v for k, v in self.state()["statusline"].items() if k != "previous"}

    def profile(self, line=None):
        profile = self.home / "profile"
        profile.mkdir()
        (profile / "settings.json").write_text(json.dumps({"statusLine": line} if line is not None else {}) + "\n")
        support.no_staff(self)
        return profile / "settings.json"

    def in_profile(self, profile, *argv):
        with mock.patch.dict(os.environ, {"HOME": str(self.h), "CLAUDE_CONFIG_DIR": str(profile.parent)}):
            return self.orche(*argv)

    def test_a_fresh_home_gets_orches_line_in_every_session_file(self):
        self.write_settings()
        profile = self.home / "profile"
        profile.mkdir()
        (profile / "settings.json").write_text("{}\n")
        support.no_staff(self)
        with mock.patch.dict(os.environ, {"HOME": str(self.h), "CLAUDE_CONFIG_DIR": str(profile)}):
            out = self.install("--mode", "settings", "--no-reconcile", "--no-jev")
        for path in (self.c / "settings.json", profile / "settings.json"):
            self.assertEqual(self.line_of(path), {"type": "command", "command": self.ours}, path)
        self.assertEqual(self.state()["statusline"], {"mode": "orche", "inner": None, "asked": False, "previous": {
            str(self.c / "settings.json"): None, str(profile / "settings.json"): None}})
        self.assertFalse((self.home / "statusline.inner").exists())
        self.assertIn("statusline: mode (Orche's status line)", out)
        self.assertNotIn("statusline: note", out)

    def test_a_foreign_command_is_kept_with_its_other_keys(self):
        self.write_settings(type="command", command=self.mine, padding=2, refreshInterval=5)
        out = self.install("--no-jev")
        self.assertEqual(self.line_of(), {"type": "command", "command": self.ours, "padding": 2,
                                          "refreshInterval": 5})
        self.assertEqual(self.state()["statusline"], {"mode": "keep", "inner": self.mine, "asked": False, "previous": {
            str(self.c / "settings.json"): {"type": "command", "command": self.mine, "padding": 2,
                                            "refreshInterval": 5}}})
        self.assertEqual((self.home / "statusline.inner").read_text(), self.mine + "\n")
        self.assertIn(f"statusline: note ({self.sl.TAKEN})", out)   # told once, on the takeover
        self.assertIn('say "use Orche\'s status line" to switch, or "turn the status line off"', out)
        self.assertNotIn("statusline: note", self.install("--no-jev"))

    def test_the_old_block_is_removed_from_statusline_command_and_from_the_script_statusline_runs(self):
        (self.c / "statusline-command.sh").write_text(STATUSLINE + OLD_BLOCK)
        own = self.c / "mine.sh"
        own.write_text("#!/bin/bash\necho mine\n" + OLD_BLOCK + "exit 0\n")
        self.write_settings(type="command", command="bash ~/.claude/mine.sh")
        out = self.install("--no-jev")
        self.assertEqual((self.c / "statusline-command.sh").read_text(), STATUSLINE)
        self.assertEqual(own.read_text(), "#!/bin/bash\necho mine\nexit 0\n")
        self.assertEqual(len(list(self.c.glob("mine.sh.bak-*"))), 1)
        self.assertEqual(len(list(self.c.glob("statusline-command.sh.bak-*"))), 1)
        self.assertIn(f"statusline: patch {own} (old Orche block removed)", out)
        self.assertEqual(self.state()["statusline"]["inner"], "bash ~/.claude/mine.sh")
        self.assertEqual(self.line_of()["command"], self.ours)
        self.install("--no-jev")   # nothing adds a block back
        self.assertNotIn("# >>> orche >>>", own.read_text() + (self.c / "statusline-command.sh").read_text())

    def test_each_mode_by_flag_and_keep_without_a_command_is_an_error(self):
        before = self.write_settings()
        code, out, err = self.orche("install", "--home", str(self.h), "--statusline", "keep")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, f"orche: {self.sl.NO_KEEP}\n")
        self.assertEqual(json.loads((self.c / "settings.json").read_text()), before)
        self.write_settings(type="command", command=self.mine, padding=1)
        self.install("--no-jev", "--statusline", "orche")
        self.assertEqual(self.rec(), {"mode": "orche", "inner": self.mine, "asked": True})
        self.assertEqual(self.line_of(), {"type": "command", "command": self.ours, "padding": 1})
        self.assertFalse((self.home / "statusline.inner").exists())
        self.install("--no-jev", "--statusline", "off")
        self.assertEqual(self.line_of(), {"type": "command", "command": self.mine, "padding": 1})
        self.assertEqual(self.state()["statusline"]["mode"], "off")
        self.install("--no-jev")   # off stays off and the user's line stays theirs
        self.assertEqual(self.line_of()["command"], self.mine)
        self.assertEqual(self.state()["statusline"]["mode"], "off")
        self.install("--no-jev", "--statusline", "keep")   # the stored command is enough
        self.assertEqual(self.line_of()["command"], self.ours)
        self.assertEqual((self.home / "statusline.inner").read_text(), self.mine + "\n")
        self.write_settings()
        self.install("--no-jev", "--statusline", "off")   # none before Orche: the key goes
        self.assertEqual(self.state()["statusline"]["mode"], "off")

    def test_off_without_a_command_removes_the_key(self):
        self.write_settings()
        self.install("--no-jev")
        self.install("--no-jev", "--statusline", "off")
        self.assertNotIn("statusLine", json.loads((self.c / "settings.json").read_text()))

    def test_a_command_set_after_install_is_wrapped_again(self):
        self.write_settings()
        self.install("--no-jev")
        data = json.loads((self.c / "settings.json").read_text())
        data["statusLine"] = {"type": "command", "command": "npx other-line", "padding": 0}
        (self.c / "settings.json").write_text(json.dumps(data) + "\n")
        out = self.install("--no-jev")
        self.assertIn("statusline: note (your status line changed after install (npx other-line); Orche runs it "
                      "now and adds its project line under it)", out)
        self.assertEqual(self.line_of(), {"type": "command", "command": self.ours, "padding": 0})
        self.assertEqual(self.rec(), {"mode": "keep", "inner": "npx other-line", "asked": False})
        self.assertEqual((self.home / "statusline.inner").read_text(), "npx other-line\n")
        self.assertEqual(self.state()["statusline"]["previous"],
                         {str(self.c / "settings.json"): {"type": "command", "command": "npx other-line", "padding": 0}})
        self.install("--no-jev")   # Orche's own value never replaces it
        self.assertEqual(self.state()["statusline"]["previous"][str(self.c / "settings.json")]["command"],
                         "npx other-line")
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertEqual(self.line_of(), {"type": "command", "command": "npx other-line", "padding": 0})

    def test_the_profiles_command_wins_over_the_home_one(self):
        self.write_settings(type="command", command=self.mine)
        profile = self.home / "profile"
        profile.mkdir()
        (profile / "settings.json").write_text(json.dumps({"statusLine": {"type": "command",
                                                                          "command": "sh /p/line.sh"}}))
        support.no_staff(self)
        with mock.patch.dict(os.environ, {"HOME": str(self.h), "CLAUDE_CONFIG_DIR": str(profile)}):
            out = self.install("--mode", "settings", "--no-reconcile", "--no-jev")
        self.assertEqual(self.state()["statusline"]["inner"], "sh /p/line.sh")
        self.assertIn(f"statusline: note (settings files name different status lines; using "
                      f"{profile / 'settings.json'}'s (the one sessions read))", out)
        self.assertEqual(self.line_of(profile / "settings.json")["command"], self.ours)

    def test_each_settings_file_gets_its_own_line_back_on_uninstall_and_off(self):
        home_line = {"type": "command", "command": self.mine, "padding": 1}
        prof_line = {"type": "command", "command": "sh /p/line.sh"}
        self.write_settings(**home_line)
        prof = self.profile(prof_line)
        args = ("install", "--home", str(self.h), "--mode", "settings", "--no-reconcile", "--no-jev")
        self.assertEqual(self.in_profile(prof, *args)[0], 0)
        self.assertEqual(self.rec()["inner"], "sh /p/line.sh")   # the run-time inner: the profile's
        self.assertEqual(self.in_profile(prof, *args, "--statusline", "off")[0], 0)
        self.assertEqual((self.line_of(), self.line_of(prof)), (home_line, prof_line))
        self.assertEqual(self.in_profile(prof, *args, "--statusline", "orche")[0], 0)
        self.assertEqual(self.line_of(prof)["command"], self.ours)
        code, out, _ = self.in_profile(prof, "uninstall", "--home", str(self.h))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.line_of(), self.line_of(prof)), (home_line, prof_line))

    def test_a_status_line_of_another_shape_is_restored_exactly(self):
        for odd in ("echo hi", ["bash", "x.sh"], {"type": "static", "text": "hi"}, {"type": "command"}):
            with self.subTest(odd=odd):
                (self.home / "install-state.json").unlink(missing_ok=True)
                data = {k: v for k, v in self.settings.items() if k != "statusLine"}
                (self.c / "settings.json").write_text(json.dumps({**data, "statusLine": odd}) + "\n")
                self.install("--no-jev")
                self.assertEqual(self.line_of()["command"], self.ours)
                self.assertEqual(self.rec(), {"mode": "orche", "inner": None, "asked": False})
                self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
                self.assertEqual(self.line_of(), odd)
                self.assertEqual(self.read_settings(), {**data, "statusLine": odd})

    def test_an_odd_home_line_falls_back_to_the_profiles_command(self):
        self.write_settings(type="static", text="hi")
        prof = self.profile({"type": "command", "command": "sh /p/line.sh"})
        args = ("install", "--home", str(self.h), "--mode", "settings", "--no-reconcile", "--no-jev")
        self.assertEqual(self.in_profile(prof, *args)[0], 0)
        self.assertEqual(self.rec(), {"mode": "keep", "inner": "sh /p/line.sh", "asked": False})
        self.assertEqual(self.in_profile(prof, *args, "--statusline", "off")[0], 0)
        self.assertEqual(self.line_of(), {"type": "static", "text": "hi"})
        self.assertEqual(self.line_of(prof), {"type": "command", "command": "sh /p/line.sh"})

    def test_changing_the_mode_on_an_installed_home_runs_only_the_status_line_step(self):
        from orche import cmd_install
        from orche.install import agents, settings, skills
        self.write_settings(type="command", command=self.mine)
        self.install("--no-jev")
        boom = mock.Mock(side_effect=AssertionError("not this step"))
        with mock.patch.object(settings, "install", boom), mock.patch.object(agents, "install", boom), \
                mock.patch.object(skills, "install", boom), mock.patch.object(cmd_install, "_reconcile_job", boom), \
                mock.patch.object(cmd_install, "_janitor_job", boom), mock.patch.object(cmd_install, "_jev_step", boom):
            out = self.install("--statusline", "orche")
        boom.assert_not_called()
        self.assertEqual(self.rec(), {"mode": "orche", "inner": self.mine, "asked": True})
        self.assertEqual(self.line_of()["command"], self.ours)
        self.assertIn("statusline: mode (Orche's status line)", out)
        self.assertFalse([ln for ln in out.splitlines() if not ln.startswith("statusline:")], out)

    def test_a_first_install_with_a_mode_runs_every_step(self):
        from orche.install import agents
        self.write_settings()
        with mock.patch.object(agents, "install", wraps=agents.install) as spy:
            self.install("--no-jev", "--statusline", "orche")
        spy.assert_called_once()
        self.assertIn(self.state()["mode"], ("plugin", "settings"))

    def test_uninstall_restores_and_removes_the_script(self):
        self.write_settings()
        self.install("--no-jev")
        (self.c / "statusline-command.sh").write_text(STATUSLINE + OLD_BLOCK)   # a leftover block
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 0, out)
        self.assertNotIn("statusLine", json.loads((self.c / "settings.json").read_text()))
        self.assertFalse((self.home / "statusline.sh").exists())
        self.assertEqual((self.c / "statusline-command.sh").read_text(), STATUSLINE)
        self.assertNotIn("statusline", self.state())

    def test_doctor_rows(self):
        from orche.install import Ctx
        ctx = Ctx(self.h)
        self.assertEqual(self.sl.rows(ctx, {})[0][:2], ("WARN", "statusline"))
        self.install("--no-jev")
        self.assertEqual(self.sl.rows(ctx, self.state()),
                         [("OK", "statusline", "your status line with Orche's project line under it")])
        self.write_settings(type="command", command="npx other-line")
        level, _, detail = self.sl.rows(ctx, self.state())[0]
        self.assertEqual(level, "WARN")
        self.assertIn(str(self.c / "settings.json"), detail)
        self.assertTrue(detail.endswith('say "repair the setup"; Main runs orche install'), detail)
        self.install("--no-jev", "--statusline", "orche")
        self.assertEqual(self.sl.rows(ctx, self.state()), [("OK", "statusline", "Orche's status line")])
        self.install("--no-jev", "--statusline", "off")
        self.assertEqual(self.sl.rows(ctx, self.state()),
                         [("OK", "statusline", "off: Orche adds nothing to the status line")])

    def test_doctor_keep_without_a_stored_command_and_an_unreadable_settings_file(self):
        from orche.install import Ctx
        ctx = Ctx(self.h)
        self.install("--no-jev")   # keep
        (self.home / "statusline.inner").write_text("\n")
        self.assertEqual(self.sl.rows(ctx, self.state()), [("WARN", "statusline", (
            "your status line command is not stored; Orche shows its own line; say \"repair the setup\"; Main runs "
            "orche install"))])
        (self.home / "statusline.inner").unlink()
        self.assertEqual(self.sl.rows(ctx, self.state())[0][0], "WARN")
        (self.c / "settings.json").write_text("{not json")
        level, _, detail = self.sl.rows(ctx, self.state())[0]
        self.assertEqual(level, "WARN")
        self.assertTrue(detail.startswith(f"cannot read {self.c / 'settings.json'}; fix the file"), detail)
        self.assertNotIn("does not run Orche's script", detail)

    def test_setup_report_states_the_mode_and_asks_once(self):
        from orche import guide
        self.install("--no-jev")   # the user's own line kept by default
        self.assertEqual(guide.statusline_mode(), "your status line with Orche's project line under it")
        ask = guide.ask_once_lines()
        self.assertEqual(ask[:3], ["", "## Ask the owner once", ""])
        for words in ("AskUserQuestion", "`orche install --statusline keep`", "`orche install --statusline orche`",
                      "`orche install --statusline off`"):
            self.assertIn(words, ask[3])
        self.assertIn("Ask the owner once", guide.SETUP_ASK)
        on, line = guide.statusline_line()
        self.assertTrue(on)
        self.assertIn('Say "use Orche\'s status line", "keep my status line" or "turn the status line off"', line)
        from orche import config
        reg = registry.empty()
        self.assertFalse(any(ln.startswith("- Status line") for ln in guide.lines(config.load(), reg)))   # on
        self.install("--no-jev", "--statusline", "keep")   # the owner chose: never asked again
        self.assertEqual(guide.ask_once_lines(), [])
        self.install("--no-jev")
        self.assertEqual(guide.ask_once_lines(), [])
        self.assertTrue(self.state()["statusline"]["asked"])
        self.install("--no-jev", "--statusline", "off")   # off: listed under "Any time" to switch back on
        lines = guide.lines(config.load(), reg)
        self.assertTrue([ln for ln in lines if ln.startswith("- Status line") and ". Now: off. " in ln], lines)

    def test_a_fresh_home_is_not_asked(self):
        from orche import guide
        self.write_settings()
        self.install("--no-jev")
        self.assertEqual(guide.ask_once_lines(), [])
        self.assertEqual(guide.statusline_mode(), "Orche's status line")


class StatuslineScriptTest(support.TempHomeCase):
    """templates/statusline.sh run with bash as Claude Code runs it: stdin JSON, exit 0, no jq."""

    def setUp(self):
        super().setUp()
        self.dir = self.home / "sl"
        self.dir.mkdir()
        self.script = self.dir / "statusline.sh"
        self.script.write_text(SCRIPT.read_text())
        self.repo = self.home / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feat-x", str(self.repo)], check=True, capture_output=True)
        self.now = int(__import__("time").time())

    def run_sh(self, stdin, env=None, cwd=None):
        env = env if env is not None else {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        res = subprocess.run(["/bin/bash", str(self.script)], input=stdin, capture_output=True, text=True, env=env,
                             cwd=str(cwd or self.home), timeout=10)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def payload(self, rate_limits=None, compact=True, **extra):
        data = {"session_id": "s", "cwd": str(self.repo), "effort": {"level": "high"},
                "model": {"id": "claude-fable-5-2", "display_name": "Fable 5.2"},
                "workspace": {"current_dir": str(self.repo), "project_dir": str(self.repo), "added_dirs": []},
                "context_window": {"used_percentage": 22, "remaining_percentage": 78}, **extra}
        if rate_limits is not None:
            data["rate_limits"] = rate_limits
        return json.dumps(data, separators=(",", ":") if compact else (", ", ": "))

    def when(self, epoch, fmt):
        import time
        return time.strftime(fmt, time.localtime(epoch))

    def weekly(self):
        reset = self.now + 2 * 86400
        return {"used_percentage": 78, "resets_at": reset}, \
            f"Weekly: 78% ▓▓▓▓▓ ▓▓▓▓▓ ▓▓▓▓┃ ▓░░░░ → Reset: {self.when(reset, '%a %I:%M %p')}"

    def test_orche_mode_on_the_sample_compact_and_spaced(self):
        week, text = self.weekly()
        for compact in (True, False):
            out = self.run_sh(self.payload({"seven_day": week}, compact=compact))
            self.assertEqual(ANSI.sub("", out), f"⎇ feat-x │ Fable 5.2 · high │ {text}\n")
            self.assertIn("\x1b[0;32m⎇ feat-x\x1b[0m", out)

    def test_missing_rate_limits_and_both_windows(self):
        self.assertEqual(ANSI.sub("", self.run_sh(self.payload())), "⎇ feat-x │ Fable 5.2 · high\n")
        week, text = self.weekly()
        five = {"used_percentage": 12.4, "resets_at": self.now + 3600}
        out = ANSI.sub("", self.run_sh(self.payload({"five_hour": five, "seven_day": week})))
        self.assertEqual(out, f"⎇ feat-x │ Fable 5.2 · high │ Session: 12% ▓▓░░░ ░░░░░ ░░░░░ ░┃░░░ → Reset: "
                              f"{self.when(self.now + 3600, '%I:%M %p')} │ {text}\n")

    def test_empty_stdin_and_no_git_print_what_they_have(self):
        plain = self.home / "plain"
        plain.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "GIT_CEILING_DIRECTORIES": str(self.home)}
        self.assertEqual(self.run_sh("", env=env, cwd=plain), "")
        out = self.run_sh(json.dumps({"model": {"display_name": "Opus"}}), env=env, cwd=plain)
        self.assertEqual(ANSI.sub("", out), "Opus\n")

    def project(self):
        (self.repo / ".orche").mkdir()
        (self.repo / ".orche" / "project").write_text("acme-shop\n")
        (self.dir / "status").mkdir()
        (self.dir / "status" / "acme-shop.line").write_text("BUILD●\nspent 2026-11-01 4.2\n")

    def test_keep_mode_runs_the_inner_command_with_the_same_stdin_then_the_project_line(self):
        self.project()
        (self.dir / "statusline.inner").write_text("printf X\n")
        self.assertEqual(self.run_sh(self.payload()), "X\nBUILD●\n")
        (self.dir / "statusline.inner").write_text("cat\n")
        stdin = self.payload()
        self.assertEqual(self.run_sh(stdin), stdin + "\nBUILD●\n")

    def test_orche_mode_adds_the_project_line_under_its_own(self):
        self.project()
        lines = ANSI.sub("", self.run_sh(self.payload())).splitlines()
        self.assertEqual(lines, ["⎇ feat-x │ Fable 5.2 · high", "BUILD●"])

    def test_a_staff_pane_gets_the_staff_label_without_a_marker(self):
        self.project()
        base = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        elsewhere = self.payload(workspace={"current_dir": str(self.home)})   # no `.orche/project` above it
        (self.dir / "status" / "demo.line").write_text("⚠ waiting 1\nspent 2026-11-01 1.0\n")
        out = ANSI.sub("", self.run_sh(elsewhere, env={**base, "ORCHE_SESSION": "staff-demo"}))
        self.assertEqual(out.splitlines()[-1], "STAFF · ⚠ waiting 1")
        (self.dir / "status" / "demo.line").unlink()
        out = ANSI.sub("", self.run_sh(elsewhere, env={**base, "ORCHE_SESSION": "staff-demo"}))
        self.assertEqual(out.splitlines()[-1], "STAFF · demo")
        (self.dir / "status" / "demo.line").write_text("")
        self.assertEqual(self.run_sh(elsewhere, env={**base, "ORCHE_SESSION": "staff-demo"}).splitlines()[-1],
                         "STAFF · demo")
        plain = self.run_sh(self.payload(), env=base)
        for session in ("main-demo", "staff-Bad/../x"):
            self.assertEqual(self.run_sh(self.payload(), env={**base, "ORCHE_SESSION": session}), plain, session)
        self.assertEqual(ANSI.sub("", plain).splitlines(), ["⎇ feat-x │ Fable 5.2 · high", "BUILD●"])

    def test_an_inner_command_that_is_orches_script_falls_back_to_the_built_in_line(self):
        for inner in (f"bash {self.script}", "bash ~/.claude/orche/statusline.sh"):
            (self.dir / "statusline.inner").write_text(inner + "\n")
            self.assertEqual(ANSI.sub("", self.run_sh(self.payload())), "⎇ feat-x │ Fable 5.2 · high\n", inner)

    def test_a_multi_line_inner_runs_whole(self):
        self.project()
        (self.dir / "statusline.inner").write_text("printf A\nprintf B\n")
        self.assertEqual(self.run_sh(self.payload()), "AB\nBUILD●\n")

    def test_an_inner_that_runs_a_copy_of_the_script_prints_the_project_line_once(self):
        self.project()
        copy = self.home / "elsewhere" / "line.sh"
        copy.parent.mkdir()
        copy.write_text(SCRIPT.read_text())
        (self.dir / "statusline.inner").write_text(f"bash {copy}\n")
        self.assertEqual(self.run_sh(self.payload()), "BUILD●\n")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "ORCHE_STATUSLINE": "1"}
        self.assertEqual(self.run_sh(self.payload(), env=env), "")

    def test_a_reset_in_the_past_shows_no_time_and_no_pace_marker(self):
        five = {"used_percentage": 12, "resets_at": self.now - 600}
        out = ANSI.sub("", self.run_sh(self.payload({"five_hour": five})))
        self.assertEqual(out, "⎇ feat-x │ Fable 5.2 · high │ Session: 12% ▓▓░░░ ░░░░░ ░░░░░ ░░░░░\n")

    def test_no_jq_needed(self):
        self.assertNotIn("jq", SCRIPT.read_text())
        week, text = self.weekly()
        out = self.run_sh(self.payload({"seven_day": week}), env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(ANSI.sub("", out), f"⎇ feat-x │ Fable 5.2 · high │ {text}\n")
        tools = self.home / "bin"   # only git and date on PATH
        tools.mkdir()
        for name in ("git", "date"):
            found = __import__("shutil").which(name, path="/usr/bin:/bin")
            (tools / name).symlink_to(found)
        out = self.run_sh(self.payload({"seven_day": week}), env={"PATH": str(tools)})
        self.assertEqual(ANSI.sub("", out), f"⎇ feat-x │ Fable 5.2 · high │ {text}\n")


LOGIN_SH = "/fake/login-sh"


class DoctorTest(InstallCase):
    def setUp(self):
        super().setUp()
        self.plugin_on()   # the hooks row is OK unless a test turns the plugin off
        # `$SHELL -lc ...` answers (never the developer's real login shell): command -> stdout or None.
        self.login = {"command -v python3 && python3 --version 2>&1": "/opt/homebrew/bin/python3\nPython 3.14.0\n",
                      "command -v orche": f"{support.ROOT / 'bin' / 'orche'}\n"}
        self.login_calls = []

    def doctor(self, missing=("age",)):
        real = doctor.shutil.which
        which = (lambda tool: None if tool in missing else real("python3") if tool == "python3" else real("sh"))
        inner = shell.RUNNER

        def runner(argv, *, timeout, input, **kw):
            if argv[0] == LOGIN_SH and argv[1] == "-lc":
                self.login_calls.append(argv)
                out = self.login.get(argv[2])
                return shell.Result(0 if out else 1, out or "", "", argv)
            return inner(argv, timeout=timeout, input=input, **kw)
        with mock.patch.object(doctor.shutil, "which", which), mock.patch.dict(os.environ, {"SHELL": LOGIN_SH}), \
                mock.patch.object(shell, "RUNNER", runner):
            return self.orche(*(["--json"] if getattr(self, "json_doctor", False) else []), "doctor", "--home",
                              str(self.h))

    OLD_PY = ("/usr/bin/python3 is 3.9.6 (needs 3.11+): brew install python; "
              "put /opt/homebrew/bin before /usr/bin in PATH")

    def test_old_login_shell_python3_warns_while_the_finder_picks_one(self):
        registry.save(registry.empty())
        self.login["command -v python3 && python3 --version 2>&1"] = "/usr/bin/python3\nPython 3.9.6\n"
        self.login[doctor.finder_command()] = "/opt/homebrew/bin/python3.14\n"
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(f"WARN\tpython3 (login shell)\t{self.OLD_PY} (launchers pick a 3.11+ interpreter themselves)",
                      out.splitlines())
        self.assertIn([LOGIN_SH, "-lc", "command -v python3 && python3 --version 2>&1"], self.login_calls)
        self.assertIn([LOGIN_SH, "-lc", doctor.finder_command()], self.login_calls)

    def test_old_login_shell_python3_fails_when_the_finder_finds_none(self):
        registry.save(registry.empty())
        self.login["command -v python3 && python3 --version 2>&1"] = "/usr/bin/python3\nPython 3.9.6\n"
        code, out, _ = self.doctor()   # the finder command answers nothing
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL\tpython3 (login shell)\t{self.OLD_PY}", out.splitlines())
        self.login["command -v python3 && python3 --version 2>&1"] = None
        _, out, _ = self.doctor()
        self.assertIn(f"FAIL\tpython3 (login shell)\t{LOGIN_SH} -lc finds no python3 (needs 3.11+): brew install "
                      "python; put /opt/homebrew/bin before /usr/bin in PATH", out.splitlines())

    def test_doctor_ends_with_the_next_steps_and_its_json_lists_them(self):
        registry.save(registry.empty())
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        at = lines.index("Next steps (optional, any time):")
        self.assertEqual(lines[at - 1], "")
        steps = lines[at + 1:]
        self.assertTrue(steps and all(ln.startswith("- ") for ln in steps), steps)
        self.assertEqual(steps[-1], '- Something wrong: any time. Say "check the setup"; Main runs `orche doctor`.')
        self.assertIn("- More agents at once: how many workers may run in parallel. Up to 4 at once (default). ", out)
        self.assertIn("- Janitor: cleans up daily (the heartbeat starts it when the last pass is a day old); ", out)
        self.assertFalse(any(ln.startswith(("WARN", "FAIL")) for ln in steps))   # install.sh greps those only
        self.json_doctor = True
        code, out, _ = self.doctor()
        data = json.loads(out)
        self.assertEqual(data["next_steps"], steps)
        self.assertTrue(all({"check", "level", "detail"} <= set(row) for row in data["checks"]))

    def test_the_notifications_row_reads_orcas_terminal_bell_setting(self):
        """Read only from Orca's file under the home: OK with Terminal Bell notifications on, WARN with
        Orca's notifications or its Terminal Bell ones off, `off` with config notify false, no row when the
        file or a key cannot be read."""
        registry.save(registry.empty())
        path = self.h.joinpath(*doctor.ORCA_DATA)
        path.parent.mkdir(parents=True)

        def row(settings=None, raw=None, cfg=None):
            if raw is not None or settings is not None:
                path.write_text(raw if raw is not None else json.dumps({"settings": settings, "other": 1}))
            return doctor.notification_rows(self.h, cfg or {})

        on = {"notifications": {"enabled": True, "terminalBell": True, "agentTaskComplete": False}}
        self.assertEqual(row(on), [("OK", "notifications", "Orca shows Orche's reminders (Terminal Bell "
                                                            "notifications on)")])
        warn = [("WARN", "notifications", "Orca's Terminal Bell notifications are off, so Orche's reminders stay "
                                          "silent. Turn on Orca Settings > Notifications > Terminal Bell")]
        self.assertEqual(row({"notifications": {"enabled": True, "terminalBell": False}}), warn)
        self.assertEqual(row({"notifications": {"enabled": False, "terminalBell": True}}), warn)
        self.assertEqual(row(on, cfg={"notify": False}), [("OK", "notifications", "off (notify false)")])
        for name, case in (("no key", {"settings": {"notifications": {"enabled": True}}}),
                           ("not a bool", {"settings": {"notifications": {"enabled": True, "terminalBell": "on"}}}),
                           ("no notifications", {"settings": {}}), ("settings a list", {"settings": []})):
            with self.subTest(name):
                self.assertEqual(row(raw=json.dumps(case)), [])
        self.assertEqual(row(raw="{not json"), [])
        path.unlink()
        self.assertEqual(row(), [])
        self.assertEqual(path.read_bytes() if path.exists() else None, None)     # never written
        row(on)
        _, out, _ = self.doctor()
        self.assertIn("OK\tnotifications\tOrca shows Orche's reminders (Terminal Bell notifications on)",
                      out.splitlines())
        self.assertEqual(json.loads(path.read_text()), {"settings": on, "other": 1})

    def test_finder_command_runs_the_shared_finder(self):
        res = subprocess.run(["/bin/sh", "-c", doctor.finder_command()], capture_output=True, text=True,
                             timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        exe = res.stdout.strip()
        ver = subprocess.run([exe, "-c", "import sys; print(sys.version_info >= (3, 11))"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        self.assertEqual(ver, "True")
        self.assertIn(str(support.ROOT / "lib" / "find_python.sh"), doctor.finder_command())

    def test_login_shell_python3_ok_reports_path_and_version(self):
        registry.save(registry.empty())
        _, out, _ = self.doctor()
        self.assertIn("OK\tpython3 (login shell)\t/opt/homebrew/bin/python3 3.14.0", out.splitlines())

    def test_claude_binary_version_and_flags(self):
        registry.save(registry.empty())
        _, out, _ = self.doctor()
        self.assertIn(f"OK\tclaude\t{self.claude} 9.9.9 (Claude Code)", out.splitlines())
        self.assertIn("OK\tclaude flags\tall 17 listed in claude --help", out.splitlines())
        (self.claude.parent / "help.txt").write_text("Usage: claude [options]\n  -p\n")
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(f"WARN\tclaude flags\tnot listed in claude --help: {', '.join(headless.FLAGS)}; "
                      "headless calls that use them fail (update Claude Code)", out.splitlines())
        (self.claude.parent / "help.txt").write_text(support.HELP_FULL.replace("  --effort <x>\n", ""))
        _, out, _ = self.doctor()
        self.assertIn("WARN\tclaude flags\tnot listed in claude --help: --effort; "
                      "headless calls that use them fail (update Claude Code)", out.splitlines())
        (self.home / "config.json").write_text(json.dumps({"claude_bin": "/nowhere/claude"}))
        _, out, _ = self.doctor(missing=("age", "/nowhere/claude"))
        self.assertIn("WARN\tclaude\t/nowhere/claude not found (config claude_bin); "
                      "orche run, intake and triage cannot start claude", out.splitlines())
        self.assertFalse(any(l.split("\t")[1:2] == ["claude flags"] for l in out.splitlines()))

    def test_orche_missing_from_login_shell_warns_with_path_hint(self):
        registry.save(registry.empty())
        _, out, _ = self.doctor()
        self.assertIn(f"OK\torche on PATH\t{support.ROOT / 'bin' / 'orche'}", out.splitlines())
        self.login["command -v orche"] = None
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        self.assertIn(f"WARN\torche on PATH\t{LOGIN_SH} -lc cannot find orche; add to your shell profile: "
                      f"export PATH=\"{support.ROOT / 'bin'}:$PATH\"", out.splitlines())

    def test_warn_for_missing_age_ok_for_valid_registry(self):
        registry.save(registry.empty())
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertIn("WARN\ttool age\tnot found on PATH", lines)
        self.assertIn("OK\tregistry\tvalid (0 project(s))", lines)
        self.assertTrue(any(l.startswith("OK\torca\t") for l in lines))
        self.assertTrue(any(l.startswith("WARN\tstatusline\t") for l in lines))
        self.assertIn(f"OK\thooks\torche plugin enabled for new sessions ({self.c})", lines)
        self.assertTrue(any(l.startswith("WARN\tcleanupPeriodDays\t") for l in lines))
        self.assertTrue(all(l.split("\t")[0] in ("OK", "WARN", "FAIL") for l in lines[:lines.index("")]))
        self.assertIn("WARN\tjev\tnot configured (Haiku fallback)", lines)  # Jev is optional
        self.assertFalse([l for l in lines if "\tupgrade\t" in l])
        self.install("--mode", "settings")
        code, out, _ = self.doctor(missing=())
        self.assertEqual([l for l in out.split("\n\n")[0].splitlines() if not l.startswith("OK\t")],
                         ["WARN\tjev\tnot configured (Haiku fallback)", f"WARN\toauth token\t{oauth.UNSET_LINE}",
                          "WARN\theartbeat\tno pass on file — install the job with orche install, "
                          "or run orche reconcile"]
                         + ([f"WARN\treconcile job\tnot loaded: launchctl bootstrap {launchd.domain()} "
                             f"{launchd.plist_path(launchd.RECONCILE, self.h)}"]
                            if sys.platform == "darwin" else []))

    def test_warn_for_a_project_rendered_by_another_version_only(self):
        reg = registry.empty()
        sample = json.loads((support.FIXTURES / "registry.sample.json").read_text())["projects"]["acme-shop"]
        reg["projects"]["acme-shop"] = dict(sample)                                   # no stamp: stale
        reg["projects"]["beta"] = dict(sample, repo_id="repo-beta", orche_version="0.0.1")   # older: stale
        reg["projects"]["gamma"] = dict(sample, repo_id="repo-gamma", orche_version=VERSION)  # current
        registry.save(reg)
        _, out, _ = self.doctor()
        self.assertEqual([l for l in out.splitlines() if "\tupgrade\t" in l], [
            f"WARN\tupgrade\tacme-shop rendered by an earlier version, now {VERSION} — run orche init acme-shop",
            f"WARN\tupgrade\tbeta rendered by 0.0.1, now {VERSION} — run orche init beta"])

    def test_no_careful_row_without_the_careful_hook(self):
        registry.save(registry.empty())
        _, out, _ = self.doctor()
        self.assertFalse(any("careful" in l for l in out.splitlines()), out)

    def test_careful_hook_in_settings_warns(self):
        registry.save(registry.empty())
        own = 'bash -c "exec bash \\"$HOME/.claude/skills/gstack/careful/bin/check-careful.sh\\""'
        self.settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": own}]})
        (self.c / "settings.json").write_text(json.dumps(self.settings, indent=2) + "\n")
        _, out, _ = self.doctor()
        self.assertIn("WARN\tcareful hook\tcareful hook registered in settings.json — its approval dialogs stall "
                      "unattended Orche panes; remove it or keep it only for your own sessions", out.splitlines())

    def test_hooks_registered_twice_warns(self):
        registry.save(registry.empty())
        support.plugin_installed(self.claude.parent, disabled_in=self.c)
        self.install("--mode", "settings")
        _, out, _ = self.doctor()
        self.assertIn(f"OK\thooks\tall 8 Orche hooks in {self.c / 'settings.json'}", out.splitlines())
        data = self.read_settings()
        data["enabledPlugins"]["orche@orche"] = True   # the owner turns the plugin on again
        (self.c / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        _, out, _ = self.doctor()
        self.assertIn(f"WARN\thooks\thooks registered twice (plugin + 8 entries in {self.c / 'settings.json'}): run "
                      "orche install (the plugin wins, the settings entries go)", out.splitlines())
        self.install()   # R4: the plugin wins and the settings entries go; doctor is quiet again
        _, out, _ = self.doctor()
        self.assertIn(f"OK\thooks\torche plugin enabled for new sessions ({self.c})", out.splitlines())

    def test_hooks_off_is_a_fail_in_plain_words(self):
        registry.save(registry.empty())
        support.plugin_installed(self.claude.parent, disabled_in=self.c)
        code, out, _ = self.doctor()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL\thooks\tOrche's hooks are off: new sessions run without Orche. Say \"repair the setup\"; "
                      f"Main runs orche install ({self.c / 'settings.json'}: 0 of 8 hooks, the orche plugin not "
                      "enabled)", out.splitlines())
        self.assertNotIn("restart", out.lower())

    def test_invalid_registry_fails_and_orca_down_warns(self):
        (self.home / "registry.json").write_text("{nope")
        shell.RUNNER, _ = ok_runner({"status": None})
        code, out, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertTrue(any(l.startswith("FAIL\tregistry\t") for l in out.splitlines()), out)
        self.assertTrue(any(l.startswith("WARN\torca\t") for l in out.splitlines()), out)


if __name__ == "__main__":
    unittest.main()
