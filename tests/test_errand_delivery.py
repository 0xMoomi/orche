"""Staff errands, delivery side (c123 WP-4): waking a project's Staff (`orche nudge staff-<p>`,
lib/orche/deliver.py) when its pane is open, closed, dead or asleep; its unread mark; and the heartbeat's
errand sweep (lib/orche/reconcile.py, errand.sweep).

In-process against the fake Orca (ORCHE_FAKE_ORCA=1) and the stateful DeadPane stand-in; spawns are mocked.
"""
import unittest
from unittest import mock

import support  # noqa: F401 - puts lib/ on sys.path
from orche import deliver, errand, inbox, orca, reconcile, registry, roster, staff
from test_deliver import DeadPane, NudgeCase
from test_reconcile import ReconcileCase, T0

UNIT = "acme-look-up-20261112-1"


class StaffCase(NudgeCase):
    """The hooks' registry (project acme: main-acme, div-acme-build), its Staff `staff-acme` (not a member)."""

    def setUp(self):
        super().setUp()
        self.canned(worktree_list={"worktrees": [
            {"repoId": "repo-acme", "path": f"{self.root}/acme", "displayName": "acme"},
            {"repoId": "repo-acme", "path": f"{self.root}/acme-staff", "displayName": "staff"}]})

    def result(self, status="done", unit=UNIT, **kw):
        """An errand result the runner filed to Staff (`via: runner`, never quiet)."""
        return inbox.write(inbox.make(unit, "staff-acme", "staff-acme", status, via="runner",
                                      summary="Two PRs touch the payment page.", **kw))

    def asleep(self):
        reg = registry.load()
        reg["projects"]["acme"]["staff"] = {"status": "asleep"}
        registry.save(reg)

    def main_items(self):
        return [p for _x, p in inbox.pending("main-acme")]

    def closed(self):
        """A DeadPane whose Staff pane is not listed at all (closed); Main and Build are."""
        fake = DeadPane("staff-acme", "t-staff", [("✳ main-acme", "t-main"), ("div-acme-build", "t-build")])
        del fake.terms["staff-acme"]
        return fake


class OwnerPaneTest(StaffCase):
    def test_main_and_staff_are_owner_panes(self):
        r = roster.Roster(registry.load())
        self.assertTrue(deliver._owner_pane(r, "main-acme"))
        self.assertTrue(deliver._owner_pane(r, "staff-acme"))
        self.assertFalse(deliver._owner_pane(r, "div-acme-build"))
        self.assertFalse(deliver._owner_pane(r, "staff-nowhere"))       # no registered project's Staff

    def test_staff_has_no_quiet_window(self):
        self.result()
        with mock.patch.object(deliver.time, "sleep", side_effect=AssertionError("waited")):
            self.assertEqual(deliver._quiet("staff-acme", {"quiet_s": 90, "quiet_max_s": 240}), 0)

    def test_an_open_staff_pane_is_typed_into_with_the_owners_rules(self):
        self.result()
        fake = DeadPane("staff-acme", "t-staff", [("✳ main-acme", "t-main")], connected=True)
        with mock.patch.object(orca, "terminal", fake), \
                mock.patch.object(deliver, "_hold", return_value=0) as hold:
            self.assertEqual(deliver.nudge("staff-acme"), (0, "nudged"))
        hold.assert_called()                                              # a person types there
        self.assertEqual([c[2] for c in fake.calls if c[0] == "send"], ["t-staff"])
        self.assertNotIn("create", fake.subs())
        self.assertIn("nudge session=staff-acme state=nudged pending=1", self.events())


class ClosedStaffTest(StaffCase):
    def test_a_closed_staff_is_opened_without_a_switch_and_woken_once(self):
        self.result()
        fake = self.closed()
        with mock.patch.object(orca, "terminal", fake):
            self.assertEqual(deliver.nudge("staff-acme"), (0, "nudged"))
        creates = [c for c in fake.calls if c[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertIn("staff-acme", creates[0])
        self.assertIn(f"id:repo-acme::{self.root}/acme-staff", creates[0])   # its own workspace
        self.assertNotIn("switch", fake.subs())
        self.assertEqual([c[2] for c in fake.calls if c[0] == "send"], ["staff-acme-new"])
        self.assertIn("wake session=staff-acme via=reopen state=nudged", self.events())
        self.assertEqual(self.main_items(), [])                          # nothing went to Main
        self.assertEqual(len(inbox.pending("staff-acme")), 1)            # Staff's prompt hook drains it

    def test_an_asleep_staff_is_never_opened_its_items_go_to_main_quietly(self):
        self.result()
        self.asleep()
        fake = self.closed()
        with mock.patch.object(orca, "terminal", fake):
            self.assertEqual(deliver.nudge("staff-acme"), (0, "forwarded"))
        self.assertNotIn("create", fake.subs())
        self.assertNotIn("send", fake.subs())
        [ptr] = self.main_items()
        self.assertEqual((ptr["unit"], ptr["from"], ptr["status"], ptr["via"], ptr["forwarded"], ptr.get("quiet")),
                         (UNIT, "staff-acme", "info", "staff", "asleep", True))
        self.assertTrue(ptr["summary"].startswith(errand.FORWARD_PREFIX))
        self.assertEqual(inbox.pending("staff-acme"), [])                # drained from Staff
        self.assertIn("errand forwarded unit=" + UNIT + " project=acme reason=asleep", self.events())
        self.assertIn("wake session=staff-acme via=reopen state=forwarded reason=asleep", self.events())

    def test_a_staff_that_cannot_be_opened_hands_its_items_to_main_waking_it(self):
        self.result()
        fake = self.closed()
        with mock.patch.object(orca, "terminal", fake), \
                mock.patch.object(staff, "open_pane", side_effect=orca.OrcaError("orca down", "orca_error")):
            self.assertEqual(deliver.nudge("staff-acme"), (0, "forwarded"))
        [ptr] = self.main_items()
        self.assertEqual((ptr["status"], ptr["via"], ptr["forwarded"]), ("info", "staff", "undelivered"))
        self.assertFalse(ptr.get("quiet"))                               # it wakes Main
        self.assertEqual(inbox.pending("staff-acme"), [])
        self.assertIn("deliver: reopen staff-acme: OrcaError", self.hooks_log())
        self.assertIn("wake session=staff-acme via=reopen state=forwarded reason=undelivered", self.events())

    def test_a_reopened_pane_that_never_shows_up_is_forwarded_too(self):
        self.result()
        fake = self.closed()
        real = fake.__call__

        def no_create(sub, *args, **kw):
            if sub == "create":
                fake.calls.append((sub, *args))
                return {}
            return real(sub, *args, **kw)
        with mock.patch.object(orca, "terminal", no_create):
            self.assertEqual(deliver.nudge("staff-acme"), (0, "forwarded"))
        self.assertEqual(fake.subs().count("create"), 1)                 # opened once, never retried
        self.assertEqual([p["forwarded"] for p in self.main_items()], ["undelivered"])

    def test_mains_own_reply_goes_back_to_main_never_dropped(self):
        inbox.write(inbox.make(UNIT, "main-acme", "staff-acme", "done", via="main", summary="Merged.",
                               report_path="/x/REPLY.md", lineage=UNIT))
        self.asleep()
        with mock.patch.object(orca, "terminal", self.closed()):
            self.assertEqual(deliver.nudge("staff-acme"), (0, "forwarded"))
        [ptr] = self.main_items()
        self.assertEqual((ptr["unit"], ptr["from"], ptr["status"], ptr["via"], ptr["forwarded"], ptr["report_path"],
                          ptr.get("quiet")),
                         (UNIT, "staff-acme", "info", "staff", "undelivered", "", True))  # asleep: quiet
        self.assertTrue(ptr["summary"].startswith(f"Your reply about {UNIT} did not reach the owner"), ptr["summary"])
        self.assertIn("Your reply: /x/REPLY.md", ptr["summary"])
        self.assertEqual(inbox.pending("staff-acme"), [])

    def test_a_division_without_a_pane_is_unchanged(self):
        inbox.write(inbox.make("acme-x-20261112-1", "main-acme", "div-acme-build", "question", via="runner"))
        fake = self.closed()
        del fake.terms["div-acme-build"]
        with mock.patch.object(orca, "terminal", fake):
            self.assertEqual(deliver.nudge("div-acme-build"), (1, "no-pane"))
        self.assertNotIn("create", fake.subs())
        self.assertNotIn("via=reopen", self.events())


class DeadStaffTest(StaffCase):
    def test_a_dead_staff_pane_is_closed_and_reopened_through_staff_open_pane(self):
        self.result()
        fake = DeadPane("staff-acme", "t-staff", [("✳ main-acme", "t-main")])
        with mock.patch.object(orca, "terminal", fake):
            self.assertEqual(deliver.nudge("staff-acme"), (0, "nudged"))
        self.assertIn(("close", "--terminal", "t-staff"), fake.calls)
        self.assertEqual(fake.subs().count("create"), 1)
        self.assertNotIn("switch", fake.subs())
        self.assertEqual([c[2] for c in fake.calls if c[0] == "send"], ["staff-acme-new"])
        self.assertIn("reopen session=staff-acme state=reopened", self.events())

    def test_an_asleep_staffs_dead_pane_is_not_reopened(self):
        self.asleep()
        fake = DeadPane("staff-acme", "t-staff")
        with mock.patch.object(orca, "terminal", fake):
            self.assertIsNone(deliver.reopen_pane(registry.load(), "staff-acme", "t-staff"))
        self.assertEqual(fake.calls, [])

    def test_an_unknown_staff_is_not_reopened(self):
        fake = DeadPane("staff-nowhere", "t-x")
        with mock.patch.object(orca, "terminal", fake):
            self.assertIsNone(deliver.reopen_pane(registry.load(), "staff-nowhere", "t-x"))
        self.assertEqual(fake.calls, [])


class StaffUnreadTest(StaffCase):
    def test_marks_the_staff_workspace(self):
        self.assertEqual(deliver.nudge("staff-acme", unread=True), (0, "marked"))
        self.assertEqual(self.orca_calls(), [
            ["orca", "worktree", "list", "--json"],
            ["orca", "worktree", "set", "--worktree", f"id:repo-acme::{self.root}/acme-staff", "--unread",
             "--json"]])
        self.assertIn("unread session=staff-acme state=marked", self.events())

    def test_no_staff_workspace_listed_is_mark_failed(self):
        self.canned(worktree_list={"worktrees": []})
        self.assertEqual(deliver.nudge("staff-acme", unread=True), (1, "mark-failed"))
        self.assertNotIn("set", [argv[2] for argv in self.orca_calls()])
        self.assertIn("unread session=staff-acme state=mark-failed", self.events())

    def test_a_division_is_still_not_marked(self):
        self.assertEqual(deliver.nudge("div-acme-build", unread=True), (1, "not-main"))
        self.assertEqual(self.orca_calls(), [])


class SweepPassTest(ReconcileCase):
    """The heartbeat pass runs errand.sweep once, after the repairs, and survives its failure."""

    def test_the_pass_calls_the_sweep_once(self):
        with mock.patch.object(errand, "sweep", return_value=["renudge staff-acme-shop"]) as sweep:
            code, lines = self.run_at(0)
        self.assertEqual(code, 0)
        sweep.assert_called_once()
        reg, now = sweep.call_args.args
        self.assertEqual(now, T0)
        self.assertEqual(set(reg["projects"]), set(registry.load()["projects"]))
        self.assertIn("woke staff-acme-shop again: errand results wait", lines)

    def test_a_sweep_that_raises_never_fails_the_pass(self):
        with mock.patch.object(errand, "sweep", side_effect=RuntimeError("boom")):
            code, _lines = self.run_at(0)
        self.assertEqual(code, 0)
        self.assertTrue(reconcile.heartbeat_path().exists())
        self.assertIn("reconcile: errand sweep: RuntimeError: boom",
                      (self.home / "logs" / "hooks.log").read_text())

    def test_a_dry_run_never_sweeps_and_says_so_only_when_staff_items_wait(self):
        with mock.patch.object(errand, "sweep") as sweep:
            _code, lines = self.run_at(0, dry_run=True)
            self.assertEqual(lines, ["reconcile: nothing to repair"])
            self.pointer("staff-acme-shop", 5, **{"from": "staff-acme-shop", "via": "runner"})
            _code, lines = self.run_at(0, dry_run=True)
        sweep.assert_not_called()
        self.assertIn("would sweep staff inboxes", lines)

    def test_a_staff_item_pending_half_an_hour_is_forwarded_to_main_by_the_pass(self):
        self.pointer("staff-acme-shop", 31, **{"from": "staff-acme-shop", "to": "staff-acme-shop",
                                               "via": "runner", "summary": "Two PRs."})
        code, lines = self.run_at(0)
        self.assertEqual(code, 0)
        self.assertIn("forwarded staff-acme-shop's waiting items to Main", lines)
        self.assertEqual(inbox.pending("staff-acme-shop"), [])
        [ptr] = [p for _x, p in inbox.pending("main-acme-shop") if p.get("forwarded")]
        self.assertEqual((ptr["from"], ptr["via"], ptr["forwarded"]), ("staff-acme-shop", "staff", "undelivered"))


if __name__ == "__main__":
    unittest.main()
