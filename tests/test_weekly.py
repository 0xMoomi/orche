"""The janitor's weekly pass (lib/orche/janitor/weekly.py): once per 7 days, the audit's new mistake
classes into STANDING.md with one pointer to Main, and one verify-skill drift unit to Build per project."""
import datetime as dt
import re
from unittest import mock

import support  # noqa: F401 - puts lib/ on sys.path

from orche import janitor, procs, registry
from orche.janitor import weekly
from orche.queue import store
from test_runner import REAL_POPEN, SPAWN, RunnerCase

T = dt.datetime(2026, 10, 27, 4, 0, tzinfo=dt.timezone.utc)
POINTER = 'audit: 1 new mistake classes in STANDING.md Enforcement; say "enforce <class>" to dispatch it'


class WeeklyTest(RunnerCase):
    def setUp(self):
        super().setUp()
        self.spawned = []

        def fake_popen(argv, **kw):
            if argv[:3] != SPAWN:
                return REAL_POPEN(argv, **kw)
            self.spawned.append(argv)
            return mock.Mock(pid=4242)
        patcher = mock.patch.object(procs.subprocess, "Popen", fake_popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pass(self, state, now=T, force=False):
        return weekly.run(registry.load(), state, now=now, force=force)

    def skill(self, app="shop"):
        path = self.repo / ".claude" / "skills" / f"verify-{app}" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# verify-{app}\n")

    def audit_pointers(self):
        return [p for p in self.pointers("main-acme") if p["unit"] == "hr-audit"]

    def test_runs_once_per_seven_days(self):
        state = {}
        self.assertEqual(self.run_pass(state), ["weekly pass (2026-10-27):", "  nothing to do"])
        self.assertEqual(state["weekly"]["last"], "2026-10-27T04:00:00+00:00")
        self.assertEqual(self.run_pass(state, T + dt.timedelta(days=6, hours=23)), [])
        self.assertNotEqual(self.run_pass(state, T + dt.timedelta(days=6, hours=23), force=True), [])
        self.assertEqual(self.run_pass(state, T + dt.timedelta(days=13)), [])
        self.assertNotEqual(self.run_pass(state, T + dt.timedelta(days=14)), [])

    def test_new_classes_go_to_standing_and_one_pointer_to_main(self):
        now = dt.datetime.now(dt.timezone.utc)
        for pr, unit in ((4, "acme-a-20261020-1"), (5, "acme-b-20261021-1")):
            store.log("acme", "returned", {"id": f"e{pr}", "pr": pr, "unit": unit}, reason="red", step="npm test")
        lines = self.run_pass({}, now)
        self.assertIn("  acme: 1 new mistake classes in STANDING.md Enforcement; Main told", lines)
        (ptr,) = self.audit_pointers()
        self.assertEqual((ptr["summary"], ptr["status"], ptr["from"]), (POINTER, "info", "janitor"))
        text = (self.repo / ".orche" / "STANDING.md").read_text()
        self.assertIn("| queue return red npm test | 2 | test | proposed |", text)
        self.run_pass({}, now)                     # nothing new: no second pointer
        self.assertEqual(len(self.audit_pointers()), 1)

    def test_drift_unit_brief_and_one_open_unit(self):
        self.skill()
        state = {}
        lines = self.run_pass(state)
        (unit,) = [p.name for p in self.units_dir().iterdir()]
        self.assertIn(f"  acme: verify-shop drift unit {unit} filed to div-acme-build", lines)
        self.assertEqual(state["weekly"]["drift"]["acme"], {"unit": unit, "app": "shop"})
        text = (self.units_dir() / unit / "BRIEF.md").read_text()
        brief_part = re.sub(r"(?m)^([A-Z]+) +", r"\1 ", text[text.index("## Brief"):])
        for line in ("GOAL Re-verify the verify-shop feature map against the current app",
                     "SCOPE .claude/skills/verify-shop/**", "TIMEBOX 60 min", "TIER mechanical",
                     "VERIFY The skill's own Doctor and Drive steps (.claude/skills/verify-shop/SKILL.md)",
                     "ACCEPT One live pass of the skill; map corrections only; product regressions reported, "
                     "not papered over"):
            self.assertIn(line, brief_part)
        self.assertLessEqual(len(brief_part.strip().splitlines()), 20)
        self.assertEqual(len(self.spawned), 1)
        lines = self.run_pass(state, force=True)   # still open (no report): none filed
        self.assertIn(f"  acme: drift unit {unit} is still open; none filed", lines)
        self.assertEqual(len(list(self.units_dir().iterdir())), 1)
        with mock.patch.object(weekly, "is_open", lambda reg, key, u: False):
            self.run_pass(state, force=True)
        self.assertEqual(len(list(self.units_dir().iterdir())), 2)

    def test_no_verify_skill_no_unit(self):
        self.run_pass({})
        self.assertFalse(self.units_dir().exists() and any(self.units_dir().iterdir()))
        self.assertEqual(self.spawned, [])

    def test_janitor_run_weekly_flag_and_state(self):
        code, out, err = self.orche("janitor", "run", "--weekly")
        self.assertEqual(code, 0, err)
        self.assertIn("weekly pass (", out)
        self.assertIsNotNone(weekly.last(janitor.load_state()))
        code, out, _ = self.orche("janitor", "run")
        self.assertNotIn("weekly pass (", out)    # ran today: not due
