"""The wake file (R-INBOX-4): lib/orche/watch.py, the SessionStart/SessionEnd records, the FileChanged hook
(hooks/inbox_wake.py), `orche nudge` / `orche deliver` waking by file before typing, the Stop hook's rearm, and
the prompt hook's handling of a wake Claude Code delivers (wrapped, or inside a running turn).

Fakes only: `claude agents --json` is deliver._agent_status (patched) or a fake shell.RUNNER; Orca is the fake
Orca of test_hooks; the session "taking" a wake is simulated by draining or unmarking when the file is written.
"""
import json
from unittest import mock

import support
from orche import deliver, inbox, registry, shell, watch
from orche.install import settings
from orche.flow import lineage
from test_deliver import NudgeCase
from test_hooks import HookCase

SID = "11111111-2222-4333-8444-555555555555"
# As Claude Code 2.1.322 gave it (the probe's hooks.log): the hook's stderr, its newline, then the closing tag.
WRAPPED = ("<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\n"
           'Stop hook blocking error from command "FileChanged": {line}\n\n</system-reminder>')


def report(to="main-acme", status="done", via="runner", unit="acme-done-20261021-1", **kw):
    return inbox.write(inbox.make(unit, "div-acme-build", to, status, via=via, **kw))


class WatchTest(HookCase):
    def test_arm_marker_and_disarm_only_by_the_named_session(self):
        self.assertIsNone(watch.marker("div-acme-build"))
        watch.arm("div-acme-build", SID)
        self.assertEqual(watch.marker("div-acme-build")["session_id"], SID)
        self.assertFalse(watch.disarm("div-acme-build", "another-session"))   # an older session ending
        self.assertIsNotNone(watch.marker("div-acme-build"))
        self.assertTrue(watch.disarm("div-acme-build", SID))
        self.assertIsNone(watch.marker("div-acme-build"))
        (inbox.box("div-acme-build") / watch.WATCH).write_text("{not json")
        self.assertIsNone(watch.marker("div-acme-build"))

    def test_ring_writes_new_content_every_time(self):
        watch.ring("main-acme")
        first = watch.wake_path("main-acme").read_text()
        watch.ring("main-acme")
        self.assertNotEqual(watch.wake_path("main-acme").read_text(), first)
        self.assertEqual(inbox.pending("main-acme"), [])        # never read as a pointer
        self.assertEqual(sorted(p.name for p in inbox.box("main-acme").iterdir()), [watch.WAKE])

    def test_due_is_the_wake_line_or_the_newest_brief_line(self):
        self.assertIsNone(watch.due("main-acme"))
        report(status="info", via=None)                          # a held note never wakes
        self.assertIsNone(watch.due("main-acme"))
        inbox.write(inbox.make("acme-a-20261021-1", "main-acme", "div-acme-build", "info", brief_rev=2, via="dispatch"))
        self.assertEqual(watch.due("div-acme-build"), "[orche] inbox: acme-a-20261021-1 rev 2")
        report(to="div-acme-build")
        self.assertEqual(watch.due("div-acme-build"), inbox.WAKE_TEXT)

    def test_changed_answers_only_for_the_sessions_own_wake_file(self):
        report()
        watch.arm("main-acme", SID)
        own = str(watch.wake_path("main-acme"))
        self.assertEqual(watch.changed(own, SID), inbox.WAKE_TEXT)
        self.assertIsNone(watch.changed(own, "another-session"))
        self.assertIsNone(watch.changed(str(self.root / ".envrc"), SID))
        self.assertIsNone(watch.changed(str(self.root / "inbox" / "main-acme" / watch.WAKE), SID))   # not ORCHE_HOME


class InboxWakeHookTest(HookCase):
    def fire(self, path, session_id=SID, event="change"):
        return self.run_hook("inbox_wake.py", {"session_id": session_id, "hook_event_name": "FileChanged",
                                               "cwd": str(self.root / "acme-build"), "file_path": str(path),
                                               "event": event})

    def test_another_file_exits_0_at_once(self):
        report(to="div-acme-build")
        watch.arm("div-acme-build", SID)
        res = self.fire(self.root / "acme-build" / ".envrc")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        res = self.fire(watch.wake_path("div-acme-build"), session_id="someone-else")
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_nothing_pending_exits_0(self):
        watch.arm("div-acme-build", SID)
        report(to="div-acme-build", status="info", via=None)
        res = self.fire(watch.wake_path("div-acme-build"))
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_pending_prints_orches_own_line_and_exits_2(self):
        watch.arm("div-acme-build", SID)
        report(to="div-acme-build")
        res = self.fire(watch.wake_path("div-acme-build"))
        self.assertEqual((res.returncode, res.stdout, res.stderr), (2, "", inbox.WAKE_TEXT + "\n"))
        self.assertEqual(self.fire(watch.wake_path("div-acme-build"), event="unlink").returncode, 0)

    def test_an_internal_error_fails_open(self):
        res = self.run_hook("inbox_wake.py", ["not", "a", "dict"])
        self.assertEqual((res.returncode, res.stderr), (0, ""))


class SessionRecordTest(HookCase):
    def start(self, cwd, **env):
        return self.run_hook("session_start.py", self.payload("session_start_clear.json", cwd=str(cwd),
                                                              session_id=SID), **env)

    def settings(self, data):
        (self.home / "claude" / "settings.json").write_text(json.dumps(data))

    def test_a_division_watches_its_wake_file_and_records_the_session(self):
        res = self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build")
        self.assertEqual(res.watch, [str(self.home / "inbox" / "div-acme-build" / ".wake")])
        self.assertTrue(json.loads(res.raw)["hookSpecificOutput"]["additionalContext"].startswith("## Orche role"))
        self.assertEqual(watch.marker("div-acme-build")["session_id"], SID)
        res = self.start(self.repo, ORCHE_SESSION="main-acme")
        self.assertEqual(res.watch, [str(self.home / "inbox" / "main-acme" / ".wake")])

    def test_only_the_panes_own_session_watches(self):
        """A session someone opened in a unit's worktree (no ORCHE_SESSION) is identified as the unit but never
        takes its wake file; a Main Orca reopened without ORCHE_SESSION, known by its transcript name, does."""
        self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build")
        stray = self.run_hook("session_start.py", self.payload("session_start_clear.json", session_id="stray-0001",
                                                               cwd=str(self.root / "acme-build")))
        self.assertIsNone(stray.watch)
        self.assertTrue(stray.stdout.startswith("## Orche role"))
        self.assertEqual(watch.marker("div-acme-build")["session_id"], SID)
        end = {"session_id": "stray-0001", "cwd": str(self.root / "acme-build"), "hook_event_name": "SessionEnd",
               "reason": "exit"}
        self.assertEqual(self.run_hook("session_end.py", end).returncode, 0)
        self.assertEqual(watch.marker("div-acme-build")["session_id"], SID)
        (self.root / "t.jsonl").write_text('{"type":"user"}\n{"type":"custom-title","customTitle":"main-acme"}\n')
        res = self.run_hook("session_start.py", self.payload("session_start_clear.json", session_id=SID,
                                                             cwd=str(self.repo)))
        self.assertEqual(res.watch, [str(watch.wake_path("main-acme"))])
        self.assertEqual(watch.marker("main-acme")["session_id"], SID)
        (self.root / "t.jsonl").write_text('{"type":"user"}\n')
        res = self.run_hook("session_start.py", self.payload("session_start_clear.json", session_id="other-0002",
                                                             cwd=str(self.repo)))
        self.assertIsNone(res.watch)
        self.assertEqual(watch.marker("main-acme")["session_id"], SID)

    def test_no_watch_unless_the_session_runs_the_wake_hook(self):
        """Settings mode before `orche install` adds the FileChanged entry: plain text, no record, typed wakes."""
        hooks = settings.plugin_hooks(support.ROOT)
        for data in ({}, {"hooks": {}}, {"enabledPlugins": {"orche@orche": False}}, "not an object"):
            with self.subTest(data=data):
                self.settings(data)
                res = self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build")
                self.assertIsNone(res.watch)
                self.assertTrue(res.raw.startswith("## Orche role"))
                self.assertIsNone(watch.marker("div-acme-build"))
        (self.home / "claude" / "settings.json").write_text("{not json")
        self.assertIsNone(self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build").watch)
        data = {}
        for event, matcher, command, flags in hooks:
            if event != "FileChanged":
                settings.add_hook(data, event, matcher, command, flags)
        self.settings(data)                                       # every hook but the wake hook
        self.assertIsNone(self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build").watch)
        for event, matcher, command, flags in hooks:
            if event == "FileChanged":
                settings.add_hook(data, event, matcher, command, flags)
        self.settings(data)                                       # settings mode with the entry
        res = self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build")
        self.assertEqual(res.watch, [str(watch.wake_path("div-acme-build"))])

    def test_a_headless_run_and_a_non_member_get_no_watch(self):
        res = self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build", ORCHE_RUNNER="1")
        self.assertIsNone(res.watch)
        self.assertTrue(res.stdout.startswith("## Orche role"))
        self.assertIsNone(watch.marker("div-acme-build"))
        res = self.start(self.root / "elsewhere")
        self.assertIsNone(res.watch)

    def test_session_end_removes_only_its_own_record(self):
        self.start(self.root / "acme-build", ORCHE_SESSION="div-acme-build")
        end = {"session_id": "older-session-0001", "cwd": str(self.root / "acme-build"),
               "hook_event_name": "SessionEnd", "reason": "exit"}
        self.assertEqual(self.run_hook("session_end.py", end).returncode, 0)
        self.assertIsNotNone(watch.marker("div-acme-build"))
        self.assertEqual(self.run_hook("session_end.py", {**end, "session_id": SID}).returncode, 0)
        self.assertIsNone(watch.marker("div-acme-build"))


class FileWakeTest(NudgeCase):
    """`orche nudge` and `orche deliver` with a WATCH record: the wake file first, typing as the fallback."""

    def setUp(self):
        super().setUp()
        for name, value in (("WAKE_WAIT_S", 0.3),):
            patcher = mock.patch.object(deliver, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def taking(self, session, how):
        """Patch watch.ring: the real write, then the session takes the wake (`how`: drain or unmark)."""
        real = watch.ring

        def ring(s):
            real(s)
            if how == "drain":
                inbox.drain([p for p, _x in inbox.pending(session)])
            elif how == "unmark":
                inbox.unmark(session)
        return mock.patch.object(watch, "ring", ring)

    def status(self, value):
        return mock.patch.object(deliver, "_agent_status", return_value=value)

    def test_an_idle_session_that_takes_the_wake_is_nudged_without_typing(self):
        report()
        watch.arm("main-acme", SID)
        with self.taking("main-acme", "unmark"), self.status("idle") as st:
            self.assertEqual(self.nudge(), (0, "nudged"))
        st.assert_called_once_with(SID, mock.ANY)
        self.assertEqual(self.orca_calls(), [])
        self.assertTrue(watch.wake_path("main-acme").is_file())
        self.assertIn("wake session=main-acme via=file state=nudged agent=idle", self.events())
        self.assertIn("nudge session=main-acme state=nudged pending=1", self.events())

    def test_an_unlisted_session_that_drains_is_nudged(self):
        report()
        watch.arm("main-acme", SID)
        with self.taking("main-acme", "drain"), self.status(None):
            self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertIn("wake session=main-acme via=file state=nudged agent=-", self.events())

    def test_a_busy_or_waiting_session_is_queued_and_nothing_is_typed(self):
        for status in ("busy", "waiting"):
            with self.subTest(status):
                report(unit=f"acme-{status}-20261021-1")
                watch.arm("main-acme", SID)
                inbox.unmark("main-acme")
                with self.status(status):
                    self.assertEqual(self.nudge(), (0, "queued"))
                self.assertEqual(self.orca_calls(), [])
                self.assertTrue(inbox.nudged("main-acme"))       # the mid-turn prompt removes it
                self.assertIsNotNone(watch.marker("main-acme"))
                self.assertIn(f"wake session=main-acme via=file state=queued agent={status}", self.events())

    def test_busy_is_asked_of_the_process_that_wrote_the_record(self):
        """_by_file passes the record's `at`, so a busy row of a later process (or of none: a record whose
        `at` cannot be read) never makes a wake `queued` (AgentStatusTest)."""
        from orche import units, util
        report()
        watch.arm("main-acme", SID)
        at = units.parse_iso(watch.marker("main-acme")["at"]).timestamp()
        with self.taking("main-acme", "unmark"), self.status("idle") as st:
            self.assertEqual(self.nudge(), (0, "nudged"))
        st.assert_called_once_with(SID, at)
        util.write_json(inbox.box("main-acme") / watch.WATCH, {"session_id": SID, "at": "garbled"})
        report(unit="acme-x-20261021-2")
        with self.taking("main-acme", "unmark"), self.status("idle") as st:
            self.assertEqual(self.nudge(), (0, "nudged"))
        st.assert_called_once_with(SID, 0.0)

    def test_no_evidence_drops_the_record_and_types_as_before(self):
        report()
        watch.arm("main-acme", SID)
        with self.status("idle"):
            self.assertEqual(self.nudge(), (0, "nudged"))
        self.assertIsNone(watch.marker("main-acme"))              # the next SessionStart writes it again
        self.assertEqual(self.calls()[-1], ["terminal", "send"])
        self.assertIn("wake session=main-acme via=typing reason=no-evidence agent=idle", self.events())

    def test_no_record_types_as_today_and_logs_the_decision(self):
        report()
        with self.status("idle") as st:
            self.assertEqual(self.nudge(), (0, "nudged"))
        st.assert_not_called()
        self.assertEqual(self.calls(), [["terminal", "list"], ["terminal", "wait"], ["terminal", "read"],
                                        ["terminal", "send"]])
        self.assertFalse(watch.wake_path("main-acme").exists())
        self.assertIn("wake session=main-acme via=typing reason=no-watch", self.events())

    def test_a_dispatched_unit_wakes_by_file(self):
        b = self.dispatched()
        watch.arm("div-acme-build", SID)
        with self.taking("div-acme-build", "drain"), self.status("idle"):
            self.assertEqual(deliver.deliver_unit(registry.load(), b.unit), (0, "nudged"))
        self.assertEqual(self.orca_calls(), [])
        self.assertIn("wake session=div-acme-build via=file state=nudged", self.events())
        b2 = self.dispatched()
        with self.status("busy"):
            self.assertEqual(deliver.deliver_unit(registry.load(), b2.unit), (0, "queued"))
        self.assertEqual(self.orca_calls(), [])

    def test_a_dispatched_unit_without_evidence_is_typed(self):
        b = self.dispatched()
        watch.arm("div-acme-build", SID)
        with self.status(None):
            self.assertEqual(deliver.deliver_unit(registry.load(), b.unit), (0, "nudged"))
        self.assertEqual(self.orca_calls()[-1][1:3], ["terminal", "send"])
        self.assertIn(f"[orche] inbox: {b.unit} rev 1", self.orca_calls()[-1])
        self.assertIsNone(watch.marker("div-acme-build"))


class AgentStatusTest(HookCase):
    def listing(self, out, code=0):
        self.addCleanup(setattr, shell, "RUNNER", shell.RUNNER)
        shell.RUNNER = lambda argv, *, timeout, input: shell.Result(code, out, "", argv)

    def test_busy_or_waiting_wins_over_an_idle_twin_row(self):
        rows = [{"sessionId": SID, "status": "idle"}, {"sessionId": SID, "status": "busy"},
                {"sessionId": "other", "status": "waiting"}]
        self.listing(json.dumps(rows))
        self.assertEqual(deliver._agent_status(SID), "busy")
        self.assertIsNone(deliver._agent_status("absent"))

    def test_only_a_process_that_armed_the_record_counts(self):
        """A row whose process started after the WATCH record was written (the same session resumed somewhere
        else, which never armed it) is not this pane's: its `busy` is not trusted."""
        armed = 1_800_000_000.0
        rows = [{"sessionId": SID, "status": "busy", "startedAt": int((armed + 60) * 1000)},
                {"sessionId": SID, "status": "idle", "startedAt": int((armed - 600) * 1000)}]
        self.listing(json.dumps(rows))
        self.assertEqual(deliver._agent_status(SID, armed), "idle")
        self.assertEqual(deliver._agent_status(SID, armed + 61), "busy")
        self.assertEqual(deliver._agent_status(SID, armed + 59 - deliver.ARMED_SLACK_S), "idle")
        self.listing(json.dumps([{"sessionId": SID, "status": "busy"}]))      # no startedAt: as listed
        self.assertEqual(deliver._agent_status(SID, armed), "busy")

    def test_a_failed_or_garbled_call_is_none(self):
        self.listing("", code=124)
        self.assertIsNone(deliver._agent_status(SID))
        self.listing("{not json")
        self.assertIsNone(deliver._agent_status(SID))


class RearmTest(HookCase):
    def setUp(self):
        super().setUp()
        (self.home / "config.json").write_text(json.dumps({"delivery": "pane", "inbox": {"wake": True}}))
        patcher = mock.patch.object(inbox, "wake")
        self.wake = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_watching_session_is_woken_by_the_nudger_never_by_a_bare_write(self):
        """The nudger writes the wake file and types when nothing takes it (deliver._by_file): a rearm with a
        dead watch still ends as a typed wake, and leaves no marker that would hold later wakes back."""
        report()
        watch.arm("main-acme", SID)
        inbox.rearm("main-acme")
        self.wake.assert_called_once_with("main-acme", False)
        self.assertFalse(watch.wake_path("main-acme").exists())
        self.assertFalse(inbox.nudged("main-acme"))

    def test_unread_or_no_record_still_starts_the_nudger(self):
        report()
        inbox.rearm("main-acme")
        self.wake.assert_called_once_with("main-acme", False)
        watch.arm("main-acme", SID)
        inbox.rearm("main-acme", unread=True)
        self.wake.assert_called_with("main-acme", True)

    def test_nothing_waking_pending_starts_nothing(self):
        report(status="info", via=None)
        watch.arm("main-acme", SID)
        inbox.rearm("main-acme")
        self.wake.assert_not_called()


class WrappedPromptTest(HookCase):
    """A wake the hook delivered is Orche's prompt, like the typed line; inside a running turn it keeps
    that turn's origin."""

    def prompt(self, text, prompt_id):
        (self.home / "sessions").mkdir(exist_ok=True)
        (self.home / "sessions" / "sess-main-0001.standing_seen").write_text("x")
        return self.run_hook("report_inject.py", self.payload("prompt.json", prompt=text, prompt_id=prompt_id),
                             ORCHE_SESSION="main-acme")

    def test_recognised_by_orches_own_line_not_the_wrapper(self):
        for line in (inbox.WAKE_TEXT, "[orche] inbox: acme-a-20261021-1 rev 3"):
            self.assertTrue(inbox.orche_prompt(WRAPPED.format(line=line)))
            self.assertTrue(inbox.orche_prompt(f"<another-wrapper>\nhook said: {line}\n</another-wrapper>"))
        self.assertTrue(inbox.orche_prompt(inbox.WAKE_TEXT))
        old = WRAPPED.format(line=inbox.WAKE_TEXT).replace("\n\n</", "\n</")   # without the blank line
        self.assertTrue(inbox.orche_prompt(old))
        for owner in ("what about the pricing page copy?", "please mention [orche] inbox: new reports later on",
                      WRAPPED.format(line="tests failed"),
                      "why did this stall? the log:\n12:01 nudge ok\n[orche] inbox: new reports",
                      "the log:\n[orche] inbox: acme-a-20261021-1 rev 3\n",
                      WRAPPED.format(line=inbox.WAKE_TEXT) + "\nand what does this mean?",
                      "see this:\n[orche] inbox: new reports\n</system-reminder>"):
            self.assertFalse(inbox.orche_prompt(owner), owner)

    def test_a_wrapped_wake_stamps_no_owner_seen_and_drains_as_orches_turn(self):
        report()
        res = self.prompt(WRAPPED.format(line=inbox.WAKE_TEXT), "p-1")
        self.assertEqual(res.returncode, 0)
        self.assertIn("## Inbox (1)", res.stdout)
        self.assertIn(inbox.REPORT_LINE, res.stdout)
        self.assertFalse((self.home / "flow" / "acme" / "owner-seen").exists())
        self.assertIs(lineage.read_turn("main-acme")["owner"], False)
        self.assertEqual(inbox.pending("main-acme"), [])
        self.prompt("what about the pricing page copy?", "p-2")
        self.assertTrue((self.home / "flow" / "acme" / "owner-seen").exists())   # the owner's own prompt does

    def test_a_mid_turn_wake_keeps_the_owners_turn(self):
        self.prompt("what about the pricing page copy?", "p-1")
        self.assertIs(lineage.read_turn("main-acme")["owner"], True)
        at = lineage.read_turn("main-acme")["at"]
        report(lineage="L9")
        res = self.prompt(WRAPPED.format(line=inbox.WAKE_TEXT), "p-1")          # same prompt_id: mid-turn
        self.assertIn("## Inbox (1)", res.stdout)
        self.assertIn(inbox.OWNER_LINE, res.stdout)                              # still the owner's turn
        self.assertEqual(inbox.pending("main-acme"), [])
        turn = lineage.read_turn("main-acme")
        self.assertEqual((turn["owner"], turn["at"], turn["lineages"]), (True, at, ["L9"]))

    def test_a_mid_turn_wake_keeps_orches_turn(self):
        report(unit="acme-a-20261021-1")
        self.prompt(WRAPPED.format(line=inbox.WAKE_TEXT), "p-7")
        report(unit="acme-b-20261021-2")
        res = self.prompt(WRAPPED.format(line=inbox.WAKE_TEXT), "p-7")
        self.assertIn(inbox.REPORT_LINE, res.stdout)
        self.assertIs(lineage.read_turn("main-acme")["owner"], False)
        self.prompt(WRAPPED.format(line=inbox.WAKE_TEXT), "p-8")                 # a new turn: a new record
        self.assertIs(lineage.read_turn("main-acme")["owner"], False)
