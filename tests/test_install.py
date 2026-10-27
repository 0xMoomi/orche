"""install / uninstall / doctor / apply-all against a temp HOME (`--home`) and a stubbed Orca."""
import hashlib
import io
import json
import os
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
from test_hr import HrCase
from test_registry import load_cli

REAL_RUNNER = shell.RUNNER
SEGMENT = PLUGIN_ROOT / "templates" / "statusline-segment.sh"
# One per hooks.json handler: dispatch_relay serves Stop and StopFailure.
HOOK_NAMES = ("dispatch_relay", "dispatch_relay", "report_inject", "env_read_gate", "scope_gate", "session_start", "session_end")
PLUGIN_CMDS = [f'"{PLUGIN_ROOT}/hooks/run.sh" {name}' for name in HOOK_NAMES]

STATUSLINE = """#!/bin/bash
input=$(cat)
current_dir_path=$(echo "$input" | grep -o '"current_dir":"[^"]*"' | sed 's/"current_dir":"//;s/"$//')
current_dir=$(basename "$current_dir_path")
output="dir:${current_dir}"
printf "%s\\n" "$output\""""  # no trailing newline, like the real script

FABLE = """## Fable orchestration mode

Model structure (fixed 2026-09-26): thinking and coordination = Claude Fable 5.1 (main session) /
execution = Claude Opus 5.5 (`claude-opus-5-5`) / chores = Claude Haiku 4.5.

Routing:
- read-only exploration and research -> model: sonnet
  (Sonnet 5, half of Opus 5.5's price; places already on Sonnet 5.5 stay as they are)
- code spans stay as they are: `Haiku 4.5`

Haiku 4.5 in an ordinary paragraph stays as it is.
"""
FIXED = FABLE.replace("Claude Haiku 4.5.", "Claude Haiku 5.5.").replace("(Sonnet 5,", "(Sonnet 5.5,")
RUNNER = "---\nname: runner\ndescription: chores\nmodel: claude-haiku-4-5-20251001\neffort: low\n---\n\nbody\n"


def tree_digest(root) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
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

    def run(argv, *, timeout, input, env=None):
        if argv[0] != "orca":
            return REAL_RUNNER(argv, timeout=timeout, input=input)
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
        (c / "fable" / "agents").mkdir(parents=True)
        (c / "agents").mkdir()
        (self.h / ".codex").mkdir()
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
        (c / "fable" / "fable.md").write_text(FABLE)
        (c / "fable" / "active.md").write_text(FABLE)
        (c / "fable" / "empty.md").write_text("<!-- fable off -->\n")
        (c / "fable" / "agents" / "runner.md").write_text(RUNNER)
        (c / "fable" / "agents" / "deep-reasoner.md").write_text("---\nname: deep-reasoner\n---\n")
        (c / "agents" / "runner.md").write_text("my own runner\n")  # pre-existing regular file
        (c / "agents" / "deep-reasoner.md").symlink_to(c / "fable" / "agents" / "deep-reasoner.md")
        (self.h / ".codex" / "AGENTS.md.recovered").write_text("codex rules\n")
        self.c = c
        # doctor probes `claude --version/--help`: a fake script, never the real binary.
        self.claude = support.write_fake_claude(self.home / "fake-claude")
        (self.home / "config.json").write_text(json.dumps({"claude_bin": str(self.claude)}))
        shell.RUNNER, self.calls = ok_runner({"status": {"running": True}})
        self.addCleanup(setattr, shell, "RUNNER", REAL_RUNNER)

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
        code, out, err = self.orche("--dry-run", "install", "--home", str(self.h), "--yes")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(tree_digest(self.home), before)
        for expected in (f"settings: would patch {self.c / 'settings.json'}",
                         "settings: would set (cleanupPeriodDays (absent) -> 3650)",
                         "settings: would set (env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (absent) -> 50)",
                         f"settings: would backup {self.c / 'settings.json'}",
                         f"statusline: would patch {self.c / 'statusline-command.sh'}",
                         f"agents: would symlink {self.c / 'agents' / 'exec.md'}",
                         f"agents: skip {self.c / 'agents' / 'runner.md'} (existing regular file; left untouched)",
                         f"runner: would patch {self.c / 'fable' / 'agents' / 'runner.md'} "
                         "(model claude-haiku-4-5-20251001 -> claude-haiku-5-5)",
                         "fable: would replace (fable.md:4 Haiku 4.5 -> Haiku 5.5)",
                         "fable: would replace (fable.md:8 Sonnet 5 -> Sonnet 5.5)",
                         f"fable: would patch {self.c / 'fable' / 'active.md'} (mirrors fable.md)",
                         f"codex: would move {self.h / '.codex' / 'AGENTS.md.recovered'}",
                         "/plugin install orche@orche"):
            self.assertIn(expected, out)
        self.assertEqual(out.splitlines()[-1], "  /plugin install orche@orche")

    def test_no_plugin_hint_drops_the_two_plugin_lines(self):
        out = self.install("--no-jev")
        self.assertIn(f"  /plugin marketplace add {PLUGIN_ROOT}", out)
        self.assertIn("  /plugin install orche@orche", out)
        out = self.install("--no-jev", "--no-plugin-hint")
        self.assertNotIn("/plugin", out)
        self.assertNotIn("Install the ORCHE hooks as a Claude Code plugin", out)
        self.assertEqual(out.splitlines()[-1], "jev: skipped (--no-jev); add later with `orche jev setup`")
        code, out, err = self.orche("--json", "install", "--home", str(self.h), "--no-jev", "--no-plugin-hint")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(json.loads(out)["instructions"], [])

    def test_install_backs_up_merges_and_is_idempotent(self):
        out = self.install()
        self.assertEqual(out.splitlines()[-1], "  /plugin install orche@orche")
        data = self.read_settings()
        for key, value in self.settings.items():
            if key not in ("env", "hooks"):
                self.assertEqual(data[key], value, key)
        self.assertEqual(data["cleanupPeriodDays"], 3650)
        self.assertEqual(data["env"], {**self.settings["env"], "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
        self.assertEqual(data["hooks"], self.settings["hooks"])  # plugin mode
        self.assertIn("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL left untouched", out)
        # Status line: one guarded block appended after the original bytes.
        script = (self.c / "statusline-command.sh").read_text()
        self.assertTrue(script.startswith(STATUSLINE + "\n# >>> orche >>>\n"))
        self.assertEqual(script.count("# >>> orche >>>"), 1)
        # Agents: ours linked, foreign files and links untouched and reported.
        for name in ("exec.md", "hr.md"):
            self.assertEqual(os.readlink(self.c / "agents" / name), str(PLUGIN_ROOT / "agents" / name))
        self.assertEqual((self.c / "agents" / "runner.md").read_text(), "my own runner\n")
        self.assertEqual(os.readlink(self.c / "agents" / "deep-reasoner.md"),
                         str(self.c / "fable" / "agents" / "deep-reasoner.md"))
        self.assertIn("existing symlink to", out)
        self.assertIn("model: claude-haiku-5-5\n", (self.c / "fable" / "agents" / "runner.md").read_text())
        # Fable: wording fixed on routing lines only, block appended, active mirrors, empty untouched.
        fable = (self.c / "fable" / "fable.md").read_text()
        self.assertTrue(fable.startswith(FIXED + "\n<!-- >>> orche >>> -->\n"))
        self.assertIn("ORCHE rules supersede the routing table above where they conflict", fable)
        self.assertIn(str(PLUGIN_ROOT / "docs" / "principles.md"), fable)
        self.assertIn("TO GOAL SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER", fable)
        self.assertNotIn("5.5.5", fable)
        self.assertEqual((self.c / "fable" / "active.md").read_text(), fable)
        self.assertEqual((self.c / "fable" / "empty.md").read_text(), "<!-- fable off -->\n")
        # Codex: not moved without --yes.
        self.assertFalse((self.h / ".codex" / "AGENTS.md").exists())
        self.assertIn("re-run with --yes", out)
        # Backups next to each patched original.
        names = {Path(b).name.split(".bak-")[0] for b in self.backups()}
        self.assertEqual(names, {"settings.json", "statusline-command.sh", "fable.md", "active.md", "runner.md"})
        self.assertEqual(json.loads(next(self.c.glob("settings.json.bak-*")).read_text()), self.settings)
        self.assertTrue(self.home.joinpath("status").is_dir())  # ORCHE_HOME dirs
        # Second run: byte-identical files, no new backups.
        snap, backups = tree_digest(self.h), self.backups()
        out2 = self.install()
        self.assertEqual(tree_digest(self.h), snap)
        self.assertEqual(self.backups(), backups)
        self.assertNotIn("would", out2)
        self.assertIn(f"settings: ok {self.c / 'settings.json'} (unchanged)", out2)

    def test_active_md_left_alone_when_it_does_not_mirror_fable(self):
        (self.c / "fable" / "active.md").write_text("<!-- fable off -->\n")
        out = self.install()
        self.assertEqual((self.c / "fable" / "active.md").read_text(), "<!-- fable off -->\n")
        self.assertIn("differs from fable.md", out)
        self.assertIn("<!-- >>> orche >>> -->", (self.c / "fable" / "fable.md").read_text())

    def test_settings_mode_registers_seven_hooks_once(self):
        self.install("--mode", "settings")
        out = self.install("--mode", "settings")
        cmds = self.commands(self.read_settings())
        for cmd in PLUGIN_CMDS:
            self.assertEqual(cmds.count(cmd), PLUGIN_CMDS.count(cmd), cmd)
        self.assertEqual(len(cmds), 2 + 7)
        pre = self.read_settings()["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], self.settings["hooks"]["PreToolUse"][0])  # existing entries keep their order
        self.assertEqual([e.get("matcher") for e in pre[1:]], ["Read|Bash|Grep|Edit|Write",
                                                               "Edit|Write|NotebookEdit|MultiEdit"])
        self.assertNotIn("/plugin install", out)
        self.assertNotIn("careful", out)

    # -- R4: switching install modes never leaves two Stop hooks --------------------------------
    def test_plugin_install_after_settings_install_removes_settings_hooks(self):
        self.install("--mode", "settings")
        backups = len(self.backups())
        out = self.install()   # plugin mode (the default)
        self.assertEqual(self.read_settings()["hooks"], self.settings["hooks"])
        self.assertIn(f"settings: remove (hook Stop {PLUGIN_CMDS[0]} (plugin mode: the plugin registers "
                      "the hooks))", out)
        self.assertEqual(len(self.backups()), backups + 1)
        self.assertEqual(self.state()["added_hooks"], [])
        self.assertEqual(self.state()["mode"], "plugin")
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
        self.install()
        self.assertEqual(self.read_settings()["hooks"], self.settings["hooks"])
        self.install("--mode", "settings")
        self.install("--mode", "settings")
        cmds = self.commands(self.read_settings())
        self.assertEqual([cmds.count(c) for c in PLUGIN_CMDS], [PLUGIN_CMDS.count(c) for c in PLUGIN_CMDS])
        self.assertEqual(len(cmds), 2 + 7)
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

    def test_agents_md_moved_only_with_yes(self):
        self.install()
        self.assertTrue((self.h / ".codex" / "AGENTS.md.recovered").exists())
        out = self.install("--yes")
        self.assertEqual((self.h / ".codex" / "AGENTS.md").read_text(), "codex rules\n")
        self.assertFalse((self.h / ".codex" / "AGENTS.md.recovered").exists())
        self.assertIn("codex: move", out)

    def test_invalid_settings_json_is_left_untouched(self):
        (self.c / "settings.json").write_text("{broken")
        code, out, _ = self.orche("install", "--home", str(self.h))
        self.assertEqual(code, 1)
        self.assertEqual((self.c / "settings.json").read_text(), "{broken")
        self.assertIn("settings: error", out)

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
        self.assertEqual((self.c / "fable" / "fable.md").read_text(), FIXED)
        self.assertEqual((self.c / "fable" / "active.md").read_text(), FIXED)
        self.assertEqual(self.read_settings(), self.settings)  # hooks removed, absent keys removed again
        for name in ("exec.md", "hr.md"):
            self.assertFalse((self.c / "agents" / name).is_symlink())
        self.assertEqual((self.c / "agents" / "runner.md").read_text(), "my own runner\n")
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
        (self.c / "statusline-command.sh").write_bytes(b"#!/bin/bash\necho \xff\n")
        code, out, err = self.orche("install", "--home", str(self.h))
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("install: error", out)
        self.assertIn("install: error (statusline-command.sh: UnicodeDecodeError: ", out)
        self.assertNotIn(f"error ({self.h}", out)
        self.assertNotIn("Traceback", out)
        self.assertIn("<!-- >>> orche >>> -->", (self.c / "fable" / "fable.md").read_text())  # later steps ran
        self.assertTrue((self.c / "agents" / "exec.md").is_symlink())
        self.assertIn("originals", self.state())
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 1)  # the statusline step fails again, the others still run
        self.assertEqual(self.read_settings(), self.settings, out)
        self.assertFalse((self.c / "agents" / "exec.md").exists())

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

    def test_agents_md_appearing_before_the_move_is_not_replaced(self):
        from orche.install import codex
        with mock.patch.object(codex.os, "link", side_effect=FileExistsError("AGENTS.md")):
            code, out, _ = self.orche("install", "--home", str(self.h), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("appeared before the move; refusing to replace it", out)
        self.assertTrue((self.h / ".codex" / "AGENTS.md.recovered").is_file())

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
        self.assertIn(f"skills: skip {self.skills() / 'orche'} (existing entry, not ORCHE's; left untouched)", out)
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
        out = self.install()
        for name in self.NAMES:
            self.assertFalse((self.skills() / name).is_symlink(), name)
            self.assertIn(f"skills: remove {self.skills() / name}", out)
        self.assertIn("plugin mode: the plugin ships the skills", out)
        self.assertTrue((self.skills() / "mine").is_dir())

    def test_plain_plugin_mode_says_nothing_about_skills(self):
        out = self.install()
        self.assertNotIn("skills:", out)
        self.assertFalse(self.skills().exists())

    def test_uninstall_removes_our_links(self):
        self.install("--mode", "settings")
        code, out, err = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(list(self.skills().iterdir()), [])
        self.assertIn(f"skills: remove {self.skills() / 'orche'}", out)


class SegmentTest(InstallCase):
    def run_sh(self, path, cwd_json, env_extra=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.h), "ORCHE_HOME": str(self.home)}
        env.update(env_extra or {})
        stdin = json.dumps({"model": {"display_name": "Fable"}, "workspace": {"current_dir": str(cwd_json)}},
                           separators=(",", ":"))
        return subprocess.run(["bash", str(path)], input=stdin, capture_output=True, text=True, env=env,
                              cwd=str(self.home), timeout=10)

    def test_segment_prints_cached_line_and_nothing_when_absent(self):
        repo = self.home / "repo"
        (repo / ".orche").mkdir(parents=True)
        (repo / "src" / "deep").mkdir(parents=True)
        (self.home / "status").mkdir()
        res = self.run_sh(SEGMENT, repo / "src" / "deep")
        self.assertEqual((res.stdout, res.stderr, res.returncode), ("", "", 0))  # no marker yet
        (repo / ".orche" / "project").write_text("acme-shop\n")
        res = self.run_sh(SEGMENT, repo / "src" / "deep")
        self.assertEqual((res.stdout, res.stderr, res.returncode), ("", "", 0))  # no .line yet
        (self.home / "status" / "acme-shop.line").write_text("R&D● BUILD●3 · new1\n")
        res = self.run_sh(SEGMENT, repo / "src" / "deep")
        self.assertEqual((res.stdout, res.stderr, res.returncode), ("R&D● BUILD●3 · new1\n", "", 0))
        (repo / ".orche" / "project").write_text("../../etc/passwd\n")  # not a project key
        self.assertEqual(self.run_sh(SEGMENT, repo).stdout, "")
        self.assertEqual(self.run_sh(SEGMENT, self.home / "elsewhere").stdout, "")

    def test_block_goes_before_a_final_exit_and_uninstall_restores(self):
        script = self.c / "statusline-command.sh"
        original = STATUSLINE.rstrip("\n") + "\nexit 0\n# trailing comment\n"
        script.write_text(original)
        self.install()
        text = script.read_text()
        self.assertLess(text.index("# >>> orche >>>"), text.index("\nexit 0"))
        repo = self.home / "repo"
        (repo / ".orche").mkdir(parents=True)
        (repo / ".orche" / "project").write_text("acme-shop\n")
        (self.home / "status").mkdir(exist_ok=True)
        (self.home / "status" / "acme-shop.line").write_text("BUILD●\n")
        self.assertEqual(self.run_sh(script, repo).stdout, "dir:repo\nBUILD●\n")
        code, out, _ = self.orche("uninstall", "--home", str(self.h))
        self.assertEqual(code, 0, out)
        self.assertEqual(script.read_text(), original)

    def test_doctor_warns_when_block_after_exit(self):
        from orche.install import statusline
        script = self.c / "statusline-command.sh"
        blk = SEGMENT.read_text()
        script.write_text(STATUSLINE.rstrip("\n") + "\nexit 0\n\n" + blk)
        self.assertTrue(statusline.after_exit(script.read_text()))
        registry.save(registry.empty())
        with mock.patch.dict(os.environ, {"SHELL": str(self.home / "no-login-shell")}):  # never the real one
            _, out, _ = self.orche("doctor", "--home", str(self.h))
        self.assertTrue(any(l.startswith("WARN\tstatusline\t") and "after an `exit`" in l
                            for l in out.splitlines()), out)
        self.install()  # re-install moves the block before the exit
        self.assertFalse(statusline.after_exit(script.read_text()))

    def test_installed_script_keeps_its_output_and_adds_the_segment(self):
        self.install()
        repo = self.home / "repo"
        (repo / ".orche").mkdir(parents=True)
        (repo / ".orche" / "project").write_text("acme-shop\n")
        (self.home / "status").mkdir(exist_ok=True)
        (self.home / "status" / "acme-shop.line").write_text("BUILD●\n")
        script = self.c / "statusline-command.sh"
        self.assertEqual(self.run_sh(script, repo).stdout, "dir:repo\nBUILD●\n")
        self.assertEqual(self.run_sh(script, self.home).stdout, f"dir:{self.home.name}\n")


LOGIN_SH = "/fake/login-sh"


class DoctorTest(InstallCase):
    def setUp(self):
        super().setUp()
        # `$SHELL -lc ...` answers (never the developer's real login shell): command -> stdout or None.
        self.login = {"command -v python3 && python3 --version 2>&1": "/opt/homebrew/bin/python3\nPython 3.14.0\n",
                      "command -v orche": f"{support.ROOT / 'bin' / 'orche'}\n"}
        self.login_calls = []

    def doctor(self, missing=("age",)):
        real = doctor.shutil.which
        which = (lambda tool: None if tool in missing else real("python3") if tool == "python3" else real("sh"))
        inner = shell.RUNNER

        def runner(argv, *, timeout, input, env=None):
            if argv[0] == LOGIN_SH and argv[1] == "-lc":
                self.login_calls.append(argv)
                out = self.login.get(argv[2])
                return shell.Result(0 if out else 1, out or "", "", argv)
            return inner(argv, timeout=timeout, input=input)
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
        self.assertFalse(any(ln.startswith(("WARN", "FAIL")) for ln in steps))   # install.sh greps those only
        self.json_doctor = True
        code, out, _ = self.doctor()
        data = json.loads(out)
        self.assertEqual(data["next_steps"], steps)
        self.assertTrue(all({"check", "level", "detail"} <= set(row) for row in data["checks"]))

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
        self.assertIn("OK\tclaude flags\tall 15 listed in claude --help", out.splitlines())
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

    def test_without_terminal_notifier_the_row_says_what_a_click_does(self):
        """c62, c65: terminal-notifier is optional, never a WARN; the row and the next steps say what a click opens."""
        registry.save(registry.empty())
        code, out, _ = self.doctor(missing=("terminal-notifier",))
        self.assertEqual(code, 0, out)
        self.assertIn("OK\ttool terminal-notifier\tnot installed (optional: brew install terminal-notifier makes a "
                      "notification click open the project's Main tab; allow its notifications once when macOS "
                      "asks)", out.splitlines())
        self.assertIn('- Notification click: opens Orca (enough for most; the exact tab needs terminal-notifier). '
                      'Say "make notification clicks open the tab"', out)

    def test_warn_for_missing_age_ok_for_valid_registry(self):
        registry.save(registry.empty())
        code, out, _ = self.doctor()
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertIn("WARN\ttool age\tnot found on PATH", lines)
        self.assertTrue(any(l.startswith("OK\ttool terminal-notifier\t") for l in lines))
        self.assertIn("OK\tregistry\tvalid (0 project(s))", lines)
        self.assertTrue(any(l.startswith("OK\torca\t") for l in lines))
        self.assertTrue(any(l.startswith("WARN\tstatusline\t") for l in lines))
        self.assertTrue(any(l.startswith("WARN\thooks\t") for l in lines))
        self.assertTrue(any(l.startswith("WARN\tcleanupPeriodDays\t") for l in lines))
        self.assertTrue(all(l.split("\t")[0] in ("OK", "WARN", "FAIL") for l in lines[:lines.index("")]))
        self.assertIn("WARN\tjev\tnot configured (Haiku fallback)", lines)  # Jev is optional
        self.assertFalse([l for l in lines if "\tupgrade\t" in l])
        self.install("--mode", "settings", "--yes")
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

    def test_author_specific_files_skipped_when_absent(self):
        registry.save(registry.empty())
        for p in (self.h / ".codex" / "AGENTS.md", self.h / ".codex" / "AGENTS.md.recovered"):
            if p.exists():
                p.unlink()
        _, out, _ = self.doctor()
        lines = out.splitlines()
        self.assertIn("OK\tcodex AGENTS.md\tskipped (not present)", lines)
        self.assertFalse(any("careful" in l for l in lines), out)
        (self.h / ".codex").mkdir(exist_ok=True)
        (self.h / ".codex" / "AGENTS.md.recovered").write_text("x")
        _, out, _ = self.doctor()
        self.assertTrue(any(l.startswith("WARN\tcodex AGENTS.md\t") and "recovered" in l
                            for l in out.splitlines()), out)

    def test_careful_hook_in_settings_warns(self):
        registry.save(registry.empty())
        own = 'bash -c "exec bash \\"$HOME/.claude/skills/gstack/careful/bin/check-careful.sh\\""'
        self.settings["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": own}]})
        (self.c / "settings.json").write_text(json.dumps(self.settings, indent=2) + "\n")
        _, out, _ = self.doctor()
        self.assertIn("WARN\tcareful hook\tcareful hook registered in settings.json — its approval dialogs stall "
                      "unattended ORCHE panes; remove it or keep it only for your own sessions", out.splitlines())

    def test_hooks_registered_twice_warns(self):
        registry.save(registry.empty())
        self.install("--mode", "settings")
        data = self.read_settings()
        data["enabledPlugins"]["orche@orche"] = True
        (self.c / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        _, out, _ = self.doctor()
        self.assertIn("WARN\thooks\thooks registered twice (plugin + 7 settings.json entries): run orche install "
                      "(plugin mode removes the settings entries) or disable the plugin", out.splitlines())
        self.install()   # R4: plugin mode removes them; doctor is quiet again
        _, out, _ = self.doctor()
        self.assertIn("OK\thooks\torche plugin enabled", out.splitlines())

    def test_invalid_registry_fails_and_orca_down_warns(self):
        (self.home / "registry.json").write_text("{nope")
        shell.RUNNER, _ = ok_runner({"status": None})
        code, out, _ = self.doctor()
        self.assertEqual(code, 1)
        self.assertTrue(any(l.startswith("FAIL\tregistry\t") for l in out.splitlines()), out)
        self.assertTrue(any(l.startswith("WARN\torca\t") for l in out.splitlines()), out)


class ApplyAllDryRunTest(InstallCase):
    def test_dry_run_lists_plans_and_calls_no_mutating_orca(self):
        reg = registry.empty()
        reg["projects"]["acme-shop"] = json.loads((support.FIXTURES / "registry.sample.json").read_text())[
            "projects"]["acme-shop"]
        registry.save(reg)
        repos = [{"id": "repo-acme", "path": "/srv/orca/acme-shop", "displayName": "Acme Shop"},
                 {"id": "repo-beta", "path": str(self.home / "beta"), "displayName": "Beta App"},
                 {"id": "repo-gamma", "path": str(self.home / "gamma"), "displayName": "Gamma"}]
        shell.RUNNER, calls = ok_runner({"status": {}, "repo list": {"repos": repos}})
        before = tree_digest(self.home)
        code, out, err = self.orche("--dry-run", "apply-all", "--wake", "build")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(tree_digest(self.home), before)
        self.assertEqual([" ".join(c[1:3]) for c in calls], ["status --json", "repo list"])
        plans = [l for l in out.splitlines() if l.startswith("plan ")]
        self.assertEqual(plans, ["plan Beta App (beta-app): orche init repo-beta --stage building --divisions rnd,research,build,gtm,ops --no-intake --wake build",
                                 "plan Gamma (gamma): orche init repo-gamma --stage building --divisions rnd,research,build,gtm,ops --no-intake --wake build"])
        self.assertIn("skip Acme Shop: already registered as acme-shop", out)
        self.assertIn("3 repo(s), 2 planned, 0 failed", out)
        self.assertIn("panes: 4 to open (main per repo, plus --wake)", out)
        self.assertEqual(out.splitlines()[-1], "intake not run for 2 projects: run `orche intake <proj>` "
                                               "per project (about $0.1–0.3 each)")

    def test_plugin_repo_itself_is_skipped(self):
        registry.save(registry.empty())
        repos = [{"id": "repo-orche", "path": str(PLUGIN_ROOT), "displayName": "ORCHE"}]
        shell.RUNNER, _ = ok_runner({"status": {}, "repo list": {"repos": repos}})
        code, out, err = self.orche("--dry-run", "apply-all")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("skip ORCHE: skipped (ORCHE plugin repo)", out)
        self.assertIn("1 repo(s), 0 planned, 0 failed", out)

    def test_refuses_when_orca_is_down(self):
        shell.RUNNER, calls = ok_runner({"status": None})
        code, out, err = self.orche("apply-all")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Orca is not running", err)
        self.assertEqual(len(calls), 1)


class ApplyAllRunTest(HrCase):
    def fake_runner(self, argv, *, timeout, input, env=None, cwd=None):
        if argv[0] == "orca" and argv[1] == "status":
            return shell.Result(0, json.dumps({"ok": True, "result": {}}), "", argv)
        if argv[0] == "orca" and argv[1:3] == ["repo", "list"]:
            self.calls.append(argv)
            repos = [{"id": "repo-acme", "path": str(self.repo), "displayName": "Acme Shop"},
                     {"id": "repo-beta", "path": str(self.beta), "displayName": "Beta"},
                     {"id": "repo-gone", "path": str(self.home / "gone"), "displayName": "Gone"}]
            if getattr(self, "dup", False):
                repos = [repos[0], {"id": "repo-acme-2", "path": str(self.repo), "displayName": "Acme Shop again"}]
            return shell.Result(0, json.dumps({"ok": True, "result": {"repos": repos}}), "", argv)
        return super().fake_runner(argv, timeout=timeout, input=input, cwd=cwd)

    def test_inits_each_unregistered_repo_honours_awake_limit_and_continues_past_failure(self):
        self.beta = self.home / "repos" / "beta"
        self.beta.mkdir(parents=True)
        (self.home / "gone").write_text("not a directory\n")  # init fails for this repo
        reg = registry.empty()
        reg["limits"]["max_awake_sessions"] = 1
        registry.save(reg)
        from orche import intake
        swept = []
        with mock.patch.object(intake, "run", lambda *a, **k: swept.append(a)):
            code, out, err = self.orche("apply-all", "--wake", "build")
        self.assertEqual(code, 1, err)
        self.assertEqual(swept, [])                    # I-3: apply-all never runs the paid intake
        reg = registry.load()
        self.assertEqual(sorted(reg["projects"]), ["acme-shop", "beta"])
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["status"], "active")
        self.assertEqual(reg["projects"]["beta"]["divisions"]["build"]["status"], "dormant")
        self.assertEqual((self.beta / ".orche" / "project").read_text(), "beta\n")
        rows = out.splitlines()
        self.assertEqual(rows[0], "repo\tkey\tcreated\tskipped\tpanes\tresult")
        self.assertTrue(rows[1].startswith("Acme Shop\tacme-shop\trnd,research,build,gtm,ops\t-\t"
                                           "main-acme-shop\tok"), rows[1])
        self.assertIn("ok [--wake build dropped (awake limit 1)]", rows[2])
        self.assertTrue(rows[3].startswith("Gone\tgone\t-\t-\t-\tfailed (exit "), rows[3])
        self.assertEqual(rows[-3], "3 repo(s), 3 processed, 1 failed")
        self.assertEqual(rows[-2], "panes: 3 opened (main per repo, plus --wake)")
        self.assertEqual(rows[-1], "intake not run for 2 projects: run `orche intake <proj>` per project "
                                   "(about $0.1–0.3 each)")

    def test_second_orca_entry_for_onboarded_path_is_already_registered(self):
        self.beta = self.home / "repos" / "beta"
        registry.save(registry.empty())
        self.dup = True
        code, out, err = self.orche("apply-all")
        self.assertIn("skip Acme Shop again: already registered as acme-shop", out)
        self.assertEqual(sorted(registry.load()["projects"]), ["acme-shop"])


if __name__ == "__main__":
    unittest.main()
