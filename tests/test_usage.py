"""Usage-limit holds (0.9.0 T4, design §8.5, R20): detection on both vendors' texts and the reset
parsers, the backoff ladder, the hold store, a held run start and a run that hits a limit (quiet
`info`, never `failed`, no wake), and the free-memory reading behind the runner's memory guard."""
import datetime as dt
import json
import time
import unittest
from unittest import mock

import support
from orche import brief, headless, inbox, procs, registry, util
from orche.flow import usage
from orche.runs import history
from test_runner import RunnerCase

T0 = dt.datetime(2026, 10, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
MIN = dt.timedelta(minutes=1)


class DetectTest(unittest.TestCase):
    def test_claude_epoch_after_a_pipe(self):
        epoch = int((T0 + dt.timedelta(hours=3)).timestamp())
        self.assertEqual(usage.detect(f"Claude AI usage limit reached|{epoch}", "claude", {}, T0, step=0),
                         (T0 + dt.timedelta(hours=3), "reset"))

    def test_resets_3pm_is_the_next_such_local_time(self):
        until, source = usage.detect("5-hour limit reached ∙ resets 3pm", "claude", {}, T0, step=0)
        self.assertEqual((until, source), (T0.replace(hour=15), "reset"))
        until, _ = usage.detect("usage limit: resets at 9:30", "claude", {}, T0, step=0)
        self.assertEqual(until, T0.replace(hour=9, minute=30) + dt.timedelta(days=1))   # past today: tomorrow

    def test_codex_try_again_in_days_and_hours(self):
        text = "You've hit your usage limit. Upgrade to Pro or try again in 1 day 2 hours."
        self.assertEqual(usage.detect(text, "codex", {}, T0, step=0), (T0 + dt.timedelta(days=1, hours=2), "reset"))

    def test_retry_after_seconds(self):
        self.assertEqual(usage.detect("429 Too Many Requests; retry-after: 120", "claude", {}, T0, step=0),
                         (T0 + dt.timedelta(seconds=120), "reset"))

    def test_a_bare_429_backs_off_5_then_15_then_60(self):
        for step, minutes in ((0, 5), (1, 15), (2, 60), (7, 60)):
            self.assertEqual(usage.detect("API Error: 429", "claude", {}, T0, step=step),
                             (T0 + minutes * MIN, "backoff"))

    def test_a_non_limit_error_is_no_hold(self):
        for text in ("API Error: 500 internal server error", "exit 1: TypeError: x is undefined", "", None):
            self.assertIsNone(usage.detect(text, "claude", {}, T0, step=0))

    def test_ordinary_error_text_with_429_quota_or_limit_is_no_hold(self):
        for text in ("Traceback: see line 429 of app.py", "merged PR #429", "OSError: Disk quota exceeded",
                     "quota: 3 of 5 seats", "context limit reached", "limit reached", "port 4290 in use"):
            self.assertFalse(usage.is_limit(text, {}), text)

    def test_a_reset_without_a_clock_time_backs_off(self):
        self.assertEqual(usage.detect("usage limit, reset 5", "claude", {}, T0, step=0), (T0 + 5 * MIN, "backoff"))
        self.assertEqual(usage.detect("usage limit; resets 17:00", "claude", {}, T0, step=0)[0], T0.replace(hour=17))
        self.assertEqual(usage.detect("usage limit; resets in 5 minutes", "claude", {}, T0, step=0),
                         (T0 + 5 * MIN, "reset"))
        self.assertEqual(usage.detect("usage limit; resets at 2026-10-24T03:30:00Z", "claude", {}, T0, step=0),
                         (dt.datetime(2026, 10, 24, 3, 30, tzinfo=dt.timezone.utc), "reset"))

    def test_patterns_and_backoff_are_config(self):
        cfg = {"runner": {"usage_limit": {"patterns": ["slow down"], "backoff_min": [2]}}}
        self.assertEqual(usage.detect("please slow down", "claude", cfg, T0, step=4), (T0 + 2 * MIN, "backoff"))
        self.assertIsNone(usage.detect("usage limit", "claude", cfg, T0, step=0))
        bad = {"runner": {"usage_limit": {"patterns": "usage", "backoff_min": [0, "x"]}}}   # malformed: defaults
        self.assertEqual(usage.detect("rate limit", "claude", bad, T0, step=0), (T0 + 5 * MIN, "backoff"))


class LimitTextTest(unittest.TestCase):
    def res(self, data, code=1, failure=None, killed=False):
        return headless.Result(code, data, None, failure, killed)

    def test_claude_reads_an_is_error_result_text_and_the_failure_line(self):
        self.assertIn("usage limit reached", self.res({"is_error": True, "result": "Claude AI usage limit reached|1761300000"}).limit_text)
        self.assertEqual(self.res({}, failure="exit 1: Error: 429 rate_limit_error").limit_text,
                         "exit 1: Error: 429 rate_limit_error")

    def test_a_successful_result_is_never_read(self):
        ok = self.res({"is_error": False, "result": "Added the rate-limit middleware (429 on overflow)"}, code=0)
        self.assertEqual(ok.limit_text, "")
        self.assertFalse(usage.is_limit(ok.limit_text, {}))

    def test_codex_reads_error_text_never_its_final_message(self):
        r = self.res({"vendor": "codex", "is_error": True, "subtype": "error_turn_failed",
                      "result": "I added a quota check", "error_text": "stream error: 429 Too Many Requests"})
        self.assertEqual(r.limit_text, "stream error: 429 Too Many Requests")

    def test_turn_cap_and_a_killed_call_are_not_limits(self):
        self.assertEqual(self.res({"is_error": True, "subtype": "error_max_turns", "result": "quota"}).limit_text, "")
        self.assertEqual(self.res({"is_error": True, "result": "usage limit"}, killed=True).limit_text, "")


class HoldStoreTest(support.TempHomeCase):
    def hit(self, text, unit, at, vendor="claude"):
        return usage.hit(vendor, text, {}, project="acme", unit=unit, stage="work", now=at)

    def test_a_hold_joins_then_climbs_the_ladder_within_a_day(self):
        h = self.hit("API Error: 429", "u1", T0)
        self.assertEqual((h["step"], h["source"], usage.until_of(h)), (0, "backoff", T0 + 5 * MIN))
        h = self.hit("rate limit", "u2", T0 + MIN)                      # active: u2 joins, same until
        self.assertEqual(([u["unit"] for u in h["units"]], usage.until_of(h)), (["u1", "u2"], T0 + 5 * MIN))
        self.assertIsNone(usage.active("codex", T0))                    # the other vendor keeps going
        self.assertIsNotNone(usage.active("claude", T0 + 4 * MIN))
        self.assertIsNone(usage.active("claude", T0 + 5 * MIN))
        h = self.hit("API Error: 429", "u1", T0 + 6 * MIN)              # again within 24 h: 15 min
        self.assertEqual((h["step"], usage.until_of(h), len(h["units"])), (1, T0 + 21 * MIN, 1))
        h = self.hit("API Error: 429", "u1", T0 + dt.timedelta(days=2))   # a day later: the first rung
        self.assertEqual(h["step"], 0)
        events = (self.home / "logs" / "events.log").read_text()
        self.assertIn(f"usage hold vendor=claude until={usage.hhmm(T0 + 5 * MIN, T0)} source=backoff step=0 "
                      "units=1\n", events)
        self.assertEqual(events.count("usage hold "), 3)                # a join logs nothing

    def test_a_later_parsed_reset_moves_the_until_and_the_text_is_masked(self):
        secret = "sk-" + "a" * 30
        h = self.hit(f"rate limit {secret}", "u1", T0)
        self.assertNotIn(secret, h["text"])
        epoch = int((T0 + dt.timedelta(hours=2)).timestamp())
        h = self.hit(f"usage limit reached|{epoch}", "u2", T0 + MIN)
        self.assertEqual((usage.until_of(h), h["source"]), (T0 + dt.timedelta(hours=2), "reset"))
        self.assertEqual(json.loads(usage.path("claude").read_text())["until"], h["until"])

    def test_a_non_limit_text_records_nothing(self):
        self.assertIsNone(self.hit("TypeError: boom", "u1", T0))
        self.assertFalse(usage.path("claude").exists())

    def test_three_holds_without_a_done_run_and_the_unit_is_not_held_again(self):
        """RUN.json `holds` (the runner's count per held run, Review B G3) decides; history rows do not."""
        root = self.home / "acme"
        folder = root / brief.UNITS / "u1"
        folder.mkdir(parents=True)
        reg = {"projects": {"acme": {"path": str(root)}}}
        util.write_json(folder / "RUN.json", {"status": "info", "stop_reason": "usage_limit", "holds": usage.REPEAT_MAX})
        history.append("acme", {"unit": "u1", "kind": "work", "status": "done", "stop_reason": "usage_limit"}, 1)
        patched = mock.patch.object(registry, "load", lambda: reg)
        patched.start()
        self.addCleanup(patched.stop)
        self.assertEqual(usage.repeats("acme", "u1"), usage.REPEAT_MAX)    # a stage's done row resets nothing
        self.assertIsNone(self.hit("API Error: 429", "u1", T0))
        self.assertFalse(usage.path("claude").exists())
        events = (self.home / "logs" / "events.log").read_text()
        self.assertIn("usage hold refused unit=u1 reason=repeat\n", events)
        self.assertIsNotNone(self.hit("API Error: 429", "u2", T0))           # another unit still holds
        util.write_json(folder / "RUN.json", {"status": "done", "stop_reason": "end_turn"})   # a done pointer: 0
        self.assertEqual(usage.repeats("acme", "u1"), 0)                     # a done run resets the count
        self.assertIsNotNone(self.hit("API Error: 429", "u1", T0 + MIN))


class FreeMemoryTest(unittest.TestCase):
    SYSCTL = "59\n25769803776\n"
    VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:      3000.\n"
               "Pages active:     500000.\nPages inactive:   200000.\nPages speculative:  1000.\n"
               "Pages wired down: 150000.\nPages purgeable:    4000.\n")

    def test_sysctl_free_percentage_times_ram(self):
        self.assertEqual(procs.free_mb(run=lambda argv: self.SYSCTL if argv[0] == "sysctl" else None), 14499)

    def test_vm_stat_when_sysctl_fails(self):
        self.assertEqual(procs.free_mb(run=lambda argv: self.VM_STAT if argv[0] == "vm_stat" else None), 3250)

    def test_unreadable_is_none(self):
        with mock.patch("orche.procs.open", create=True, side_effect=OSError):
            self.assertIsNone(procs.free_mb(run=lambda argv: "garbage"))


class HeldRunTest(RunnerCase):
    """A Claude limit text in a run's result: the unit is held (quiet `info`), never `failed`; the
    next runs of that vendor never start while the hold is on."""

    def test_three_units_three_quiet_infos_no_failed_no_wake(self):
        epoch = int(time.time()) + 3600
        self.reply(f"Claude AI usage limit reached|{epoch}", is_error=True, code=1, cost=0)
        first, second, third = self.brief(), self.brief(), self.brief()
        codes = [self.orche("run", u)[0] for u in (first, second, third)]
        self.assertEqual(codes, [0, 0, 0])
        ptrs = self.pointers("main-acme")
        self.assertEqual(sorted(p["unit"] for p in ptrs), sorted([first, second, third]))
        self.assertEqual({p["status"] for p in ptrs}, {"info"})
        self.assertFalse(any(inbox.wakes(p) for p in ptrs))
        self.assertTrue(all(p["summary"].startswith("held: claude usage limit until ") for p in ptrs))
        rec = self.run_record(first)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["stage"]), ("info", "usage_limit", "work"))
        self.assertEqual([self.run_record(u)["stop_reason"] for u in (second, third)], ["held", "held"])
        hold = usage.read("claude")
        self.assertEqual((hold["source"], sorted(u["unit"] for u in hold["units"])),
                         ("reset", sorted([first, second, third])))
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self.assertEqual(sum(history.runs_of("acme", u, since) for u in (first, second, third)), 0)   # not G5 runs

    def test_a_held_vendor_never_starts_the_worker(self):
        usage.hit("claude", "API Error: 429", {}, project="acme", unit="other", stage="work")
        self.reply("STATUS: done")
        unit = self.brief()
        code, _, _ = self.orche("run", unit)
        self.assertEqual((code, self.run_record(unit)["stop_reason"]), (0, "held"))
        self.assertFalse((self.fake / "stdin.txt").exists())
        [ptr] = self.pointers("main-acme")
        self.assertTrue(ptr.get("quiet"))
        self.assertIn(unit, [u["unit"] for u in usage.read("claude")["units"]])

    def test_a_plain_failure_is_still_failed(self):
        self.reply("", is_error=True, subtype="error_during_execution", code=1)
        unit = self.brief()
        self.orche("run", unit)
        self.assertEqual(self.run_record(unit)["status"], "failed")
        self.assertIsNone(usage.read("claude"))


class QuotaPatternTest(unittest.TestCase):
    def test_a_disc_quota_error_never_holds_and_the_vendor_quota_texts_still_do(self):
        """Review B G8f: macOS EDQUOT ("Disc quota exceeded") is a disk error, not a usage limit."""
        for text in ("OSError: [Errno 69] Disc quota exceeded", "write error: Disk quota exceeded"):
            self.assertFalse(usage.is_limit(text, {}), text)
        for text in ("You exceeded your current quota, please check your plan and billing details.",
                     "Quota exceeded for quota metric 'Generate Content API requests per minute'",
                     "the usage quota of this workspace is used up"):
            self.assertTrue(usage.is_limit(text, {}), text)


if __name__ == "__main__":
    unittest.main()
