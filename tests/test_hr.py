"""HR: init onboarding, feat new/sleep/wake/retire, limits, stage set, hr audit/correct (stubbed Orca)."""
import copy
import datetime as dt
import io
import json
import os
import shlex
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from orche import hr, registry, roster, shell, statusline, templates, units
from orche.hr import audit, book, limits, onboarding
from orche.paths import PLUGIN_ROOT
from test_registry import load_cli

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
PANE = f"PATH={support.ROOT / 'bin'}:$PATH "   # every pane command puts the plugin's bin/ on PATH
REAL_RUNNER = shell.RUNNER
ALL = ",".join(registry.DIVISIONS)   # the five standard divisions (the setup default leaves ops out)


def ago(**kw) -> str:
    return (NOW - dt.timedelta(**kw)).isoformat()


def git(*argv, cwd):
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)


def git_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", branch, cwd=path)
    git("config", "user.email", "t@example.com", cwd=path)
    git("config", "user.name", "t", cwd=path)
    git("config", "commit.gpgsign", "false", cwd=path)
    git("config", "core.excludesFile", os.devnull, cwd=path)  # ignore the machine's global gitignore
    (path / "README.md").write_text("x\n")
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    return path


class HrCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.repo = self.home / "repos" / "acme-shop"
        self.repo.mkdir(parents=True)
        self.wtroot = self.home / "worktrees"
        self.claude_root = self.home / "claude-projects"
        self.claude_root.mkdir()
        (self.home / "config.json").write_text(json.dumps(
            {"notify": False, "janitor": {"claude_projects": str(self.claude_root)}}))
        self._orig_now, units.now = units.now, lambda: NOW
        self.addCleanup(setattr, units, "now", self._orig_now)
        self.personas = self.home / "no-personas"
        self._orig_personas, onboarding.PERSONAS = onboarding.PERSONAS, self.personas
        self.addCleanup(setattr, onboarding, "PERSONAS", self._orig_personas)
        self.calls, self.terminals = [], []
        self.feature_branch = True
        self.nest = False
        self.worktree_list = []
        shell.RUNNER = self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", REAL_RUNNER)

    def fake_runner(self, argv, *, timeout, input):
        if argv[0] != "orca":
            return REAL_RUNNER(argv, timeout=timeout, input=input)
        self.calls.append(argv)
        key = " ".join(argv[1:3])
        result = {}
        if key == "repo list":
            result = {"repos": [{"id": "repo-acme", "path": str(self.repo), "displayName": "Acme Shop",
                                 "kind": "git"}]}
        elif key == "terminal list":
            result = {"terminals": self.terminals}
        elif key == "terminal create":   # as in Orca: the new pane is listed from now on
            title = argv[argv.index("--title") + 1]
            self.terminals.append({"handle": f"t-{title}", "title": title})
            result = {"terminal": {"handle": f"t-{title}", "title": title}}
        elif key == "worktree list":
            result = {"worktrees": self.worktree_list}
        elif key == "worktree create":
            name = argv[argv.index("--name") + 1]
            path = (self.repo / ".worktrees" if self.nest else self.wtroot) / name
            path.mkdir(parents=True, exist_ok=True)
            result = {"worktree": {"id": f"repo-acme::{path}", "path": str(path)}}
            if name.startswith("feat-") and self.feature_branch:
                result["worktree"]["branch"] = "refs/heads/feat/" + name[5:]
        return shell.Result(0, json.dumps({"ok": True, "result": result}), "", argv)

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def orca_calls(self, group_sub):
        return [c for c in self.calls if " ".join(c[1:3]) == group_sub]

    def sample(self):
        reg = copy.deepcopy(SAMPLE)
        proj = reg["projects"]["acme-shop"]
        proj["path"] = str(self.repo)
        for feat in proj["divisions"]["build"]["features"]:
            feat["created"] = feat["last_activity"] = ago(days=2)
        proj["divisions"]["build"]["last_activity"] = ago(hours=1)
        proj["divisions"]["rnd"]["last_activity"] = ago(days=1)
        reg["events"] = []
        return reg

    def save(self, reg):
        registry.save(reg)
        return reg


# ---- init ---------------------------------------------------------------------------------------

class InitTest(HrCase):
    def test_dry_run_prints_full_sequence_with_ops_dormant(self):
        code, out, err = self.orche("--dry-run", "init", "Acme Shop", "--divisions", ALL)
        self.assertEqual(code, 0, err)
        dry = [l for l in err.splitlines() if l.startswith("[dry-run] orca ")]
        creates = [l for l in dry if " worktree create " in l]
        self.assertEqual(creates, [f"[dry-run] orca worktree create --repo id:repo-acme --name div-{d} --no-parent --json"
                                   for d in registry.DIVISIONS])
        panes = [l for l in dry if " terminal create " in l]
        self.assertEqual(len(panes), 1)
        self.assertIn(f"--title main-acme-shop --command {PANE}ORCHE_SESSION=main-acme-shop claude "
                      "--permission-mode bypassPermissions --name main-acme-shop --json", panes[0])
        autos = [l for l in dry if " automations create " in l]
        self.assertEqual(len(autos), 2)
        self.assertIn("--name orche-hr-audit-acme-shop --trigger weekly --day 1 --time 09:00", autos[0])
        self.assertIn(f"--workspace path:{PLUGIN_ROOT}", autos[0])
        self.assertIn(f"--name orche-card-refresh-acme-shop", autos[1])
        self.assertIn("Run `orche card refresh acme-shop --background` and stop; do nothing else.", autos[1])
        self.assertIn(f"--workspace path:{os.path.realpath(self.repo)}", autos[1])
        self.assertIn("[dry-run] write ", err)
        self.assertIn("ops: dormant (gated by stage)", out)
        # Only read-only Orca calls ran; nothing was written.
        self.assertEqual({" ".join(c[1:3]) for c in self.calls}, {"repo list", "terminal list"})
        self.assertEqual(registry.load()["projects"], {})
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertFalse(self.wtroot.exists())

    def test_real_run_onboards_divisions(self):
        code, out, err = self.orche("init", "Acme Shop", "--aliases", "Acme,SHOP", "--wake", "build,gtm",
                                    "--divisions", ALL)
        self.assertEqual(code, 0, err)
        reg = registry.load()
        proj = reg["projects"]["acme-shop"]
        self.assertEqual((proj["repo_id"], proj["path"], proj["stage"], proj["aliases"]),
                         ("repo-acme", os.path.realpath(self.repo), "building", ["acme", "shop"]))
        divs = proj["divisions"]
        self.assertEqual({d: v["status"] for d, v in divs.items()},
                         {"rnd": "dormant", "research": "dormant", "build": "active", "gtm": "active",
                          "ops": "dormant"})
        self.assertTrue(divs["ops"]["gated_by_stage"])
        self.assertNotIn("gated_by_stage", divs["build"])
        self.assertEqual((divs["research"]["model"], divs["rnd"]["model"]),
                         ("claude-sonnet-5-5", "claude-opus-5-5"))
        self.assertFalse([d for d, v in divs.items() if "effort" in v])   # the persona holds it, not the record
        self.assertEqual(divs["build"]["branch"], "div/build")  # no branch in the result -> fallback
        wt = self.wtroot / "div-build"
        self.assertEqual(divs["build"]["worktree"], f"repo-acme::{wt}")
        self.assertEqual(sorted(p.name for p in (wt / ".orche" / "div" / "build").iterdir()),
                         ["CHARTER.md", "DECISIONS.tsv", "HANDOFF.md", "STATUS.md"])
        self.assertEqual(json.loads((wt / ".claude" / "settings.local.json").read_text()),
                         {"agent": "div-acme-shop-build", "crossSessionInbound": "accept"})
        persona = (self.repo / ".claude" / "agents" / "div-acme-shop-build.md").read_text()
        self.assertTrue(persona.startswith("---\nname: div-acme-shop-build\ndescription: "))
        self.assertIn("model: claude-opus-5-5\n---\n", persona)
        self.assertTrue((wt / ".claude" / "agents" / "div-acme-shop-build.md").is_file())
        self.assertEqual(sorted(p.name for p in (self.repo / ".claude" / "agents").iterdir()),   # division personas only
                         [f"div-acme-shop-{d}.md" for d in ("build", "gtm", "ops", "research", "rnd")])
        self.assertFalse((self.repo / ".claude" / "settings.local.json").exists())
        self.assertIn("persona template missing for division build", hr.log_path().read_text())
        # Panes: the two woken divisions, then main.
        panes = [c[c.index("--title") + 1:c.index("--title") + 4] for c in self.orca_calls("terminal create")]
        self.assertEqual(panes, [["div-acme-shop-build", "--command", f"{PANE}ORCHE_SESSION=div-acme-shop-build claude --permission-mode bypassPermissions --name div-acme-shop-build"],
                                 ["div-acme-shop-gtm", "--command", f"{PANE}ORCHE_SESSION=div-acme-shop-gtm claude --permission-mode bypassPermissions --name div-acme-shop-gtm"],
                                 ["main-acme-shop", "--command", f"{PANE}ORCHE_SESSION=main-acme-shop claude --permission-mode bypassPermissions --name main-acme-shop"]])
        self.assertEqual(self.orca_calls("automations create"), [])
        self.assertIn("[dry-run] orca automations create --name orche-hr-audit-acme-shop", err)
        # Project templates and card symlink.
        for rel in (".orche/STANDING.md", "PROJECT_CARD.md", "docs/RUBRIC.md", "docs/LEDGER.tsv",
                    "docs/GRAVEYARD.md", ".env.example"):
            self.assertTrue((self.repo / rel).is_file(), rel)
        link = self.home / "cards" / "acme-shop.md"
        self.assertEqual(os.readlink(link), str(Path(os.path.realpath(self.repo)) / "PROJECT_CARD.md"))
        self.assertEqual(proj["card"], str(link))
        self.assertEqual([(e["type"], e["unit"]) for e in reg["events"]],
                         [("create", d) for d in registry.DIVISIONS])

    def test_init_writes_project_marker_idempotently(self):
        code, _, err = self.orche("--dry-run", "init", "Acme Shop", "--divisions", "build")
        self.assertEqual(code, 0, err)
        marker = Path(os.path.realpath(self.repo)) / ".orche" / "project"
        self.assertIn(f"[dry-run] write {marker} (acme-shop)", err)
        self.assertFalse(marker.exists())
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        self.assertEqual(marker.read_text(), "acme-shop\n")
        marker.write_text("stale\n")
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build,gtm")[0], 0)  # resumed init
        self.assertEqual(marker.read_text(), "acme-shop\n")
        self.assertEqual(roster.Roster(registry.load()).place(self.repo / "src").project, "acme-shop")

    def test_wake_ops_refused_while_building(self):
        code, _, err = self.orche("init", "Acme Shop", "--wake", "ops", "--divisions", ALL)
        self.assertEqual(code, 3)
        self.assertIn("ops is gated by stage", err)
        self.assertEqual(registry.load()["projects"], {})
        self.assertEqual(self.orca_calls("worktree create"), [])

    def test_wake_beyond_awake_limit(self):
        reg = registry.empty()
        reg["limits"]["max_awake_sessions"] = 1
        self.save(reg)
        code, _, err = self.orche("init", "Acme Shop", "--wake", "build,gtm")
        self.assertEqual(code, 3)
        self.assertIn("awake limit would be exceeded (0+2/1); sleep a unit first", err)
        self.assertEqual(self.orca_calls("worktree create"), [])

    def test_init_by_registry_key_or_alias(self):
        """R5: the registry key (`acme-shop`) or an alias resolves before the Orca display name."""
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build", "--aliases", "shop")[0], 0)
        self.calls.clear()
        for needle in ("acme-shop", "SHOP"):
            code, out, err = self.orche("init", needle, "--divisions", "build,gtm")
            self.assertEqual(code, 0, err)
            self.assertIn("build: skipped (already onboarded)", out)
        self.assertEqual(sorted(registry.load()["projects"]), ["acme-shop"])
        self.assertEqual([c[c.index("--name") + 1] for c in self.orca_calls("worktree create")], ["div-gtm"])

    def test_registry_key_keeps_its_key_when_orca_name_differs(self):
        reg = registry.empty()
        reg["projects"]["shopfront"] = {"repo_id": "repo-acme", "path": str(self.repo), "stage": "building",
                                        "aliases": [], "main_session": "main-shopfront",
                                        "card": "~/c.md",
                                        "standing": ".orche/STANDING.md", "divisions": {}}
        self.save(reg)
        code, _, err = self.orche("init", "shopfront", "--divisions", "build")
        self.assertEqual(code, 0, err)
        reg = registry.load()
        self.assertEqual(sorted(reg["projects"]), ["shopfront"])
        self.assertEqual(reg["projects"]["shopfront"]["divisions"]["build"]["session"], "div-shopfront-build")

    def test_registry_match_without_orca_repo_does_not_fall_through(self):
        """Review M-1: alias "acme shop" belongs to `shopfront`, whose repo Orca no longer lists; the
        Orca repo displayed as "Acme Shop" must not be onboarded in its place."""
        reg = registry.empty()
        reg["projects"]["shopfront"] = {"repo_id": "repo-gone", "path": str(self.home / "gone"),
                                        "stage": "building", "aliases": ["acme shop"],
                                        "main_session": "main-shopfront",
                                        "card": "~/c.md", "standing": ".orche/STANDING.md", "divisions": {}}
        self.save(reg)
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "orche: 'Acme Shop' is registered as shopfront but its Orca repo is not in "
                              "`orca repo list`\n")
        self.assertEqual(self.calls, [["orca", "repo", "list", "--json"]])
        self.assertEqual(registry.load(), reg)

    def test_unknown_repo_and_division(self):
        code, _, err = self.orche("init", "nope")
        self.assertEqual(code, 2)
        self.assertEqual(err, "orche: no registry project or Orca repo matches 'nope' "
                              "(see: orche projects, orca repo list)\n")
        # A setup the flags describe wrongly is a user error (exit 1); 2 stays "not found".
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "rnd,sales")[0], 1)
        self.assertEqual(self.orche("init", str(self.repo), "--divisions", "build", "--wake", "gtm")[0], 1)

    def test_automations_flag_creates_both(self):
        code, _, err = self.orche("init", "acme shop", "--divisions", "build", "--automations")
        self.assertEqual(code, 0, err)
        autos = self.orca_calls("automations create")
        self.assertEqual(autos[0], ["orca", "automations", "create", "--name", "orche-hr-audit-acme-shop",
                                    "--trigger", "weekly", "--day", "1", "--time", "09:00", "--provider", "claude",
                                    "--workspace", f"path:{PLUGIN_ROOT}", "--prompt",
                                    "Run `orche hr audit --json` and act on its proposals; apply only sleep "
                                    "proposals with `--apply`.", "--json"])
        card_prompt = autos[1][autos[1].index("--prompt") + 1]
        self.assertEqual(card_prompt, "Run `orche card refresh acme-shop --background` and stop; do nothing else.")
        self.assertEqual(list(registry.load()["projects"]["acme-shop"]["divisions"]), ["build"])

    def test_settings_merge_keeps_other_keys_and_existing_panes_skipped(self):
        settings = self.wtroot / "div-build" / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "agent": "old"}))
        self.terminals = [{"handle": "t1", "title": "✳ main-acme-shop"}]
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        self.assertEqual(json.loads(settings.read_text()),
                         {"permissions": {"allow": ["Bash(ls)"]}, "agent": "div-acme-shop-build",
                          "crossSessionInbound": "accept"})
        titles = [c[c.index("--title") + 1] for c in self.orca_calls("terminal create")]
        self.assertEqual(titles, [])
        self.assertEqual(self.orca_calls("terminal switch"), [["orca", "terminal", "switch", "--terminal", "t1", "--json"]])

    def test_persona_templates_prefer_specific_then_generic(self):
        self.personas.mkdir()
        (self.personas / "div-build.md").write_text("---\nname: {{SESSION}}\n---\nBuild {{PROJECT}} {{MODEL}}\n")
        (self.personas / "division.md").write_text("---\nname: {{SESSION}}\n---\nGeneric {{NAME}} {{EFFORT}}\n")
        existing = self.repo / ".claude" / "agents" / "div-acme-shop-gtm.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("mine\n")
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build,research,gtm")[0], 0)
        agents = self.repo / ".claude" / "agents"
        header = onboarding.PERSONA_HEADER.format(file="{}") + "\n"
        self.assertEqual((agents / "div-acme-shop-build.md").read_text(),
                         "---\nname: div-acme-shop-build\n---\n" + header.format("div-build.md")
                         + "Build acme-shop claude-opus-5-5\n")
        generic = "---\nname: {}\n---\n" + header.format("division.md") + "Generic {} medium\n"
        self.assertEqual((agents / "div-acme-shop-research.md").read_text(),
                         generic.format("div-acme-shop-research", "research"))
        # A persona is ORCHE's: one already at the path is rendered over (T10; was: never overwritten).
        self.assertEqual(existing.read_text(), "---\nname: div-acme-shop-gtm\n---\n" + header.format("division.md")
                         + "Generic gtm high\n")
        self.assertNotIn("persona template missing for division", hr.log_path().read_text())

    def test_rerun_resumes_partial_init_without_duplicates(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "rnd,build", "--wake", "build")[0], 0)
        self.calls.clear()
        self.terminals = [{"handle": "t1", "title": "main-acme-shop"}, {"handle": "t3", "title": "div-acme-shop-build"}]
        code, out, err = self.orche("init", "Acme Shop", "--wake", "build,gtm", "--divisions", ALL)
        self.assertEqual(code, 0, err)
        self.assertIn("skipped --wake build (already onboarded; use `orche wake`)", out)
        self.assertEqual([c[c.index("--name") + 1] for c in self.orca_calls("worktree create")],
                         ["div-research", "div-gtm", "div-ops"])
        self.assertEqual([c[c.index("--title") + 1] for c in self.orca_calls("terminal create")], ["div-acme-shop-gtm"])
        self.assertIn("rnd: skipped (already onboarded)\nbuild: skipped (already onboarded)", out)
        reg = registry.load()
        self.assertEqual(sorted(reg["projects"]), ["acme-shop"])
        self.assertEqual({d: v["status"] for d, v in reg["projects"]["acme-shop"]["divisions"].items()},
                         {"rnd": "dormant", "build": "active", "research": "dormant", "gtm": "active",
                          "ops": "dormant"})
        self.assertEqual(sorted(e["unit"] for e in reg["events"]), sorted(registry.DIVISIONS))
        self.assertEqual(self.orche("init", "Acme Shop")[0], 0)  # nothing left: a no-op
        self.assertEqual(len(registry.load()["events"]), 5)

    def test_same_repo_under_another_key_refused(self):
        reg = registry.empty()
        reg["projects"]["shop"] = {"repo_id": "repo-acme", "path": "/elsewhere", "stage": "building",
                                   "aliases": [], "main_session": "main-shop",
                                   "card": "~/c.md", "standing": ".orche/STANDING.md", "divisions": {}}
        self.save(reg)
        code, _, err = self.orche("init", "Acme Shop")
        self.assertEqual(code, 1)
        self.assertIn("already registered as project shop", err)
        self.assertEqual(self.calls, [["orca", "repo", "list", "--json"]])

    def test_symlinked_claude_dir_refused_then_resumed_without_second_worktree(self):
        (self.repo / ".claude").mkdir()
        wt = self.wtroot / "div-build"
        wt.mkdir(parents=True)
        (wt / ".claude").symlink_to(self.repo / ".claude")
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "build")
        self.assertEqual(code, 3)
        self.assertIn("resolves into the main checkout", err)
        self.assertEqual(list((self.repo / ".claude").iterdir()), [])
        build = registry.load()["projects"]["acme-shop"]["divisions"]["build"]
        self.assertEqual((build["status"], build["worktree"]), ("onboarding", f"repo-acme::{wt}"))
        self.assertIn("build◐", self.orche("status")[1])
        self.assertEqual(registry.awake_sessions(registry.load()), [])
        (wt / ".claude").unlink()
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.orca_calls("worktree create")), 1)
        build = registry.load()["projects"]["acme-shop"]["divisions"]["build"]
        self.assertEqual((build["status"], build["worktree"]), ("dormant", f"repo-acme::{wt}"))
        self.assertEqual(json.loads((wt / ".claude" / "settings.local.json").read_text())["agent"],
                         "div-acme-shop-build")
        self.assertEqual([e["unit"] for e in registry.load()["events"]], ["build"])

    def test_symlinked_unit_dir_refused(self):
        target = self.repo / "elsewhere"
        target.mkdir()
        wt = self.wtroot / "div-build"
        (wt / ".orche" / "div").mkdir(parents=True)
        (wt / ".orche" / "div" / "build").symlink_to(target)
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "build")
        self.assertEqual(code, 3)
        self.assertIn("resolves into the main checkout", err)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((wt / ".claude").exists())

    def test_onboarding_unit_cannot_be_woken(self):
        wt = self.wtroot / "div-build"
        wt.mkdir(parents=True)
        (wt / ".claude").symlink_to(self.wtroot)  # outside the worktree: onboarding stops
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 3)
        self.calls.clear()
        code, _, err = self.orche("wake", "div-acme-shop-build")
        self.assertEqual(code, 3)
        self.assertEqual(err, "orche: onboarding unfinished: re-run `orche init acme-shop`\n")
        self.assertEqual(self.calls, [])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["build"]["status"], "onboarding")

    def test_existing_orca_worktree_is_adopted(self):
        wt = self.wtroot / "elsewhere"
        wt.mkdir(parents=True)
        self.worktree_list = [{"id": f"repo-acme::{wt}", "repoId": "repo-acme", "path": str(wt),
                               "displayName": "div-build", "branch": "refs/heads/div/build"},
                              {"id": "repo-other::/x/div-gtm", "repoId": "repo-other", "path": "/x/div-gtm",
                               "displayName": "div-gtm"}]
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build,gtm")[0], 0)
        self.assertEqual([c[c.index("--name") + 1] for c in self.orca_calls("worktree create")], ["div-gtm"])
        divs = registry.load()["projects"]["acme-shop"]["divisions"]
        self.assertEqual((divs["build"]["worktree"], divs["build"]["branch"]), (f"repo-acme::{wt}", "div/build"))

    def test_worktree_nested_under_repo_root_onboards(self):
        self.nest = True
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "build")
        self.assertEqual(code, 0, err)
        wt = self.repo / ".worktrees" / "div-build"
        self.assertEqual(json.loads((wt / ".claude" / "settings.local.json").read_text())["agent"],
                         "div-acme-shop-build")
        self.assertFalse((self.repo / ".claude" / "settings.local.json").exists())

    def test_same_key_from_another_repo_refused(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        reg = registry.load()
        reg["projects"]["acme-shop"]["repo_id"] = "repo-old"
        self.save(reg)
        code, _, err = self.orche("init", "Acme Shop")
        self.assertEqual(code, 1)
        self.assertIn("project key acme-shop is taken by repo repo-old", err)
        self.assertIn("rename the Orca repo or remove the stale registry entry", err)
        self.assertEqual(list(registry.load()["projects"]["acme-shop"]["divisions"]), ["build"])

    def test_empty_path_entry_is_not_a_conflict(self):
        reg = registry.empty()
        reg["projects"]["ghost"] = {"repo_id": "repo-ghost", "path": "", "stage": "building", "aliases": [],
                                    "main_session": "main-ghost",
                                    "card": "~/c.md", "standing": ".orche/STANDING.md", "divisions": {}}
        self.save(reg)
        old = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, old)
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)

    def test_exclude_settings_shared_and_idempotent(self):
        main = git_repo(self.home / "main-repo")
        linked = self.home / "linked"
        git("worktree", "add", "-q", "-b", "div/build", str(linked), cwd=main)
        first, wrote = onboarding.exclude_settings(linked)
        self.assertTrue(wrote)
        self.assertEqual(onboarding.exclude_settings(linked), (first, False))
        self.assertEqual(os.path.realpath(first), os.path.realpath(main / ".git" / "info" / "exclude"))
        self.assertEqual(first.read_text().splitlines().count(".claude/settings.local.json"), 1)
        self.assertEqual(first.read_text().splitlines().count(".orche/units/"), 1)
        onboarding.write_settings(linked, "div-x-build")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=linked,
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        self.assertEqual(onboarding.exclude_settings(self.home / "not-git"), (None, False))

    def test_init_excludes_units_dir_in_main_checkout(self):
        git_repo(self.repo)
        code, _, err = self.orche("init", "Acme Shop")
        self.assertEqual(code, 0, err)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(exclude.count(".orche/units/"), 1)
        brief = self.repo / ".orche" / "units" / "acme-shop-x-20261016-1" / "BRIEF.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("rev: 1\n")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", ".orche/units"],
                                cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(status, "")

    def test_parse_worktree_shapes(self):
        self.assertEqual(onboarding.parse_worktree({"worktree": {"path": "/w/x", "branch": "refs/heads/feat/x"}},
                                                   "r", "x", "feat/fallback"), ("r::/w/x", "/w/x", "feat/x"))
        self.assertEqual(onboarding.parse_worktree({"id": "r::/w/y", "path": "/w/y"}, "r", "y", "div/y"),
                         ("r::/w/y", "/w/y", "div/y"))


# ---- refresh (T10): re-running init re-renders ORCHE-owned unit files -------------------------------

class RefreshTest(HrCase):
    """A registered project: build (active), gtm (dormant), rnd (no persona template), research (retired),
    a custom division design (dormant, div-custom.md) and the active feature checkout."""
    OLD = "---\nname: old\n---\nhand-edited rules\n"

    def setUp(self):
        super().setUp()
        self.personas.mkdir()
        for name in ("div-build.md", "div-gtm.md", "div-research.md", "div-custom.md", "feature.md"):
            (self.personas / name).write_text(f"---\nname: {{{{SESSION}}}}\nmodel: {{{{MODEL}}}}\n---\n{name} "
                                              "{{EFFORT}}\n")
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "rnd,research,build,gtm", "--custom",
                                  "design=Design:owns the visual system", "--wake", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        code, _, err = self.orche("feat", "new", "acme-shop", "checkout", "--scope", "src/checkout/**",
                                  "--reason", "r")
        self.assertEqual(code, 0, err)
        reg = registry.load()
        reg["projects"]["acme-shop"]["divisions"]["research"]["status"] = "retired"
        registry.save(reg)
        self.root = os.path.realpath(self.repo)

    def copies(self, session):
        wt = {"feat": "feat-checkout"}.get(session.split("-")[0]) or "div-" + session.rsplit("-", 1)[1]
        return [Path(self.root) / ".claude" / "agents" / f"{session}.md",
                self.wtroot / wt / ".claude" / "agents" / f"{session}.md"]

    def outdate(self, *sessions):
        for session in sessions:
            for path in self.copies(session):
                path.write_text(self.OLD)

    def refresh(self, ts="T1", dry_run=False):
        return hr.refresh_project(registry.load(), "acme-shop", root=self.root, ts=ts, dry_run=dry_run)

    def state(self):
        files = [p for p in (*Path(self.root).rglob("*"), *self.wtroot.rglob("*")) if p.is_file()]
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in files}

    def test_outdated_personas_are_rerendered_with_backups_and_a_second_run_writes_nothing(self):
        live = ["div-acme-shop-build", "feat-acme-shop-checkout", "div-acme-shop-gtm", "div-acme-shop-design"]
        self.outdate(*live, "div-acme-shop-research")
        ref = self.refresh()
        self.assertEqual([str(p) for p in ref.changed], [str(p) for s in live for p in self.copies(s)])
        self.assertEqual(ref.restart, ["div-acme-shop-build", "feat-acme-shop-checkout"])   # active units only
        header = onboarding.PERSONA_HEADER.format(file="div-build.md")
        for path in self.copies("div-acme-shop-build"):
            self.assertEqual(path.read_text(), "---\nname: div-acme-shop-build\nmodel: claude-opus-5-5\n---\n"
                                               f"{header}\ndiv-build.md high\n")
        self.assertIn("div-custom.md", self.copies("div-acme-shop-design")[0].read_text())
        backups = self.home / "backups" / "acme-shop" / "T1"
        self.assertEqual(sorted(p.name for p in backups.iterdir()),
                         sorted(f"{s}.{c}.md" for s in live for c in ("main", "worktree")))
        self.assertTrue(all(p.read_text() == self.OLD for p in backups.iterdir()))
        for path in self.copies("div-acme-shop-research"):   # retired: untouched
            self.assertEqual(path.read_text(), self.OLD)
        before = self.state()
        again = self.refresh(ts="T2")
        self.assertEqual((again.changed, again.restart), ([], []))
        self.assertEqual(self.state(), before)               # content and mtime
        self.assertFalse((self.home / "backups" / "acme-shop" / "T2").exists())

    def test_onboarding_over_a_differing_persona_leaves_a_backup(self):
        """F5: `orche feat new` over a hand-edited persona backs it up as `orche init` does."""
        persona = Path(self.root) / ".claude" / "agents" / "feat-acme-shop-payments.md"
        persona.write_text(self.OLD)
        code, _, err = self.orche("feat", "new", "acme-shop", "payments", "--scope", "src/payments/**",
                                  "--reason", "r")
        self.assertEqual(code, 0, err)
        self.assertIn("feature.md", persona.read_text())
        [backup] = (self.home / "backups" / "acme-shop").glob("*/feat-acme-shop-payments.main.md")
        self.assertEqual(backup.read_text(), self.OLD)
        self.assertEqual([p.name for p in backup.parent.iterdir()], [backup.name])   # the new worktree copy: none

    def test_a_unit_without_a_template_is_kept_byte_identical(self):
        self.outdate("div-acme-shop-rnd")
        ref = self.refresh()
        self.assertEqual([(p, w) for p, w in ref.kept if "rnd" in p],
                         [(str(p), "no persona template") for p in self.copies("div-acme-shop-rnd")])
        self.assertTrue(all(p.read_text() == self.OLD for p in self.copies("div-acme-shop-rnd")))
        self.assertEqual(ref.changed, [])

    def test_a_missing_worktree_refreshes_the_main_copy_only(self):
        self.outdate("div-acme-shop-gtm")
        main_copy, _ = self.copies("div-acme-shop-gtm")
        shutil.rmtree(self.wtroot / "div-gtm")
        ref = self.refresh()
        self.assertEqual(ref.changed, [main_copy])
        self.assertIn((str(self.wtroot / "div-gtm"), "missing"), ref.kept)
        self.assertFalse((self.wtroot / "div-gtm").exists())

    def test_settings_keep_user_keys_and_an_unchanged_file_is_not_rewritten(self):
        build = self.wtroot / "div-build" / ".claude" / "settings.local.json"
        build.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "agent": "old"}))
        gtm = self.wtroot / "div-gtm" / ".claude" / "settings.local.json"
        stat = gtm.stat().st_mtime_ns
        ref = self.refresh()
        self.assertEqual(ref.changed, [build])
        self.assertEqual(json.loads(build.read_text()), {"permissions": {"allow": ["Bash(ls)"]},
                                                         "agent": "div-acme-shop-build",
                                                         "crossSessionInbound": "accept"})
        self.assertEqual(gtm.stat().st_mtime_ns, stat)
        self.assertEqual(ref.restart, [])   # a settings change alone restarts nothing

    def test_dry_run_says_would_refresh_and_writes_nothing(self):
        self.outdate("div-acme-shop-build")
        before, reg_bytes = self.state(), registry.path().read_bytes()
        err = io.StringIO()
        with redirect_stderr(err):
            ref = self.refresh(dry_run=True)
        self.assertEqual(ref.changed, self.copies("div-acme-shop-build"))
        self.assertEqual(err.getvalue().splitlines(), [f"[dry-run] refresh {p}" for p in ref.changed])
        self.assertEqual(self.state(), before)
        self.assertFalse((self.home / "backups").exists())
        code, out, err = self.orche("--dry-run", "init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual([ln for ln in out.splitlines() if "refresh" in ln],
                         [f"would refresh {p}" for p in ref.changed])
        self.assertEqual(self.state(), before)
        self.assertEqual(registry.path().read_bytes(), reg_bytes)

    def test_an_unsafe_target_refuses(self):
        agents = self.wtroot / "div-gtm" / ".claude" / "agents"
        shutil.rmtree(agents)
        agents.symlink_to(Path(self.root) / ".claude" / "agents")
        with self.assertRaises(onboarding.UnsafeTarget):
            self.refresh()


class RegistryKeysTest(unittest.TestCase):
    def test_optional_gated_by_stage_and_batch(self):
        reg = copy.deepcopy(SAMPLE)
        reg["projects"]["acme-shop"]["divisions"]["rnd"]["gated_by_stage"] = True
        reg["events"][0]["batch"] = "req-1"
        self.assertEqual(registry.validate(reg), [])
        reg["projects"]["acme-shop"]["divisions"]["rnd"]["gated_by_stage"] = "yes"
        reg["events"][0]["batch"] = 7
        self.assertEqual(registry.validate(reg), [
            "projects.acme-shop.divisions.rnd.gated_by_stage: must be a boolean", "events[0].batch: must be a string"])


# ---- limits -------------------------------------------------------------------------------------

class LimitsTest(unittest.TestCase):
    def test_globs_overlap(self):
        for a, b in (("src/checkout/**", "src/checkout/api/**"), ("src/**", "src/a/b.py"),
                     ("src/*.ts", "src/x.ts"), ("src/**/*.ts", "src/checkout/**")):
            self.assertTrue(limits.globs_overlap(a, b), (a, b))
            self.assertTrue(limits.globs_overlap(b, a), (b, a))
        for a, b in (("src/a/**", "src/b/**"), ("docs/x.md", "src/**"), ("a.py", "b.py")):
            self.assertFalse(limits.globs_overlap(a, b), (a, b))


# ---- feat ---------------------------------------------------------------------------------------

class FeatCase(HrCase):
    def setUp(self):
        super().setUp()
        self.reg = self.save(self.sample())

    def creates(self, n, *, project="acme-shop", hours=1, batch=None):
        reg = registry.load()
        for i in range(n):
            ev = {"at": ago(hours=hours), "type": "create", "project": project, "unit": f"build/old{i}",
                  "by": "user"}
            if batch:
                ev["batch"] = batch
            reg["events"].append(ev)
        return self.save(reg)


class FeatNewTest(FeatCase):
    def test_new_creates_child_worktree_and_records(self):
        code, out, err = self.orche("feat", "new", "acme", "payments", "--scope", "src/payments/**",
                                    "--reason", "own payments", "--by", "div-acme-shop-build", "--batch-id", "b1")
        self.assertEqual((code, out, err), (0, "feat-acme-shop-payments active (feat/payments)\n", ""))
        create = self.orca_calls("worktree create")[0]
        self.assertEqual(create, ["orca", "worktree", "create", "--repo", "id:repo-acme", "--name", "feat-payments",
                                  "--parent-worktree", "id:repo-acme::/srv/orca/acme-shop-build", "--json"])
        wt = self.wtroot / "feat-payments"
        pane = self.orca_calls("terminal create")[0]
        self.assertEqual(pane[pane.index("--command") + 1], f"{PANE}ORCHE_SESSION=feat-acme-shop-payments claude --permission-mode bypassPermissions --name feat-acme-shop-payments")
        self.assertEqual(self.orca_calls("worktree set")[0],
                         ["orca", "worktree", "set", "--worktree", f"id:repo-acme::{wt}", "--unread", "--json"])
        reg = registry.load()
        feat = reg["projects"]["acme-shop"]["divisions"]["build"]["features"][-1]
        self.assertEqual({k: feat[k] for k in ("name", "branch", "scope", "session", "status", "reason")},
                         {"name": "payments", "branch": "feat/payments", "scope": ["src/payments/**"],
                          "session": "feat-acme-shop-payments", "status": "active", "reason": "own payments"})
        self.assertEqual(reg["events"][-1] | {"at": None},
                         {"at": None, "type": "create", "project": "acme-shop", "unit": "build/payments",
                          "by": "div-acme-shop-build", "batch": "b1"})
        self.assertTrue((wt / ".orche" / "feat" / "payments" / "HANDOFF.md").is_file())
        self.assertEqual(json.loads((wt / ".claude" / "settings.local.json").read_text())["agent"],
                         "feat-acme-shop-payments")
        self.assertTrue((self.repo / ".claude" / "agents" / "feat-acme-shop-payments.md").is_file())
        self.assertIn("create feat-acme-shop-payments by div-acme-shop-build: own payments",
                      hr.log_path().read_text())
        self.assertIn("BUILD●2", (self.home / "status" / "acme-shop.line").read_text())

    def test_failed_onboarding_resumes_without_second_worktree(self):
        wt = self.wtroot / "feat-pay"
        wt.mkdir(parents=True)
        (self.repo / ".claude").mkdir()
        (wt / ".claude").symlink_to(self.repo / ".claude")
        args = ("feat", "new", "acme", "pay", "--scope", "src/pay/**", "--reason", "r")
        self.assertEqual(self.orche(*args)[0], 3)
        feats = registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"]
        self.assertEqual((feats[-1]["name"], feats[-1]["status"]), ("pay", "onboarding"))
        self.assertEqual([e["type"] for e in registry.load()["events"]], [])
        (wt / ".claude").unlink()
        code, out, err = self.orche(*args)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.orca_calls("worktree create")), 1)
        feats = registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"]
        self.assertEqual([(f["name"], f["status"]) for f in feats],
                         [("checkout", "active"), ("search", "paused"), ("pay", "active")])
        self.assertEqual([e["unit"] for e in registry.load()["events"]], ["build/pay"])

    def test_feat_wake_refuses_onboarding_feature(self):
        feats = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"]
        feats[1]["status"] = "onboarding"
        self.save(self.reg)
        code, _, err = self.orche("feat", "wake", "acme", "search")
        self.assertEqual(code, 3)
        self.assertIn("onboarding unfinished: re-run `orche feat new acme-shop search`", err)
        self.assertEqual(self.calls, [])
        self.assertEqual(registry.load(), self.reg)

    def test_branch_fallback_and_dry_run(self):
        code, out, err = self.orche("--dry-run", "feat", "new", "acme", "pay", "--scope", "src/pay/**",
                                    "--reason", "r")
        self.assertEqual((code, out), (0, "would create feat-acme-shop-pay (dry-run)\n"))
        self.assertIn("[dry-run] orca worktree create --repo id:repo-acme --name feat-pay --parent-worktree", err)
        self.assertIn(f"--command {PANE}ORCHE_SESSION=feat-acme-shop-pay claude --permission-mode bypassPermissions --name feat-acme-shop-pay --json", err)
        self.assertIn("[dry-run] orca worktree set --worktree id:repo-acme::<feat-pay worktree> --unread", err)
        self.assertEqual(registry.load(), self.reg)
        self.assertEqual(self.calls, [])
        self.feature_branch = False
        self.assertEqual(self.orche("feat", "new", "acme", "pay", "--scope", "src/pay/**", "--reason", "r")[0], 0)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"][-1]["branch"],
                         "feat/pay")

    def add_project_acme(self, features=()):
        """A second project keyed `acme` (acme-shop loses its `Acme` alias): `feat-acme-shop-x` is
        acme-shop's feature `x` and acme's feature `shop-x`."""
        reg = registry.load()
        reg["projects"]["acme-shop"]["aliases"] = ["shop"]
        build = {**copy.deepcopy(reg["projects"]["acme-shop"]["divisions"]["build"]),
                 "worktree": "repo-acme2::/srv/orca/acme-build", "session": "div-acme-build",
                 "features": [dict(copy.deepcopy(reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]),
                                   name=f, session=f"feat-acme-{f}", branch=f"feat/{f}") for f in features]}
        reg["projects"]["acme"] = {**copy.deepcopy(reg["projects"]["acme-shop"]), "repo_id": "repo-acme2",
                                   "path": str(self.home / "acme"), "aliases": [], "main_session": "main-acme",
                                   "divisions": {"build": build}}
        return self.save(reg)

    def test_a_feature_session_another_project_holds_is_refused_both_ways(self):
        """I-1: session names are unique registry-wide (Roster.member resolves one member per name)."""
        before = self.add_project_acme(features=("shop-pay",))
        code, _, err = self.orche("feat", "new", "acme-shop", "pay", "--scope", "src/pay/**", "--reason", "r")
        self.assertEqual((code, err), (1, "orche: session feat-acme-shop-pay is taken by project acme; "
                                          "pick another name\n"))
        # The other way: acme-shop already has `checkout`, so acme's `shop-checkout` is taken.
        code, _, err = self.orche("feat", "new", "acme", "shop-checkout", "--scope", "src/c/**", "--reason", "r")
        self.assertEqual((code, err), (1, "orche: session feat-acme-shop-checkout is taken by project "
                                          "acme-shop; pick another name\n"))
        self.assertEqual((registry.load(), self.calls), (before, []))
        self.assertEqual(self.orche("feat", "new", "acme", "shop-other", "--scope", "src/o/**", "--reason", "r")[0],
                         0)                                      # a free name still goes through

    def test_fourth_in_a_day_blocked_frozen_unchanged(self):
        self.creates(3)
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r")
        self.assertEqual(code, 3)
        self.assertIn("acme-shop: daily limit reached (3/3 in 24h)", err)
        self.assertFalse(registry.load()["frozen"])
        self.assertEqual(self.calls, [])

    def test_old_creates_do_not_count_but_weekly_limit_does(self):
        self.creates(3, hours=30)
        self.assertEqual(self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r")[0], 0)
        self.creates(6, hours=50)
        code, _, err = self.orche("feat", "new", "acme", "y", "--scope", "src/y/**", "--reason", "r")
        self.assertEqual(code, 3)
        self.assertIn("weekly limit reached (10/10 in 7 days)", err)

    def test_twenty_first_global_freezes(self):
        self.creates(20, project="docs-site")
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r")
        self.assertEqual(code, 3)
        self.assertIn("global limit reached (20/20 in 24h)", err)
        reg = registry.load()
        self.assertTrue(reg["frozen"])
        self.assertEqual((reg["events"][-1]["type"], reg["events"][-1]["unit"]), ("freeze", "hr"))
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r")
        self.assertEqual(code, 3)
        self.assertIn("frozen; run: orche hr unfreeze", err)

    def test_batch_limit(self):
        self.creates(2, project="docs-site", batch="req-7")
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r",
                                  "--batch-id", "req-7")
        self.assertEqual(code, 3)
        self.assertIn("batch req-7: per-request limit reached (2/2)", err)
        self.assertEqual(self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r",
                                    "--batch-id", "req-8")[0], 0)

    def test_scope_overlap_warns_or_blocks_with_strict(self):
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/checkout/api/**", "--reason", "r",
                                  "--strict")
        self.assertEqual(code, 3)
        self.assertIn("scope src/checkout/api/** overlaps feature checkout (src/checkout/**)", err)
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/search/x.py,lib/**", "--reason", "r")
        self.assertEqual(code, 0)
        self.assertEqual(err, "orche: warning: scope src/search/x.py overlaps feature search (src/search/**)\n")

    def test_awake_limit_and_inactive_build_warning(self):
        self.reg["limits"]["max_awake_sessions"] = 2
        self.save(self.reg)
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r")
        self.assertEqual(code, 3)
        self.assertIn("awake limit reached (2/2); sleep a unit first", err)
        self.reg["limits"]["max_awake_sessions"] = 12
        self.reg["projects"]["acme-shop"]["divisions"]["build"]["status"] = "dormant"
        self.save(self.reg)
        code, _, err = self.orche("feat", "new", "acme", "x", "--scope", "src/x/**", "--reason", "r")
        self.assertEqual((code, err), (0, "orche: warning: parent division build is dormant\n"))

    def test_bad_inputs(self):
        self.assertEqual(self.orche("feat", "new", "nope", "x", "--scope", "a", "--reason", "r")[0], 2)
        self.assertEqual(self.orche("feat", "new", "acme", "Bad Name", "--scope", "a", "--reason", "r")[0], 1)
        self.assertEqual(self.orche("feat", "new", "acme", "checkout", "--scope", "a", "--reason", "r")[0], 1)
        self.assertEqual(self.orche("feat", "new", "acme", "x", "--scope", " , ", "--reason", "r")[0], 1)


class FeatSleepWakeTest(FeatCase):
    def test_wake_and_sleep_reuse_unit_flow(self):
        code, out, _ = self.orche("feat", "wake", "acme", "search")
        self.assertEqual((code, out), (0, "feat-acme-shop-search active\n"))
        pane = self.orca_calls("terminal create")[0]
        self.assertEqual(pane[pane.index("--command") + 1], f"{PANE}ORCHE_SESSION=feat-acme-shop-search claude --permission-mode bypassPermissions --name feat-acme-shop-search")
        code, _, err = self.orche("feat", "sleep", "acme", "search")
        self.assertEqual(code, 3)
        self.assertIn("HANDOFF.md is stale (missing:", err)
        self.assertEqual(self.orche("feat", "wake", "acme", "nope")[0], 2)

    def test_feat_wake_active_feature_reopens_missing_pane_only(self):
        """R3: `feat wake` on an active feature whose pane is gone reopens it; with a pane it does not."""
        code, out, _ = self.orche("feat", "wake", "acme", "checkout")
        self.assertEqual((code, out), (0, "feat-acme-shop-checkout pane reopened\n"))
        [pane] = self.orca_calls("terminal create")
        self.assertEqual(pane[pane.index("--command") + 1],
                         f"{PANE}ORCHE_SESSION=feat-acme-shop-checkout claude --permission-mode bypassPermissions --name feat-acme-shop-checkout")
        self.calls.clear()
        self.terminals = [{"handle": "t-3", "title": "feat-acme-shop-checkout"}]
        code, out, _ = self.orche("feat", "wake", "acme", "checkout")
        self.assertEqual((code, out), (0, "feat-acme-shop-checkout is already active\n"))
        self.assertEqual(self.orca_calls("terminal create"), [])

    def test_wake_refreshes_status_line_and_last_activity(self):
        line = self.home / "status" / "acme-shop.line"
        line.parent.mkdir(parents=True, exist_ok=True)
        line.write_text("stale\n")
        self.assertEqual(self.orche("feat", "wake", "acme", "search")[0], 0)
        self.assertNotEqual(line.read_text(), "stale\n")
        self.assertTrue(line.read_text().startswith("R&D"), line.read_text())
        feat = roster.Roster(registry.load()).member("feat-acme-shop-search").entry
        self.assertGreater(units.parse_iso(feat["last_activity"]), NOW - dt.timedelta(minutes=5))
        self.assertTrue((self.home / "activity" / "feat-acme-shop-search").is_file())


class FeatRetireTest(FeatCase):
    def setUp(self):
        super().setUp()
        self.wt = self.feature_worktree("wt-checkout", "Final: merged in #12, nothing in flight")
        feat = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        feat["worktree"] = f"repo-acme::{self.wt}"
        self.save(self.reg)
        self.handoff = self.wt / ".orche" / "feat" / "checkout" / "HANDOFF.md"
        slug = self.claude_root / "-proj"
        slug.mkdir()
        self.mine = slug / "1111.jsonl"
        self.mine.write_text(json.dumps({"cwd": str(self.wt / "src")}) + "\n")
        self.main = slug / "2222.jsonl"
        self.main.write_text(json.dumps({"cwd": str(self.repo)}) + "\n")
        self.named = slug / "main-acme-shop.jsonl"
        self.named.write_text(json.dumps({"cwd": str(self.wt)}) + "\n")
        self.tag = f"archive/feat-checkout-{NOW.strftime('%Y%m%d')}"
        self.kept = self.home / "archive" / "feat-acme-shop-checkout" / "unit-files" / self.tag

    def fake_runner(self, argv, *, timeout, input):
        """Pane checks (`terminal list`) are tracked apart from the destructive calls asserted below.

        `worktree rm` behaves like Orca: it refuses a worktree whose `git status` is not empty
        (or fails once with `self.rm_error`).
        """
        res = super().fake_runner(argv, timeout=timeout, input=input)
        if argv[:3] == ["orca", "terminal", "list"]:
            self.calls.remove(argv)
            self.pane_checks = getattr(self, "pane_checks", 0) + 1
        if argv[:3] == ["orca", "worktree", "rm"]:
            error, self.rm_error = getattr(self, "rm_error", None), None
            status = self.wt_status()
            if error or status:
                message = error or f"Failed to delete worktree {self.wt}: {status}"
                return shell.Result(0, json.dumps({"ok": False, "error": {"message": message}}), "", argv)
        return res

    def wt_status(self):
        return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.wt,
                              capture_output=True, text=True).stdout.strip()

    def kept_files(self):
        return {p.name: p.read_text() for p in self.kept.iterdir() if p.is_file()}

    # -- F2: untracked ORCHE files no longer stop `worktree rm`; a re-run completes ------------
    def test_untracked_unit_files_are_kept_cleared_and_retire_completes(self):
        memory = self.wt / ".claude" / "agent-memory" / "feat-acme-shop-checkout" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("- prefer small commits\n")
        self.assertIn("?? .orche/feat/checkout/CHARTER.md", self.wt_status())
        code, out, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual((code, out), (0, f"feat-acme-shop-checkout retired (tag {self.tag})\n"), err)
        self.assertEqual(self.wt_status(), "")
        self.assertFalse((self.wt / ".claude").exists())
        kept_memory = self.kept / "_worktree" / ".claude" / "agent-memory" / "feat-acme-shop-checkout" / "MEMORY.md"
        self.assertEqual(kept_memory.read_text(), "- prefer small commits\n")
        self.assertIn("Final: merged in #12", (self.kept / "HANDOFF.md").read_text())
        self.assertEqual(self.tags(), [self.tag])
        self.assertIn(f"feature checkout (worktree {self.wt}", (self.repo / "docs" / "GRAVEYARD.md").read_text())

    def test_foreign_untracked_file_blocks_before_copy_and_tag(self):
        (self.wt / "notes.txt").write_text("mine\n")
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("worktree has uncommitted work: notes.txt;", err)
        self.assertEqual((self.calls, self.tags()), ([], []))
        self.assertFalse(self.kept.exists())
        self.assertIn("?? .orche/feat/checkout/HANDOFF.md", self.wt_status())

    def test_modified_tracked_unit_file_is_uncommitted_work(self):
        git("add", ".orche/feat/checkout", cwd=self.wt)
        git("commit", "-q", "-m", "unit files", cwd=self.wt)
        self.handoff.write_text("# HANDOFF\n\nFinal: done, edited after the commit\n")
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("worktree has uncommitted work: .orche/feat/checkout/HANDOFF.md;", err)
        self.assertEqual((self.calls, self.tags()), ([], []))

    def test_rerun_after_rm_failure_completes(self):
        self.rm_error = "worktree is locked"
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(f"worktree is locked (tag {self.tag} and unit files in {self.kept} "
                      "kept; re-run `orche feat retire acme-shop checkout --yes`", err)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"][0]["status"],
                         "active")
        self.assertFalse(self.handoff.exists())  # cleared; the kept copy carries the final line
        code, out, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual((code, out), (0, f"feat-acme-shop-checkout retired (tag {self.tag})\n"), err)
        self.assertIn(f"already kept in {self.kept}; reusing them", err)
        self.assertIn(f"tag {self.tag} already points at feat/checkout; reusing it", err)
        self.assertEqual(self.tags(), [self.tag])
        self.assertIn("Final: merged in #12", (self.kept / "HANDOFF.md").read_text())

    def test_rerun_from_pilot_stuck_state_completes(self):
        """The 0.1.0 failure: files copied and tag set, then `worktree rm` refused the untracked unit files."""
        unit_dir = self.wt / ".orche" / "feat" / "checkout"
        self.kept.mkdir(parents=True)
        for p in unit_dir.iterdir():
            (self.kept / p.name).write_bytes(p.read_bytes())
        git("tag", self.tag, "feat/checkout", cwd=self.wt)
        code, out, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual((code, out), (0, f"feat-acme-shop-checkout retired (tag {self.tag})\n"), err)
        self.assertIn("reusing them", err)
        self.assertEqual(self.wt_status(), "")

    def test_rerun_with_tag_at_another_commit_fails(self):
        git("tag", self.tag, "feat/checkout", cwd=self.wt)
        git("commit", "-q", "--allow-empty", "-m", "later", cwd=self.wt)
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(f"git tag {self.tag} failed", err)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.handoff.exists())

    def test_open_pane_refused_dry_run_prints_check(self):
        self.terminals = [{"handle": "t-9", "title": "✳ feat-acme-shop-checkout"}]
        code, out, err = self.orche("--dry-run", "feat", "retire", "acme", "checkout")
        self.assertEqual(code, 3)
        self.assertIn("[dry-run] check no Orca pane is titled feat-acme-shop-checkout", err)
        self.assertIn("pane open — run `orche feat sleep acme-shop checkout` first", err)
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual((self.tags(), self.calls), ([], []))
        self.assertTrue(self.wt.exists() and self.mine.exists())

    def test_onboarding_feature_refused(self):
        feat = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        feat["status"] = "onboarding"
        self.save(self.reg)
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("still onboarding", err)
        self.assertEqual(self.calls, [])

    def test_ignored_files_listed_as_destroyed(self):
        (self.wt / ".gitignore").write_text(".env.local\nlocal.db\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ignore"], cwd=self.wt, check=True)
        (self.wt / ".env.local").write_text("X=1\n")
        (self.wt / "local.db").write_text("db")
        code, _, err = self.orche("--dry-run", "feat", "retire", "acme", "checkout")
        self.assertEqual(code, 0, err)
        self.assertIn("[dry-run] 2 git-ignored file(s) will be destroyed by worktree rm: .env.local, local.db", err)
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: 2 git-ignored file(s) will be destroyed by worktree rm", err)

    def feature_worktree(self, dirname, final):
        """A git worktree holding exactly what `feat new` leaves there, all untracked (no info/exclude)."""
        wt = git_repo(self.home / dirname, branch="feat/checkout")
        session = "feat-acme-shop-checkout"
        templates.copy_unit(wt / ".orche" / "feat" / "checkout", "feature", PROJECT="acme-shop",
                            UNIT="checkout", SESSION=session)
        onboarding.write_settings(wt, session)
        onboarding.write_persona([wt], session, "feature", project="acme-shop", name="checkout",
                                 session=session, model="claude-opus-5-5", effort="high")
        (wt / ".orche" / "feat" / "checkout" / "HANDOFF.md").write_text(f"# HANDOFF — checkout\n\n{final}\n")
        return wt

    def recreate(self):
        """Retire the first checkout feature, then register a second one with the same name."""
        self.assertEqual(self.orche("feat", "retire", "acme", "checkout", "--yes")[0], 0)
        wt2 = self.feature_worktree("wt-checkout-2", "Final: second run")
        reg = registry.load()
        feats = reg["projects"]["acme-shop"]["divisions"]["build"]["features"]
        feats.append({**feats[0], "worktree": f"repo-acme::{wt2}", "status": "active"})
        self.save(reg)
        self.calls.clear()
        return wt2

    def test_same_day_second_retirement_refused_first_archive_intact(self):
        wt2 = self.recreate()
        first = self.kept_files()
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.kept} already holds the unit files of an earlier retirement", err)
        self.assertEqual(self.calls, [])
        tags = subprocess.run(["git", "tag", "-l"], cwd=wt2, capture_output=True, text=True).stdout
        self.assertEqual(tags, "")
        self.assertEqual(self.kept_files(), first)
        self.assertIn("Final: merged in #12", first["HANDOFF.md"])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"][-1]["status"],
                         "active")

    def test_retirement_on_another_day_keeps_both_archives(self):
        self.recreate()
        later = NOW + dt.timedelta(days=1)
        units.now = lambda: later
        self.assertEqual(self.orche("feat", "retire", "acme", "checkout", "--yes")[0], 0)
        second = self.kept.parent / f"feat-checkout-{later.strftime('%Y%m%d')}"
        self.assertIn("Final: merged in #12", (self.kept / "HANDOFF.md").read_text())
        self.assertIn("Final: second run", (second / "HANDOFF.md").read_text())
        feats = registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"]
        self.assertEqual([f["status"] for f in feats if f["name"] == "checkout"], ["retired", "retired"])

    def tags(self):
        return subprocess.run(["git", "tag", "-l"], cwd=self.wt, capture_output=True, text=True).stdout.split()

    def test_refuses_dirty_worktree(self):
        (self.wt / "wip.py").write_text("x\n")
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("worktree has uncommitted work: wip.py; commit or stash it (or `orche report stop`) "
                      "before retiring", err)
        self.assertEqual((self.calls, self.tags()), ([], []))
        self.assertFalse(self.kept.exists())
        self.assertTrue((self.wt / ".orche" / "feat" / "checkout" / "CHARTER.md").exists())

    def test_onboarding_files_are_untracked_but_not_dirty(self):
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.wt,
                                capture_output=True, text=True).stdout
        self.assertIn("?? .claude/settings.local.json", status)
        self.assertIn("?? .claude/agents/feat-acme-shop-checkout.md", status)
        self.assertIn("?? .orche/feat/checkout/CHARTER.md", status)
        self.assertEqual(self.orche("--dry-run", "feat", "retire", "acme", "checkout")[0], 0)

    def test_modified_tracked_file_blocks(self):
        (self.wt / "README.md").write_text("changed\n")
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("worktree has uncommitted work: README.md;", err)

    def test_untracked_persona_of_another_unit_is_dirty(self):
        (self.wt / ".claude" / "agents" / "feat-acme-shop-other.md").write_text("x\n")
        self.assertEqual(self.orche("feat", "retire", "acme", "checkout", "--yes")[0], 3)

    def test_unit_files_copy_failure_refuses_before_anything_destructive(self):
        (self.home / "archive").write_text("not a dir\n")
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("could not keep the unit files of feat-acme-shop-checkout", err)
        self.assertEqual((self.tags(), self.calls), ([], []))
        self.assertEqual(registry.load(), self.reg)
        self.assertTrue(self.mine.exists())

    def test_graveyard_failure_after_removal_still_retires(self):
        self.mine.unlink()
        (self.repo / "docs" / "GRAVEYARD.md").mkdir(parents=True)  # unwritable: a directory
        code, out, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 0)
        self.assertIn(f"add by hand to {self.repo / 'docs' / 'GRAVEYARD.md'}", err)
        self.assertIn(f"git checkout -b feat/checkout {self.tag}", err)
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]["status"], "retired")
        self.assertEqual(reg["events"][-1]["type"], "retire")
        self.assertIn("GRAVEYARD row failed", hr.log_path().read_text())

    def test_refuses_without_final_line(self):
        self.handoff.write_text("# HANDOFF\n\n## Next\nfinish tests\n")
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("HANDOFF.md has no 'final:' line", err)

    def test_refuses_main_checkout(self):
        feat = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        feat["worktree"] = f"repo-acme::{self.repo}"
        self.save(self.reg)
        code, _, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("main checkout", err)

    def test_requires_yes_and_dry_run_touches_nothing(self):
        self.assertEqual(self.orche("feat", "retire", "acme", "checkout")[0], 1)
        code, out, err = self.orche("--dry-run", "feat", "retire", "acme", "checkout")
        self.assertEqual((code, out), (0, "would retire feat-acme-shop-checkout (dry-run)\n"))
        self.assertEqual(err.splitlines(), [
            "[dry-run] check no Orca pane is titled feat-acme-shop-checkout",
            f"[dry-run] copy {self.wt / '.orche' / 'feat' / 'checkout'} -> {self.kept}",
            f"[dry-run] copy 2 other ORCHE file(s) -> {self.kept / '_worktree'}",
            f"[dry-run] git -C {shlex.quote(str(self.wt))} tag {self.tag} feat/checkout",
            f"[dry-run] delete 6 ORCHE-owned untracked file(s) from {self.wt}",
            f"[dry-run] orca worktree rm --worktree id:repo-acme::{self.wt} --json",
            "[dry-run] archive 1 transcript file(s) of feat-acme-shop-checkout",
            f"[dry-run] append GRAVEYARD row in {self.repo / 'docs' / 'GRAVEYARD.md'}"])
        self.assertEqual((self.tags(), self.calls), ([], []))
        self.assertEqual(registry.load(), self.reg)
        self.assertTrue(self.mine.exists())
        self.assertFalse(self.kept.exists())

    def test_retire_tags_removes_archives_and_records(self):
        unit_dir = self.wt / ".orche" / "feat" / "checkout"
        before = {p.name: p.read_text() for p in unit_dir.iterdir()}
        code, out, err = self.orche("feat", "retire", "acme", "checkout", "--yes")
        self.assertEqual((code, out, err), (0, f"feat-acme-shop-checkout retired (tag {self.tag})\n",
                                            f"orche: unit files of feat-acme-shop-checkout kept in {self.kept}\n"))
        self.assertEqual(sorted(before), ["CHARTER.md", "DECISIONS.tsv", "HANDOFF.md", "STATUS.md"])
        self.assertEqual(self.kept_files(), before)
        self.assertEqual(sorted(str(p.relative_to(self.kept / "_worktree"))
                                for p in (self.kept / "_worktree").rglob("*") if p.is_file()),
                         [".claude/agents/feat-acme-shop-checkout.md", ".claude/settings.local.json"])
        self.assertFalse((self.wt / ".orche").exists())
        self.assertEqual(self.wt_status(), "")
        self.assertEqual(self.tags(), [self.tag])
        self.assertEqual(self.calls, [["orca", "worktree", "rm", "--worktree", f"id:repo-acme::{self.wt}", "--json"]])
        self.assertFalse(self.mine.exists())
        self.assertTrue(self.main.exists() and self.named.exists())
        self.assertEqual(len(list((self.home / "archive" / "feat-acme-shop-checkout").glob("*.manifest.json"))), 1)
        graveyard = (self.repo / "docs" / "GRAVEYARD.md").read_text()
        self.assertIn("archived 1 session files of feat-acme-shop-checkout", graveyard)
        self.assertIn(f"git checkout -b feat/checkout {self.tag}; unit files: {self.kept}", graveyard)
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]["status"], "retired")
        self.assertEqual((reg["events"][-1]["type"], reg["events"][-1]["unit"]), ("retire", "build/checkout"))
        self.assertIn(f"retire feat-acme-shop-checkout by user (tag {self.tag})", hr.log_path().read_text())

    def test_retire_records_through_hr(self):  # R2
        notes, order = [], []

        def spy(name, real):
            def call(*a, **kw):
                order.append(name)
                return real(*a, **kw)
            return call

        with mock.patch.object(book, "notify", spy("notify", notes.append)), \
                mock.patch.object(registry, "append_event", spy("event", registry.append_event)), \
                mock.patch.object(registry, "save", spy("save", registry.save)), \
                mock.patch.object(book, "log", spy("log", book.log)), \
                mock.patch.object(statusline, "refresh", wraps=spy("refresh", statusline.refresh)) as refresh:
            self.assertEqual(self.orche("feat", "retire", "acme", "checkout", "--yes")[0], 0)
        self.assertEqual(notes, ["Feature agent feat-acme-shop-checkout retired"])
        self.assertEqual(order, ["event", "save", "log", "refresh", "notify"])
        ev = registry.load()["events"][-1]
        self.assertEqual((ev["type"], ev["project"], ev["unit"], ev["by"]), ("retire", "acme-shop", "build/checkout", "user"))
        self.assertIn(f"retire feat-acme-shop-checkout by user (tag {self.tag})", book.log_path().read_text())
        self.assertEqual(refresh.call_count, 1)

    def test_retire_without_transcripts_logs(self):
        self.mine.unlink()
        self.assertEqual(self.orche("feat", "retire", "acme", "checkout", "--yes")[0], 0)
        self.assertIn("no transcripts to archive", hr.log_path().read_text())


# ---- stage --------------------------------------------------------------------------------------

class StageTest(FeatCase):
    def setUp(self):
        super().setUp()
        self.reg["projects"]["acme-shop"]["divisions"]["ops"] = {
            "status": "dormant", "worktree": "repo-acme::/srv/orca/acme-shop-ops", "branch": "div/ops",
            "session": "div-acme-shop-ops", "model": "claude-opus-5-5",
            "last_activity": ago(days=1), "gated_by_stage": True}
        self.save(self.reg)

    def test_onboarding_ops_not_woken_but_stage_flips(self):
        self.reg["projects"]["acme-shop"]["divisions"]["ops"]["status"] = "onboarding"
        self.save(self.reg)
        code, out, err = self.orche("stage", "set", "acme", "public", "--yes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("onboarding unfinished: re-run `orche init acme-shop`", out)
        reg = registry.load()
        self.assertEqual((reg["projects"]["acme-shop"]["stage"], [e["type"] for e in reg["events"]]),
                         ("public", ["stage"]))
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["ops"]["status"], "onboarding")
        self.assertEqual(self.calls, [])

    def test_ops_without_session_still_flips_stage(self):
        self.reg["projects"]["acme-shop"]["divisions"]["ops"]["session"] = ""
        self.save(self.reg)
        code, out, err = self.orche("stage", "set", "acme", "public", "--yes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ops has no session yet", out)
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["stage"], "public")
        self.assertEqual([e["type"] for e in reg["events"]], ["stage"])
        self.assertEqual(self.calls, [])

    def test_requires_yes(self):
        self.assertEqual(self.orche("stage", "set", "acme", "public")[0], 1)
        self.assertEqual(registry.load(), self.reg)

    def test_ops_wake_refused_while_building(self):
        code, _, err = self.orche("wake", "div-acme-shop-ops")
        self.assertEqual(code, 3)
        self.assertIn("gated by stage and acme-shop is building", err)

    def test_public_wakes_ops_and_records(self):
        code, out, err = self.orche("--json", "stage", "set", "acme", "public", "--yes")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["stage"], data["woke"], len(data["checklist"])), ("public", True, 4))
        self.assertTrue(any("docs/runbooks/" in c for c in data["checklist"]))
        pane = self.orca_calls("terminal create")[0]
        self.assertEqual(pane[pane.index("--command") + 1], f"{PANE}ORCHE_SESSION=div-acme-shop-ops claude --permission-mode bypassPermissions --name div-acme-shop-ops")
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["stage"], "public")
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["ops"]["status"], "active")
        self.assertEqual([(e["type"], e["unit"]) for e in reg["events"]], [("stage", "public"), ("wake", "ops")])
        self.assertIn("stage acme-shop -> public", hr.log_path().read_text())

    def test_public_no_wake_dry_run_and_building(self):
        code, out, err = self.orche("--dry-run", "stage", "set", "acme", "public")
        self.assertEqual(code, 0)
        self.assertIn("[dry-run] orca terminal create --worktree id:repo-acme::/srv/orca/acme-shop-ops", err)
        self.assertEqual(registry.load(), self.reg)
        code, out, _ = self.orche("stage", "set", "acme", "public", "--yes", "--no-wake")
        self.assertEqual(code, 0)
        self.assertIn("ops checklist: release ladder", out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.orche("stage", "set", "acme", "building", "--yes")[0], 0)
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["stage"], "building")
        self.assertEqual([e["unit"] for e in reg["events"]], ["public", "building"])


    def test_revert_to_building_sleeps_ops_or_reports_refusal(self):
        self.reg["projects"]["acme-shop"]["divisions"]["ops"]["worktree"] = f"repo-acme::{self.home / 'ops-wt'}"
        self.save(self.reg)
        self.assertEqual(self.orche("stage", "set", "acme", "public", "--yes")[0], 0)
        self.terminals = [{"handle": "t-ops", "title": "div-acme-shop-ops"}]
        code, out, err = self.orche("stage", "set", "acme", "building", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("ops not slept: HANDOFF.md is stale", out)
        self.assertIn("warning: ops not slept", err)
        reg = registry.load()
        self.assertEqual((reg["projects"]["acme-shop"]["stage"],
                          reg["projects"]["acme-shop"]["divisions"]["ops"]["status"]), ("building", "active"))
        handoff = roster.Roster(reg).member("div-acme-shop-ops").memory / "HANDOFF.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("fresh\n")
        self.assertEqual(self.orche("stage", "set", "acme", "public", "--yes", "--no-wake")[0], 0)
        code, out, _ = self.orche("stage", "set", "acme", "building", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("div-acme-shop-ops paused", out)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["ops"]["status"], "paused")
        self.assertEqual(self.orca_calls("terminal close")[0][-3:], ["--terminal", "t-ops", "--json"])


# ---- hr audit / correct -------------------------------------------------------------------------

class AuditTest(FeatCase):
    def setUp(self):
        super().setUp()
        checkout = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        checkout["last_activity"] = ago(days=20)
        self.save(self.reg)
        (self.repo / ".orche").mkdir()
        (self.repo / ".orche" / "STANDING.md").write_text(
            "# STANDING\n\n1. Run the tests before reporting\n2. Never push\n3. Run the tests before reporting\n")

    def test_fixture_lists_one_idle_unit_and_one_repeated_correction(self):
        code, out, err = self.orche("--json", "hr", "audit")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual([(p["unit"], p["why"]) for p in data["proposals"]],
                         [("feat-acme-shop-checkout", "idle 20d > 14d")])
        self.assertEqual([(c["source"], c["text"], c["count"]) for c in data["corrections"]],
                         [("STANDING.md", "Run the tests before reporting", 2)])
        self.assertEqual(data["metrics"], "metrics (7d): 0 responses, $0.00 list price, main wake-ups 0, routine 0%")
        report = audit.report_path(NOW)
        self.assertEqual(data["report"], str(report))
        text = report.read_text()
        self.assertIn("- sleep feat-acme-shop-checkout: idle 20d > 14d", text)
        self.assertIn("- acme-shop STANDING.md: Run the tests before reporting (x2)", text)
        self.assertEqual(registry.load(), self.reg)  # audit without --apply changes nothing

    def test_human_output_and_apply_sleeps_idle(self):
        self.terminals = [{"handle": "t-3", "title": "feat-acme-shop-checkout"}]
        code, out, _ = self.orche("hr", "audit", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("propose: sleep feat-acme-shop-checkout (idle 20d > 14d)", out)
        self.assertIn("candidate for structural enforcement: acme-shop STANDING.md: Run the tests before "
                      "reporting x2", out)
        self.assertIn("applied: feat-acme-shop-checkout paused", out)
        self.assertEqual(self.orca_calls("terminal close")[0][-3:], ["--terminal", "t-3", "--json"])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["build"]["features"][0]["status"],
                         "paused")
        self.assertEqual(self.orca_calls("terminal create"), [])

    def test_apply_refreshes_status_line(self):
        self.terminals = [{"handle": "t-3", "title": "feat-acme-shop-checkout"}]
        line = self.home / "status" / "acme-shop.line"
        line.parent.mkdir(parents=True, exist_ok=True)
        line.write_text("stale\n")
        self.assertEqual(self.orche("hr", "audit", "--apply")[0], 0)
        self.assertNotEqual(line.read_text(), "stale\n")

    def test_dry_run_writes_no_report(self):
        code, out, _ = self.orche("--dry-run", "hr", "audit", "--apply")
        self.assertEqual(code, 0)
        self.assertIn(f"would write {audit.report_path(NOW)}", out)
        self.assertFalse(audit.report_path(NOW).exists())
        self.assertEqual(registry.load(), self.reg)

    def test_touched_unit_not_proposed_untouched_15_day_unit_is(self):
        checkout = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        checkout["last_activity"] = ago(days=15)
        self.save(self.reg)
        data = json.loads(self.orche("--json", "hr", "audit")[1])
        self.assertEqual([(p["unit"], p["handoff"]) for p in data["proposals"]],
                         [("feat-acme-shop-checkout", False)])
        units.touch_activity("feat-acme-shop-checkout")
        self.assertEqual((self.home / "activity" / "feat-acme-shop-checkout").is_file(), True)
        data = json.loads(self.orche("--json", "hr", "audit")[1])
        self.assertEqual(data["proposals"], [])
        depts = json.loads(self.orche("--json", "depts")[1])
        self.assertEqual([r["idle_days"] for r in depts if r["session"] == "feat-acme-shop-checkout"], [0])

    def test_quiet_silent_when_nothing_but_report_written(self):
        (self.repo / ".orche" / "STANDING.md").write_text("1. Never push\n")
        checkout = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        checkout["last_activity"] = ago(days=1)
        self.save(self.reg)
        self.assertEqual(self.orche("hr", "audit", "--quiet"), (0, "", ""))
        self.assertTrue(audit.report_path(NOW).is_file())

    def test_over_limit_proposes_oldest_idle_and_overlaps(self):
        self.reg["limits"]["max_awake_sessions"] = 1
        checkout = self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        checkout["last_activity"] = ago(days=3)
        self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][1]["scope"] = ["src/**"]
        self.save(self.reg)
        data = json.loads(self.orche("--json", "hr", "audit")[1])
        self.assertEqual([(p["unit"], p["handoff"]) for p in data["proposals"]], [("feat-acme-shop-checkout", True)])
        self.assertEqual([(o["a"], o["b"]) for o in data["overlaps"]], [("checkout", "search")])
        code, out, _ = self.orche("hr", "audit", "--apply")
        self.assertIn("applied: refused feat-acme-shop-checkout: HANDOFF.md is stale", out)

    def test_git_history_and_decision_rev_rows(self):
        git_repo(self.repo)  # commits the three fixture orders
        with open(self.repo / ".orche" / "STANDING.md", "a") as fh:
            fh.write("4. Never push\n")
        (self.repo / ".orche" / "DECISIONS.tsv").write_text(
            "ts\tphase\tdecision\twhy\tevidence\tresult\n"
            f"{ago(days=2)}\tbuild\tbrief rev 2: add tests\tmissed acceptance\t-\tok\n"
            f"{ago(days=9)}\tbuild\tbrief rev 3: old\tx\t-\tok\n"
            f"{ago(days=1)}\tbuild\tship\tready\t-\tok\n")
        data = json.loads(self.orche("--json", "hr", "audit")[1])
        self.assertEqual([(c["source"], c["text"], c["count"]) for c in data["corrections"]],
                         [("STANDING.md", "Never push", 2), ("STANDING.md", "Run the tests before reporting", 2),
                          ("DECISIONS.tsv", "brief rev 2: add tests", 1)])


class CorrectTest(FeatCase):
    def test_numbering_from_template_and_log(self):
        code, out, _ = self.orche("hr", "correct", "acme", "--text", "Run the tests\nbefore reporting",
                                  "--by", "main-acme-shop")
        self.assertEqual((code, out), (0, "7\n"))
        path = self.repo / ".orche" / "STANDING.md"
        text = path.read_text()
        self.assertTrue(text.startswith("# STANDING ORDERS — acme-shop"))
        self.assertTrue(text.endswith("\n6. Externals auto-accept list: (empty) — every external request needs confirmation.\n"
                                      "7. Run the tests before reporting\n"))
        self.assertEqual(self.orche("hr", "correct", "acme", "--text", "Never push")[:2], (0, "8\n"))
        self.assertTrue(path.read_text().endswith("7. Run the tests before reporting\n8. Never push\n"))
        log = hr.log_path().read_text()
        self.assertIn("correct acme-shop #7 by main-acme-shop: Run the tests before reporting", log)
        code, out, err = self.orche("--dry-run", "hr", "correct", "acme", "--text", "x")
        self.assertEqual((code, out), (0, "9\n"))
        self.assertIn("[dry-run] append to", err)
        self.assertFalse(path.read_text().endswith("x\n"))
        self.assertEqual(self.orche("hr", "correct", "nope", "--text", "x")[0], 2)
        self.assertEqual(self.orche("hr", "correct", "acme", "--text", "  ")[0], 1)

    def test_correct_text_is_masked(self):
        canary = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        self.assertEqual(self.orche("hr", "correct", "acme", "--text", f"Use key {canary} for staging")[0], 0)
        text = (self.repo / ".orche" / "STANDING.md").read_text()
        self.assertNotIn(canary, text)
        self.assertIn("Use key", text)
        self.assertNotIn(canary, hr.log_path().read_text())


if __name__ == "__main__":
    unittest.main()
