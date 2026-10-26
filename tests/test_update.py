"""orche update (design 2026-10-23 §11, R-PLAIN-2): a temp bare `origin`, a clone as the checkout whose
bin/orche.py is a fake that logs what the NEW code was asked to do; real git, no network."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from orche import cmd_init, cmd_update, registry
from test_hr import HrCase

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, sys
here = pathlib.Path(__file__).resolve().parent.parent
version = re.search(r'"(.+)"', (here / "lib/orche/__init__.py").read_text()).group(1)
home = pathlib.Path(os.environ["ORCHE_HOME"])
with open(home / "fake-calls.log", "a") as fh:
    fh.write(json.dumps({"version": version, "argv": sys.argv[1:]}) + "\n")
if sys.argv[1] == "install":
    print("settings.json: write (hooks)")
    print("reconcile job: write /h/Library/LaunchAgents/com.orche.reconcile.plist (every 300s, loaded)")
    if (home / "fail-install").exists():
        sys.exit(1)
    state = json.loads((home / "install-state.json").read_text())
    state["version"] = version   # as the real install records it on success
    (home / "install-state.json").write_text(json.dumps(state))
    sys.exit(0)
print(f"refreshed {sys.argv[2]}: 2 file(s)")
'''


def git(cwd, *argv):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
                           *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(root: Path, version: str, skill="old") -> None:
    (root / "lib" / "orche").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "orche" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "bin").mkdir(exist_ok=True)
    (root / "bin" / "orche.py").write_text(FAKE)
    (root / "skills" / "orche-main").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "orche-main" / "SKILL.md").write_text(skill + "\n")


class UpdateTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self._t = tempfile.TemporaryDirectory()
        base = Path(self._t.name)
        self.src, self.origin, self.co = base / "src", base / "origin.git", base / "co"
        self.src.mkdir()
        git(self.src, "init", "-q")
        write(self.src, "0.7.3")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "0.7.3")
        git(base, "clone", "-q", "--bare", str(self.src), str(self.origin))
        git(base, "clone", "-q", str(self.origin), str(self.co))
        self.base = base
        (self.home / "install-state.json").write_text(json.dumps({"mode": "settings", "version": "0.7.3"}))
        reg = registry.load()
        for key in ("acme", "beta"):
            reg["projects"][key] = {"path": str(base / key), "repo_id": key, "main_session": f"main-{key}",
                                    "card": "x", "standing": ".orche/STANDING.md", "stage": "building",
                                    "aliases": [], "divisions": {}}
        registry.save(reg)
        patcher = mock.patch.object(cmd_update, "ROOT", self.co)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._t.cleanup()
        super().tearDown()

    def publish(self, version="0.8.0", skill="new"):
        write(self.src, version, skill)
        git(self.src, "commit", "-qam", version)
        git(self.src, "push", "-q", str(self.origin), "main")

    def update(self, dry_run=False, json_=False):
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            code = cmd_update.cmd_update(argparse.Namespace(json=json_, dry_run=dry_run))
        return code, so.getvalue(), se.getvalue()

    def calls(self):
        log = self.home / "fake-calls.log"
        return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []

    def head(self, ref="HEAD"):
        return git(self.co, "rev-parse", ref).strip()

    def test_already_up_to_date(self):
        code, out, _ = self.update()
        self.assertEqual((code, out.strip()), (0, "already up to date (0.7.3)"))
        self.assertEqual(self.calls(), [])

    def test_fast_forward_reinstalls_and_refreshes_with_the_new_code(self):
        self.publish()
        code, out, err = self.update()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.head(), self.head("origin/main"))
        lines = out.splitlines()
        self.assertEqual(lines[0], "ORCHE 0.7.3 → 0.8.0")
        self.assertIn("refreshed acme: 2 file(s)", lines)
        self.assertIn("refreshed beta: 2 file(s)", lines)
        self.assertIn("reconcile job: write /h/Library/LaunchAgents/com.orche.reconcile.plist (every 300s, loaded)",
                      lines)
        self.assertNotIn("settings.json: write (hooks)", lines)
        self.assertEqual(lines[-1], cmd_update.RESTART)
        self.assertIn("update: fast-forwarding", err)
        calls = self.calls()
        self.assertEqual({c["version"] for c in calls}, {"0.8.0"})   # the NEW code ran every step
        self.assertEqual(calls[0]["argv"], ["install", "--mode", "settings", "--no-jev"])
        self.assertEqual([c["argv"] for c in calls[1:]], [["init", "acme", "--refresh"], ["init", "beta", "--refresh"]])

    def test_no_restart_line_without_a_session_file_change(self):
        self.publish(skill="old")
        code, out, _ = self.update()
        self.assertEqual(code, 0)
        self.assertNotIn(cmd_update.RESTART, out)

    def test_dry_run_changes_nothing(self):
        self.publish()
        before = (self.head(), self.head("origin/main"))
        code, out, _ = self.update(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual((self.head(), self.head("origin/main")), before)
        self.assertEqual(self.calls(), [])
        self.assertIn("ORCHE 0.7.3 → 0.8.0 (dry run: nothing changed)", out)
        self.assertIn("would refresh acme: orche init acme --refresh", out)
        self.assertIn("would run: orche install --mode settings --no-jev", out)

    def test_dry_run_on_a_development_checkout_plans_no_merge(self):
        git(self.co, "checkout", "-qb", "feature")
        write(self.co, "0.7.4", "dev")
        git(self.co, "commit", "-qam", "dev")
        code, out, err = self.update(dry_run=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(out.splitlines()[0], "ORCHE 0.7.3 → 0.7.4 (dry run: nothing changed)")
        self.assertIn("would run: orche install --mode settings --no-jev", out)
        self.assertIn("would refresh beta: orche init beta --refresh", out)
        self.assertNotIn("would run: git merge", out)
        self.assertNotIn("fetching", err)

    def assert_aligned(self, why, version):
        """The install and every project ran the checkout's own code; nothing was fetched or merged."""
        code, out, err = self.update()
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"ORCHE 0.7.3 → {version}")
        self.assertIn("refreshed acme: 2 file(s)", lines)
        self.assertIn("refreshed beta: 2 file(s)", lines)
        self.assertEqual(lines[-1], cmd_update.RESTART)
        self.assertIn(f"update: development checkout ({why}): nothing pulled", err)
        calls = self.calls()
        self.assertEqual({c["version"] for c in calls}, {version})
        self.assertEqual([c["argv"][0] for c in calls], ["install", "init", "init"])
        return out, err

    def test_a_dirty_checkout_aligns_the_install_without_pulling(self):
        self.publish()   # origin has 0.8.0: a dirty checkout must not fetch it
        before = (self.head(), self.head("origin/main"))
        (self.co / "lib" / "orche" / "__init__.py").write_text('__version__ = "0.7.3-dev"\n')
        _, err = self.assert_aligned("uncommitted changes", "0.7.3-dev")
        self.assertNotIn("fetching", err)
        self.assertEqual((self.head(), self.head("origin/main")), before)
        self.assertIn("0.7.3-dev", (self.co / "lib" / "orche" / "__init__.py").read_text())

    def test_another_branch_with_a_current_install_is_up_to_date(self):
        self.publish()
        git(self.co, "checkout", "-qb", "feature")
        code, out, err = self.update()
        self.assertEqual((code, out.strip()), (0, "already up to date (0.7.3; development checkout, nothing pulled)"))
        self.assertEqual(self.calls(), [])
        self.assertNotIn("fetching", err)

    def test_local_commits_align_without_a_merge(self):
        self.publish()
        write(self.co, "0.7.4", "dev")
        git(self.co, "commit", "-qam", "local")
        before = self.head()
        self.assert_aligned("1 commit(s) not on origin/main", "0.7.4")
        self.assertEqual(self.head(), before)

    def test_a_failing_install_names_the_step(self):
        self.publish()
        (self.home / "fail-install").touch()
        code, out, _ = self.update()
        self.assertEqual(code, 1)
        self.assertIn("orche update: step install failed: exit 1", out)
        self.assertEqual([c["argv"][0] for c in self.calls()], ["install"])   # no refresh after it

    def test_a_rerun_after_a_failed_install_resumes(self):
        self.publish()
        (self.home / "fail-install").touch()
        self.assertEqual(self.update()[0], 1)
        (self.home / "fail-install").unlink()
        code, out, err = self.update()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("already up to date", out)
        self.assertEqual(out.splitlines()[0], "ORCHE 0.7.3 → 0.8.0")
        self.assertIn("update: resuming", err)
        self.assertEqual([c["argv"][0] for c in self.calls()], ["install", "install", "init", "init"])
        self.assertEqual(self.update()[1].strip(), "already up to date (0.8.0)")   # the install recorded 0.8.0

    def cache_copy(self):
        """A plugin cache copy: the same files, no .git."""
        cache = self.base / "cache" / "orche" / "0.7.3"
        shutil.copytree(self.co, cache, ignore=shutil.ignore_patterns(".git"))
        cmd_update.ROOT = cache   # the patcher restores it
        return cache

    def test_the_plugin_cache_copy_updates_the_recorded_clone(self):
        cache = self.cache_copy()
        state = json.loads((self.home / "install-state.json").read_text())
        (self.home / "install-state.json").write_text(json.dumps({**state, "clone": str(self.co)}))
        self.publish()
        code, out, err = self.update()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.head(), self.head("origin/main"))
        self.assertEqual({c["version"] for c in self.calls()}, {"0.8.0"})   # the clone's new bin/orche.py ran
        self.assertIn('"0.7.3"', (cache / "lib" / "orche" / "__init__.py").read_text())

    def test_no_recorded_clone_and_no_git_is_named(self):
        cache = self.cache_copy()
        code, out, err = self.update()
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), f"not a git checkout: {cache}; reinstall with the README prompt")
        self.assertNotIn("development checkout", out + err)

    def test_fetch_never_prompts_is_bounded_and_masks_its_error(self):
        seen, real = [], subprocess.run

        def spy(argv, **kw):
            if "fetch" in argv:
                seen.append(kw)
            return real(argv, **kw)
        bad = self.base / "x token=hunter2xyz" / "nope.git"
        git(self.co, "remote", "set-url", "origin", str(bad))
        with mock.patch.object(cmd_update.subprocess, "run", spy):
            code, out, err = self.update()
        self.assertEqual(code, 1)
        self.assertEqual((seen[0]["env"]["GIT_TERMINAL_PROMPT"], seen[0]["timeout"]), ("0", 120))
        self.assertIn("step git fetch failed", out)
        self.assertNotIn("hunter2xyz", out + err)

    def test_json_covers_refusals_and_results(self):
        git(self.co, "checkout", "-qb", "feature")
        code, out, _ = self.update(json_=True)
        self.assertEqual((code, json.loads(out)), (0, {"status": "current", "version": "0.7.3",
                                                       "development": "on feature, not main"}))
        git(self.co, "checkout", "-q", "main")
        self.publish()
        code, out, _ = self.update(json_=True)
        data = json.loads(out)
        self.assertEqual((code, data["status"], data["new"], data["development"]), (0, "updated", "0.8.0", None))
        self.cache_copy()
        code, out, _ = self.update(json_=True)
        self.assertEqual((code, json.loads(out)["status"]), (2, "refused"))



class InitRefreshTest(HrCase):
    """`orche init <project> --refresh`, the step `orche update` runs per project: ORCHE-owned files
    only — no Orca pane, automation, worktree or intake."""

    def setUp(self):
        super().setUp()
        self.personas.mkdir()
        (self.personas / "div-build.md").write_text("---\nname: {{SESSION}}\nmodel: {{MODEL}}\n---\nnew {{EFFORT}}\n")
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "build", "--wake", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.calls.clear()

    def test_refresh_rerenders_an_outdated_persona_and_touches_no_orca(self):
        persona = Path(os.path.realpath(self.repo)) / ".claude" / "agents" / "div-acme-shop-build.md"
        persona.write_text("---\nname: old\n---\nold\n")
        code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("refreshed acme-shop: "), out)
        self.assertNotIn("already current", out)
        self.assertIn("new", persona.read_text())
        self.assertEqual([c for c in self.calls if c[1] != "worktree" or c[2] != "list"], [])
        code, out, _ = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual((code, out.splitlines()[0]), (0, "refreshed acme-shop: already current"))

    def test_refresh_never_recreates_a_project_template_the_user_deleted(self):
        root = Path(os.path.realpath(self.repo))
        for rel in (".env.example", "docs/RUBRIC.md", ".orche/STANDING.md"):
            (root / rel).unlink()
        code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual((code, out.splitlines()[0]), (0, "refreshed acme-shop: already current"), err)
        for rel in (".env.example", "docs/RUBRIC.md", ".orche/STANDING.md"):
            self.assertFalse((root / rel).exists(), rel)

    def test_a_privacy_blocked_project_folder_is_named_unreadable(self):
        root = os.path.realpath(self.repo)
        before = registry.path().read_bytes()

        def denied(path="."):
            raise PermissionError(1, "Operation not permitted", path)
        with mock.patch.object(cmd_init.os, "listdir", denied), \
                mock.patch.object(cmd_init.privacy, "protected", lambda p: p == root):
            code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual(code, 3, out)
        self.assertIn(f"unreadable (privacy): {root}", err)
        self.assertNotIn("missing", err)
        self.assertEqual(registry.path().read_bytes(), before)

    def test_refresh_refuses_setup_flags_and_unknown_projects(self):
        code, _, err = self.orche("init", "acme-shop", "--refresh", "--divisions", "build")
        self.assertEqual(code, 1)
        self.assertIn("--refresh only re-renders", err)
        code, _, err = self.orche("init", "nope", "--refresh")
        self.assertEqual(code, 2)
        self.assertIn("needs a registered project", err)


if __name__ == "__main__":
    unittest.main()
