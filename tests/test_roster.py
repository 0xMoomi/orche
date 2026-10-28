"""Roster tables: identity, place, members, recipient and TO resolution."""
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from orche import roster
from orche.roster import ENV, Roster


def _div(root, name, session, status="active", **extra):
    return {"status": status, "worktree": f"repo-acme::{root / name}", "branch": f"div/{name}",
            "session": session, "model": "claude-opus-5-5", **extra}


def _feat(root, name, worktree, session, scope, status="active"):
    return {"name": name, "worktree": f"repo-acme::{root / worktree}", "branch": f"feat/{name}",
            "scope": scope, "session": session, "status": status}


def build_registry(root: Path) -> dict:
    """The §7.1 fixture under `root` (a realpath'd directory)."""
    for d in ("acme/src", "acme-build/nested/src", "acme-checkout/src", "acme-old", "acme-rnd", "acme-design",
              "beta", "beta-build", "build-proj", "elsewhere", "clone/src", "acme/vendor/x/src"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "clone/.orche").mkdir(parents=True, exist_ok=True)
    (root / "clone/.orche/project").write_text("acme\n")
    (root / "acme/vendor/x/.orche").mkdir(parents=True, exist_ok=True)
    (root / "acme/vendor/x/.orche/project").write_text("zzz\n")
    os.symlink(root, root / "link")
    acme_build = _div(root, "acme-build", "div-acme-build", features=[
        _feat(root, "checkout", "acme-checkout", "feat-acme-checkout", ["src/checkout/**"]),
        _feat(root, "nested", "acme-build/nested", "feat-acme-nested", ["src/**"]),
        _feat(root, "old", "acme-old", "feat-acme-old", ["src/old/**"], status="retired"),
    ])
    acme_build["branch"] = "div/build"
    return {"version": 1, "frozen": False, "events": [], "projects": {
        "acme": {"repo_id": "repo-acme", "path": str(root / "acme"), "aliases": ["shop"],
                 "main_session": "main-acme", "liaison_session": "liaison-acme",
                 "custom_divisions": {"design": {"label": "Design", "charter": "Owns the visual system.",
                                                 "model": "claude-opus-5-5", "effort": "high"}},
                 "divisions": {"rnd": _div(root, "acme-rnd", "div-acme-rnd", status="dormant"),
                               "design": _div(root, "acme-design", "div-acme-design"),
                               "build": acme_build}},
        "beta": {"repo_id": "repo-beta", "path": str(root / "beta"), "aliases": ["bee"],
                 "main_session": "main-beta",
                 "divisions": {"build": _div(root, "beta-build", "div-beta-build")}},
        "build": {"repo_id": "repo-build", "path": str(root / "build-proj"), "aliases": [],
                  "main_session": "main-build", "divisions": {}},
    }}


class RosterCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self._tmp.name))
        self.reg = build_registry(self.root)
        self.r = Roster(self.reg)

    def tearDown(self):
        self._tmp.cleanup()

    def edited(self, edit):
        reg = copy.deepcopy(self.reg)
        edit(reg)
        return Roster(reg)


class IdentifyTest(RosterCase):
    def test_identify_table(self):
        def blank_build(reg):
            reg["projects"]["acme"]["divisions"]["build"]["session"] = ""

        def retired_twin(reg):
            feats = reg["projects"]["acme"]["divisions"]["build"]["features"]
            feats.insert(0, _feat(self.root, "checkout", "acme-checkout", "feat-acme-checkout", ["old/**"],
                                  status="retired"))

        def live_twin_first(reg):
            feats = reg["projects"]["acme"]["divisions"]["build"]["features"]
            feats.insert(0, _feat(self.root, "early", "acme-checkout", "feat-acme-early", ["src/**"]))

        def retired_twin_elsewhere(reg):
            feats = reg["projects"]["acme"]["divisions"]["build"]["features"]
            feats.insert(0, _feat(self.root, "checkout", "elsewhere", "feat-acme-checkout", ["old/**"],
                                  status="retired"))

        # (row, cwd, env, edit, session, member session, project, unit session)
        rows = [
            (1, "acme", None, None, None, None, "acme", None),
            (2, "acme/src", None, None, None, None, "acme", None),
            (3, "acme-build", None, None, "div-acme-build", "div-acme-build", "acme", "div-acme-build"),
            (4, "acme-checkout/src", None, None, "feat-acme-checkout", "feat-acme-checkout", "acme",
             "feat-acme-checkout"),
            (5, "elsewhere", None, None, None, None, None, None),
            (6, "elsewhere", "main-acme", None, "main-acme", "main-acme", None, None),
            (7, "acme", "liaison-acme", None, "liaison-acme", None, "acme", None),   # an old registry key: no member
            (8, "acme-build", "main-acme", None, "main-acme", "main-acme", "acme", "div-acme-build"),
            (9, "acme-build", "ghost-acme", None, "ghost-acme", None, "acme", "div-acme-build"),
            (10, "acme-build", "", None, "div-acme-build", "div-acme-build", "acme", "div-acme-build"),
            (11, "acme-build", None, blank_build, None, None, "acme", ""),
            (12, "acme-checkout", None, retired_twin, "feat-acme-checkout", "feat-acme-checkout", "acme",
             "feat-acme-checkout"),
            (13, "elsewhere", "feat-acme-old", None, "feat-acme-old", "feat-acme-old", None, None),
            (14, "link/acme-build", None, None, "div-acme-build", "div-acme-build", "acme", "div-acme-build"),
            (15, "clone/src", None, None, None, None, "acme", None),
            (16, "acme/vendor/x/src", None, None, None, None, None, None),
            (17, "acme-build/nested/src", None, None, "feat-acme-nested", "feat-acme-nested", "acme",
             "feat-acme-nested"),
            (18, "acme-checkout/src", None, live_twin_first, "feat-acme-early", "feat-acme-early", "acme",
             "feat-acme-early"),                       # two live records, one worktree: registry order wins
            (19, "elsewhere", None, retired_twin_elsewhere, "feat-acme-checkout", "feat-acme-checkout", "acme",
             "feat-acme-checkout"),                    # no env at a retired twin's worktree: that record
        ]
        for row, cwd, env, edit, session, member, project, unit in rows:
            with self.subTest(row=row, cwd=cwd, env=env):
                r = self.edited(edit) if edit else self.r
                who = r.identify(self.root / cwd, {} if env is None else {ENV: env})
                self.assertEqual(who.session, session)
                self.assertEqual(who.member.session if who.member else None, member)
                self.assertEqual(who.place.project, project)
                self.assertEqual(who.place.unit.session if who.place.unit else None, unit)
                if row == 11:
                    self.assertEqual((who.place.unit.kind, who.place.unit.name), ("div", "build"))
                if row == 12:
                    self.assertEqual(who.member.status, "active")
                    self.assertEqual(who.member.entry["scope"], ["src/checkout/**"])
                    self.assertIs(who.place.unit.entry, who.member.entry)
                if row == 13:
                    self.assertEqual(who.member.status, "retired")
                if row == 19:
                    self.assertEqual(who.member.status, "retired")
                    self.assertIs(who.member.entry, who.place.unit.entry)
                    live = r.member(session)
                    self.assertEqual(live.status, "active")
                    self.assertEqual(live, who.member)              # == ignores entry: twins compare equal
                    self.assertIsNot(live.entry, who.member.entry)

    def test_place_of_nothing(self):
        for cwd in (None, ""):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.r.place(cwd), roster.Place(None, None))

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_unreadable_marker_gives_no_project(self):
        marker = self.root / "acme/locked/.orche/project"
        marker.parent.mkdir(parents=True)
        marker.write_text("acme\n")
        marker.chmod(0)
        try:
            self.assertIsNone(self.r.place(self.root / "acme/locked").project)   # stops the walk: not acme
        finally:
            marker.chmod(0o644)

    def _counting(self):
        """Patch realpath and Path.is_file (the project walk) to count calls; returns the counter dict."""
        calls = {"realpath": 0, "is_file": 0}
        real_realpath, real_is_file = os.path.realpath, Path.is_file

        def realpath(path, *a, **k):
            calls["realpath"] += 1
            return real_realpath(path, *a, **k)

        def is_file(path):
            calls["is_file"] += 1
            return real_is_file(path)

        for target, name, fake in ((roster.os.path, "realpath", realpath), (Path, "is_file", is_file)):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def test_identify_with_env_does_no_filesystem_work_until_place_is_read(self):
        calls = self._counting()
        who = self.r.identify(self.root / "clone/src", {ENV: "main-acme"})
        self.assertEqual((who.session, who.member.session), ("main-acme", "main-acme"))
        self.assertEqual(calls, {"realpath": 0, "is_file": 0})
        self.assertNotIn("_index", vars(self.r))
        self.assertEqual(who.place.project, "acme")                  # the walk runs on first read, then caches
        self.assertGreater(calls["is_file"], 0)
        seen = dict(calls)
        self.assertEqual(who.place.project, "acme")
        self.assertEqual(calls, seen)

    def test_unit_lookup_does_not_walk_for_the_project(self):
        calls = self._counting()
        place = self.r.place(self.root / "clone/src")
        self.assertIsNone(place.unit)
        self.assertEqual(calls["is_file"], 0)
        self.assertEqual(self.r.place(self.root / "acme-build/nested/src").unit.session, "feat-acme-nested")
        self.assertEqual(calls["is_file"], 0)
        self.assertEqual(place.project, "acme")
        self.assertGreater(calls["is_file"], 0)

    def test_place_and_identity_compare_by_value_and_are_read_only(self):
        place = self.r.place(self.root / "acme-build")
        self.assertEqual(place, roster.Place("acme", self.r.member("div-acme-build")))
        self.assertEqual(self.r.identify(self.root / "acme", {ENV: "main-acme"}),
                         roster.Identity("main-acme", self.r.member("main-acme"), roster.Place("acme", None)))
        with self.assertRaises(AttributeError):
            place.unit = None

    def test_place_index_is_fixed_after_first_place_call(self):
        self.assertEqual(self.r.place(self.root / "elsewhere").unit, None)
        self.reg["projects"]["acme"]["divisions"]["rnd"]["worktree"] = f"repo-acme::{self.root / 'elsewhere'}"
        self.assertIsNone(self.r.place(self.root / "elsewhere").unit)
        self.assertEqual(Roster(self.reg).place(self.root / "elsewhere").unit.session, "div-acme-rnd")


class MemberTest(RosterCase):
    def test_members(self):
        root = self.root
        build = self.r.member("div-acme-build")
        self.assertEqual((build.kind, build.role, build.division, build.event_unit, build.name),
                         ("div", "division", "build", "build", "build"))
        self.assertEqual(build.memory, root / "acme-build/.orche/div/build")
        self.assertTrue(build.owns_worktree)
        self.assertEqual(build.worktree, root / "acme-build")
        self.assertEqual(build.home, root / "acme")
        checkout = self.r.member("feat-acme-checkout")
        self.assertEqual((checkout.kind, checkout.role, checkout.division, checkout.event_unit),
                         ("feat", "feature", "build", "build/checkout"))
        self.assertEqual(checkout.memory, root / "acme-checkout/.orche/feat/checkout")
        self.assertEqual((checkout.branch, checkout.status), ("feat/checkout", "active"))
        main = self.r.member("main-acme")
        self.assertEqual((main.kind, main.role, main.name, main.division), ("main", "main", "", None))
        self.assertEqual(main.memory, root / "acme/.orche")
        self.assertIsNone(main.event_unit)
        self.assertIsNone(main.worktree)
        self.assertFalse(main.owns_worktree)
        self.assertEqual((main.branch, main.status), ("", ""))
        self.assertIsNone(self.r.member("liaison-acme"))   # an old liaison_session key indexes nothing

    def test_unknown_and_twin(self):
        for name in ("nope", "", None):
            with self.subTest(name=name):
                self.assertIsNone(self.r.member(name))
        feats = self.reg["projects"]["acme"]["divisions"]["build"]["features"]
        feats.insert(0, _feat(self.root, "checkout", "acme-old", "feat-acme-checkout", ["old/**"], status="retired"))
        live = Roster(self.reg).member("feat-acme-checkout")
        self.assertEqual(live.status, "active")
        self.assertEqual(live.worktree, self.root / "acme-checkout")

    def test_main(self):
        self.assertEqual(self.r.main("acme").session, "main-acme")
        self.assertIsNone(self.r.main("nope"))
        self.assertIsNone(self.r.main(None))
        self.reg["projects"]["beta"]["main_session"] = ""
        self.assertIsNone(Roster(self.reg).main("beta"))

    def test_sessions(self):
        self.reg["projects"]["acme"]["divisions"]["rnd"]["session"] = ""
        self.assertEqual(Roster(self.reg).sessions(), [
            "div-acme-build", "div-acme-design", "div-beta-build", "feat-acme-checkout", "feat-acme-nested",
            "feat-acme-old", "main-acme", "main-beta", "main-build"])

    def test_attribution(self):
        self.assertEqual(roster.actor({}), "user")
        self.assertEqual(roster.actor({ENV: "x"}), "x")
        self.assertEqual(roster.actor({ENV: ""}), "user")
        self.assertIsNone(roster.env_session({ENV: ""}))
        self.assertIsNone(roster.env_session({}))
        self.assertEqual(roster.env_session({ENV: "main-acme"}), "main-acme")


class RecipientTest(RosterCase):
    def test_recipient_table(self):
        def blank_division(reg):
            reg["projects"]["acme"]["divisions"]["build"]["session"] = ""

        def no_main(reg):
            reg["projects"]["acme"]["main_session"] = ""

        rows = [
            ("main-acme", None, "main-acme"),
            ("liaison-acme", None, None),
            ("div-acme-build", None, "main-acme"),
            ("feat-acme-checkout", None, "div-acme-build"),
            ("feat-acme-checkout", blank_division, "main-acme"),
            ("div-acme-build", no_main, None),
            ("ghost", None, None),
            (None, None, None),
        ]
        for session, edit, want in rows:
            with self.subTest(session=session, edit=edit and edit.__name__):
                r = self.edited(edit) if edit else self.r
                self.assertEqual(r.recipient(session), want)

    def test_recipient_is_the_sender(self):
        def retire_design(reg):
            reg["projects"]["acme"]["divisions"]["design"]["status"] = "retired"

        rows = [
            ("feat-acme-checkout", "main-acme", None, "main-acme"),
            ("feat-acme-checkout", "div-acme-build", None, "div-acme-build"),
            ("feat-acme-checkout", "div-acme-design", None, "div-acme-design"),
            ("feat-acme-checkout", "div-acme-design", retire_design, "div-acme-build"),   # retired
            ("feat-acme-checkout", "ghost", None, "div-acme-build"),                      # unknown
            ("feat-acme-checkout", "main-beta", None, "div-acme-build"),                  # other project
            ("feat-acme-checkout", "div-beta-build", None, "div-acme-build"),             # other project
            ("feat-acme-checkout", "feat-acme-nested", None, "div-acme-build"),           # not Main/division
            ("feat-acme-checkout", "", None, "div-acme-build"),
            ("div-acme-rnd", "main-acme", None, "main-acme"),
            ("div-acme-rnd", "div-acme-build", None, "div-acme-build"),
            ("div-acme-build", "div-acme-build", None, "main-acme"),                      # sender == session
            ("main-acme", "main-acme", None, "main-acme"),
            ("ghost", "main-acme", None, None),
        ]
        for session, sender, edit, want in rows:
            with self.subTest(session=session, sender=sender, edit=edit and edit.__name__):
                r = self.edited(edit) if edit else self.r
                self.assertEqual(r.recipient(session, sender), want)


class ResolveToTest(RosterCase):
    def test_resolve_to_table(self):
        def no_beta_main(reg):
            reg["projects"]["beta"]["main_session"] = ""

        def blank_build(reg):
            reg["projects"]["acme"]["divisions"]["build"]["session"] = ""

        # (text, home, want[, edit])
        rows = [
            ("div-acme-rnd", "acme", "div-acme-rnd"),
            ("build", "acme", "div-acme-build"),          # L24: the division beats the project keyed build
            ("design", "acme", "div-acme-design"),
            ("ops", "acme", None),
            ("shop", "acme", "main-acme"),
            ("SHOP", "acme", "main-acme"),
            ("beta", "acme", "main-beta"),
            ("bee", "acme", "main-beta"),
            ("main-beta", "acme", "main-beta"),            # L25
            ("div-beta-build", "acme", "div-beta-build"),
            ("main-build", "acme", "main-build"),
            (" build ", "acme", "div-acme-build"),
            ("unknown", "acme", None),
            ("", "acme", None),
            (None, "acme", None),
            ("build", None, "main-build"),
            ("beta", "acme", None, no_beta_main),           # L26: a project whose Main is "" resolves to None
            ("build", "acme", "main-build", blank_build),   # a home division with "" falls to the project key
        ]
        for text, home, want, *edit in rows:
            with self.subTest(text=text, home=home, edit=edit and edit[0].__name__):
                r = self.edited(edit[0]) if edit else self.r
                got = r.resolve_to(text, home)
                self.assertEqual(got.session if got else None, want)
        self.assertEqual(self.r.resolve_to("div-beta-build", "acme").project, "beta")
        self.assertEqual(self.r.resolve_to("build", None).project, "build")


class HeadTest(RosterCase):
    def test_a_workers_head_is_its_division_and_build_is_the_fallback(self):
        def hero(reg):
            reg["projects"]["acme"]["divisions"]["design"].setdefault("features", []).append(
                _feat(self.root, "hero", "acme-design", "feat-acme-hero", ["src/**"]))
        r = self.edited(hero)
        name = lambda m: m.session if m else None
        self.assertEqual(name(r.head(r.member("div-acme-design"))), "div-acme-design")
        self.assertEqual(name(r.head(r.member("feat-acme-hero"))), "div-acme-design")
        self.assertEqual(name(r.head(r.member("feat-acme-checkout"))), "div-acme-build")
        self.assertEqual(name(r.head(r.member("main-acme"))), "div-acme-build")
        self.assertEqual(name(r.head(None, "acme")), "div-acme-build")
        self.assertIsNone(r.head(None, "build"))

        def headless(reg):
            hero(reg)
            reg["projects"]["acme"]["divisions"]["design"]["session"] = ""
        r = self.edited(headless)
        self.assertEqual(name(r.head(r.member("feat-acme-hero"))), "div-acme-build")


class ImportClosureTest(unittest.TestCase):
    def test_imports_only_the_allowed_modules(self):
        code = "import sys, orche.roster; print(sorted(m for m in sys.modules if m.startswith('orche')))"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONPATH": str(support.ROOT / "lib")}).stdout.strip()
        self.assertEqual(out, str(['orche', 'orche.paths', 'orche.registry', 'orche.roster', 'orche.units',
                                   'orche.util']))


if __name__ == "__main__":
    unittest.main()
