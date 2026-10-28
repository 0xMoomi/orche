"""T10 — the owner's heartbeat (lib/orche/flow/owner.py `heartbeat`, run by `orche reconcile`): an open
ask is re-notified 30, 60, 120, 240 min after it, then never; quiet hours (a silent first, none inside, one
at their end); an owner prompt or the filled secrets answer it (Main hears once which unit to resume); the
10-minute fallback for a Main that did not tag; Main's closing line is Orca's to notify (ORCHE sends
none); a held notice or escalation is sent once; repeats; the gap boundary; the `owner:` status line; a dry run writes and sends nothing. A fake clock
throughout (`now` arguments, backdated mtimes); every osascript argv is recorded, none runs."""
import copy
import datetime as dt
import io
import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from orche import inbox, notify, project, reconcile, registry, shell, units, util
from orche.flow import guard, owner
from orche.secrets import store
from test_reconcile import T0 as RT0, WAKE, ReconcileCase
from test_registry import load_cli

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
P, MAIN = "acme-shop", "main-acme-shop"
UNIT, OTHER = "acme-shop-pay-20261020-1", "acme-shop-ux-20261020-2"
T0 = dt.datetime(2026, 10, 20, 12, 0).astimezone()          # local noon
NIGHT = dt.datetime(2026, 10, 20, 23, 10).astimezone()      # inside 23:00-08:00


def at(minutes, base=T0):
    return base + dt.timedelta(minutes=minutes)


def iso(t):
    return t.astimezone().isoformat(timespec="seconds")


def hm(t):
    return t.astimezone().strftime("%H:%M")


class BeatCase(support.TempHomeCase):
    """The sample registry, quiet hours off, notifications on macOS, Orca running (the fake runner answers
    `pgrep` with 0): every osascript argv is recorded."""

    def setUp(self):
        super().setUp()
        registry.save(copy.deepcopy(SAMPLE))
        self.reg = registry.load()
        self.config()
        self.calls = []

        def runner(argv, *, timeout, input, env=None):
            self.calls.append(list(argv))
            return shell.Result(0, "", "", list(argv))
        for obj, name, value in ((shell, "RUNNER", runner), (notify, "PLATFORM", "darwin")):
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, quiet=""):
        (self.home / "config.json").write_text(json.dumps(support.no_wake({"flow": {"quiet_hours": quiet}})))

    def notes(self):
        return [c for c in self.calls if c[0] == "osascript"]

    def bodies(self):
        return [c[-2] for c in self.notes()]

    def beat(self, when, dry_run=False):
        return owner.heartbeat(self.reg, when, dry_run=dry_run, keys={P})

    def shown_at(self, minutes, base=T0):
        """The passes, among `minutes` after `base`, that showed a notification."""
        out = []
        for m in minutes:
            before = len(self.notes())
            self.beat(at(m, base))
            out += [m] * (len(self.notes()) - before)
        return out

    def stamp(self, name, when):
        path = guard.folder(P) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (when.timestamp(), when.timestamp()))

    def report(self, status, when, unit=UNIT, frm="div-acme-shop-build", **extra):
        ptr = inbox.make(unit, frm, MAIN, status, summary="s", **extra)
        ptr["at"] = iso(when)
        inbox.write(ptr)

    def request(self, name, status, filled=None, asked=None):
        rid = f"20261020T115500-{name}"
        req = {"name": name, "envs": ["dev"], "project": P, "at": iso(asked or at(-5)), "status": status}
        util.write_json(store.requests_dir() / f"{rid}.json", dict(req, filled_at=iso(filled)) if filled else req)
        return rid

    def ask(self):
        return owner.read_ask(P)

    def sent(self):
        return json.loads((guard.folder(P) / owner.SENT).read_text())

    def events(self):
        path = self.home / "logs" / "events.log"
        return path.read_text() if path.exists() else ""

    def infos(self):
        return [p for _path, p in inbox.pending(MAIN) if p.get("from") == "orche"]


class RenotifyTest(BeatCase):
    def test_30_60_120_240_min_after_the_ask_then_none(self):
        owner.closing(P, "OWNER: pick a plan", now=T0)
        self.assertEqual(self.shown_at([5, 29, 30, 35, 59, 60, 61, 119, 120, 239, 240, 245, 480, 1440, 2880]),
                         [30, 60, 120, 240])
        self.assertEqual(self.bodies()[0], f"OWNER (asked {hm(T0)}): pick a plan")    # the first was Orca's
        self.assertEqual((self.sent()["n"], self.sent()["ask_last"]), (5, iso(at(240))))
        self.assertIsNotNone(owner.open_ask(P))                              # the owner: line stays
        self.assertIn("owner notify project=acme-shop kind=owner n=5 quiet=0\n", self.events())

    def test_a_slot_held_by_the_gap_goes_at_the_next_pass(self):
        owner.closing(P, "OWNER: pick a plan", now=T0)
        owner.announce(P, "stuck", "Main is closed", at(28))
        self.assertEqual(self.shown_at([30, 32, 33, 59, 60]), [33, 60])

    def test_the_first_reminder_comes_30_min_after_the_ask_with_no_first_notification(self):
        owner.closing(P, "OWNER: pick a plan", now=T0)
        self.assertEqual((self.notes(), self.sent()["n"]), ([], 1))              # Orca showed the first
        self.assertEqual(self.shown_at([1, 5, 29, 30, 60]), [30, 60])
        self.assertIn("owner recorded project=acme-shop kind=owner by=orca\n", self.events())


class FailedShowTest(BeatCase):
    """A failed show (non-zero exit, the 5 s timeout) is a try: the next waits for the next slot."""

    def failing(self):
        def runner(argv, *, timeout, input, env=None):
            self.calls.append(list(argv))
            return shell.Result(1, "", "err", list(argv))
        return mock.patch.object(shell, "RUNNER", runner)

    def test_an_ask_gets_five_tries_over_the_schedule_not_one_a_pass(self):
        with self.failing():
            owner.parked(P, UNIT, "not started: pick a plan", now=T0)
            got = self.shown_at(range(5, 3 * 1440, 5))
        self.assertEqual((len(self.notes()), got), (5, [30, 60, 120, 240]))
        self.assertEqual(self.events().count("reason=failed"), 5)

    def test_a_notice_waits_for_its_slots_and_still_expires_after_a_day(self):
        with self.failing():
            owner.announce(P, "stuck", "Main is closed", T0)
            got = self.shown_at(range(5, 2 * 1440, 5))
        self.assertEqual((len(self.notes()), got), (5, [30, 60, 120, 240]))
        self.assertIn("owner dropped project=acme-shop kind=stuck\n", self.events())


class QuietHoursTest(BeatCase):
    def test_a_silent_first_none_inside_one_at_the_end(self):
        self.config(quiet="23:00-08:00")
        owner.parked(P, UNIT, "approve the deploy", now=NIGHT)
        [first] = self.notes()
        self.assertEqual(first[4:7], [notify.TELL[0], "-e", notify.SCRIPT[1]])  # Orca runs (pgrep 0); no sound
        got = self.shown_at(range(5, 12 * 60 + 1, 5), base=NIGHT)            # every 5 min until 11:10
        self.assertEqual([hm(at(m, NIGHT)) for m in got], ["08:00"])
        self.assertIn('sound name "Glass"', self.notes()[-1][6])
        self.assertEqual(self.bodies()[-1], "OWNER (asked 23:10): approve the deploy")


class AnsweredTest(BeatCase):
    def test_an_owner_prompt_answers_it(self):
        owner.closing(P, "OWNER: pick a plan", now=T0)
        self.stamp("owner-seen", at(10))                                     # the prompt hook missed it
        self.assertEqual(self.shown_at([30, 60, 120, 240]), [])
        self.assertEqual(self.ask()["answered_at"], iso(at(30)))
        self.assertIn("owner answered project=acme-shop by=prompt after=30m\n", self.events())
        self.assertIsNone(owner.status(P, at(30)))

    def test_the_prompt_hook_answers_it_at_once(self):
        owner.closing(P, "OWNER: pick a plan", now=T0)
        self.stamp("owner-seen", at(1))
        self.assertEqual(owner.answer(P, at(1)), "prompt")
        self.assertIsNone(owner.answer(P, at(2)))
        self.assertEqual(self.shown_at([30, 60]), [])

    def test_a_filled_secret_answers_it_and_main_hears_once_which_unit_to_resume(self):
        self.report("blocked", at(-5), owner="secret STRIPE_KEY", brief_rev=2)
        owner.closing(P, "OWNER: secret STRIPE_KEY", now=T0)
        rid = self.request("STRIPE_KEY", "pending")
        self.beat(at(5))
        self.assertNotIn("answered_at", self.ask())
        self.request("STRIPE_KEY", "filled", filled=at(7))
        self.assertEqual(owner.secret_filled(P, "STRIPE_KEY", rid, at(7), iso(at(7))), [UNIT])
        self.assertEqual(self.ask()["answered_at"], iso(at(7)))
        self.assertIn("owner answered project=acme-shop by=secret after=7m\n", self.events())
        self.assertEqual(owner.secret_filled(P, "STRIPE_KEY", rid, at(8)), [])   # never twice
        self.assertEqual(self.shown_at([10, 30, 60]), [])                        # nor from the heartbeat
        [info] = self.infos()
        self.assertEqual((info["unit"], info["to"], info["status"], info["urgent"], info["brief_rev"]),
                         (UNIT, MAIN, "info", True, 2))
        self.assertEqual(info["summary"], f"owner step done: secret STRIPE_KEY filled at {hm(at(7))} — resume "
                                          f"{UNIT} (AMEND or orche run {UNIT})")
        self.assertIn(f"owner secret-filled project=acme-shop name=STRIPE_KEY unit={UNIT}\n", self.events())
        self.assertEqual(self.notes(), [])

    def test_a_secret_filled_during_mains_turn_answers_the_ask_at_once(self):
        self.request("GH_TOKEN", "filled", filled=at(-1))
        owner.closing(P, "OWNER: secret GH_TOKEN", now=T0)
        self.assertEqual((self.ask()["answered_at"], self.notes()), (iso(T0), []))

    def test_the_heartbeat_tells_main_when_the_dialog_died(self):
        self.report("question", at(-5), owner="secret GH_TOKEN")
        owner.closing(P, "RUNNING: waiting on the key", now=at(-4))
        self.request("GH_TOKEN", "filled", filled=at(1))
        rows = self.beat(at(5))
        self.assertEqual([(r["action"], r["target"]) for r in rows], [("resume", UNIT)])
        self.beat(at(10))
        self.assertEqual(len(self.infos()), 1)


class SecretResumeTest(BeatCase):
    """Review 4 (B1, N4): the lookup runs before any marker; only reports since the request (less a minute);
    every asking unit, each once per report; a step naming several secrets waits for all of them; a fill that
    resumes a unit is the owner's word (owner-seen, never moved back; not while paused)."""

    def marks(self):
        folder = guard.folder(P) / owner.RESUMED
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def resumes(self, when):
        return [r["target"] for r in self.beat(when) if r["action"] == "resume"]

    def test_a_fill_before_the_report_is_told_by_the_heartbeat_once(self):
        rid = self.request("STRIPE_KEY", "filled", filled=at(-3))
        self.assertEqual(owner.secret_filled(P, "STRIPE_KEY", rid, at(-3), iso(at(-3))), [])
        self.assertEqual(self.marks(), [])                                       # nobody asked yet: no marker
        self.report("blocked", at(-1), owner="secret STRIPE_KEY", brief_rev=2)  # the runner files after the fill
        self.assertEqual((self.resumes(at(5)), self.resumes(at(10))), ([UNIT], []))
        [info] = self.infos()
        self.assertEqual((info["unit"], info["brief_rev"], len(self.marks())), (UNIT, 2, 1))

    def test_an_old_report_from_another_unit_is_not_told(self):
        self.report("blocked", at(-3 * 1440), unit=OTHER, owner="secret STRIPE_KEY")
        inbox.drain([path for path, _ptr in inbox.pending(MAIN)])
        rid = self.request("STRIPE_KEY", "filled", filled=at(-3))
        self.assertEqual(owner.secret_filled(P, "STRIPE_KEY", rid, at(-3), iso(at(-3))), [])
        self.report("blocked", at(-1), owner="secret STRIPE_KEY")
        self.assertEqual(self.resumes(at(5)), [UNIT])
        self.assertEqual([i["unit"] for i in self.infos()], [UNIT])

    def test_two_units_on_one_merged_request_are_each_told_once(self):
        self.report("blocked", at(-4), unit=UNIT, owner="secret K_ONE")
        self.report("question", at(-3), unit=OTHER, owner="secret K_ONE")
        rid = self.request("K_ONE", "filled", filled=at(-1))
        self.assertEqual(owner.secret_filled(P, "K_ONE", rid, at(-1), iso(at(-1))), [UNIT, OTHER])
        self.assertEqual(sorted(i["unit"] for i in self.infos()), [UNIT, OTHER])
        self.assertEqual((owner.secret_filled(P, "K_ONE", rid, at(2), iso(at(-1))), self.resumes(at(5))), ([], []))
        self.assertEqual(len(self.infos()), 2)

    def test_a_step_naming_two_secrets_waits_for_both_and_tells_once(self):
        self.report("blocked", at(-4), owner="secret K_A, K_B")
        ra, rb = self.request("K_A", "filled", filled=at(1)), self.request("K_B", "pending")
        self.assertEqual((owner.secret_filled(P, "K_A", ra, at(1), iso(at(1))), self.resumes(at(5))), ([], []))
        self.assertEqual((self.infos(), self.marks()), ([], []))
        self.request("K_B", "filled", filled=at(6))
        self.assertEqual(owner.secret_filled(P, "K_B", rb, at(6), iso(at(6))), [UNIT])
        [info] = self.infos()
        self.assertEqual(info["summary"], f"owner step done: secret K_A, K_B filled at {hm(at(6))} — resume {UNIT} "
                                          f"(AMEND or orche run {UNIT})")
        self.assertEqual(self.resumes(at(10)), [])                               # K_A's request: told already

    def test_two_secrets_filled_before_the_report_tell_main_once(self):
        ra, rb = self.request("K_A", "filled", filled=at(-3)), self.request("K_B", "filled", filled=at(-2))
        for name, rid in (("K_A", ra), ("K_B", rb)):
            self.assertEqual(owner.secret_filled(P, name, rid, at(-2), iso(at(-2))), [])
        self.report("blocked", at(-1), owner="secret K_A, K_B")
        self.assertEqual((self.resumes(at(5)), self.resumes(at(10)), len(self.infos())), ([UNIT], [], 1))

    def test_a_fill_that_resumes_a_unit_is_the_owners_word(self):
        seen = guard.folder(P) / "owner-seen"
        self.report("blocked", at(-4), owner="secret STRIPE_KEY")
        rid = self.request("STRIPE_KEY", "filled", filled=at(2))
        guard.pause(P, "owner")
        self.assertEqual((owner.secret_filled(P, "STRIPE_KEY", rid, at(2), iso(at(2))), seen.exists()), ([], False))
        (guard.folder(P) / "pause.json").unlink()
        self.assertEqual(owner.secret_filled(P, "STRIPE_KEY", rid, at(2), iso(at(2))), [UNIT])
        self.assertEqual(seen.stat().st_mtime, at(2).timestamp())
        self.assertEqual(guard.window(P, at(3)), at(2))                          # G1–G5 count from the fill

    def test_owner_seen_never_moves_back(self):
        rid = self.request("STRIPE_KEY", "filled", filled=at(2))
        owner.secret_filled(P, "STRIPE_KEY", rid, at(2), iso(at(2)))
        self.assertFalse((guard.folder(P) / "owner-seen").exists())              # nothing resumed: no word
        self.stamp("owner-seen", at(3))
        self.report("blocked", at(4), owner="secret STRIPE_KEY")
        self.assertEqual(self.resumes(at(5)), [UNIT])
        self.assertEqual((guard.folder(P) / "owner-seen").stat().st_mtime, at(3).timestamp())


class FallbackTest(BeatCase):
    def test_at_10_min_once_per_cause(self):
        self.report("question", T0)
        self.assertEqual(self.shown_at([9]), [])
        self.assertEqual([r["target"] for r in self.beat(at(10))], [UNIT])
        self.assertEqual(self.bodies(), [f"OWNER: {UNIT}: question"])
        self.assertEqual(self.ask(), {"text": f"{UNIT}: question", "at": iso(at(10)), "kind": "fallback",
                                      "source": "heartbeat", "unit": UNIT})
        self.assertEqual(self.shown_at([15, 20, 25]), [])                        # once per cause
        self.report("blocked", at(26), unit=OTHER)                                # a new cause
        self.assertEqual(self.shown_at([30, 35, 36]), [36])
        self.assertEqual(self.bodies()[-1], f"OWNER: {OTHER}: blocked")

    def test_not_when_main_tagged_a_turn_since_or_the_owner_spoke(self):
        self.report("blocked", T0)
        owner.closing(P, "RUNNING: build retries the payment tests", now=at(3), turn_at=at(2))
        self.assertEqual(self.shown_at([10, 20]), [])
        self.report("question", at(30), unit=OTHER)
        self.stamp("owner-seen", at(31))
        self.assertEqual(self.shown_at([40, 50]), [])
        self.assertIsNone(self.ask())

    def test_an_untagged_turn_does_not_count(self):
        self.report("blocked", T0)
        owner.closing(P, "Looked at it; back soon.", now=at(3))
        self.assertEqual(self.shown_at([10]), [10])

    def test_a_report_that_landed_mid_turn_is_the_next_turns(self):
        self.report("blocked", T0, owner="approve the paid plan")              # lands while Main's turn runs
        owner.closing(P, "RUNNING: build runs the tests", now=at(1), turn_at=at(-2))     # that turn never read it
        owner.closing(P, "Read the blocked report; will wait.", now=at(3), turn_at=at(2))
        self.assertEqual(self.shown_at([9, 10]), [10])
        self.assertEqual(self.ask()["text"], f"{UNIT}: approve the paid plan")

    def test_a_held_fallback_is_a_held_row(self):
        self.report("blocked", T0)
        owner.announce(P, "stuck", "Main is closed", at(8))
        [row] = self.beat(at(10))
        self.assertEqual((row["action"], row["line"]), ("held", f"hold the owner's notification: {UNIT} waits in "
                                                                  f"main-{P} (fallback: Main did not tag it; a later "
                                                                  f"pass sends it)"))
        self.assertEqual(self.shown_at([11, 15]), [15])

    def test_reports_written_before_the_window_are_not_read(self):
        self.report("blocked", T0)
        [(path, _ptr)] = inbox.pending(MAIN)
        old = at(-2 * 1440).timestamp()
        os.utime(path, (old, old))                       # the file predates the day: never opened
        self.assertEqual(self.shown_at([10]), [])

    def test_a_failed_report_or_a_lineage_close_is_one_notice_not_an_ask(self):
        self.report("failed", T0)
        self.assertEqual(self.shown_at([10, 15, 40, 70, 130]), [10])
        self.assertEqual((self.bodies(), self.ask()), ([f"{UNIT}: failed"], None))
        self.report("done", at(100), unit="acme-shop-lin-20261020-3", frm="orche",
                    lineage="acme-shop-lin-20261020-3", lineage_closed=True)
        self.assertEqual(self.shown_at([105, 110, 140]), [110])
        self.assertEqual(self.bodies()[-1], "acme-shop-lin-20261020-3: lineage closed")


class HeldTest(BeatCase):
    def test_a_gap_held_notice_is_sent_once_at_the_next_pass(self):
        owner.parked(P, UNIT, "not started: pick a plan", now=T0)
        owner.close(P, at(1))
        owner.announce(P, "stuck", "Main is closed", at(2))                       # inside the 300 s gap
        self.assertEqual(self.bodies(), ["OWNER: not started: pick a plan"])
        self.assertEqual(self.shown_at([4, 5, 10, 35, 65, 125, 245]), [5])
        self.assertEqual(self.bodies()[-1], "Main is closed")
        stuck = json.loads((guard.folder(P) / "stuck.json").read_text())
        self.assertEqual((stuck["at"], self.sent()["shown"]), (iso(at(2)), {"stuck": stuck["id"]}))

    def test_a_lock_held_notice_is_not_lost(self):
        with mock.patch.object(owner, "LOCK_WAIT_S", 0.0), owner._locked(guard.folder(P) / owner.NOTIFY_LOCK):
            owner.announce(P, "stuck", "Main is closed", T0)
        self.assertEqual(self.notes(), [])
        self.assertIn("owner held project=acme-shop kind=stuck reason=lock\n", self.events())
        self.assertEqual(self.shown_at([1, 6]), [1])

    def test_mains_done_is_orcas_to_notify_and_never_flushed(self):
        owner.closing(P, "DONE: shipped", now=T0)
        self.assertEqual(self.shown_at([0.5, 5, 10, 60]), [])
        done = json.loads((guard.folder(P) / "done.json").read_text())
        self.assertEqual(self.sent(), {"shown": {"done": done["id"]}})          # no gap: ORCHE showed nothing
        self.assertIn("owner recorded project=acme-shop kind=done by=orca\n", self.events())

    def test_a_held_notice_is_dropped_once_the_owner_prompted_after_it(self):
        owner.parked(P, UNIT, "not started: pick a plan", now=T0)
        owner.close(P, at(1))
        owner.announce(P, "stuck", "Main is closed", at(2))                       # held by the gap
        self.stamp("owner-seen", at(3))                                           # the owner wrote to Main again
        self.assertEqual(self.shown_at([10, 20]), [])
        self.assertIn("owner dropped project=acme-shop kind=stuck\n", self.events())

    def test_an_escalation_follows_the_same_path_and_rules(self):
        reconcile._tell_owner(P, "main-acme-shop is closed: orche wake main-acme-shop", T0)
        reconcile._tell_owner(P, "Main has 3 report(s) unread", T0)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.shown_at([4, 5, 10]), [5])
        self.assertEqual(self.bodies()[-1], "Main has 3 report(s) unread")
        self.assertIn("owner notify project=acme-shop kind=stuck n=1", self.events())


class RepeatTest(BeatCase):
    def test_the_same_owner_line_again_is_not_a_new_ask(self):
        for s in (0, 301, 602):
            owner.closing(P, "OWNER: secret STRIPE_KEY", now=T0 + dt.timedelta(seconds=s))
        self.assertEqual((len(self.notes()), self.ask()["at"], self.sent()["n"]), (0, iso(T0), 1))
        self.assertIn("owner repeat project=acme-shop tag=OWNER\n", self.events())
        self.assertEqual(self.shown_at([29, 30]), [30])                          # the schedule runs from the first

    def test_the_same_done_again_is_one_notice_until_an_ask_comes_between(self):
        owner.closing(P, "DONE: shipped v2", now=T0)
        owner.closing(P, "DONE: shipped v2", now=at(6))
        self.assertEqual(self.shown_at([10, 20]), [])
        self.assertIn("owner repeat project=acme-shop tag=DONE\n", self.events())
        owner.closing(P, "OWNER: next step?", now=at(30))
        owner.closing(P, "DONE: shipped v2", now=at(40))
        self.assertEqual(self.events().count("owner recorded project=acme-shop kind=done by=orca\n"), 2)
        self.assertEqual(self.notes(), [])


class BoundaryTest(BeatCase):
    def test_gap_299_s_holds_301_s_shows(self):
        for s, text in ((0, "a"), (299, "b"), (301, "c")):
            owner.parked(P, UNIT, text, now=T0 + dt.timedelta(seconds=s))
        self.assertEqual(self.bodies(), ["OWNER: a", "OWNER: c"])


class OwnerLineTest(BeatCase):
    def line(self, when):
        return project.owner_lines(owner.status(P, when))

    def test_the_owner_line(self):
        self.assertEqual(self.line(T0), [])
        owner.closing(P, "OWNER: " + "あ" * 150, now=T0)
        text = "あ" * 99 + "…"
        self.assertEqual(self.line(at(22)),
                         [f"  owner: asked {hm(T0)} (22 min, notified 1×, next {hm(at(30))}) — {text}"])
        self.shown_at([30, 60, 120, 240])
        self.assertEqual(self.line(at(300)), [f"  owner: asked {hm(T0)} (300 min, notified 5×) — {text}"])
        out = io.StringIO()
        with mock.patch.object(units, "now", lambda: at(301)), redirect_stdout(out), redirect_stderr(io.StringIO()):
            load_cli().main(["status", "--project", P])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith(f"{P} stage="), lines)
        self.assertEqual(lines[1], f"  owner: asked {hm(T0)} (301 min, notified 5×) — {text}")

    def test_waiting_for_quiet_hours_to_end(self):
        self.config(quiet="23:00-08:00")
        owner.closing(P, "OWNER: approve the deploy", now=NIGHT)
        self.assertEqual(self.line(at(50, NIGHT)), ["  owner: asked 23:10 (50 min, notified 1×, waiting for quiet "
                                                    "hours to end) — approve the deploy"])


class DryRunTest(BeatCase):
    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.stat().st_size, p.stat().st_mtime_ns) if p.is_file() else "dir"
                for p in self.home.rglob("*")}

    def test_a_dry_run_creates_no_folder(self):
        owner.closing(P, "OWNER: secret GH_TOKEN", now=T0)
        shutil.rmtree(self.home / "secrets", ignore_errors=True)
        before = self.snapshot()
        self.beat(at(30), dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.home / "secrets").exists())

    def test_a_dry_run_writes_and_sends_nothing(self):
        self.report("question", at(-4), owner="secret GH_TOKEN")
        owner.closing(P, "OWNER: pick a plan", now=T0)
        self.request("GH_TOKEN", "filled", filled=at(20))
        before, calls = self.snapshot(), len(self.calls)
        rows = self.beat(at(30), dry_run=True)
        self.assertEqual([r["action"] for r in rows], ["resume", "notify"])
        self.assertTrue(all(r["line"].startswith("would ") for r in rows), rows)
        self.assertEqual((self.snapshot(), len(self.calls)), (before, calls))
        self.assertEqual([r["line"] for r in self.beat(at(30))],
                         [f"tell main-{P}: secret GH_TOKEN filled — resume {UNIT}",
                          f"re-notify the owner: {P} ask from {hm(T0)} (1/4)"])
        self.assertEqual(len(self.notes()), 1)

    def test_a_dry_run_fallback(self):
        self.report("blocked", T0)
        before = self.snapshot()
        [row] = self.beat(at(10), dry_run=True)
        self.assertEqual(row["line"], f"would notify the owner: {UNIT} waits in main-{P} (fallback: Main did not tag it)")
        self.assertEqual((self.snapshot(), self.notes(), self.ask()), (before, [], None))


class PassTest(ReconcileCase):
    def test_the_pass_runs_the_owner_step_and_logs_it(self):
        (self.home / "config.json").write_text(json.dumps({**WAKE, "flow": {"quiet_hours": ""}}))
        calls = []

        def run(argv, *, timeout, input, env=None):
            calls.append(list(argv))
            return shell.Result(0 if argv[0] == "osascript" else 113, "", "", list(argv))
        with mock.patch.object(notify, "PLATFORM", "darwin"), mock.patch.object(shell, "RUNNER", run):
            owner.closing(P, "OWNER: pick a plan", now=RT0)
            dry = self.run_at(30, dry_run=True)[1]
            live = self.run_at(30)[1]
        self.assertIn(f"would re-notify the owner: {P} ask from {hm(RT0)} (1/4)", dry)
        self.assertIn(f"re-notify the owner: {P} ask from {hm(RT0)} (1/4)", live)
        self.assertEqual(len([c for c in calls if c[0] == "osascript"]), 1)
        self.assertIn(f"reconcile action=notify project={P} target=owner rev=- attempt=1/4", "\n".join(self.events()))
