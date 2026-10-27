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
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
import contextlib
from orche import brief, deliver, hr, inbox, notify, orca, procs, registry, roster, runs, util
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


class DeadPane:
    """A stateful stand-in for orca.terminal: `title`'s pane is listed `connected: false` (Orca kept the
    terminal, its agent is gone) until it is closed; a `create` then lists a new connected pane.
    `others` are listed connected; busy=True makes every tui-idle wait unsatisfied. Calls in `calls`."""

    def __init__(self, title, handle, others=(), *, connected=False, busy=False):
        self.terms = {t: {"handle": h, "title": t, "connected": True} for t, h in others}
        self.terms[title] = {"handle": handle, "title": title, "connected": connected}
        self.busy, self.calls = busy, []

    def subs(self):
        return [c[0] for c in self.calls]

    def __call__(self, sub, *args, **kw):
        self.calls.append((sub, *args))
        if sub == "list":
            return {"terminals": [dict(t) for t in self.terms.values()]}
        if sub == "create":
            title = args[args.index("--title") + 1]
            self.terms[title] = {"handle": f"{title}-new", "title": title, "connected": True}
            return {}
        handle = args[args.index("--terminal") + 1] if "--terminal" in args else None
        live = any(t["handle"] == handle and t["connected"] for t in self.terms.values())
        if sub == "close":
            self.terms = {k: t for k, t in self.terms.items() if t["handle"] != handle}
            return {}
        if sub == "wait":
            return {"wait": {"handle": handle, "satisfied": not self.busy, "status": "running" if live else "exited"}}
        if sub == "read":
            return {"terminal": {"handle": handle, "status": "running" if live else "exited", "tail": []}}
        if sub == "send":
            return {"send": {"accepted": True, "handle": handle,
                             "prompt": {"provider": "claude", "stages": ["input_accepted", "turn_started"]}}}
        return {}


class NudgeCase(HookCase):
    """The hooks' registry (main-acme, div-acme-build, …), wake off, fake Orca on, short lock waits."""

    def setUp(self):
        super().setUp()
        os.environ["ORCHE_FAKE_ORCA"] = "1"
        self.canned()
        for name, value in (("LOCK_WAIT_S", 0.3), ("WAKE_HOLD_S", 0.3), ("POLL_S", 0.05), ("REOPEN_WAIT_S", 0.3)):
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

    def quiet_cap(self, cap):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["inbox"] = {**cfg.get("inbox", {}), "quiet_max_s": cap}
        (self.home / "config.json").write_text(json.dumps(cfg))

    def test_locked(self):
        self.report()
        self.quiet_cap(0)
        folder = inbox.box("main-acme")
        with open(folder / deliver.WAKE_LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.nudge(), (1, "locked"))
        self.assertEqual(self.orca_calls(), [])
        self.assertIn("nudge session=main-acme state=locked pending=1", self.events())
        last = folder / deliver.LAST_NUDGE
        self.assertFalse(last.exists())                            # a `locked` nudger records nothing
        self.assertEqual(self.nudge(), (0, "nudged"))             # released: the next nudger types
        self.assertEqual(json.loads(last.read_text())["state"], "nudged")   # written under the wake lock

    def test_the_wake_lock_wait_follows_the_quiet_cap(self):
        self.assertEqual(deliver.wake_wait_s({"quiet_max_s": 30}), deliver.WAKE_HOLD_S + 30)
        self.report()
        self.quiet_cap(0.6)
        with open(inbox.box("main-acme") / deliver.WAKE_LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            began = time.monotonic()
            self.assertEqual(self.nudge(), (1, "locked"))
            self.assertGreaterEqual(time.monotonic() - began, 0.85)   # WAKE_HOLD_S 0.3 + quiet_max_s 0.6

    def test_a_deliverers_lock_never_holds_up_a_wake(self):
        self.report()
        with open(inbox.box("main-acme") / deliver.DELIVER_LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.nudge(), (0, "nudged"))

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
                self.assertEqual([c for c in self.orca_calls() if c[1:3] == ["terminal", "send"]][-1], SEND_ARGV)
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
        with mock.patch.object(runs, "running", return_value={"pid": 1, "rev": 1}), mock.patch.object(runs, "start") as start:
            self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (0, "running"))
        start.assert_not_called()

    def test_already_ran_never_runs_again(self):
        b = self.brief()
        util.write_json(b.folder / "RUN.json", {"started": util.now_iso()})
        with mock.patch.object(runs, "start") as start:
            self.assertEqual(self.deliver(b.unit, {"delivery": "runner"}), (0, "already-ran"))
        start.assert_not_called()
        earlier = _dt.datetime.fromtimestamp(b.path.stat().st_mtime - 60).astimezone().isoformat(timespec="seconds")
        util.write_json(b.folder / "RUN.json", {"started": earlier})     # an earlier revision's run, written later
        (b.folder / "REPORT.md").write_text("done")                       # REPORT.md names no revision
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
        cases = [  # (to, sender, canned, state, hint)
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

    # -- a dead pane is reopened before the brief is typed (C38) ---------------------------------
    def test_a_disconnected_pane_is_closed_reopened_and_delivered_to(self):
        b = self.dispatched()
        fake = DeadPane("div-acme-build", "t-build", [("✳ main-acme", "t-main")])
        with mock.patch.object(orca, "terminal", fake):
            self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertIn(("close", "--terminal", "t-build"), fake.calls)
        self.assertEqual(fake.subs().count("create"), 1)                  # one reopen, through hr.wake
        create = next(c for c in fake.calls if c[0] == "create")
        self.assertIn("div-acme-build", create)
        sends = [c for c in fake.calls if c[0] == "send"]
        self.assertEqual(len(sends), 1)                                   # the normal nudge, nothing extra
        self.assertEqual(sends[0][1:3], ("--terminal", "div-acme-build-new"))
        self.assertIn("reopen session=div-acme-build state=reopened", self.events())
        self.assertIn(f"deliver unit={b.unit} rev=1 to=div-acme-build state=reopened",
                      (self.home / "logs" / "deliver.log").read_text())
        self.assertTrue(deliver.last_delivery("div-acme-build", b.unit).get("reopened"))

    def test_a_failed_send_on_a_pane_listed_disconnected_afterwards_is_reopened_once(self):
        b = self.dispatched()
        fake = DeadPane("div-acme-build", "t-build", [("✳ main-acme", "t-main")], connected=True)
        real_call = fake.__call__

        def dies_on_send(sub, *args, **kw):
            if sub == "send" and "t-build" in args:
                fake.calls.append((sub, *args))
                fake.terms["div-acme-build"]["connected"] = False
                return {"send": {"accepted": True, "prompt": {"provider": "claude", "stages": ["input_accepted"]}}}
            return real_call(sub, *args, **kw)
        with mock.patch.object(orca, "terminal", dies_on_send):
            self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertEqual(fake.subs().count("create"), 1)
        self.assertEqual([c[2] for c in fake.calls if c[0] == "send"], ["t-build", "div-acme-build-new"])

    def test_the_heartbeats_first_retry_reopens_a_dead_pane_and_delivers(self):
        b = self.dispatched()
        fake = DeadPane("div-acme-build", "t-build", [("✳ main-acme", "t-main")])
        with mock.patch.object(orca, "terminal", fake):            # what `orche deliver --retry` runs
            self.assertEqual(deliver.deliver_unit(registry.load(), b.unit, retry=True), (0, "nudged"))
        self.assertEqual(fake.subs().count("create"), 1)
        self.assertEqual([c[2] for c in fake.calls if c[0] == "send"], ["div-acme-build-new"])
        self.assertEqual(self.failures(), [])                      # attempt 1 repaired: nothing to escalate

    def test_a_connected_busy_pane_is_not_reopened(self):
        b = self.dispatched()
        fake = DeadPane("div-acme-build", "t-build", [("✳ main-acme", "t-main")], connected=True, busy=True)
        with mock.patch.object(orca, "terminal", fake):
            _code, state = self.deliver(b.unit)
        self.assertNotEqual(state, "no-agent")
        self.assertNotIn("close", fake.subs())
        self.assertNotIn("create", fake.subs())
        self.assertNotIn("state=reopened", self.events())
        self.assertNotIn("reopened", deliver.last_delivery("div-acme-build", b.unit))

    def test_a_nudge_to_a_disconnected_main_reopens_it_resumed_and_asks_where_things_stand(self):
        self.report()
        fake = DeadPane("main-acme", "t-main", [("div-acme-build", "t-build")])
        with mock.patch.object(orca, "terminal", fake):
            self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertIn(("close", "--terminal", "t-main"), fake.calls)
        self.assertEqual(fake.subs().count("create"), 1)                  # hr.project_pane, as `orche wake` opens it
        self.assertNotIn("switch", fake.subs())
        self.assertEqual([c[2] for c in fake.calls if c[0] == "send"], ["main-acme-new"])
        self.assertIn(("orche", True, hr.panes.RESUMED),
                      [(p["from"], p["urgent"], p["summary"]) for _f, p in inbox.pending("main-acme")])

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
        with open(inbox.box("div-acme-build") / deliver.DELIVER_LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.deliver(b.unit), (1, "locked"))
        self.assertEqual(self.orca_calls(), [])
        self.assertEqual(len(self.failures()), 1)

    def test_a_nudgers_wake_lock_never_holds_up_a_delivery(self):
        b = self.dispatched()
        with open(inbox.box("div-acme-build") / deliver.WAKE_LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertEqual(self.failures(), [])

    def test_the_failure_pointer_wakes_the_sender(self):
        b = self.dispatched("div-acme-rnd")
        (self.home / "config.json").write_text(json.dumps({"delivery": "pane", "inbox": {"wake": True}}))
        with mock.patch.object(procs, "spawn_detached") as spawn, \
                mock.patch.object(runs, "start", side_effect=OSError("no slot")):
            self.assertEqual(self.deliver(b.unit), (1, "needs-wake"))
        spawn.assert_called_once_with(WakeTest.ARGV, self.home / "logs" / "nudge.log")

    def test_a_division_or_feature_without_a_pane_runs_headless(self):
        rest = [{"handle": "t-main", "title": "main-acme"}]
        cases = [("div-acme-rnd", "main-acme", {}, "needs-wake"),
                 ("div-acme-build", "main-acme", {"terminal_list": {"terminals": rest}}, "wake-requested"),
                 ("feat-acme-checkout", "div-acme-build", {}, "no-pane")]
        for to, sender, over, state in cases:
            with self.subTest(state=state, to=to):
                self.canned(**over)
                b = self.dispatched(to, sender)
                with mock.patch.object(runs, "start") as start:
                    self.assertEqual(self.deliver(b.unit), (0, "runner-fallback"))
                start.assert_called_once_with(b.unit)
                self.assertEqual(self.failures(sender), [])
                self.assertIn(f"deliver unit={b.unit} rev=1 to={to} state=runner-fallback", self.events())
                with mock.patch.object(runs, "start", side_effect=OSError("no slot")):
                    self.assertEqual(self.deliver(b.unit), (1, state))
                [ptr] = self.failures(sender)
                self.assertEqual(ptr["summary"], f"not delivered: {b.unit} → {to} rev 1 ({state}) — target has no "
                                                 f"pane: run `orche wake {to}`, then `orche deliver {b.unit}`")
                for path, _x in inbox.pending(sender):
                    path.unlink()

    def test_a_second_failed_deliver_files_no_second_failure_while_the_first_is_pending(self):
        self.canned(terminal_list={"terminals": [{"handle": "t-main", "title": "main-acme"}]})
        b = self.dispatched("div-acme-build", "main-acme")
        with mock.patch.object(runs, "start", side_effect=OSError("fork failed")):
            first, second = self.deliver(b.unit), self.deliver(b.unit)
        self.assertEqual(first, (1, "wake-requested"))
        self.assertEqual(second, first)
        self.assertEqual(len(self.failures("main-acme")), 1)

    def test_a_revision_that_ran_or_runs_is_already_ran_and_its_pointer_drained(self):
        b = self.dispatched("div-acme-rnd")
        util.write_json(b.folder / "RUN.json", {"started": util.now_iso()})
        with mock.patch.object(runs, "start") as start:
            self.assertEqual(self.deliver(b.unit), (0, "already-ran"))
        start.assert_not_called()
        self.assertEqual(inbox.pending("div-acme-rnd"), [])
        b = self.dispatched()                      # a pane target: running → drained, nothing typed
        with mock.patch.object(runs, "running", return_value={"pid": 1, "rev": 1}):
            self.assertEqual(self.deliver(b.unit), (0, "already-ran"))
        self.assertNotIn(["terminal", "send"], self.calls())
        self.assertEqual([p for _x, p in inbox.pending("div-acme-build") if p["unit"] == b.unit], [])
        self.assertEqual(deliver.last_delivery("div-acme-build", b.unit)["state"], "already-ran")

    # -- STOP and AMEND (review B2) -------------------------------------------------------
    def stop_pointer(self, b, to, sender="main-acme"):
        inbox.write(inbox.make(b.unit, sender, to, "stop", report_path=str(b.path), brief_rev=b.rev,
                               summary="STOP requested", via="dispatch"))

    def amend(self, b, to):
        """BRIEF.md moved to rev 2 (what an AMEND writes) and its dispatch pointer."""
        b.path.write_text(b.path.read_text().replace("rev: 1", "rev: 2", 1))
        b = brief.find(registry.load(), b.unit)
        inbox.write(inbox.make(b.unit, "main-acme", to, "info", report_path=str(b.path), brief_rev=2, via="dispatch"))
        return b

    def test_a_stop_stops_a_live_run_and_is_drained(self):
        b = self.brief("div-acme-rnd")
        self.stop_pointer(b, "div-acme-rnd")
        with mock.patch.object(runs, "running", return_value={"pid": 1, "rev": 1}), \
                mock.patch.object(runs, "stop", return_value=True) as stop, \
                mock.patch.object(runs, "start", side_effect=AssertionError("a STOP never starts a run")):
            self.assertEqual(self.deliver(b.unit), (0, "stopped"))
        self.assertEqual(stop.call_args[0], (b.project, b.unit))
        self.assertEqual(inbox.pending("div-acme-rnd"), [])
        self.assertNotIn(["terminal", "send"], self.calls())

    def test_a_stop_with_no_pane_and_nothing_live_is_dropped(self):
        b = self.brief("div-acme-rnd")                      # no pane titled div-acme-rnd
        self.stop_pointer(b, "div-acme-rnd")
        with mock.patch.object(runs, "start", side_effect=AssertionError("a STOP never starts a run")), \
                mock.patch.object(runs, "restart", side_effect=AssertionError("restarted")):
            self.assertEqual(self.deliver(b.unit), (0, "stop-idle"))
        self.assertEqual(inbox.pending("div-acme-rnd"), [])
        self.assertEqual(self.failures(), [])

    def test_a_stop_after_a_blocked_report_is_typed_not_drained(self):
        b = self.brief()                                    # div-acme-build has a pane
        inbox.write(inbox.make(b.unit, "div-acme-build", "main-acme", "blocked", brief_rev=1))
        self.stop_pointer(b, "div-acme-build")
        self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertIn(self.send_argv(b.unit), self.orca_calls())
        self.assertEqual([p["status"] for _x, p in inbox.pending("div-acme-build")], ["stop"])

    def test_an_amend_while_the_old_revision_runs_is_delivered_never_drained(self):
        b = self.amend(self.dispatched(), "div-acme-build")          # a pane target
        with mock.patch.object(runs, "running", return_value={"pid": 1, "rev": 1}):
            self.assertEqual(self.deliver(b.unit), (0, "nudged"))
        self.assertIn(["terminal", "send"], self.calls())
        self.assertEqual(len(inbox.pending("div-acme-build")), 2)
        c = self.amend(self.dispatched("div-acme-rnd"), "div-acme-rnd")   # no pane: the old run is restarted
        with mock.patch.object(runs, "running", return_value={"pid": 1, "rev": 1}), \
                mock.patch.object(runs, "restart", return_value=True) as restart, \
                mock.patch.object(runs, "start", side_effect=AssertionError("start beside a live run")):
            self.assertEqual(self.deliver(c.unit), (0, "runner-fallback"))
        self.assertEqual(restart.call_args[0], (c.project, c.unit))
        self.assertEqual(deliver.last_delivery("div-acme-rnd", c.unit)["state"], "runner-fallback")

    def test_a_unit_settled_while_waiting_for_the_lock_is_drained_not_typed(self):
        b = self.dispatched()
        box = inbox.box("div-acme-build")
        held = open(box / deliver.DELIVER_LOCK, "a")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)     # a first deliverer holds the pane
        waiting, got, real = threading.Event(), [], deliver._lock

        @contextlib.contextmanager
        def watched(session, name, wait_s, poll_s=0.05):
            if wait_s:
                waiting.set()
            with real(session, name, wait_s, 0.05) as ok:
                yield ok
        with mock.patch.object(deliver, "_lock", watched), mock.patch.object(deliver, "LOCK_WAIT_S", 5.0):
            t = threading.Thread(target=lambda: got.append(self.deliver(b.unit)))
            t.start()
            self.assertTrue(waiting.wait(5))
            inbox.write(inbox.make(b.unit, "div-acme-build", "main-acme", "done", brief_rev=1))   # settles meanwhile
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()
            t.join(5)
        self.assertEqual(got, [(0, "already-ran")])
        self.assertNotIn(["terminal", "send"], self.calls())
        self.assertEqual([p for _x, p in inbox.pending("div-acme-build") if p.get("via") == "dispatch"], [])

    # -- `--remind` (S5, S10) -------------------------------------------------------------
    def test_remind_takes_only_the_two_reasons(self):
        b = self.dispatched()
        for bad in ("idle 5 min\nrm -rf ~", "please report", "idle 5 min ", "TIMEBOX 100h0m passed", ""):
            with self.subTest(bad=bad):
                self.assertEqual(deliver.remind(registry.load(), b.unit, bad), (2, "bad-reason"))
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(load_cli().main(["deliver", "--remind", bad, b.unit]), 2)
        self.assertNotIn(["terminal", "send"], self.calls())

    def test_remind_types_once_and_never_after_a_report(self):
        b = self.dispatched()
        inbox.drain([p for p, _x in inbox.pending("div-acme-build")])       # delivered
        self.assertEqual(deliver.remind(registry.load(), b.unit, "idle 25 min"), (0, "nudged"))
        [send] = [a for a in self.orca_calls() if a[1:3] == ["terminal", "send"]]
        self.assertTrue(send[send.index("--text") + 1].startswith(f"[orche] report due: {b.unit} rev 1 — idle 25 min;"))

        def report_during_the_wait(sub, *args, **kw):
            if sub == "wait":
                inbox.write(inbox.make(b.unit, "div-acme-build", "main-acme", "stop", brief_rev=1))
            return REAL_TERMINAL(sub, *args, **kw)
        with mock.patch.object(orca, "terminal", report_during_the_wait):     # still_needed() false: no send
            self.assertEqual(deliver.remind(registry.load(), b.unit, "TIMEBOX 0h20m passed"), (0, "already-reported"))
        self.assertEqual(deliver.remind(registry.load(), b.unit, "idle 30 min"), (0, "already-reported"))
        self.assertEqual(len([a for a in self.orca_calls() if a[1:3] == ["terminal", "send"]]), 1)
        self.assertIn(f"remind unit={b.unit} rev=1 to=div-acme-build state=already-reported", self.events())

    def test_a_report_at_this_revision_is_already_ran(self):
        b = self.dispatched()
        inbox.write(inbox.make(b.unit, "div-acme-build", "main-acme", "done", brief_rev=1))
        self.assertEqual(self.deliver(b.unit), (0, "already-ran"))
        self.assertNotIn(["terminal", "send"], self.calls())

    def test_a_retry_posts_no_failure_pointer(self):
        b = self.dispatched()
        self.canned(terminal_send={"send": {"accepted": False}})
        self.assertEqual(deliver.deliver_unit(registry.load(), b.unit, retry=True), (1, "refused"))
        self.assertEqual(self.failures(), [])

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


class NudgeNotifiesNothingTest(NudgeCase):
    """R26: the 0.6.2 per-pointer notification is gone — an urgent report to Main wakes its pane and
    nothing else; the owner hears Main's own closing line (flow.owner, tests/test_owner.py)."""

    def test_an_urgent_report_to_main_shows_no_notification(self):
        with mock.patch.object(notify, "PLATFORM", "darwin"):
            for status in inbox.URGENT:
                self.report(status=status)
                self.marker().unlink(missing_ok=True)
                self.assertEqual(self.nudge()[0], 0)
        self.assertFalse((self.home / support.OSASCRIPT_CALLS).exists())
        self.assertFalse((inbox.box("main-acme") / ".notified").exists())
        self.assertNotIn("owner notify", (self.home / "logs" / "events.log").read_text())


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
        inbox.post(self.ptr("blocked", via=None, urgent=True))    # an urgent note wakes (not a repeat)
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
        for status in inbox.URGENT:                               # urgent or not: the fresh wake covers it
            inbox.post(self.ptr(status))                           # (R26: no nudger spawned to notify)
        self.spawn.assert_not_called()
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
