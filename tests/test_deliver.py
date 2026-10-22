"""The wake (C9 T2): inbox.post/wake/rearm and `orche nudge <session>` (lib/orche/deliver.py, design §4.4);
the deliverer (C9 T3): `orche deliver <unit>` (design §4.5).

In-process against the fake Orca (ORCHE_FAKE_ORCA=1, canned `fake-orca.json`, calls recorded in
`logs/fake-orca.jsonl`); spawns are mocked (procs.spawn_detached), never a real Orca or nudger.
"""
import datetime as _dt
import fcntl
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from orche import brief, deliver, inbox, orca, procs, registry, roster, runs
from test_hooks import HookCase
from test_registry import load_cli

CANNED = {"terminal list": {"terminals": [{"handle": "t-main", "title": "✳ main-acme"},
                                          {"handle": "t-build", "title": "div-acme-build"}]},
          "terminal wait": {"wait": {"handle": "t-main", "condition": "tui-idle", "satisfied": True,
                                     "status": "running", "exitCode": None}},
          "terminal read": {"terminal": {"handle": "t-main", "status": "running", "tail": []}},
          "terminal send": {"send": {"accepted": True, "handle": "t-main", "bytesWritten": 27}}}
SEND_ARGV = ["orca", "terminal", "send", "--terminal", "t-main", "--text", inbox.WAKE_TEXT,
             "--enter", "--wait-submit", "20", "--json"]
REAL_TERMINAL = orca.terminal


class NudgeCase(HookCase):
    """The hooks' registry (main-acme, div-acme-build, …), wake off, fake Orca on, short lock waits."""

    def setUp(self):
        super().setUp()
        os.environ["ORCHE_FAKE_ORCA"] = "1"
        self.canned()
        for name, value in (("LOCK_WAIT_S", 0.3), ("POLL_S", 0.05)):
            patcher = mock.patch.object(deliver, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def canned(self, **over):
        self.fake_orca({**CANNED, **{k.replace("_", " "): v for k, v in over.items()}})

    def report(self, status="done", via="runner", **kw):
        return inbox.write(inbox.make(f"acme-{status}-20261021-1", "div-acme-build", "main-acme", status,
                                      via=via, **kw))

    def nudge(self, session="main-acme"):
        return deliver.nudge(session)

    def calls(self):
        return [argv[1:3] for argv in self.orca_calls()]

    def marker(self):
        return self.home / "inbox" / "main-acme" / inbox.NUDGED

    def brief(self, to="div-acme-build", sender="main-acme"):
        fields = {"TO": to, "GOAL": "Add coupon field", "SCOPE": "src/**", "CONTEXT": "none", "ACCEPT": "works",
                  "VERIFY": "true", "TIMEBOX": "20m", "FORBID": "merge", "REPORT": "status", "TIER": "standard"}
        target = roster.Roster(registry.load()).member(to)
        return brief.write(brief.Block("brief", fields), target, sender, "1. Never force-push.", timeout=0)

    def dispatched(self, to="div-acme-build", sender="main-acme"):
        """A brief and its pane pointer, as the Stop hook leaves them."""
        b = self.brief(to, sender)
        inbox.write(inbox.make(b.unit, sender, to, "info", report_path=str(b.path), brief_rev=b.rev,
                               summary="Add coupon field", via="dispatch"))
        return b


class NudgeTest(NudgeCase):
    def test_nudged(self):
        self.report()
        seen = []

        def spy(sub, *args, **kw):
            if sub == "send":
                seen.append(self.marker().exists())      # the marker is written before the send
            return REAL_TERMINAL(sub, *args, **kw)
        with mock.patch.object(orca, "terminal", spy):
            self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertEqual(seen, [True])
        self.assertEqual(self.calls(), [["terminal", "list"], ["terminal", "wait"], ["terminal", "read"],
                                        ["terminal", "send"]])
        wait = self.orca_calls()[1]
        self.assertEqual(wait[3:], ["--terminal", "t-main", "--for", "tui-idle", "--timeout-ms", "120000", "--json"])
        self.assertEqual(self.orca_calls()[2][3:], ["--terminal", "t-main", "--json"])
        self.assertEqual(self.orca_calls()[3], SEND_ARGV)
        self.assertTrue(self.marker().is_file())
        self.assertIn("nudge session=main-acme state=nudged pending=1 waited=0s", self.events())
        self.assertEqual(len(inbox.pending("main-acme")), 1)      # the wake prompt drains it, not the nudger

    def test_a_fresh_marker_is_already_nudged_without_an_orca_call(self):
        self.report()
        self.marker().write_text("x")
        self.assertEqual(self.nudge(), (0, "already-nudged"))
        self.assertEqual(self.orca_calls(), [])
        self.assertIn("nudge session=main-acme state=already-nudged pending=1", self.events())

    def test_a_stale_marker_is_nudged_again(self):
        self.report()
        self.marker().write_text("x")
        old = time.time() - 11 * 60
        os.utime(self.marker(), (old, old))
        self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertGreater(self.marker().stat().st_mtime, old)

    def test_nothing_pending(self):
        self.report(status="info", via=None)                       # a held note never wakes
        self.report(status="info", via="dispatch")                 # nor a pane delivery
        self.assertEqual(self.nudge(), (0, "nothing-pending"))
        self.assertEqual(self.orca_calls(), [])
        self.assertIn("nudge session=main-acme state=nothing-pending pending=0 waited=0s", self.events())

    def test_failures_type_nothing_and_leave_no_marker(self):
        cases = {
            "no-pane": {"terminal_list": {"terminals": [{"handle": "t-build", "title": "div-acme-build"}]}},
            "busy": {"terminal_wait": {"wait": {"satisfied": False, "status": "running"}}},
            "dialog": {"terminal_wait": {"wait": {"satisfied": False, "status": "running",
                                                  "blockedReason": "permission"}}},
            "no-agent": {"terminal_wait": {"wait": {"satisfied": True, "status": "exited", "exitCode": 0}}},
            "user-typing": {"terminal_read": {"terminal": {"handle": "t-main", "status": "running",
                                                           "draft": "half typed", "tail": []}}},
        }
        self.report()
        for state, over in cases.items():
            with self.subTest(state):
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                self.canned(**over)
                self.assertEqual(self.nudge(), (1, state))
                self.assertNotIn(["terminal", "send"], self.calls())
                self.assertFalse(self.marker().exists())
                self.assertIn(f"nudge session=main-acme state={state} pending=1", self.events())

    def test_a_failed_send_removes_the_marker(self):
        self.report()
        for state, over in (("refused", {"send": {"accepted": False, "handle": "t-main"}}),
                            ("dialog", {"send": {"accepted": True, "prompt": {"observation": "permission"}}})):
            with self.subTest(state):
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                self.canned(terminal_send=over)
                self.assertEqual(self.nudge(), (1, state))
                self.assertEqual(self.orca_calls()[-1], SEND_ARGV)
                self.assertFalse(self.marker().exists())

    def test_an_orca_error_on_send_is_nudge_failed_and_removes_the_marker(self):
        self.report()
        seen = []

        def failing(sub, *args, **kw):
            if sub == "send":
                seen.append(self.marker().exists())
                raise orca.OrcaError("socket closed", "orca_error")
            return REAL_TERMINAL(sub, *args, **kw)
        with mock.patch.object(orca, "terminal", failing):
            self.assertEqual(self.nudge(), (1, "nudge-failed"))
        self.assertEqual(seen, [True])
        self.assertFalse(self.marker().exists())
        self.assertIn("nudge session=main-acme state=nudge-failed pending=1", self.events())

    def test_drained_during_the_wait_is_nothing_pending_and_no_send(self):
        self.report()

        def draining(sub, *args, **kw):
            res = REAL_TERMINAL(sub, *args, **kw)
            if sub == "wait":                       # Main's own prompt injected the report meanwhile
                inbox.drain([p for p, _x in inbox.pending("main-acme")])
            return res
        with mock.patch.object(orca, "terminal", draining):
            self.assertEqual(self.nudge(), (0, "nothing-pending"))
        self.assertEqual(self.calls(), [["terminal", "list"], ["terminal", "wait"]])
        self.assertFalse(self.marker().exists())

    def test_locked(self):
        self.report()
        folder = inbox.box("main-acme")
        with open(folder / deliver.LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.nudge(), (1, "locked"))
        self.assertEqual(self.orca_calls(), [])
        self.assertIn("nudge session=main-acme state=locked pending=1", self.events())
        self.assertEqual(self.nudge(), (0, "nudged"))             # released: the next nudger types

    def failing_on(self, sub, exc):
        def fake(name, *args, **kw):
            if name == sub:
                raise exc
            return REAL_TERMINAL(name, *args, **kw)
        return mock.patch.object(orca, "terminal", fake)

    def test_a_wait_timeout_is_busy_and_any_other_wait_error_is_nudge_failed(self):
        self.report()
        for code, state in (("timeout", "busy"), ("wait_timeout", "busy"), ("orca_error", "nudge-failed")):
            with self.subTest(code):
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                with self.failing_on("wait", orca.OrcaError("no idle", code)):
                    self.assertEqual(self.nudge(), (1, state))
                self.assertEqual(self.calls(), [["terminal", "list"]])
                self.assertFalse(self.marker().exists())
                self.assertIn(f"nudge session=main-acme state={state} pending=1", self.events())

    def test_a_failed_list_or_read_is_nudge_failed_without_marker_or_send(self):
        self.report()
        for sub in ("list", "read"):
            with self.subTest(sub):
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                with self.failing_on(sub, orca.OrcaError("socket closed", "orca_error")):
                    self.assertEqual(self.nudge(), (1, "nudge-failed"))
                self.assertNotIn(["terminal", "send"], self.calls())
                self.assertFalse(self.marker().exists())
                self.assertIn(f"deliver: terminal {sub} for main-acme", (self.home / "logs" / "hooks.log").read_text())

    def test_an_exception_inside_the_lock_releases_it_and_writes_the_events_line(self):
        self.report()
        with mock.patch.object(deliver, "_type", side_effect=RuntimeError("boom")):
            self.assertEqual(self.nudge(), (1, "nudge-failed"))
        self.assertIn("nudge session=main-acme state=nudge-failed pending=1", self.events())
        self.assertIn("RuntimeError: boom", (self.home / "logs" / "hooks.log").read_text())
        self.assertEqual(self.nudge(), (0, "nudged"))             # the lock was released

    def test_a_send_without_a_proven_turn_start_is_nudge_failed_and_removes_the_marker(self):
        self.report()
        unproven = {"provider": "claude", "stages": ["input_accepted"]}
        cases = [({"accepted": True, "prompt": unproven}, "nudge-failed"),
                 ({"accepted": True, "prompt": {"provider": "claude"}}, "nudge-failed"),
                 ({"accepted": True, "prompt": {**unproven, "stages": ["input_accepted", "turn_started"],
                                                "observation": "incarnation_replaced"}}, "nudge-failed"),
                 ({"accepted": True, "prompt": {"provider": "claude", "stages": ["input_accepted", "turn_started"]}},
                  "nudged"),
                 ({"accepted": True, "prompt": {"provider": "old-host", "stages": []}}, "nudged"),
                 ({"accepted": True, "prompt": {"provider": "unsupported"}}, "nudged"),
                 ({"accepted": True, "handle": "t-main"}, "nudged")]          # no prompt object: an older host
        for send, state in cases:
            with self.subTest(send=send):
                self.marker().unlink(missing_ok=True)
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                self.canned(terminal_send={"send": send})
                self.assertEqual(self.nudge(), (0 if state == "nudged" else 1, state))
                self.assertEqual(self.orca_calls()[-1], SEND_ARGV)
                self.assertEqual(self.marker().exists(), state == "nudged")
        self.assertIn("not submitted", (self.home / "logs" / "hooks.log").read_text())

    def test_a_stale_orche_line_in_the_draft_is_nudge_failed_and_never_typed_again(self):
        self.report()
        for draft in (inbox.WAKE_TEXT, "[orche] inbox: acme-x-20261021-1 rev 2"):
            with self.subTest(draft):
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                self.canned(terminal_read={"terminal": {"handle": "t-main", "status": "running", "draft": draft}})
                self.assertEqual(self.nudge(), (1, "nudge-failed"))
                self.assertNotIn(["terminal", "send"], self.calls())
                self.assertFalse(self.marker().exists())
        self.assertIn("stale ORCHE line", (self.home / "logs" / "hooks.log").read_text())

    def test_a_marker_that_cannot_be_removed_is_logged_not_raised(self):
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text("x")
        with mock.patch.object(type(self.marker()), "unlink", side_effect=OSError("read-only")):
            deliver._unmark("main-acme")                            # the send's state stands
        self.assertIn("cannot remove main-acme's wake marker", (self.home / "logs" / "hooks.log").read_text())

    def test_the_prompt_hook_removes_the_marker_after_it_drains(self):
        """A nudger waiting on the lock must not see "no marker" while the reports are still pending."""
        self.report()
        self.marker().write_text("x")
        r = roster.Roster(registry.load())
        who = r.identify(str(self.repo), {"ORCHE_SESSION": "main-acme"})
        seen, real = [], inbox.drain

        def drain(paths_):
            seen.append(self.marker().exists())
            return real(paths_)
        with mock.patch.object(inbox, "drain", drain):
            text = inbox.handle_prompt({"prompt": inbox.WAKE_TEXT, "session_id": "s-1"}, time.monotonic(), r, who)
        self.assertIn("## Inbox (1)", text)
        self.assertEqual(seen, [True])
        self.assertFalse(self.marker().exists())


class DeliverTest(NudgeCase):
    """`deliver.deliver_unit` in-process (HookCase config: `delivery: pane`, wake off)."""

    def deliver(self, unit, cfg=None):
        return deliver.deliver_unit(registry.load(), unit, cfg)

    def failures(self, session="main-acme"):
        return [p for _x, p in inbox.pending(session) if p["from"] == "orche" and p["urgent"]]

    def send_argv(self, unit, handle="t-build"):
        return ["orca", "terminal", "send", "--terminal", handle, "--text", f"[orche] inbox: {unit} rev 1",
                "--enter", "--wait-submit", "20", "--json"]

    # -- runner units ----------------------------------------------------------------
    def test_runner_started(self):
        b = self.brief()
        with mock.patch.object(runs, "start") as start:
            self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (0, "runner-started"))
        start.assert_called_once_with(b.unit)
        self.assertEqual(self.orca_calls(), [])
        self.assertIn(f"deliver unit={b.unit} rev=1 to=div-acme-build state=runner-started waited=0s", self.events())

    def test_running(self):
        b = self.brief()
        with mock.patch.object(runs, "running", return_value={"pid": 1}), mock.patch.object(runs, "start") as start:
            self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (0, "running"))
        start.assert_not_called()

    def test_already_ran_never_runs_again(self):
        for name in ("RUN.json", "REPORT.md"):
            with self.subTest(name):
                b = self.brief()
                (b.folder / name).write_text("{}")
                with mock.patch.object(runs, "start") as start:
                    self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (0, "already-ran"))
                start.assert_not_called()
                old = b.path.stat().st_mtime - 60              # an earlier revision's record
                os.utime(b.folder / name, (old, old))
                with mock.patch.object(runs, "start") as start:
                    self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (0, "runner-started"))

    def test_a_runner_that_cannot_start_is_runner_failed(self):
        b = self.brief()
        with mock.patch.object(runs, "start", side_effect=OSError("no exec")):
            self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (1, "runner-failed"))

    # -- pane units ------------------------------------------------------------------
    def test_picked_up(self):
        b = self.brief()                                    # no pending `via: dispatch` pointer
        self.assertEqual(self.deliver(b.unit), (0, "picked-up"))
        self.assertEqual(self.orca_calls(), [])
        self.assertIn(f"deliver unit={b.unit} rev=1 to=div-acme-build state=picked-up", self.events())

    def test_nudged(self):
        b = self.dispatched()
        self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertEqual(self.calls(), [["terminal", "list"], ["terminal", "wait"], ["terminal", "read"],
                                        ["terminal", "send"]])
        self.assertEqual(self.orca_calls()[1][3:], ["--terminal", "t-build", "--for", "tui-idle", "--timeout-ms",
                                                    "60000", "--json"])
        self.assertEqual(self.orca_calls()[3], self.send_argv(b.unit))
        self.assertIn(f"deliver unit={b.unit} rev=1 to=div-acme-build state=nudged waited=0s", self.events())
        self.assertEqual(len(inbox.pending("div-acme-build")), 1)      # the target's prompt injects it
        self.assertFalse((self.home / "inbox" / "div-acme-build" / inbox.NUDGED).exists())
        self.assertEqual(self.failures(), [])

    def test_not_idle_after_the_wait_is_typed_anyway(self):
        b = self.dispatched()
        self.canned(terminal_wait={"wait": {"satisfied": False, "status": "running"}})
        self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertEqual(self.orca_calls()[-1], self.send_argv(b.unit))

    def test_a_busy_pane_that_accepts_without_a_turn_start_is_queued(self):
        """Typed into a working pane: Claude Code queues it; no failure pointer, the pointer stays pending."""
        b = self.dispatched()
        self.canned(terminal_wait={"wait": {"satisfied": False, "status": "running"}},
                    terminal_send={"send": {"accepted": True, "prompt": {"provider": "claude",
                                                                         "stages": ["input_accepted"]}}})
        self.assertEqual(self.deliver(b.unit), (0, "queued"))
        self.assertEqual(self.orca_calls()[-1], self.send_argv(b.unit))
        self.assertEqual(self.failures(), [])
        self.assertEqual(len(inbox.pending("div-acme-build")), 1)      # the target's next prompt injects it
        self.assertIn(f"deliver unit={b.unit} rev=1 to=div-acme-build state=queued", self.events())

    def test_a_busy_pane_whose_process_was_replaced_is_still_nudge_failed(self):
        b = self.dispatched()
        self.canned(terminal_wait={"wait": {"satisfied": False, "status": "running"}},
                    terminal_send={"send": {"accepted": True, "prompt": {
                        "provider": "claude", "stages": ["input_accepted"], "observation": "incarnation_replaced"}}})
        self.assertEqual(self.deliver(b.unit), (1, "nudge-failed"))
        self.assertEqual(len(self.failures()), 1)

    def test_user_typing_is_exit_0_without_a_pointer(self):
        b = self.dispatched()
        self.canned(terminal_read={"terminal": {"handle": "t-build", "status": "running", "draft": "half", "tail": []}})
        self.assertEqual(self.deliver(b.unit), (0, "user-typing"))
        self.assertNotIn(["terminal", "send"], self.calls())
        self.assertEqual(self.failures(), [])

    def test_failures_post_an_urgent_pointer_to_the_sender_with_the_fix(self):
        rest = [{"handle": "t-main", "title": "main-acme"}]
        no_build = {"terminal_list": {"terminals": rest}}
        cases = [  # (to, sender, canned, state, hint)
            ("div-acme-rnd", "main-acme", {}, "needs-wake",
             "target has no pane: run `orche wake div-acme-rnd`, then `orche deliver {u}`"),
            ("div-acme-build", "main-acme", no_build, "wake-requested",
             "target has no pane: run `orche wake div-acme-build`, then `orche deliver {u}`"),
            ("feat-acme-checkout", "div-acme-build", {}, "no-pane",
             "target has no pane: run `orche wake feat-acme-checkout`, then `orche deliver {u}`"),
            ("main-acme", "div-acme-build", {"terminal_list": {"terminals": []}}, "wake-requested", None),
            ("div-acme-build", "main-acme", {"terminal_wait": {"wait": {"satisfied": False, "status": "running",
                                                                         "blockedReason": "permission"}}},
             "dialog", "the pane shows a dialog; answer or STOP it, then `orche deliver {u}`"),
            ("div-acme-build", "main-acme", {"terminal_wait": {"wait": {"satisfied": True, "status": "exited"}}},
             "no-agent", "retry with `orche deliver {u}`"),
            ("div-acme-build", "main-acme", {"terminal_send": {"send": {"accepted": False}}}, "refused",
             "retry with `orche deliver {u}`"),
        ]
        for to, sender, over, state, hint in cases:
            with self.subTest(state=state, to=to):
                self.canned(**over)
                for path, _x in inbox.pending(sender):
                    path.unlink()
                b = self.dispatched(to, sender)
                self.assertEqual(self.deliver(b.unit), (1, state))
                [ptr] = self.failures(sender)
                self.assertEqual((ptr["unit"], ptr["to"], ptr["status"], ptr["brief_rev"]), (b.unit, sender, "info", 1))
                want = f"not delivered: {b.unit} → {to} rev 1 ({state})"
                self.assertEqual(ptr["summary"], want + (" — " + hint.format(u=b.unit) if hint else ""))
                self.assertIn(f"deliver unit={b.unit} rev=1 to={to} state={state}", self.events())

    def test_an_orca_error_on_send_is_nudge_failed(self):
        b = self.dispatched()

        def failing(sub, *args, **kw):
            if sub == "send":
                raise orca.OrcaError("socket closed", "orca_error")
            return REAL_TERMINAL(sub, *args, **kw)
        with mock.patch.object(orca, "terminal", failing):
            self.assertEqual(self.deliver(b.unit), (1, "nudge-failed"))
        [ptr] = self.failures()
        self.assertTrue(ptr["summary"].endswith(f"(nudge-failed) — retry with `orche deliver {b.unit}`"), ptr)

    def test_an_unsubmitted_send_or_a_stale_draft_is_nudge_failed_with_the_urgent_pointer(self):
        cases = {"no turn start": {"terminal_send": {"send": {"accepted": True, "prompt": {
                     "provider": "claude", "stages": ["input_accepted"]}}}},
                 "replaced": {"terminal_send": {"send": {"accepted": True, "prompt": {
                     "provider": "claude", "stages": ["input_accepted"], "observation": "incarnation_replaced"}}}},
                 "stale draft": {"terminal_read": {"terminal": {"handle": "t-build", "status": "running",
                                                                "draft": "[orche] inbox: acme-y-20261021-1 rev 1"}}}}
        for name, over in cases.items():
            with self.subTest(name):
                for path, _x in inbox.pending("main-acme"):
                    path.unlink()
                (self.home / "logs" / "fake-orca.jsonl").unlink(missing_ok=True)
                self.canned(**over)
                b = self.dispatched()
                self.assertEqual(self.deliver(b.unit), (1, "nudge-failed"))
                self.assertEqual(["terminal", "send"] in self.calls(), name != "stale draft")
                [ptr] = self.failures()
                self.assertTrue(ptr["summary"].endswith(f"(nudge-failed) — retry with `orche deliver {b.unit}`"), ptr)

    def test_locked(self):
        b = self.dispatched()
        with open(inbox.box("div-acme-build") / deliver.LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.deliver(b.unit), (1, "locked"))
        self.assertEqual(self.orca_calls(), [])
        self.assertEqual(len(self.failures()), 1)

    def test_the_failure_pointer_wakes_the_sender(self):
        b = self.dispatched("div-acme-rnd")
        (self.home / "config.json").write_text(json.dumps({"delivery": "pane", "inbox": {"wake": True}}))
        with mock.patch.object(procs, "spawn_detached") as spawn:
            self.assertEqual(self.deliver(b.unit), (1, "needs-wake"))
        spawn.assert_called_once_with(WakeTest.ARGV, self.home / "logs" / "nudge.log")

    def test_unknown_unit_or_target_is_exit_2(self):
        self.assertEqual(self.deliver("acme-nope-20261021-1"), (2, "no-brief"))
        b = self.brief()
        reg = json.loads((self.home / "registry.json").read_text())
        reg["projects"]["acme"]["divisions"]["build"]["status"] = "retired"
        (self.home / "registry.json").write_text(json.dumps(reg))
        self.assertEqual(self.deliver(b.unit), (2, "retired-target"))
        self.assertNotIn("deliver unit=", self.events())


class FakeClock:
    """deliver's `time`: sleep() advances the clock at once and fires the callbacks due by then."""

    def __init__(self):
        self.t0, self.now, self.due = float(int(time.time())), 0.0, []

    def monotonic(self):
        return self.now

    def time(self):
        return self.t0 + self.now

    def sleep(self, seconds):
        self.now += seconds
        for item in [d for d in self.due if d[0] <= self.now]:
            self.due.remove(item)
            item[1]()

    def at(self, offset=None):
        return _dt.datetime.fromtimestamp(self.t0 + (self.now if offset is None else offset)).astimezone() \
            .isoformat(timespec="seconds")


class QuietTest(NudgeCase):
    """The quiet window (0.6.2): one wake per batch of reports; urgent statuses wake at once."""

    def setUp(self):
        super().setUp()
        (self.home / "config.json").write_text(json.dumps(
            {"delivery": "pane", "inbox": {"wake": False, "quiet_s": 90, "quiet_max_s": 240}}))
        self.clock = FakeClock()
        patcher = mock.patch.object(deliver, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, n, status="done", offset=None):
        inbox.write(inbox.make(f"acme-r{n}-20261021-1", "div-acme-build", "main-acme", status, via="runner",
                               at=self.clock.at(offset)))

    def later(self, when, n, status="done"):
        self.clock.due.append((when, lambda: self.post(n, status)))

    def test_three_reports_ten_seconds_apart_are_one_wake_after_the_quiet_window(self):
        self.post(1)
        self.later(10, 2)
        self.later(20, 3)
        self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertEqual(self.clock.now, 110)                      # 90 s after the newest report
        self.assertEqual(self.calls(), [["terminal", "list"], ["terminal", "wait"], ["terminal", "read"],
                                        ["terminal", "wait"], ["terminal", "read"], ["terminal", "send"]])
        self.assertIn("nudge session=main-acme state=nudged pending=3 waited=110s quiet=110s", self.events())

    def test_an_urgent_report_wakes_at_once(self):
        for n, status in enumerate(("blocked", "question", "failed")):
            with self.subTest(status=status):
                self.post(n, status)
                self.marker().unlink(missing_ok=True)
                start = self.clock.now
                self.assertEqual(self.nudge(), (0, "nudged"))
                self.assertEqual(self.clock.now, start)
                self.assertIn(f"nudge session=main-acme state=nudged pending={n + 1} waited=0s quiet=0s",
                              self.events())

    def test_an_urgent_report_ends_the_window_early(self):
        self.post(1)
        self.later(30, 2, "question")
        self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertEqual(self.clock.now, 30)
        self.assertIn("state=nudged pending=2 waited=30s quiet=30s", self.events())

    def test_the_window_is_capped(self):
        self.post(0)
        for n in range(1, 20):
            self.later(n * 30, n)                                  # a report every 30 s, never quiet
        self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertEqual(self.clock.now, 240)
        self.assertIn("waited=240s quiet=240s", self.events())

    def test_an_old_report_does_not_wait(self):
        self.post(1, offset=-300)
        self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertEqual(self.clock.now, 0)
        self.assertEqual(len(self.calls()), 4)
        self.assertIn("waited=0s quiet=0s", self.events())


class NotifyTest(NudgeCase):
    """0.6.2: the nudger notifies the owner (macOS) once per urgent pointer for a Main session."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(deliver, "PLATFORM", "darwin")
        patcher.start()
        self.addCleanup(patcher.stop)

    def notes(self):
        log_ = self.home / support.OSASCRIPT_CALLS
        return log_.read_text().splitlines() if log_.is_file() else []

    def test_one_notification_per_urgent_pointer(self):
        self.report(status="blocked")
        with mock.patch.object(deliver.shell, "run", wraps=deliver.shell.run) as run:
            self.nudge()
        run.assert_called_once_with(["osascript", "-e", 'display notification "blocked-20261021-1 blocked — Main에 '
                                     '보고가 있습니다" with title "ORCHE acme" sound name "Glass"'], timeout=5)
        self.assertEqual(len(self.notes()), 1)
        self.marker().unlink(missing_ok=True)
        self.nudge()                                               # the same pointer: no second notification
        self.assertEqual(len(self.notes()), 1)
        self.report(status="question")
        self.marker().unlink(missing_ok=True)
        self.nudge()
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("question-20261021-1 question", self.notes()[1])

    def test_an_urgent_report_notifies_while_a_wake_is_fresh(self):
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text("x")
        self.report(status="failed")
        self.assertEqual(self.nudge(), (0, "already-nudged"))
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("failed-20261021-1 failed", self.notes()[0])
        self.assertEqual(self.nudge(), (0, "already-nudged"))       # a re-run never re-notifies
        self.assertEqual(len(self.notes()), 1)

    def test_no_notification(self):
        cases = {"done": ("done", {}, "main-acme"), "notify-off": ("blocked", {"notify": False}, "main-acme"),
                 "not-main": ("blocked", {}, "div-acme-build"), "linux": ("blocked", {}, "main-acme")}
        for name, (status, cfg, to) in cases.items():
            with self.subTest(name):
                (self.home / "config.json").write_text(json.dumps(
                    {"delivery": "pane", "inbox": {"wake": False, "quiet_s": 0, **cfg}}))
                inbox.write(inbox.make(f"acme-{name}-20261021-1", "main-acme" if to != "main-acme" else
                                       "div-acme-build", to, status, via="runner"))
                with mock.patch.object(deliver, "PLATFORM", "linux" if name == "linux" else "darwin"):
                    deliver.notify_owner(to)
                self.assertEqual(self.notes(), [])

    def test_a_failing_osascript_is_logged_and_never_raises(self):
        self.report(status="blocked")
        with mock.patch.object(deliver.shell, "run", return_value=deliver.shell.Result(1, "", "boom", [])):
            deliver.notify_owner("main-acme")
        self.assertIn("deliver: notify main-acme: osascript exit 1", (self.home / "logs" / "hooks.log").read_text())


class WakeTest(support.TempHomeCase):
    ARGV = [sys.executable, str(support.ROOT / "bin" / "orche.py"), "nudge", "--", "main-acme"]

    def setUp(self):
        super().setUp()
        self.wake_config(True)
        patcher = mock.patch.object(procs, "spawn_detached")
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)

    def wake_config(self, on):
        (self.home / "config.json").write_text(json.dumps({"inbox": {"wake": on}}))

    def ptr(self, status="done", via="runner", **kw):
        return inbox.make("acme-x-20261021-1", "div-acme-build", "main-acme", status, via=via, **kw)

    def test_post_spawns_the_nudger_iff_the_pointer_wakes(self):
        path = inbox.post(self.ptr())
        self.assertTrue(path.is_file())
        self.spawn.assert_called_once_with(self.ARGV, self.home / "logs" / "nudge.log")
        self.spawn.reset_mock()
        for ptr in (self.ptr("info", via=None), self.ptr("info", via="dispatch"), self.ptr("done", via="dispatch")):
            self.assertTrue(inbox.post(ptr).is_file())
        self.spawn.assert_not_called()
        inbox.post(self.ptr("info", via=None, urgent=True))       # an urgent note wakes
        self.assertEqual(self.spawn.call_count, 1)

    def test_wake_off_spawns_nothing(self):
        self.wake_config(False)
        inbox.post(self.ptr())
        self.spawn.assert_not_called()

    def test_a_fresh_marker_spawns_nothing(self):
        folder = inbox.box("main-acme")
        folder.mkdir(parents=True)
        (folder / inbox.NUDGED).write_text("x")
        inbox.post(self.ptr())
        self.spawn.assert_not_called()
        for status in inbox.URGENT:                               # an urgent pointer still reaches the nudger
            inbox.post(self.ptr(status))                           # (it notifies, then exits already-nudged)
        self.assertEqual(self.spawn.call_count, len(inbox.URGENT))
        self.spawn.reset_mock()
        unit_box = inbox.box("div-acme-build")
        unit_box.mkdir(parents=True, exist_ok=True)
        (unit_box / inbox.NUDGED).write_text("x")
        inbox.post(inbox.make("acme-y-20261021-1", "main-acme", "div-acme-build", "blocked", via="runner"))
        self.spawn.assert_not_called()                             # no owner notification for a unit

    def test_an_unsafe_session_spawns_nothing_and_logs(self):
        inbox.wake("../x")
        self.spawn.assert_not_called()
        self.assertIn("inbox: wake '../x'", (self.home / "logs" / "hooks.log").read_text())

    def test_a_failing_spawn_is_logged_and_post_still_returns_the_path(self):
        self.spawn.side_effect = OSError("fork failed")
        path = inbox.post(self.ptr())
        self.assertTrue(path.is_file())
        self.assertIn("OSError: fork failed", (self.home / "logs" / "hooks.log").read_text())

    def test_rearm_wakes_only_for_a_pending_waking_pointer(self):
        inbox.write(self.ptr("info", via=None))
        inbox.rearm("main-acme")
        self.spawn.assert_not_called()
        inbox.write(self.ptr())
        inbox.rearm("main-acme")
        self.spawn.assert_called_once_with(self.ARGV, self.home / "logs" / "nudge.log")
        inbox.rearm("../x")                                        # never raises


class TearDownGuardTest(unittest.TestCase):
    """support.TempHomeCase fails a test that started a nudger without `self.wakes = True`."""

    def run_inner(self, wakes):
        class Inner(support.TempHomeCase):
            def test_post(self):
                self.wakes = wakes
                (self.home / "config.json").write_text(json.dumps({"inbox": {"wake": True}}))
                with mock.patch.object(procs.subprocess, "Popen") as popen:     # no process, the log is real
                    inbox.post(inbox.make("acme-x-20261021-1", "div-acme-build", "main-acme", "done", via="runner"))
                popen.assert_called_once()
        result = unittest.TestResult()
        Inner("test_post").run(result)
        return result

    def test_a_nudger_without_wakes_fails_the_test(self):
        result = self.run_inner(False)
        self.assertEqual((result.testsRun, len(result.errors)), (1, 0), result.errors)
        [(_case, trace)] = result.failures
        self.assertIn("a nudger was started (logs/nudge.log)", trace)

    def test_a_nudger_with_wakes_passes(self):
        result = self.run_inner(True)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)


class CliTest(NudgeCase):
    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = load_cli().main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_orche_nudge_prints_the_state(self):
        self.report()
        self.assertEqual(self.orche("nudge", "main-acme")[:2], (0, "nudged\n"))
        self.assertEqual(self.orche("nudge", "main-acme")[:2], (0, "already-nudged\n"))

    def test_orche_deliver_prints_the_state(self):
        b = self.dispatched()
        self.assertEqual(self.orche("deliver", b.unit)[:2], (0, "nudged\n"))
        code, out, err = self.orche("deliver", "rnd-x")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("bad unit id", err)
        code, out, err = self.orche("deliver", "acme-nope-20261021-1")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no-brief: acme-nope-20261021-1", err)

    def test_the_spawned_argv_form_parses(self):
        """The nudger and the deliverer are spawned with `--` before the name (WakeTest.ARGV)."""
        self.report()
        self.assertEqual(self.orche("nudge", "--", "main-acme")[:2], (0, "nudged\n"))
        b = self.dispatched()
        self.assertEqual(self.orche("deliver", "--", b.unit)[:2], (0, "nudged\n"))

    def test_a_bad_session_name_exits_2(self):
        code, out, err = self.orche("nudge", "../x")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unsafe session name", err)
        self.assertEqual(self.orca_calls(), [])
