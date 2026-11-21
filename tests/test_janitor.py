"""Janitor: state-based archive/trash, GRAVEYARD rows, undo/restore, purge, discard, the daily rule.

Synthetic world in a temp dir: an active unit in a live terminal, a retired unit with two transcripts
(its worktree already gone), a dirty worktree, 10-day-old `.next` dirs, a fresh `.next`, a GRAVEYARD
`path:` row, and `.env*` / `memory/` files that must never be listed. git/Orca/launchctl go through a
fake `shell.RUNNER`.
"""
import datetime as dt
import io
import json
import os
import shutil
import tarfile
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import janitor, registry, shell, units
from orche.janitor import archive, discover, rules, trash
from test_registry import load_cli

DAY = 86400


def iso(days_ago):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


def div(status, wt, session, days_ago, **extra):
    return {"status": status, "worktree": f"repo-acme::{wt}", "branch": f"div/{session}", "session": session,
            "model": "claude-opus-5-5", "last_activity": iso(days_ago), **extra}


class JanitorCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self._world = tempfile.TemporaryDirectory()
        self.addCleanup(self._world.cleanup)
        self.w = Path(self._world.name).resolve()
        self._old_home_env = os.environ.get("HOME")
        os.environ["HOME"] = str(self.w / "userhome")
        self.addCleanup(self._restore_home)
        self._orig_zstd, archive.zstd_bin = archive.zstd_bin, lambda: None
        self.addCleanup(setattr, archive, "zstd_bin", self._orig_zstd)
        self.calls, self.dirty, self.not_ignored, self.landed, self.ignored_dirs = [], {}, set(), set(), {}
        self._orig_runner, shell.RUNNER = shell.RUNNER, self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", self._orig_runner)
        self.claude = self.w / "claude-projects"
        # Fixture trees are a few bytes: the worth floor is off unless a test sets it.
        self._orig_worth, rules.WORTH_MIN = rules.WORTH_MIN, 0
        self.addCleanup(setattr, rules, "WORTH_MIN", self._orig_worth)
        (self.home / "config.json").write_text(json.dumps(support.no_wake({"notify": False, "janitor": {
            "codex_tmp": str(self.w / "codex-tmp"), "scratch_root": str(self.w / "scratch"),
            "claude_projects": str(self.claude)}})))
        self.build_world()

    def _restore_home(self):
        if self._old_home_env is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home_env

    # ---- fakes ---------------------------------------------------------------------------------
    def fake_runner(self, argv, *, timeout, input, env=None):
        self.calls.append(argv)
        if argv[1:3] == ["worktree", "list"]:
            return self.ok({"worktrees": [{"id": f"repo-acme::{p}", "path": str(p)} for p in self.orca_paths]})
        if argv[1:3] == ["terminal", "list"]:
            return self.ok({"terminals": self.terminals})
        if argv[0] == "git" and argv[3] == "status":
            return shell.Result(0, self.dirty.get(argv[2], ""), "", argv)
        if argv[1:3] == ["worktree", "rm"]:
            return self.ok({"removed": True})
        if argv[0] == "git" and argv[3] == "ls-files" and "--directory" in argv:
            return shell.Result(0, self.ignored_dirs.get(argv[2], ""), "", argv)
        if argv[0] == "git" and argv[3] == "ls-files":
            return shell.Result(0, "", "", argv)
        if argv[0] == "git" and argv[3] == "check-ignore":
            return shell.Result(1 if argv[-1] in self.not_ignored else 0, "", "", argv)
        if argv[0] == "git" and argv[3] == "symbolic-ref":
            return shell.Result(0, "origin/main\n", "", argv)
        if argv[0] == "git" and argv[3:5] == ["merge-base", "--is-ancestor"]:
            return shell.Result(0 if argv[5] in self.landed else 1, "", "", argv)
        if argv[0] == "git" and argv[3] == "tag":
            return shell.Result(0, "", "", argv)
        if argv[0] == "launchctl":
            return shell.Result(0, "", "", argv)
        return shell.Result(127, "", "unexpected", argv)

    @staticmethod
    def ok(result):
        return shell.Result(0, json.dumps({"ok": True, "result": result}), "", [])

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *extra):
        code, out, err = self.orche("--json", "janitor", "run", *extra)
        self.assertEqual(code, 0, err + out)
        return json.loads(out)

    # ---- world ---------------------------------------------------------------------------------
    def touch(self, rel, text="x", days_ago=0, base=None):
        path = (base or self.w) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.age(path, days_ago)
        return path

    @staticmethod
    def age(path, days_ago):
        t = time.time() - days_ago * DAY
        for p in [path, *[q for q in Path(path).parents]][:3]:
            os.utime(p, (t, t))

    def age_tree(self, root, days_ago):
        t = time.time() - days_ago * DAY
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                os.utime(os.path.join(dirpath, n), (t, t))
        os.utime(root, (t, t))

    def transcript(self, cwd, name, days_ago=2):
        slug = self.claude / discover.claude_slug(cwd)
        path = slug / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "cwd": str(cwd)}) + "\n" + json.dumps({"x": 1}) + "\n")
        self.age(path, days_ago)
        return path

    def build_world(self):
        w = self.w
        self.proj = w / "acme"
        self.wt = {n: w / f"acme-{n}" for n in ("build", "rnd", "research", "gtm", "ops", "search")}
        for p in [self.proj, *self.wt.values()]:
            p.mkdir(parents=True)
        reg = registry.empty()
        search = {"name": "search", "worktree": f"repo-acme::{self.wt['search']}", "branch": "feat/search",
                  "scope": ["src/search/**"], "session": "feat-acme-search", "status": "retired",
                  "created": iso(30), "last_activity": iso(10), "reason": "search rewrite"}
        reg["projects"]["acme"] = {
            "repo_id": "repo-acme", "path": str(self.proj), "stage": "building", "aliases": [],
            "main_session": "main-acme", "card": "cards/acme.md",
            "standing": ".orche/STANDING.md", "divisions": {
                "build": div("active", self.wt["build"], "div-acme-build", 1, features=[search]),
                "rnd": div("dormant", self.wt["rnd"], "div-acme-rnd", 10),
                "research": div("dormant", self.wt["research"], "div-acme-research", 10),
                "gtm": div("dormant", self.wt["gtm"], "div-acme-gtm", 10),
                "ops": div("paused", self.wt["ops"], "div-acme-ops", 10)}}
        registry.save(reg)
        self.orca_paths = [self.proj, *self.wt.values()]
        self.terminals = [{"handle": "t1", "title": "✳ div-acme-build", "worktreePath": str(self.wt["build"])}]
        self.dirty = {str(self.wt["gtm"]): " M src/app.ts\n?? notes.txt\n"}
        # caches
        self.touch("acme-build/.next/old.js", days_ago=10)          # active unit: never listed
        self.age_tree(self.wt["build"] / ".next", 10)
        self.touch("acme-rnd/.next/fresh.js", days_ago=2)           # fresh: skip
        self.old_next = self.touch("acme-research/.next/cache.js", days_ago=10).parent
        self.age_tree(self.old_next, 10)                             # old: trash
        self.touch("acme-research/.env.local", "SECRET=1")
        self.touch("acme-research/memory/notes.md")
        self.touch("acme-gtm/.next/cache.js", days_ago=10)          # dirty: wip:2
        self.age_tree(self.wt["gtm"] / ".next", 10)
        self.touch("acme-ops/.next/cache.js", days_ago=10)          # paused: never listed
        self.age_tree(self.wt["ops"] / ".next", 10)
        # transcripts: retired unit (two files; its worktree was removed at retirement), active unit, claude memory
        self.t_named = self.transcript(self.wt["search"], "feat-acme-search.jsonl")
        self.t_cwd = self.transcript(self.wt["search"], "0f1e2d3c-uuid.jsonl")
        self.wt["search"].rmdir()
        self.t_active = self.transcript(self.wt["build"], "aaaa-active.jsonl")
        self.claude_memory = self.touch("memory/MEMORY.md", base=self.t_named.parent)
        # GRAVEYARD rows (path pointer + protected pointer)
        self.scratch_dir = self.touch("acme/scratch/old-proto/a.txt", "proto").parent
        self.touch("acme/.env.local", "SECRET=3")
        gy = (Path(support.ROOT) / "templates/project/docs/GRAVEYARD.md").read_text()
        (self.proj / "docs").mkdir()
        (self.proj / "docs/GRAVEYARD.md").write_text(
            gy + "| 2026-10-01 | path:scratch/old-proto | superseded by v2 | - | user |\n"
                 "| 2026-10-01 | path:.env.local | stale | - | user |\n")


class DryRunTest(JanitorCase):
    def test_dry_run_lists_exactly_the_candidates(self):
        data = self.run_json()
        self.assertEqual(data["mode"], "dry-run")
        acts = {(c["action"], c["target"]) for c in data["candidates"]}
        self.assertEqual(acts, {
            ("archive", "sessions:feat-acme-search (2 files)"),
            ("delete", str(self.old_next)),
            ("delete", str(self.wt["gtm"] / ".next")),       # uncommitted work no longer holds a cache
            ("delete", str(self.wt["ops"] / ".next")),       # paused, quiet 10 days
            ("trash", "scratch/old-proto"),
            ("skip", str(self.wt["build"])),
            ("skip", str(self.wt["rnd"] / ".next")),
        })
        by_target = {c["target"]: c for c in data["candidates"]}
        self.assertEqual(by_target[str(self.wt["build"])]["reason"], "live terminal div-acme-build")
        self.assertTrue(by_target[str(self.wt["rnd"] / ".next")]["reason"].startswith("fresh"))
        archived = by_target["sessions:feat-acme-search (2 files)"]
        self.assertEqual(sorted(archived["files"]), sorted([str(self.t_named), str(self.t_cwd)]))
        blob = json.dumps(data)
        self.assertNotIn(".env", blob)
        self.assertNotIn("/memory", blob)
        self.assertNotIn("acme-build/", blob)
        # nothing changed
        self.assertTrue(self.old_next.exists() and self.t_named.exists() and self.scratch_dir.exists())
        self.assertFalse(any((self.home / "trash").iterdir()))
        self.assertIn("(dry-run)", janitor_report(self))

    def test_dry_run_flag_wins_over_enable(self):
        self.orche("janitor", "enable")
        data = self.run_json("--dry-run")
        self.assertEqual(data["mode"], "dry-run")
        self.assertTrue(self.old_next.exists())

    def test_report_command(self):
        janitor.report_path().unlink()   # support's fresh report
        self.assertEqual(self.orche("janitor", "report")[0], 2)
        self.run_json()
        code, out, _ = self.orche("janitor", "report")
        self.assertEqual(code, 0)
        self.assertIn("| delete | age:build-cache |", out)


def janitor_report(case):
    return (case.home / "janitor" / "last-report.md").read_text()


class LiveRunTest(JanitorCase):
    def enable_and_run(self):
        self.assertEqual(self.orche("janitor", "enable")[0], 0)
        return self.run_json()

    def trash_manifests(self):
        return [json.loads(p.read_text()) for p in (self.home / "trash").glob("*/*/manifest.json")]

    def test_run_performs_with_manifests_and_graveyard_rows(self):
        data = self.enable_and_run()
        self.assertEqual(data["mode"], "live")
        self.assertFalse(any(c.get("error") for c in data["candidates"]))
        # trash (the GRAVEYARD row) and delete (regenerable dirs: gone for real, no trash item)
        self.assertFalse(self.scratch_dir.exists())
        for gone in (self.old_next, self.wt["gtm"] / ".next", self.wt["ops"] / ".next"):
            self.assertFalse(gone.exists(), gone)
        manifests = {m["path"]: m for m in self.trash_manifests()}
        self.assertEqual(set(manifests), {str(self.scratch_dir)})
        for m in manifests.values():
            self.assertEqual(m["status"], "trashed")
            self.assertTrue(Path(m["stored"]).exists())
        self.assertEqual(len(list((self.home / "trash").glob("*/*"))), 1)
        # archive
        self.assertFalse(self.t_named.exists() or self.t_cwd.exists())
        self.assertTrue(self.t_active.exists() and self.claude_memory.exists())
        unit_dir = self.home / "archive" / "feat-acme-search"
        (tar_path,) = unit_dir.glob("sessions-*.tar.gz")
        (man_path,) = unit_dir.glob("sessions-*.manifest.json")
        man = json.loads(man_path.read_text())
        with tarfile.open(tar_path, "r:gz") as tf:
            self.assertEqual(len([m for m in tf.getmembers() if m.isfile()]), man["count"])
        self.assertEqual(man["count"], 2)
        self.assertEqual(oct(tar_path.stat().st_mode & 0o777), "0o600")
        # untouched
        for keep in ("acme-research/.env.local", "acme-research/memory/notes.md",
                     "acme-rnd/.next/fresh.js", "acme-build/.next/old.js", "acme/.env.local"):
            self.assertTrue((self.w / keep).exists(), keep)
        # GRAVEYARD rows: project + global
        rows = (self.proj / "docs/GRAVEYARD.md").read_text()
        self.assertIn("| archived 2 session files of feat-acme-search | retired: unit retired |", rows)
        self.assertIn("orche janitor restore feat-acme-search | janitor |", rows)
        self.assertIn("| trashed scratch/old-proto | graveyard: superseded by v2 | orche janitor undo ", rows)
        self.assertNotIn("| deleted ", rows)       # the project's file is tracked: no row per cache
        glob_rows = (self.home / "graveyard.md").read_text()
        self.assertEqual(glob_rows.count("| acme: "), 5)
        self.assertIn(f"| acme: deleted {self.old_next} | age:build-cache: unit idle 10d", glob_rows)
        self.assertIn("| none: regenerable (reinstall or rebuild) | janitor |", glob_rows)
        # second pass: nothing left to do, rows settled
        again = self.run_json()
        self.assertEqual([c for c in again["candidates"] if c["action"] != "skip"], [])
        state = json.loads((self.home / "janitor/state.json").read_text())
        self.assertEqual(len(state["processed"]), 2)  # path row + refused protected row

    def test_undo_restores_trashed_path(self):
        self.enable_and_run()
        m = next(m for m in self.trash_manifests() if m["path"] == str(self.scratch_dir))
        self.scratch_dir.mkdir(parents=True)
        self.assertEqual(self.orche("janitor", "undo", m["id"])[0], 3)          # original exists
        self.scratch_dir.rmdir()
        code, out, err = self.orche("janitor", "undo", m["id"])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.scratch_dir / "a.txt").exists())
        self.assertEqual(self.orche("janitor", "undo", m["id"])[0], 3)          # already restored
        self.assertEqual(self.orche("janitor", "undo", "nope")[0], 2)

    def test_restore_archived_sessions(self):
        self.enable_and_run()
        code, out, err = self.orche("--json", "janitor", "restore", "feat-acme-search")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(json.loads(out)["files"]), sorted([str(self.t_named), str(self.t_cwd)]))
        self.assertIn('"cwd"', self.t_named.read_text())
        self.assertEqual(self.orche("janitor", "restore", "feat-acme-search")[0], 2)  # nothing pending
        self.assertEqual(self.orche("janitor", "restore", "nobody")[0], 2)

    def test_restore_refuses_to_overwrite(self):
        self.enable_and_run()
        self.t_named.write_text("new")
        self.assertEqual(self.orche("janitor", "restore", "feat-acme-search")[0], 3)
        self.assertEqual(self.t_named.read_text(), "new")
        self.assertFalse(self.t_cwd.exists())

    def test_disable_returns_to_dry_run(self):
        self.orche("janitor", "enable")
        self.orche("janitor", "disable")
        self.assertEqual(self.run_json()["mode"], "dry-run")
        self.assertTrue(self.old_next.exists())


class PurgeTest(JanitorCase):
    def test_purge_31_days_keeps_29(self):
        today = dt.date.today()
        old = self.home / "trash" / (today - dt.timedelta(days=31)).isoformat()
        young = self.home / "trash" / (today - dt.timedelta(days=29)).isoformat()
        for d in (old, young):
            (d / "x1").mkdir(parents=True)
            (d / "x1/manifest.json").write_text("{}")
        dry = self.run_json()
        self.assertIn(("purge", f"trash/{old.name}"), {(c["action"], c["target"]) for c in dry["candidates"]})
        self.assertTrue(old.exists())
        self.orche("janitor", "enable")
        self.run_json()
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertIn(f"| purged trash/{old.name} |", (self.home / "graveyard.md").read_text())


class DiscardTest(JanitorCase):
    def test_discard_path_writes_row_and_next_run_trashes(self):
        junk = self.touch("acme/tmp-export/data.csv").parent
        code, out, err = self.orche("discard", str(junk), "--why", "one-off export", "--by", "main-acme")
        self.assertEqual(code, 0, err)
        rows = (self.proj / "docs/GRAVEYARD.md").read_text()
        self.assertIn("| path:tmp-export | one-off export | pending: next `orche janitor run` | main-acme |", rows)
        self.assertIn("| acme: path:tmp-export |", (self.home / "graveyard.md").read_text())
        data = self.run_json()
        self.assertIn(("trash", "tmp-export"), {(c["action"], c["target"]) for c in data["candidates"]})

    def test_discard_session_and_units(self):
        self.assertEqual(self.orche("discard", "div-acme-build", "--why", "x")[0], 3)    # active unit
        self.assertEqual(self.orche("discard", "main-acme", "--why", "x")[0], 3)         # project session
        self.assertEqual(self.orche("discard", str(self.proj / ".env.local"), "--why", "x")[0], 3)
        code, _, err = self.orche("discard", "feat-acme-search", "--why", "done")
        self.assertEqual(code, 0, err)
        self.assertIn("| session:feat-acme-search | done |", (self.proj / "docs/GRAVEYARD.md").read_text())
        stray = self.transcript(self.w / "elsewhere", "one-off-session.jsonl")
        code, _, err = self.orche("discard", "one-off-session", "--why", "experiment", "--project", "acme")
        self.assertEqual(code, 0, err)
        data = self.run_json()
        acts = {(c["action"], c["target"]) for c in data["candidates"]}
        self.assertIn(("archive", "sessions:one-off-session (1 files)"), acts)
        self.assertIn(("archive", "sessions:feat-acme-search (2 files)"), acts)  # deduped with rule 1
        self.orche("janitor", "enable")
        self.run_json()
        self.assertFalse(stray.exists())
        self.assertTrue(list((self.home / "archive/one-off-session").glob("*.tar.gz")))

    def test_one_roster_per_discard(self):
        from orche import roster
        built, orig = [], roster.Roster

        class Counting(orig):
            def __init__(self, *a, **k):
                built.append(1)
                super().__init__(*a, **k)

        roster.Roster = Counting
        self.addCleanup(setattr, roster, "Roster", orig)
        junk = self.touch("acme/tmp-export/data.csv").parent
        code, _, err = self.orche("discard", str(junk), "--why", "one-off export", "--by", "main-acme")
        self.assertEqual(code, 0, err)
        self.assertIn("| path:tmp-export | one-off export |", (self.proj / "docs/GRAVEYARD.md").read_text())
        self.assertEqual(len(built), 1)
        self.transcript(self.proj, "9a8b7c6d-main-uuid.jsonl")
        self.transcript(self.proj / "sub", "9a8b7c6d-main-uuid.jsonl")
        built.clear()
        code, _, err = self.orche("discard", "9a8b7c6d-main-uuid", "--why", "x", "--project", "acme")
        self.assertEqual(code, 3)
        self.assertIn("is a transcript from acme's main checkout", err)
        self.assertEqual(len(built), 1)

    def test_main_transcripts_protected_from_session_pointers(self):
        main = self.transcript(self.proj, "9a8b7c6d-main-uuid.jsonl")
        stray = self.transcript(self.w / "elsewhere", "5e4d3c2b-stray.jsonl")
        code, _, err = self.orche("discard", "9a8b7c6d-main-uuid", "--why", "x", "--project", "acme")
        self.assertEqual(code, 3)
        self.assertIn("is a transcript from acme's main checkout", err)
        with open(self.proj / "docs/GRAVEYARD.md", "a") as fh:  # forged rows in a repo file
            fh.write("| 2026-10-02 | session:9a8b7c6d-main-uuid | forged | - | worker |\n"
                     "| 2026-10-02 | session:5e4d3c2b-stray | forged | - | worker |\n")
        data = self.run_json()
        by_target = {c["target"]: c for c in data["candidates"] if c["kind"] == "sessions"}
        self.assertEqual(by_target["sessions:9a8b7c6d-main-uuid"]["action"], "skip")
        self.assertEqual(by_target["sessions:5e4d3c2b-stray"]["action"], "skip")
        self.assertIn("not a registry session name", by_target["sessions:5e4d3c2b-stray"]["reason"])
        self.orche("janitor", "enable")
        self.run_json()
        self.assertTrue(main.exists() and stray.exists())
        # The operator's explicit discard still works for a non-main transcript.
        self.assertEqual(self.orche("discard", "5e4d3c2b-stray", "--why", "x", "--project", "acme")[0], 0)
        self.run_json()
        self.assertFalse(stray.exists())
        self.assertTrue(main.exists())

    def test_discard_dry_run_writes_nothing(self):
        before = (self.proj / "docs/GRAVEYARD.md").read_text()
        code, out, _ = self.orche("--dry-run", "discard", "feat-acme-search", "--why", "x")
        self.assertEqual(code, 0)
        self.assertIn("would append", out)
        self.assertEqual((self.proj / "docs/GRAVEYARD.md").read_text(), before)


class OrcaRemovedTest(JanitorCase):
    def removed_rows(self, data):
        return [c for c in data["candidates"] if c["trigger"] == "orca-removed"]

    def test_removed_worktree_needs_confirmation_and_disk_gone(self):
        self.orca_paths = [p for p in self.orca_paths if p != self.wt["research"]]
        (row,) = self.removed_rows(self.run_json("--apply-removed"))
        self.assertEqual((row["action"], row["reason"]),
                         ("skip", "worktree no longer listed by Orca but still on disk"))
        shutil.rmtree(self.wt["research"])
        (row,) = self.removed_rows(self.run_json())
        self.assertEqual((row["action"], row["target"]), ("skip", "div-acme-research"))
        self.assertIn("--apply-removed", row["reason"])
        t = self.transcript(self.wt["research"], "research-sess.jsonl")
        self.orche("janitor", "enable")
        data = self.run_json("--apply-removed")
        acts = {(c["action"], c["target"]) for c in data["candidates"]}
        self.assertIn(("retire", "div-acme-research"), acts)
        self.assertIn(("archive", "sessions:div-acme-research (1 files)"), acts)
        self.assertEqual(registry.load()["projects"]["acme"]["divisions"]["research"]["status"], "retired")
        self.assertEqual(registry.load()["events"][-1]["type"], "retire")
        self.assertFalse(t.exists())

    def test_retire_writes_graveyard_row_and_unresolvable_worktree_skipped(self):
        reg = registry.load()
        reg["projects"]["acme"]["divisions"]["gtm"]["worktree"] = "repo-acme::relative/garbage"
        registry.save(reg)
        self.orca_paths = [p for p in self.orca_paths if p not in (self.wt["research"], self.wt["gtm"])]
        shutil.rmtree(self.wt["research"])
        self.orche("janitor", "enable")
        data = self.run_json("--apply-removed")
        self.assertEqual([(c["action"], c["target"]) for c in self.removed_rows(data)],
                         [("retire", "div-acme-research")])
        self.assertEqual(self.statuses()["gtm"], "dormant")
        rows = (self.proj / "docs/GRAVEYARD.md").read_text()
        self.assertIn(f"| unit div-acme-research (worktree repo-acme::{self.wt['research']}) |", rows)
        self.assertIn(f"session archive: {self.home / 'archive' / 'div-acme-research'}", rows)

    def statuses(self):
        divs = registry.load()["projects"]["acme"]["divisions"]
        return {k: v["status"] for k, v in divs.items()}

    def test_empty_listing_retires_nothing(self):
        before = self.statuses()
        self.orca_paths = []
        for wt in self.wt.values():
            shutil.rmtree(wt, ignore_errors=True)
        self.orche("janitor", "enable")
        data = self.run_json("--apply-removed")
        self.assertEqual(self.removed_rows(data), [])
        self.assertEqual(self.statuses(), before)

    def test_listing_without_main_checkout_is_skipped(self):
        before = self.statuses()
        self.orca_paths = [p for p in self.orca_paths if p not in (self.proj, self.wt["research"])]
        shutil.rmtree(self.wt["research"])
        self.orche("janitor", "enable")
        self.assertEqual(self.removed_rows(self.run_json("--apply-removed")), [])
        self.assertEqual(self.statuses(), before)

    def test_active_and_paused_never_retired(self):
        self.orca_paths = [p for p in self.orca_paths if p not in (self.wt["build"], self.wt["ops"])]
        shutil.rmtree(self.wt["build"])
        shutil.rmtree(self.wt["ops"])
        self.terminals = []
        self.orche("janitor", "enable")
        self.assertEqual(self.removed_rows(self.run_json("--apply-removed")), [])
        self.assertEqual(self.statuses()["build"], "active")
        self.assertEqual(self.statuses()["ops"], "paused")

    def test_orca_failure_skips_checkouts(self):
        self.terminals = None
        orig = self.fake_runner

        def failing(argv, *, timeout, input, env=None):
            if argv[1:3] in (["terminal", "list"], ["worktree", "list"]):
                return shell.Result(1, "", "orca down", argv)
            return orig(argv, timeout=timeout, input=input)
        shell.RUNNER = failing
        data = self.run_json()
        self.assertEqual([c for c in data["candidates"] if c["action"] != "skip"], [])
        self.assertFalse(any(c["trigger"] == "orca-removed" for c in data["candidates"]))


class AgeTest(JanitorCase):
    def test_codex_tmp_and_scratch(self):
        codex = self.w / "codex-tmp"
        old_tmp = self.touch("codex-tmp/old-run/f", days_ago=9).parent
        self.age_tree(old_tmp, 9)
        self.touch("codex-tmp/new-run/f", days_ago=1)
        scratch = self.w / "scratch"
        owned = scratch / discover.claude_slug(self.proj)
        old_s = self.touch("sess-old/out.txt", days_ago=9, base=owned).parent
        self.age_tree(old_s, 9)
        self.touch("sess-new/out.txt", days_ago=1, base=owned)
        unknown = self.touch("-somewhere-else/sess/out.txt", days_ago=20, base=scratch).parent
        self.age_tree(unknown, 20)
        active = self.touch("sess/out.txt", days_ago=20, base=scratch / discover.claude_slug(self.wt["build"]))
        self.age_tree(active.parent, 20)
        data = self.run_json()
        trashed = {(c["trigger"], c["target"]) for c in data["candidates"] if c["action"] == "trash"}
        self.assertIn(("age:codex-tmp", str(old_tmp)), trashed)
        self.assertIn(("age:scratch", str(old_s)), trashed)
        blob = json.dumps(data)
        for absent in ("new-run", "sess-new", "-somewhere-else", str(active.parent)):
            self.assertNotIn(absent, blob)
        self.orche("janitor", "enable")
        self.run_json()
        self.assertFalse(old_tmp.exists() or old_s.exists())
        self.assertTrue(unknown.exists() and active.exists() and codex.exists())


class ConfigPathTest(JanitorCase):
    def set_codex(self, value, drop=False):
        cfg = json.loads((self.home / "config.json").read_text())
        if drop:
            cfg["janitor"].pop("codex_tmp")
        else:
            cfg["janitor"]["codex_tmp"] = value
        (self.home / "config.json").write_text(json.dumps(cfg))

    def codex_rows(self):
        return [c for c in self.run_json()["candidates"] if c["trigger"] == "age:codex-tmp"]

    def test_codex_tmp_is_opt_in_and_absolute_only(self):
        cwd = self.w / "cwd"
        old = self.touch("cwd/Documents/f", days_ago=30).parent
        self.age_tree(cwd, 30)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)
        for value, drop in ((None, True), ("", False), (".", False), ("cwd", False), ("Documents", False)):
            self.set_codex(value, drop)
            self.assertEqual(self.codex_rows(), [], repr(value))
        self.set_codex(str(cwd))
        self.assertEqual([c["target"] for c in self.codex_rows()], [str(old)])

    def test_empty_scratch_and_claude_roots_list_nothing(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["janitor"].update(scratch_root="", claude_projects="relative/dir")
        (self.home / "config.json").write_text(json.dumps(cfg))
        data = self.run_json()
        self.assertFalse(any(c["trigger"] == "age:scratch" or c["kind"] == "sessions"
                             for c in data["candidates"]))


class LedgerWipTest(JanitorCase):
    def targets(self):
        return {c["target"]: c for c in self.run_json()["candidates"]}

    def test_ledger_edits_do_not_block_discard(self):
        self.dirty[str(self.proj)] = "?? docs/GRAVEYARD.md\n M docs/LEDGER.tsv\n M .orche/div/x/HANDOFF.md\n"
        self.assertEqual(self.targets()["scratch/old-proto"]["action"], "trash")
        self.dirty[str(self.proj)] = " M docs/GRAVEYARD.md\n M src/app.ts\n"
        row = self.targets()["scratch/old-proto"]
        self.assertEqual((row["action"], row["reason"]), ("skip", "wip:1"))

    def test_git_dir_pointer_refused(self):
        (self.proj / ".git").mkdir()
        (self.proj / ".git/HEAD").write_text("ref")
        with open(self.proj / "docs/GRAVEYARD.md", "a") as fh:
            fh.write("| 2026-10-02 | path:.git | oops | - | user |\n")
        data = self.run_json()
        self.assertNotIn(".git", json.dumps(data))
        self.orche("janitor", "enable")
        self.run_json()
        self.assertTrue((self.proj / ".git/HEAD").exists())
        self.assertTrue(rules.is_protected("repo/.git/config"))


class MainTranscriptGuardTest(JanitorCase):
    def test_retired_unit_at_project_root_never_pulls_main_transcripts(self):
        reg = registry.load()
        below = self.proj / "wt" / "below"
        below.mkdir(parents=True)
        feats = reg["projects"]["acme"]["divisions"]["build"]["features"]
        for name, wt in (("legacy", self.proj), ("below", below)):
            feats.append({"name": name, "worktree": f"repo-acme::{wt}", "branch": f"feat/{name}",
                          "scope": ["x/**"], "session": f"feat-acme-{name}", "status": "retired",
                          "created": iso(30), "last_activity": iso(10), "reason": "done"})
        registry.save(reg)
        self.orca_paths.append(below)
        main = self.transcript(self.proj, "main-uuid.jsonl")
        main_sub = self.transcript(self.proj / "src", "main-sub-uuid.jsonl")
        legacy_named = self.transcript(self.proj, "feat-acme-legacy.jsonl")
        mine = self.transcript(below, "below-uuid.jsonl")
        files = {c["unit"]: c.get("files", []) for c in self.run_json()["candidates"] if c["kind"] == "sessions"}
        self.assertEqual(files["feat-acme-legacy"], [str(legacy_named)])
        self.assertEqual(files["feat-acme-below"], [str(mine)])
        blob = json.dumps(files)
        self.assertNotIn(str(main), blob)
        self.assertNotIn(str(main_sub), blob)


class RegenerableTest(JanitorCase):
    """Rule A: git-ignored dependencies and build output of a quiet unit (any status but onboarding) are deleted."""

    def rows(self):
        return {c["target"]: c for c in self.run_json()["candidates"] if c["trigger"] == "age:build-cache"}

    def set_unit(self, name, **fields):
        reg = registry.load()
        reg["projects"]["acme"]["divisions"][name].update(fields)
        registry.save(reg)

    def aged(self, rel, days_ago=10, text="x"):
        path = self.touch(rel, text, days_ago=days_ago)
        top = self.w / Path(rel).parts[0] / Path(rel).parts[1]
        self.age_tree(top, days_ago)
        return top

    def test_the_touch_file_is_the_clock(self):
        support_activity(self, "div-acme-research", 2)        # registry says 10 days, the touch file 2
        row = self.rows()[str(self.old_next)]
        self.assertEqual((row["action"], row["reason"]), ("skip", "unit active 2d ago"))

    def test_an_active_quiet_unit_loses_its_build_output_for_real(self):
        self.set_unit("build", last_activity=iso(10))
        self.terminals = []
        target = self.wt["build"] / ".next"
        self.assertEqual(self.rows()[str(target)]["action"], "delete")
        self.orche("janitor", "enable")
        data = self.run_json()
        row = next(c for c in data["candidates"] if c["target"] == str(target))
        self.assertEqual((row["result"], row.get("error")), ("deleted", None))
        self.assertFalse(target.exists())
        self.assertEqual([m for m in (self.home / "trash").glob("*/*/manifest.json")
                          if json.loads(m.read_text())["path"] == str(target)], [])
        self.assertIn(f"| acme: deleted {target} |", (self.home / "graveyard.md").read_text())
        self.assertNotIn(f"deleted {target}", (self.proj / "docs/GRAVEYARD.md").read_text())

    def test_uncommitted_work_does_not_hold_ignored_output(self):
        self.dirty[str(self.wt["research"])] = " M src/app.ts\n?? notes.txt\n"
        self.assertEqual(self.rows()[str(self.old_next)]["action"], "delete")

    def test_an_untracked_dir_git_does_not_ignore_stays(self):
        self.not_ignored.add(".next")
        row = self.rows()[str(self.old_next)]
        self.assertEqual((row["action"], row["reason"]), ("skip", "not ignored by git"))

    def test_patterns_match_suffixed_build_dirs(self):
        suffixed = self.aged("acme-research/.next-3100/a.js")
        self.assertEqual(self.rows()[str(suffixed)]["action"], "delete")

    def test_a_package_example_env_is_not_the_owners_but_a_real_env_is(self):
        self.touch("acme-research/package-lock.json", "{}")
        modules = self.aged("acme-research/node_modules/pkg/.env.example")
        dist = self.aged("acme-research/dist/.env", text="SECRET=4")
        rows = self.rows()
        self.assertEqual(rows[str(modules)]["action"], "delete")
        self.assertNotIn(str(dist), rows)
        self.orche("janitor", "enable")
        self.run_json()
        self.assertFalse(modules.exists())
        self.assertTrue((dist / ".env").exists())

    def test_memory_and_cards_inside_build_output_are_not_the_owners(self):
        self.touch("acme-research/.next/server/app/api/next/cards/route.js", days_ago=10)
        self.touch("acme-research/.next/node_modules/next/dist/lib/memory/index.js", days_ago=10)
        self.age_tree(self.old_next, 10)
        self.assertEqual(self.rows()[str(self.old_next)]["action"], "delete")

    def test_below_the_worth_floor_no_row(self):
        rules.WORTH_MIN = 10 ** 12
        self.assertEqual(self.rows(), {})

    def test_shared_files_free_too_little(self):
        rules.WORTH_MIN = 1
        orig = discover.reclaim
        discover.reclaim = lambda path: 0
        self.addCleanup(setattr, discover, "reclaim", orig)
        row = self.rows()[str(self.old_next)]
        self.assertEqual((row["action"], row["reason"]),
                         ("skip", "frees only 0B (files shared with the package store)"))

    def test_reclaim_counts_a_hard_linked_file_as_nothing(self):
        orig = discover._private_size
        discover._private_size = lambda path: None          # the fallback path
        self.addCleanup(setattr, discover, "_private_size", orig)
        tree = self.w / "reclaim"
        one = self.touch("reclaim/one.bin", "x" * 5000)
        self.assertEqual(discover.reclaim(tree), os.lstat(one).st_blocks * 512)
        os.link(one, tree / "two.bin")
        self.assertEqual(discover.reclaim(tree), 0)
        self.assertEqual(discover.reclaim(self.w / "missing"), 0)

    def test_node_modules_needs_a_lockfile(self):
        top = self.aged("acme-research/node_modules/pkg/index.js")
        nested = self.touch("acme-research/apps/web/node_modules/pkg/index.js", days_ago=10).parents[1]
        self.age_tree(nested, 10)
        rows = self.rows()
        for path in (top, nested):
            self.assertEqual((rows[str(path)]["action"], rows[str(path)]["reason"]), ("skip", rules.NO_LOCKFILE))
        self.touch("acme-research/pnpm-lock.yaml", "lock")
        rows = self.rows()
        self.assertEqual((rows[str(top)]["action"], rows[str(nested)]["action"]), ("delete", "delete"))

    def test_a_live_terminal_holds_the_worktree(self):
        self.terminals.append({"handle": "t2", "title": "someone", "worktreePath": str(self.wt["research"])})
        rows = self.rows()
        self.assertEqual(rows[str(self.wt["research"])]["reason"], "live terminal someone")
        self.assertNotIn(str(self.old_next), rows)

    def test_onboarding_is_untouched(self):
        self.set_unit("research", status="onboarding")
        self.assertFalse(any("acme-research" in t for t in self.rows()))

    def test_delete_refuses_a_path_outside_its_worktree(self):
        cand = janitor.Candidate("age:build-cache", "delete", str(self.old_next), "x", path=str(self.old_next),
                                 root=str(self.wt["rnd"]), project="acme")
        with self.assertRaises(ValueError):
            janitor._delete(janitor.context(registry.load()), cand)
        self.assertTrue(self.old_next.exists())


class CostlyTest(JanitorCase):
    """Rule E: a large ignored folder rule A cannot recreate in one command is asked, never deleted."""

    def setUp(self):
        super().setUp()
        self.big = self.sparse("acme-research/data/big.bin")
        self.small = self.touch("acme-research/small/f.txt", days_ago=10).parent
        self.age_tree(self.small, 10)
        self.hidden = self.sparse("acme-research/.orche/cache/big.bin")
        self.ignored_dirs[str(self.wt["research"])] = "data/\nsmall/\n.orche/cache/\n.next/\n.env.local\n"
        orig = discover.reclaim                  # a sparse file frees nothing on disk: count its apparent size
        discover.reclaim = lambda path: discover.tree_stats(path)[0]
        self.addCleanup(setattr, discover, "reclaim", orig)

    def sparse(self, rel):
        path = self.w / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            os.truncate(fh.fileno(), 300 * 2**20)
        self.age_tree(path.parent, 10)
        return path.parent

    def asks(self, data):
        return [c for c in data["candidates"] if c["trigger"] == "age:costly"]

    def pointers(self):
        from orche import inbox
        return [json.loads(f.read_text()) for f in inbox.box("main-acme").glob("*.json")
                if json.loads(f.read_text()).get("from") == "janitor"]

    def test_a_large_unknown_folder_is_asked_once(self):
        (row,) = self.asks(self.run_json())          # dry run: a plan row, nothing posted
        self.assertEqual((row["action"], row["target"], row["size"]), ("ask", str(self.big), 300 * 2**20))
        self.assertEqual(self.pointers(), [])
        self.orche("janitor", "enable")
        (row,) = self.asks(self.run_json())
        self.assertEqual(row["result"], "asked")
        (ptr,) = self.pointers()
        self.assertEqual((ptr["status"], ptr["urgent"], ptr["to"]), ("info", True, "main-acme"))
        self.assertIn("janitor: 1 large folders (300.0M) in quiet worktrees were left alone", ptr["summary"])
        self.assertIn(f"- {self.big} (300.0M, unit div-acme-research)", Path(ptr["report_path"]).read_text())
        (row,) = self.asks(self.run_json())
        self.assertTrue(row["result"].startswith("asked ") and row["result"].endswith("; left for the owner"))
        self.assertEqual(len(self.pointers()), 1)
        self.assertTrue((self.big / "big.bin").exists() and (self.hidden / "big.bin").exists())
        self.assertNotIn(".orche", json.dumps(self.asks(self.run_json())))

    def test_a_paused_project_is_asked_later(self):
        from orche.flow import guard
        guard.pause("acme", "owner")
        self.orche("janitor", "enable")
        (row,) = self.asks(self.run_json())
        self.assertEqual(row["result"], "not asked: project paused")
        self.assertEqual(self.pointers(), [])
        guard.folder("acme").joinpath("pause.json").unlink()
        self.assertEqual(self.asks(self.run_json())[0]["result"], "asked")

    def test_a_quiet_unit_only(self):
        support_activity(self, "div-acme-research", 2)
        self.assertEqual(self.asks(self.run_json()), [])


class FinishedWorkerTest(JanitorCase):
    """Rule B: a landed, quiet worker is retired; a retired worker's leftover worktree is removed."""

    def setUp(self):
        super().setUp()
        self.pay = self.w / "acme-pay"
        self.pay.mkdir()
        self.next = self.touch("acme-pay/.next/a.js", days_ago=10).parent
        self.age_tree(self.next, 10)
        reg = registry.load()
        reg["projects"]["acme"]["divisions"]["build"]["features"].append(
            {"name": "pay", "worktree": f"repo-acme::{self.pay}", "branch": "feat/pay", "scope": ["src/pay/**"],
             "session": "feat-acme-pay", "status": "active", "created": iso(20), "last_activity": iso(10),
             "reason": "payments"})
        registry.save(reg)
        self.orca_paths.append(self.pay)
        self.landed.update({"feat/pay", "HEAD"})

    def set_pay(self, **fields):
        reg = registry.load()
        feat = next(f for f in reg["projects"]["acme"]["divisions"]["build"]["features"] if f["name"] == "pay")
        feat.update(fields)
        registry.save(reg)

    def pay_status(self):
        feats = registry.load()["projects"]["acme"]["divisions"]["build"]["features"]
        return next(f for f in feats if f["name"] == "pay")["status"]

    def rows(self, data=None):
        data = data or self.run_json()
        return [c for c in data["candidates"] if c["trigger"] in ("landed", "retired") and c["kind"] != "sessions"]

    def test_a_landed_quiet_worker_is_retired(self):
        (row,) = self.rows()
        self.assertEqual((row["action"], row["target"], row["reason"]),
                         ("retire", "feat-acme-pay", "work landed, quiet 10d"))
        data = self.run_json()
        self.assertFalse(any(c["target"] == str(self.next) for c in data["candidates"]))  # leaving: no cache row
        self.orche("janitor", "enable")
        (row,) = self.rows(self.run_json())
        self.assertEqual(row.get("error"), None)
        self.assertIn("feat-acme-pay retired (tag archive/feat-pay-", row["result"])
        self.assertEqual(self.pay_status(), "retired")
        self.assertIn(["orca", "worktree", "rm", "--worktree", f"id:repo-acme::{self.pay}"],
                      [c[:5] for c in self.calls])
        self.assertIn("retired: landed", (self.proj / "docs/GRAVEYARD.md").read_text())
        self.assertEqual(registry.load()["events"][-1]["by"], "janitor")

    def test_recent_activity_or_unlanded_work_gives_no_row(self):
        path = support_activity(self, "feat-acme-pay", 2)
        self.assertEqual(self.rows(), [])
        path.unlink()
        self.landed.clear()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.pay_status(), "active")

    def test_a_paused_worker_counts(self):
        self.set_pay(status="paused")
        self.assertEqual([r["action"] for r in self.rows()], ["retire"])

    def test_a_stale_registry_branch_is_not_enough(self):
        self.landed.discard("HEAD")          # the checkout carries a commit not in origin/<base> nor the queue log
        self.assertEqual(self.rows(), [])

    def test_a_division_never(self):
        self.landed.update({"div/div-acme-research", "div/div-acme-rnd"})
        self.assertEqual([r["unit"] for r in self.rows()], ["feat-acme-pay"])

    def test_an_open_pane_or_uncommitted_work_holds_it(self):
        self.terminals.append({"handle": "t3", "title": "feat-acme-pay"})
        (row,) = self.rows()
        self.assertEqual((row["action"], row["reason"]),
                         ("skip", "work landed, quiet 10d; live terminal feat-acme-pay"))
        self.terminals.pop()
        self.dirty[str(self.pay)] = " M src/pay/a.py\n?? .orche/feat/pay/HANDOFF.md\n"
        (row,) = self.rows()
        self.assertEqual((row["action"], row["reason"]),
                         ("skip", "work landed, quiet 10d; uncommitted work: src/pay/a.py"))
        self.orche("janitor", "enable")
        self.run_json()
        self.assertEqual(self.pay_status(), "active")

    def test_a_retired_leftover_listed_by_orca_is_removed(self):
        self.set_pay(status="retired")
        (row,) = self.rows()
        self.assertEqual((row["action"], row["target"]), ("remove", str(self.pay)))
        self.orche("janitor", "enable")
        (row,) = self.rows(self.run_json())
        self.assertEqual((row["result"], row.get("error")), ("removed", None))
        self.assertIn(["orca", "worktree", "rm", "--worktree", f"id:repo-acme::{self.pay}"],
                      [c[:5] for c in self.calls])
        rows = (self.proj / "docs/GRAVEYARD.md").read_text()
        self.assertIn(f"| worktree {self.pay} of retired unit feat-acme-pay | retired: ", rows)
        self.assertIn("branch feat/pay was tagged at retirement", rows)

    def test_a_retired_leftover_orca_does_not_list_stays(self):
        self.set_pay(status="retired")
        self.orca_paths.remove(self.pay)
        (row,) = self.rows()
        self.assertEqual((row["action"], row["reason"]), ("skip", "not listed by Orca"))


def support_activity(case, session, days_ago):
    path = units.activity_file(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    t = time.time() - days_ago * DAY
    os.utime(path, (t, t))
    return path


class RulesTest(support.TempHomeCase):
    def test_protected(self):
        for p in ("a/.env", "a/.env.local", "x/memory/m.md", "registry.json", "cards/x.md",
                  "docs/LEDGER.tsv", "docs/GRAVEYARD.md", ".orche/div/x/HANDOFF.md", "STATUS.md",
                  ".claude/agent-memory/x"):
            self.assertTrue(rules.is_protected(p), p)
        for p in ("a/.next", "dist/env.js", "src/status.ts"):
            self.assertFalse(rules.is_protected(p), p)
        self.assertFalse(rules.is_protected("/x/memory/proj/dist", root="/x/memory/proj"))

    def test_session_name_guard(self):
        for bad in ("", ".", "..", "../other", "a/b", ".hidden"):
            self.assertFalse(discover.valid_session_name(bad), bad)
        self.assertEqual(discover.session_files(self.home, "../x"), [])

    def test_pointer_and_purge_due(self):
        self.assertEqual(rules.pointer("path:scratch/x old"), ("path", "scratch/x"))
        self.assertEqual(rules.pointer("drop session:feat-a"), ("session", "feat-a"))
        self.assertIsNone(rules.pointer("trashed scratch/x"))
        today = dt.date(2026, 10, 16)
        self.assertTrue(trash.purge_due("2026-09-15", today, 30))
        self.assertFalse(trash.purge_due("2026-09-17", today, 30))
        self.assertFalse(trash.purge_due("junk", today, 30))


class DailyTest(JanitorCase):
    """The heartbeat's rule (janitor.due) reads the report's mtime; one pass at a time (`.pass.lock`)."""

    def test_due_reads_the_report_age(self):
        report = janitor.report_path()
        report.unlink()
        self.assertTrue(janitor.due())
        self.assertEqual(janitor.last_pass_text(), "never")
        report.write_text("x")
        self.assertFalse(janitor.due())
        old = time.time() - janitor.DAILY_S - 60
        os.utime(report, (old, old))
        self.assertTrue(janitor.due())
        self.assertEqual(janitor.last_pass_text(), "1d ago")

    def test_a_run_writes_the_report_and_frees_the_lock(self):
        janitor.report_path().unlink()
        self.run_json("--dry-run")
        self.assertFalse(janitor.due())
        self.assertFalse(janitor.lock_path().exists())

    def test_a_held_lock_refuses_and_a_stale_one_is_taken_over(self):
        janitor.lock_path().touch()
        self.assertTrue(janitor.running())
        code, _out, err = self.orche("janitor", "run", "--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("another janitor pass is running", err)
        old = time.time() - janitor.LOCK_STALE_S - 60
        os.utime(janitor.lock_path(), (old, old))
        self.assertFalse(janitor.running())
        self.run_json("--dry-run")
        self.assertFalse(janitor.lock_path().exists())


class ZstdTest(JanitorCase):
    def test_zstd_archive_roundtrip(self):
        real = self._orig_zstd()
        if not real:
            self.skipTest("zstd not installed")
        archive.zstd_bin = lambda: real
        orig = self.fake_runner

        def runner(argv, *, timeout, input, env=None):
            if argv[0] == real:
                return self._orig_runner(argv, timeout=timeout, input=input)
            return orig(argv, timeout=timeout, input=input)
        shell.RUNNER = runner
        self.orche("janitor", "enable")
        self.run_json()
        self.assertTrue(list((self.home / "archive/feat-acme-search").glob("*.tar.zst")))
        self.assertFalse(list((self.home / "archive/feat-acme-search").glob("*.tar")))
        self.assertEqual(self.orche("janitor", "restore", "feat-acme-search")[0], 0)
        self.assertTrue(self.t_cwd.exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
