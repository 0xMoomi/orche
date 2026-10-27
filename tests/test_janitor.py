"""Janitor: state-based archive/trash, GRAVEYARD rows, undo/restore, purge, discard, launchd schedule.

Synthetic world in a temp dir: an active unit, a retired unit with two transcripts, a dirty
worktree, a 10-day-old `.next`, a fresh `.next`, a GRAVEYARD `path:` row, and `.env*` / `memory/`
files that must never be listed. git/Orca/launchctl go through a fake `shell.RUNNER`.
"""
import datetime as dt
import io
import json
import os
import plistlib
import shutil
import tarfile
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import registry, shell
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
        self.calls, self.dirty = [], {}
        self._orig_runner, shell.RUNNER = shell.RUNNER, self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", self._orig_runner)
        self.claude = self.w / "claude-projects"
        (self.home / "config.json").write_text(json.dumps({"notify": False, "janitor": {
            "codex_tmp": str(self.w / "codex-tmp"), "scratch_root": str(self.w / "scratch"),
            "claude_projects": str(self.claude)}}))
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
        if argv[0] == "git" and argv[3] == "ls-files":
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
        self.touch("acme-search/.env.local", "SECRET=2")            # retired worktree: never listed
        # transcripts: retired unit (two files), active unit, claude memory
        self.t_named = self.transcript(self.wt["search"], "feat-acme-search.jsonl")
        self.t_cwd = self.transcript(self.wt["search"], "0f1e2d3c-uuid.jsonl")
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
            ("trash", str(self.old_next)),
            ("trash", "scratch/old-proto"),
            ("skip", str(self.wt["gtm"])),
            ("skip", str(self.wt["rnd"] / ".next")),
        })
        by_target = {c["target"]: c for c in data["candidates"]}
        self.assertEqual(by_target[str(self.wt["gtm"])]["reason"], "wip:2")
        self.assertTrue(by_target[str(self.wt["rnd"] / ".next")]["reason"].startswith("fresh"))
        archived = by_target["sessions:feat-acme-search (2 files)"]
        self.assertEqual(sorted(archived["files"]), sorted([str(self.t_named), str(self.t_cwd)]))
        blob = json.dumps(data)
        self.assertNotIn(".env", blob)
        self.assertNotIn("/memory", blob)
        for name in ("acme-build", "acme-ops"):
            self.assertNotIn(name, blob)
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
        self.assertEqual(self.orche("janitor", "report")[0], 2)
        self.run_json()
        code, out, _ = self.orche("janitor", "report")
        self.assertEqual(code, 0)
        self.assertIn("| trash | age:build-cache |", out)


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
        # trash
        self.assertFalse(self.old_next.exists())
        self.assertFalse(self.scratch_dir.exists())
        manifests = {m["path"]: m for m in self.trash_manifests()}
        self.assertEqual(set(manifests), {str(self.old_next), str(self.scratch_dir)})
        for m in manifests.values():
            self.assertEqual(m["status"], "trashed")
            self.assertTrue(Path(m["stored"]).exists())
        self.assertTrue(manifests[str(self.old_next)]["stored"].endswith("/.next"))
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
        for keep in ("acme-research/.env.local", "acme-research/memory/notes.md", "acme-gtm/.next/cache.js",
                     "acme-rnd/.next/fresh.js", "acme-build/.next/old.js", "acme-ops/.next/cache.js",
                     "acme/.env.local", "acme-search/.env.local"):
            self.assertTrue((self.w / keep).exists(), keep)
        # GRAVEYARD rows: project + global
        rows = (self.proj / "docs/GRAVEYARD.md").read_text()
        self.assertIn("| archived 2 session files of feat-acme-search | retired: unit retired |", rows)
        self.assertIn("orche janitor restore feat-acme-search | janitor |", rows)
        self.assertIn("| trashed scratch/old-proto | graveyard: superseded by v2 | orche janitor undo ", rows)
        self.assertIn(f"| trashed {self.old_next} | age:build-cache:", rows)
        glob_rows = (self.home / "graveyard.md").read_text()
        self.assertEqual(glob_rows.count("| acme: "), 3)
        # second pass: nothing left to do, rows settled
        again = self.run_json()
        self.assertEqual([c for c in again["candidates"] if c["action"] != "skip"], [])
        state = json.loads((self.home / "janitor/state.json").read_text())
        self.assertEqual(len(state["processed"]), 2)  # path row + refused protected row

    def test_undo_restores_trashed_path(self):
        self.enable_and_run()
        m = next(m for m in self.trash_manifests() if m["path"] == str(self.old_next))
        code, out, err = self.orche("janitor", "undo", m["id"])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.old_next / "cache.js").exists())
        self.assertEqual(self.orche("janitor", "undo", m["id"])[0], 3)          # already restored
        self.assertEqual(self.orche("janitor", "undo", "nope")[0], 2)
        other = next(m for m in self.trash_manifests() if m["path"] == str(self.scratch_dir))
        self.scratch_dir.mkdir(parents=True)
        self.assertEqual(self.orche("janitor", "undo", other["id"])[0], 3)       # original exists

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
            shutil.rmtree(wt)
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


class ScheduleTest(JanitorCase):
    def test_install_dry_run_prints_argv_and_path(self):
        code, out, err = self.orche("--dry-run", "janitor", "install-schedule")
        self.assertEqual(code, 0, err)
        plist = self.w / "userhome/Library/LaunchAgents/com.orche.janitor.plist"
        self.assertIn(f"would write {plist}", out)
        self.assertIn(f"would run: launchctl bootout gui/{os.getuid()}/com.orche.janitor", out)
        self.assertIn(f"would run: launchctl bootstrap gui/{os.getuid()} {plist}", out)
        self.assertFalse(plist.exists())
        self.assertFalse(any(c[0] == "launchctl" for c in self.calls))

    def test_install_and_uninstall(self):
        code, out, err = self.orche("janitor", "install-schedule")
        self.assertEqual(code, 0, err)
        plist = self.w / "userhome/Library/LaunchAgents/com.orche.janitor.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["ProgramArguments"][1:], ["janitor", "run"])
        self.assertTrue(data["ProgramArguments"][0].endswith("/bin/orche"))
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 4, "Minute": 0})
        self.assertEqual(data["StandardOutPath"], f"{self.home}/logs/janitor.log")
        self.assertEqual(data["EnvironmentVariables"]["ORCHE_HOME"], str(self.home))
        self.assertIn(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], self.calls)
        self.assertEqual(self.orche("janitor", "uninstall-schedule")[0], 0)
        self.assertIn(["launchctl", "bootout", f"gui/{os.getuid()}/com.orche.janitor"], self.calls)
        self.assertFalse(plist.exists())
        self.assertEqual(self.orche("janitor", "uninstall-schedule")[0], 2)

    def test_template_has_no_user_paths(self):
        text = (Path(support.ROOT) / "templates/launchd/com.orche.janitor.plist").read_text()
        self.assertNotIn("/" + "Users/", text)  # split so the repo hygiene grep stays clean
        plistlib.loads(text.replace("{{", "").replace("}}", "").encode())


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
