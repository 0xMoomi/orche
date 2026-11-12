"""Staff errands, Main's side (WP-3): Staff and Main items are never held, Main's turn record carries the turn's
errand lineages and `staff_only`, a handover's lineage takes Main's new briefs in an Orche turn, and the Stop hook
forwards Main's reply to Staff (and leaves Main unmarked when the turn was Staff's alone)."""
import json
import os
import time
from unittest import mock

import test_errand
import test_runner
from orche import brief, dispatch, errand, inbox, registry, roster
from orche.flow import lineage, owner

STAFF, MAIN = test_errand.STAFF, test_errand.MAIN
WOKEN = f"<x>\n{inbox.WAKE_TEXT}\n</x>"


class MainCase(test_errand.ErrandCase):
    def handover(self, goal="Fix the typo on the pricing page"):
        """A direct handover from Staff (owner's turn in Staff): the errand unit, its request in Main's inbox."""
        return test_errand.errand.hand_over(registry.load(), STAFF, goal=goal, context="Owner's words: please")

    def build_unit(self, goal="Other work"):
        reg = registry.load()
        return brief.write(brief.Block("brief", {"TO": "div-acme-build", "GOAL": goal, "SCOPE": "x", "CONTEXT": "x",
                                                 "ACCEPT": "x", "VERIFY": "x", "TIMEBOX": "10m", "FORBID": "x",
                                                 "REPORT": "x", "TIER": "standard"}),
                           roster.Roster(reg).member("div-acme-build"), MAIN, "", timeout=0).unit

    def post(self, unit, frm, status, **extra):
        return inbox.write(inbox.make(unit, frm, MAIN, status, summary=f"{status} from {frm}", **extra))

    def prompt(self, text=WOKEN, prompt_id="p1", session=MAIN):
        r = roster.Roster(registry.load())
        who = r.identify(str(self.repo), env={"ORCHE_SESSION": session})
        return inbox.handle_prompt({"prompt": text, "session_id": "s1", "prompt_id": prompt_id},
                                   time.monotonic(), r, who)

    def turn(self, session=MAIN):
        return lineage.read_turn(session)

    def stop(self, text, session=MAIN, **env):
        """dispatch.handle_stop in-process: (code, rearm's unread flag, owner.closing called)."""
        rearm, closing = [], []
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": session, **env}), \
                mock.patch.object(inbox, "rearm", side_effect=lambda s, unread=False: rearm.append(unread)), \
                mock.patch.object(owner, "closing", side_effect=lambda *a, **k: closing.append(a)):
            code, _msg = dispatch.handle_stop({"last_assistant_message": text, "cwd": str(self.repo),
                                               "session_id": "s1"})
        return code, rearm[-1] if rearm else None, bool(closing)


class HeldTest(MainCase):
    def test_staff_and_main_items_are_never_held(self):
        for via in ("staff", "main"):
            self.assertFalse(inbox.held({"status": "info", "via": via}))
        self.assertTrue(inbox.held({"status": "info", "from": "orche"}))
        self.assertFalse(inbox.held({"status": "info", "via": "runner"}))

    def test_a_staff_note_shows_in_full_at_any_prompt(self):
        inbox.write(inbox.make("adhoc-staff-acme-20261112", STAFF, MAIN, "info", summary="The owner prefers blue",
                               via="staff", quiet=True))
        text = self.prompt("what next?")
        self.assertIn("The owner prefers blue", text)
        self.assertNotIn("held", text)
        self.assertEqual(self.inbox(MAIN), [])

    def test_reply_md_is_inlined(self):
        unit = self.start()
        reply = self.units_dir() / unit / "REPLY.md"
        reply.write_text("Build is changing the copy.\nRUNNING: build is on it\n")
        ptr = inbox.make(unit, MAIN, STAFF, "info", report_path=str(reply), summary="Build is", via="main")
        text, shown = inbox.fit([(reply, ptr)])
        self.assertIn("RUNNING: build is on it", text)
        self.assertEqual(len(shown), 1)


class TurnRecordTest(MainCase):
    def test_a_handover_alone_is_staff_only(self):
        unit = self.handover()
        text = self.prompt()
        self.assertIn("The owner asks Main (via Staff)", text)
        turn = self.turn()
        self.assertEqual((turn["owner"], turn["errands"], turn["staff_only"]), (False, {unit: "handover"}, True))
        self.assertEqual(turn["lineages"], [unit])

    def test_mixed_items_are_not_staff_only(self):
        unit = self.handover()
        other = self.build_unit()
        self.post(other, "div-acme-build", "done")          # no lineage field: the brief names it
        self.prompt()
        turn = self.turn()
        self.assertEqual((turn["errands"], turn["staff_only"]), ({unit: "handover"}, False))
        self.assertEqual(sorted(turn["lineages"]), sorted([unit, other]))

    def test_a_forwarded_result_is_no_errand_item(self):
        unit = self.start()
        self.post(unit, STAFF, "info", via="staff", forwarded="undelivered", lineage=unit)
        self.prompt()
        self.assertEqual((self.turn()["errands"], self.turn()["staff_only"]), ({}, False))

    def test_a_close_and_an_owner_turn(self):
        unit = self.handover()
        self.post(unit, "orche", "done", lineage=unit, lineage_closed=True)
        self.prompt()
        self.assertEqual(self.turn()["errands"], {unit: "close"})
        self.assertTrue(self.turn()["staff_only"])
        self.handover("Another request")
        self.prompt("go on", prompt_id="p2")                # the owner's own turn: never staff_only
        self.assertEqual((self.turn()["owner"], self.turn()["staff_only"]), (True, False))

    def test_a_mid_turn_delivery_merges(self):
        unit = self.handover()
        self.prompt()
        self.post(unit, "orche", "done", lineage=unit, lineage_closed=True)
        self.prompt(prompt_id="p1")                         # same prompt id: inside the running turn
        turn = self.turn()
        self.assertEqual((turn["owner"], turn["errands"], turn["staff_only"]), (False, {unit: "close"}, True))
        self.prompt(prompt_id="p1")                         # nothing shown: the record stays
        self.assertTrue(self.turn()["staff_only"])
        self.post(self.build_unit(), "div-acme-build", "done")
        self.prompt(prompt_id="p1")
        self.assertEqual((self.turn()["errands"], self.turn()["staff_only"]), ({unit: "close"}, False))

    def test_an_owner_turn_keeps_its_owner_mid_turn(self):
        self.prompt("hello")
        unit = self.handover()
        self.prompt(prompt_id="p1")
        turn = self.turn()
        self.assertEqual((turn["owner"], turn["errands"], turn["staff_only"]), (True, {unit: "handover"}, False))

    def test_staff_write_turn_positional_stays_valid(self):
        lineage.write_turn(STAFF, False, [])
        self.assertEqual((self.turn(STAFF)["owner"], self.turn(STAFF)["errands"], self.turn(STAFF)["staff_only"]),
                         (False, {}, False))


class HandoverPromptTest(MainCase):
    """The live round trip: what Main's prompt hook shows for a Staff handover, then the real close."""

    def lineage_lines(self, text, unit):
        return [ln for ln in text.splitlines() if ln.startswith(f"[orche] lineage {unit}:")]

    def test_a_handover_reads_open_and_carries_its_order(self):
        unit = self.handover()
        text = self.prompt()
        self.assertIn("The owner asks Main (via Staff)", text)
        self.assertFalse([ln for ln in text.splitlines() if "closed" in ln and unit in ln])
        self.assertEqual(self.lineage_lines(text, unit), [])
        line = inbox.HANDOVER_LINE.format(staff=STAFF)
        self.assertIn(line + " " + inbox.CLOSING, text)
        self.assertNotIn(inbox.REPORT_LINE, text)            # a handover-only turn: its line stands in
        self.assertNotIn("ledger_row", text)
        self.assertLess(text.index("## Inbox"), text.index(line))
        self.assertTrue(text.rstrip().endswith(inbox.CLOSING))
        self.assertEqual(lineage.line(registry.load(), "acme", unit),
                         f"[orche] lineage {unit}: open; the owner's request handed over by his Staff waits on "
                         f"your units")

        # Main answers with a unit: it joins the lineage, which stays open until that unit ends, then closes.
        r = roster.Roster(registry.load())
        with mock.patch.dict(os.environ, {dispatch.SANDBOX: "seatbelt"}):   # the run waits for the heartbeat
            o = dispatch.relay_outcome(r, [test_runner.BODY.format(to="div-acme-build", tier="standard")],
                                       r.member(MAIN), deadline=time.monotonic() + 3)
        other = o.done[0]["unit"]
        self.assertEqual(brief.find(registry.load(), other, project="acme").lineage, unit)
        self.assertIn("1 of 2 units running; waiting on", lineage.line(registry.load(), "acme", unit))
        self.stop("Sent build the typo fix.\nRUNNING: build is on it")
        inbox.post(inbox.make(other, "div-acme-build", MAIN, "done", brief_rev=1, summary="typo fixed",
                              lineage=unit))
        text = self.prompt(prompt_id="p2")
        [closed] = self.lineage_lines(text, unit)
        self.assertIn("closed", closed)
        self.assertNotIn(line, text)
        self.assertIn(inbox.REPORT_LINE, text)
        self.assertEqual(self.turn()["errands"], {unit: "close"})
        self.stop("The typo is fixed.\nDONE: typo fixed")
        replies = [p["status"] for p in self.staff_items() if p.get("via") == "main"]
        self.assertEqual(sorted(replies), ["done", "info"])   # the handover turn's RUNNING, the close's DONE

    def test_a_handover_with_other_reports_keeps_the_report_line(self):
        unit = self.handover()
        self.post(self.build_unit(), "div-acme-build", "done")
        text = self.prompt()
        line = inbox.HANDOVER_LINE.format(staff=STAFF)
        self.assertIn(line + "\n", text)
        self.assertNotIn(line + " " + inbox.CLOSING, text)
        self.assertTrue(text.rstrip().endswith(inbox.REPORT_LINE))
        self.assertLess(text.index(line), text.index(inbox.REPORT_LINE))
        self.assertIn("ledger_row no", text)                  # the ordinary report keeps its pointer line
        self.assertEqual(self.lineage_lines(text, unit), [])

    def test_in_the_owners_turn_his_request_comes_first(self):
        self.prompt("hello")
        self.handover()
        text = self.prompt(prompt_id="p1")
        cut = inbox.HANDOVER_LINE.format(staff=STAFF).split(" Your final message")[0]
        self.assertTrue(text.rstrip().endswith(cut + " " + inbox.HANDOVER_OWNER))
        self.assertLess(text.index(inbox.OWNER_LINE), text.index(cut))
        self.assertNotIn("do not ask him to come to this window", text)

    def test_a_cross_project_request_shows_no_report_fields(self):
        inbox.write(inbox.make("adhoc-main-other-20261112", "main-other", MAIN, "external-request",
                               summary="Please expose the API"))
        text = self.prompt()
        self.assertIn("Please expose the API", text)
        self.assertNotIn("ledger_row", text)
        self.assertNotIn(inbox.HANDOVER_LINE.format(staff=STAFF), text)

    def staff_items(self):
        return [json.loads(p.read_text()) for p in self.inbox(STAFF)]


class ForwardStatusTest(MainCase):
    def test_the_status_follows_the_closing_tag(self):
        unit = self.handover()
        b = brief.find(registry.load(), unit, project="acme")
        turn = {"owner": False, "errands": {unit: "handover"}}
        for text, want in (("Pick one.\nOWNER: blue or red; default blue", "question"),
                           ("Changed the copy myself.\nDONE: copy changed", "done"),
                           ("Build runs.\nRUNNING: build", "info"), ("No tag here.", "info")):
            self.assertEqual(errand.forward_reply(registry.load(), MAIN, text, turn), [unit])
            self.assertEqual(self.newest(STAFF)["status"], want)
            self.assertEqual(errand.state(registry.load(), b), "handed")   # replies never change the state

    def test_a_close_without_done_is_no_done(self):
        unit = self.handover()
        turn = {"owner": False, "errands": {unit: "close"}}
        errand.forward_reply(registry.load(), MAIN, "One more step.\nRUNNING: build", turn)
        self.assertEqual(self.newest(STAFF)["status"], "info")


class AssignTest(MainCase):
    def test_a_handover_takes_main_briefs_in_an_orche_turn_only(self):
        unit = self.handover()
        other = self.build_unit()
        main = roster.Roster(registry.load()).member(MAIN)
        lineage.write_turn(MAIN, False, [other, unit], errands={unit: "handover"}, staff_only=False)
        self.assertEqual(lineage.assign(registry.load(), main), (unit, None))
        lineage.write_turn(MAIN, True, [unit], errands={unit: "handover"})
        self.assertEqual(lineage.assign(registry.load(), main), (None, None))
        lineage.write_turn(MAIN, False, [other, unit], errands={unit: "item"})
        self.assertEqual(lineage.assign(registry.load(), main), (None, None))

    def test_a_brief_main_sends_joins_the_errand(self):
        unit = self.handover()
        self.prompt()
        r = roster.Roster(registry.load())
        with mock.patch.dict(os.environ, {dispatch.SANDBOX: "seatbelt"}):   # the run waits for the heartbeat
            o = dispatch.relay_outcome(r, [test_runner.BODY.format(to="div-acme-build", tier="standard")],
                                       r.member(MAIN), deadline=time.monotonic() + 3)
        [done] = o.done
        self.assertEqual(brief.find(registry.load(), done["unit"], project="acme").lineage, unit)


class StopTest(MainCase):
    def staff_items(self):
        return [json.loads(p.read_text()) for p in self.inbox(STAFF)]

    def test_a_staff_only_turn_forwards_and_marks_nothing(self):
        unit = self.handover()
        self.prompt()
        code, unread, closed = self.stop("Sent build the copy change.\nRUNNING: build is on it")
        self.assertEqual((code, unread, closed), (0, False, False))
        [ptr] = [p for p in self.staff_items() if p.get("via") == "main"]
        self.assertEqual((ptr["status"], ptr["unit"], ptr["from"]), ("info", unit, MAIN))
        self.assertTrue((self.units_dir() / unit / "REPLY.md").is_file())
        self.assertIn("main reply project=acme by=orche tag=RUNNING lines=2 errand=1", self.events())

    def test_statuses_follow_the_closing_tag_and_the_close(self):
        unit = self.handover()
        self.prompt()
        self.stop("Which color?\nOWNER: blue or red; default blue")
        self.post(unit, "orche", "done", lineage=unit, lineage_closed=True)
        self.prompt(prompt_id="p2")
        self.stop("Merged PR #3.\nDONE: merged")
        statuses = [p["status"] for p in sorted(self.staff_items(), key=lambda p: p["at"]) if p.get("via") == "main"]
        self.assertEqual(sorted(statuses), ["done", "question"])

    def test_a_mixed_orche_turn_forwards_and_stays_normal(self):
        self.handover()
        self.post(self.build_unit(), "div-acme-build", "done")
        self.prompt()
        code, unread, closed = self.stop("Two things.\nRUNNING: build runs")
        self.assertEqual((code, unread, closed), (0, True, True))
        self.assertEqual(len([p for p in self.staff_items() if p.get("via") == "main"]), 1)
        self.assertNotIn("errand=1", self.events())

    def test_an_owner_turn_forwards_nothing(self):
        self.prompt("hello")
        self.handover()
        self.prompt(prompt_id="p1")
        code, unread, closed = self.stop("Done both.\nRUNNING: build runs")
        self.assertEqual((code, unread, closed), (0, False, True))
        self.assertEqual([p for p in self.staff_items() if p.get("via") == "main"], [])

    def test_an_asleep_staff_gets_nothing_and_main_is_normal(self):
        self.handover()
        self.prompt()
        registry.update(lambda r: r["projects"]["acme"].__setitem__("staff", {"status": "asleep"}))
        code, unread, closed = self.stop("Sent build.\nRUNNING: build is on it")
        self.assertEqual((code, unread, closed), (0, True, True))
        self.assertEqual([p for p in self.staff_items() if p.get("via") == "main"], [])

    def test_a_staff_turn_orche_started_marks_staff_unread(self):
        lineage.write_turn(STAFF, False, [])
        self.assertEqual(self.stop("The answer is 42.", session=STAFF)[:2], (0, True))
        lineage.write_turn(STAFF, True, [])
        self.assertEqual(self.stop("The answer is 42.", session=STAFF)[:2], (0, False))

    def test_stop_failure_marks_a_staff_orche_turn(self):
        lineage.write_turn(STAFF, False, [])
        seen = []
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": STAFF}), \
                mock.patch.object(inbox, "rearm", side_effect=lambda s, unread=False: seen.append((s, unread))):
            dispatch.handle_stop_failure({"cwd": str(self.repo)})
        self.assertEqual(seen, [(STAFF, True)])


class RunnerStopTest(MainCase):
    """A headless run's Stop (ORCHE_RUNNER) behaves as in 0.20.1: nothing errands added runs for it."""

    def test_an_errand_run_never_marks_staff_unread(self):
        lineage.write_turn(STAFF, False, [])                # the Staff pane's last turn was Orche's
        self.assertEqual(self.stop("STATUS: done\nthe answer", session=STAFF, ORCHE_RUNNER="1")[:2], (0, False))
        seen = []
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": STAFF, "ORCHE_RUNNER": "1"}), \
                mock.patch.object(inbox, "rearm", side_effect=lambda s, unread=False: seen.append((s, unread))):
            dispatch.handle_stop_failure({"cwd": str(self.repo)})
        self.assertEqual(seen, [(STAFF, False)])

    def test_a_run_aimed_at_main_forwards_nothing(self):
        unit = self.handover()
        self.prompt()                                       # Main's turn record names the errand
        with mock.patch.object(errand, "forward_reply") as fwd:
            code, unread, closed = self.stop("STATUS: done\nrunner text", ORCHE_RUNNER="1")
        fwd.assert_not_called()
        self.assertEqual((code, unread, closed), (0, True, True))     # 0.20.1: Main's Orche turn, unread
        self.assertEqual([p for p in self.staff_items() if p.get("via") == "main"], [])
        self.assertFalse((self.units_dir() / unit / "REPLY.md").exists())
        self.assertNotIn("errand=1", self.events())

    def staff_items(self):
        return [json.loads(p.read_text()) for p in self.inbox(STAFF)]


class UnreachableStaffTest(MainCase):
    def test_mains_reply_comes_back_to_main_and_does_not_loop(self):
        unit = self.handover()
        self.prompt()
        self.assertEqual(self.stop("Which color?\nOWNER: blue or red; default blue")[:3], (0, False, False))
        n = errand.forward(registry.load(), "acme", inbox.pending(STAFF), "undelivered")
        self.assertEqual(n, 1)
        self.assertEqual(inbox.pending(STAFF), [])
        [back] = [p for _x, p in inbox.pending(MAIN) if p.get("forwarded")]
        self.assertEqual((back["unit"], back["status"], back["via"], back["forwarded"], back["lineage"]),
                         (unit, "info", "staff", "undelivered", unit))
        self.assertEqual(back["report_path"], "")
        self.assertTrue(back["summary"].startswith(f"Your reply about {unit} did not reach the owner"))
        self.assertIn(f"Your reply: {self.units_dir() / unit / 'REPLY.md'}", back["summary"])
        text = self.prompt(prompt_id="p2")
        self.assertIn(f"from {STAFF} · status info", text)              # Main gets its reply back to tell him
        self.assertIn("did not reach the owner", text)
        self.assertIn(f"{unit}/REPLY.md", text)
        self.assertEqual((self.turn()["errands"], self.turn()["staff_only"]), ({}, False))
        code, unread, closed = self.stop("The owner: which color? blue or red.\nOWNER: blue or red; default blue")
        self.assertEqual((code, unread, closed), (0, True, True))     # a normal turn: closing recorded, unread
        self.assertEqual(inbox.pending(STAFF), [])                    # nothing forwarded again: no loop
        self.assertEqual(errand.forward(registry.load(), "acme", inbox.pending(STAFF), "undelivered"), 0)


class JoinedStopTest(MainCase):
    def test_two_handovers_in_one_turn_mark_the_second_joined(self):
        a, b = self.handover("Fix typo A"), self.handover("Fix typo B")
        first, second = sorted([a, b])
        self.prompt()
        self.assertEqual(self.turn()["errands"], {a: "handover", b: "handover"})
        self.assertEqual(lineage.assign(registry.load(), roster.Roster(registry.load()).member(MAIN)), (first, None))
        self.stop("Sent both to build.\nRUNNING: build")
        self.assertEqual((self.units_dir() / second / errand.JOINED).read_text().strip(), first)
        self.post(first, "orche", "done", lineage=first, lineage_closed=True)
        self.prompt(prompt_id="p2")
        self.stop("Both fixed.\nDONE: merged")
        done = sorted(p["unit"] for p in (json.loads(x.read_text()) for x in self.inbox(STAFF))
                      if p.get("via") == "main" and p["status"] == "done")
        self.assertEqual(done, [first, second])
        self.assertFalse((self.units_dir() / second / errand.JOINED).exists())


class InterruptedTurnTest(MainCase):
    def test_an_errand_main_took_can_go_to_main_again(self):
        unit = self.handover()
        with self.assertRaises(errand.Refused):
            errand.hand_over(registry.load(), STAFF, unit=unit, context="again please")
        self.prompt()                                       # Main took it; its turn ended without a Stop
        self.assertEqual(errand.hand_over(registry.load(), STAFF, unit=unit, context="again please"), unit)
