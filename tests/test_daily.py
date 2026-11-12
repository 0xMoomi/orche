"""The daily brief (lib/orche/flow/daily.py): the janitor's pass builds it once per brief day, the heartbeat
posts it to Main as one waking pointer outside quiet hours; a quiet project gets nothing; `orche flow brief
off|on`. A fake clock (`now` arguments); quiet hours are set per test."""
import datetime as dt
import json
import os
import time
from unittest import mock

import support  # noqa: F401 - puts lib/ on sys.path
from orche import config, inbox, janitor, registry, units, util
from orche.flow import daily, owner, usage
from orche.queue import store
from test_cmd_dispatch import BODY
from test_flow import FlowCase, header
from test_reconcile import T0, ReconcileCase


class DailyCase(FlowCase):
    def setUp(self):
        super().setUp()
        self.now = units.now().replace(microsecond=0)
        self.quiet("")                  # no quiet hours: the brief day starts at midnight

    def quiet(self, hours):
        cfg = self.home / "config.json"
        data = json.loads(cfg.read_text())
        data.setdefault("flow", {})["quiet_hours"] = hours
        cfg.write_text(json.dumps(data))

    def unit(self, label):
        f = self.first(BODY.replace("Add coupon field", f"Add field {label}"))
        header(f, f.name, "-")
        return f.name

    def post(self, unit, status, summary, at=None, **extra):
        ptr = inbox.make(unit, "div-acme-build", "main-acme", status, brief_rev=1, summary=summary, lineage=unit,
                         **extra)
        if at is not None:
            ptr["at"] = units.iso(at)
        with mock.patch.object(inbox, "wake"):
            inbox.post(ptr)

    def merged(self, unit, pr, at):
        store.log("acme", "merged", entry={"id": f"pr{pr}-abcdef1", "pr": pr, "unit": unit})
        path = store.log_path("acme")
        lines = path.read_text().splitlines()
        last = json.loads(lines[-1])
        last["at"] = units.iso(at)
        path.write_text("\n".join(lines[:-1] + [json.dumps(last)]) + "\n")

    def build(self, now=None):
        return daily.build(registry.load(), now or self.now, root=str(self.home / "no-transcripts"))

    def deliver(self, now=None, dry_run=False):
        with mock.patch.object(inbox, "wake") as wake:
            rows = daily.heartbeat(registry.load(), now or self.now, dry_run)
        return rows, wake

    def briefs(self):
        return [p for _path, p in inbox.pending("main-acme") if p.get("unit") == daily.DAILY_UNIT]


class QuietProjectTest(DailyCase):
    def test_a_quiet_project_gets_no_brief_no_wake_and_no_second_janitor_pass(self):
        self.assertEqual(self.build(), ["daily brief (%s):" % self.now.date().isoformat(),
                                        "  acme: daily brief not needed (nothing to say)"])
        rec = daily.read("acme")
        self.assertIsNone(rec["text"])
        self.assertFalse(daily.report_path("acme").exists())
        rows, wake = self.deliver()
        self.assertEqual(rows, [])
        wake.assert_not_called()
        self.assertEqual(self.briefs(), [])
        old = time.time() - 3 * 86400        # the janitor's last pass long ago: not wanted, the day is built
        self.assertFalse(daily.wanted(registry.load(), self.now, json.loads((self.home / "config.json").read_text()),
                                      old))


class TriggerTest(DailyCase):
    def brief_text(self):
        self.build()
        text = daily.read("acme")["text"]
        self.assertIsNotNone(text)
        self.assertEqual(daily.report_path("acme").read_text(), text)
        return text

    def test_a_landed_pr(self):
        u = self.unit("A")
        self.post(u, "done", "Coupon field added and tested")
        self.merged(u, 41, self.now - dt.timedelta(hours=2))
        self.assertIn("Landed (1):\n  - PR #41: Coupon field added and tested\n", self.brief_text())

    def test_a_unit_that_ended_without_landing(self):
        u = self.unit("A")
        self.post(u, "failed", "Tests fail on the cart page")
        self.assertIn(f"Ended without landing (1):\n  - {u} failed: Tests fail on the cart page\n", self.brief_text())

    def test_a_decision_waiting_for_the_owner(self):
        owner.record("acme", "Ship the coupon page today? I recommend yes; default yes at 18:00",
                     kind="owner", source="main", now=self.now - dt.timedelta(minutes=95))
        self.assertIn("Waiting for the owner, oldest first (1):\n  - waiting 1 h 35 min: OWNER: Ship the coupon "
                      "page today? I recommend yes; default yes at 18:00\n", self.brief_text())

    def test_a_blocked_unit(self):
        u = self.unit("A")
        self.post(u, "blocked", "No staging database")
        self.assertIn(f"  - {u} blocked: No staging database\n", self.brief_text())

    def test_work_held_by_a_usage_limit(self):
        u = self.unit("A")
        until = self.now + dt.timedelta(hours=3)
        util.write_json(usage.path("claude"), {"vendor": "claude", "at": units.iso(self.now), "until": units.iso(until),
                                               "units": [{"project": "acme", "unit": u}]})
        self.assertIn(f"Held or stalled (1):\n  - {u} held by the Claude usage limit until "
                      f"{usage.hhmm(until, self.now)}\n", self.brief_text())


class FactsTextTest(DailyCase):
    def test_the_facts_text_for_a_fixture(self):
        now = self.now
        a, b, c, d = self.unit("A"), self.unit("B"), self.unit("C"), self.unit("D")
        self.post(a, "done", "Coupon field on the cart page", at=now - dt.timedelta(hours=5))
        self.merged(a, 41, now - dt.timedelta(hours=4))
        self.post(b, "failed", "The tax test fails on main", at=now - dt.timedelta(hours=3))
        self.post(c, "question", "Which rounding rule?", at=now - dt.timedelta(days=2, hours=3, minutes=5),
                  owner="Pick round half up or banker's rounding; I recommend half up, default half up")
        self.post(d, "done", "Old work", at=now - dt.timedelta(days=3))     # before the window: not listed
        owner.record("acme", "Approve the launch post? I recommend yes; default hold", kind="owner",
                     source="main", now=now - dt.timedelta(minutes=50))
        since = now - dt.timedelta(hours=24)
        f = daily.facts(registry.load(), "acme", now, since)
        text = daily.render("acme", f, now, since, True, (12.5, [("Main", 5.0), ("build pane", 4.25),
                                                               ("workers", 3.25)]))
        yesterday = (now.date() - dt.timedelta(days=1)).isoformat()
        self.assertEqual(text, "\n".join([
            f"Daily brief for acme, {now.strftime('%A %Y-%m-%d %H:%M')}. "
            f"Since {since.strftime('%Y-%m-%d %H:%M')} (the last 24 hours).",
            "Landed (1):",
            "  - PR #41: Coupon field on the cart page",
            "Ended without landing (1):",
            f"  - {b} failed: The tax test fails on main",
            "Waiting for the owner, oldest first (2):",
            f"  - waiting 2 d 3 h: {c} asks: Pick round half up or banker's rounding; I recommend half up, "
            "default half up",
            "  - waiting 50 min: OWNER: Approve the launch post? I recommend yes; default hold",
            f"Spend yesterday ({yesterday}, list price): $12.50 (Main $5.00, build pane $4.25, workers $3.25)",
        ]) + "\n")
        self.assertEqual(daily.summary(f), "daily brief: 1 landed, 1 ended without landing, 2 wait for the owner, "
                                           "0 held or stalled")

    def test_ages(self):
        self.assertEqual([daily.age(s) for s in (0, 59, 60, 3599, 3600, 3 * 3600 + 1200, 86400, 2 * 86400 + 3 * 3600 + 300)],
                         ["1 min", "1 min", "1 min", "59 min", "1 h", "3 h 20 min", "1 d", "2 d 3 h"])

    def test_lists_stop_at_eight_and_say_how_many_more(self):
        lines = [f"  - PR #{n}: x" for n in range(11)]
        self.assertEqual(daily._listed(lines)[-1], "  - and 3 more")
        self.assertEqual(len(daily._listed(lines)), 9)

    def test_yesterdays_spend_by_role_from_the_transcripts(self):
        root = self.home / "claude-projects"
        noon = (self.now.replace(hour=12, minute=0, second=0) - dt.timedelta(days=1)).astimezone(dt.timezone.utc)
        folder = root / "-some-folder"
        folder.mkdir(parents=True)
        rows = [{"type": "custom-title", "customTitle": "main-acme"},
                {"type": "assistant", "timestamp": noon.isoformat().replace("+00:00", "Z"),
                 "message": {"id": "m1", "model": "claude-opus-5", "usage": {"input_tokens": 1000000}}}]
        (folder / "s1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        got = daily.spend(registry.load(), ["acme"], self.now, root=str(root))
        total, by_role = got["acme"]
        self.assertGreater(total, 0)
        self.assertEqual([r for r, _usd in by_role], ["Main"])


class OncePerDayTest(DailyCase):
    def test_built_and_sent_once_a_day_then_again_the_next_day(self):
        u = self.unit("A")
        self.post(u, "failed", "Broke", at=self.now - dt.timedelta(minutes=1))
        self.assertTrue(self.build())
        self.assertEqual(self.build(), [])                       # the same day: not built again
        rows, wake = self.deliver()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.deliver()[0], [])                  # never twice for one day
        self.assertEqual(len(self.briefs()), 1)
        rec = daily.read("acme")
        self.assertEqual(rec["last_sent"], rec["at"])
        tomorrow = self.now + dt.timedelta(days=1)
        self.build(tomorrow)
        self.assertEqual(daily.read("acme")["since"], rec["at"])    # the next window starts at the last brief
        self.assertIsNone(daily.read("acme")["text"])                # nothing new since: no brief
        self.assertEqual(self.deliver(tomorrow)[0], [])

    def test_the_janitor_pass_is_started_once_for_a_new_brief_day(self):
        cfg = json.loads((self.home / "config.json").read_text())
        start = daily.day_start(cfg, self.now).timestamp()
        reg = registry.load()
        self.assertTrue(daily.wanted(reg, self.now, cfg, start - 60))     # the last pass before the day began
        self.assertFalse(daily.wanted(reg, self.now, cfg, start + 1))     # a pass of this day already ran
        report = janitor.report_path()
        os.utime(report, (start - 60, start - 60))
        with mock.patch.object(units, "now", lambda: self.now):
            rows = janitor.heartbeat(True)
            self.assertEqual(len(rows), 1)
            self.build()
            self.assertEqual(janitor.heartbeat(True), [])                  # built: no second pass


class QuietHoursTest(DailyCase):
    def test_held_in_quiet_hours_and_sent_after(self):
        self.quiet("23:00-08:00")
        evening = self.now.replace(hour=22, minute=50, second=0)
        night, morning = evening + dt.timedelta(minutes=20), evening + dt.timedelta(hours=9, minutes=20)
        u = self.unit("A")
        self.post(u, "failed", "Broke", at=evening - dt.timedelta(minutes=5))
        self.assertTrue(self.build(evening))
        self.assertEqual(self.build(night), [])                  # nothing is built inside quiet hours
        self.assertEqual(self.deliver(night)[0], [])             # held
        self.assertEqual(self.briefs(), [])
        self.assertTrue(self.build(morning))                     # the next brief day: its brief covers the held one
        self.assertEqual(daily.read("acme")["since"], units.iso(morning - dt.timedelta(hours=24)))
        rows, _wake = self.deliver(morning)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.briefs()), 1)
        self.assertIn(f"{u} failed: Broke", daily.read("acme")["text"])

    def test_the_brief_day_starts_where_quiet_hours_end(self):
        cfg = {"flow": {"quiet_hours": "23:00-08:00"}}
        base = self.now.replace(hour=12, minute=0, second=0)
        self.assertEqual(daily.day(cfg, base.replace(hour=7, minute=59)), (base.date() - dt.timedelta(days=1)).isoformat())
        self.assertEqual(daily.day(cfg, base.replace(hour=8, minute=0)), base.date().isoformat())
        self.assertEqual(daily.day({"flow": {"quiet_hours": "junk"}}, base.replace(hour=0, minute=1)),
                         base.date().isoformat())


class OffSwitchTest(DailyCase):
    def test_the_owners_words_switch_it_off_and_on(self):
        u = self.unit("A")
        self.post(u, "failed", "Broke")
        res = self.orche("flow", "brief", "off")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("daily brief off", res.stdout)
        self.assertIs(json.loads((self.home / "config.json").read_text())["flow"]["daily_brief"], False)
        self.assertEqual(self.build(), [])
        self.assertEqual(self.deliver()[0], [])
        self.assertEqual(self.orche("flow", "brief", "on").returncode, 0)
        self.assertTrue(self.build())
        self.assertEqual(len(self.deliver()[0]), 1)

    def test_off_after_the_build_holds_the_delivery(self):
        u = self.unit("A")
        self.post(u, "failed", "Broke")
        self.build()
        self.orche("flow", "brief", "off")
        self.assertEqual(self.deliver()[0], [])


class WakeTest(DailyCase):
    def test_the_pointer_wakes_main_through_the_inbox_and_carries_the_facts(self):
        u = self.unit("A")
        self.post(u, "failed", "Broke")
        self.build()
        rows, wake = self.deliver()
        self.assertEqual([r["line"] for r in rows],
                         ["post the daily brief to main-acme (daily brief: 0 landed, 1 ended without landing, "
                          "0 wait for the owner, 0 held or stalled)"])
        wake.assert_called_once_with("main-acme")
        [(path, ptr)] = [(p, x) for p, x in inbox.pending("main-acme") if x.get("unit") == daily.DAILY_UNIT]
        self.assertEqual((ptr["from"], ptr["status"], ptr["urgent"]), ("orche", "info", True))
        self.assertTrue(inbox.wakes(ptr) and inbox.is_urgent(ptr))
        self.assertTrue(ptr["summary"].startswith("daily brief: "))
        text, _shown = inbox.fit([(path, ptr)])
        self.assertIn(f"{u} failed: Broke", text)                 # the facts are inlined under the pointer

    def test_a_dry_run_posts_nothing_and_a_paused_project_waits(self):
        u = self.unit("A")
        self.post(u, "failed", "Broke")
        self.build()
        rows, _wake = self.deliver(dry_run=True)
        self.assertTrue(rows[0]["line"].startswith("would post the daily brief"))
        self.assertEqual(self.briefs(), [])
        from orche.flow import guard
        guard.pause("acme", "main-acme")
        self.assertEqual(self.deliver()[0], [])
        self.assertIsNone(daily.read("acme")["sent_at"])


class EndedWaitTest(DailyCase):
    """c127: a unit that waited for the owner and was then stopped by Main, or closed by Main's own report, is
    no longer listed as waiting."""

    def test_a_wait_main_stopped_or_closed_is_not_listed(self):
        now, since = self.now, self.now - dt.timedelta(hours=24)
        a, b, c = self.unit("A"), self.unit("B"), self.unit("C")
        self.post(a, "blocked", "Needs the deploy key", at=now - dt.timedelta(hours=3), owner="approve the deploy")
        self.post(b, "question", "Which rounding rule?", at=now - dt.timedelta(hours=3), owner="pick a rule")
        self.post(c, "question", "Which tax rule?", at=now - dt.timedelta(hours=3), owner="pick a tax rule")
        self.assertEqual(len(daily.facts(registry.load(), "acme", now, since)["waiting"]), 3)
        stop = inbox.make(a, "main-acme", "div-acme-build", "stop", brief_rev=1, via="dispatch", summary="STOP")
        stop["at"] = units.iso(now - dt.timedelta(hours=2))
        inbox.write(stop)
        done = inbox.make(b, "main-acme", "main-acme", "done", brief_rev=1, summary="Closed by Main", lineage=b)
        done["at"] = units.iso(now - dt.timedelta(hours=1))
        with mock.patch.object(inbox, "wake"):
            inbox.post(done)
        f = daily.facts(registry.load(), "acme", now, since)
        self.assertEqual(f["waiting"], [f"  - waiting 3 h: {c} asks: pick a tax rule"])
        self.assertEqual(f["ended"], [])
        self.assertEqual(f["landed"], [f"  - {b} (done, no PR): Closed by Main"])


class PassTest(ReconcileCase):
    """`orche reconcile` posts a built brief in its `daily` step: once, through the inbox's wake."""

    def test_the_heartbeat_pass_posts_the_brief_once(self):
        cfg = config.load()
        rec = {"day": daily.day(cfg, T0), "at": units.iso(T0), "since": units.iso(T0), "text": "Daily brief.\n",
               "summary": "daily brief: 1 landed, 0 ended without landing, 0 wait for the owner, 0 held or stalled",
               "sent_at": None, "last_sent": None}
        util.write_json(daily._path("acme-shop"), rec)
        util.atomic_write(daily.report_path("acme-shop"), rec["text"])
        code, lines = self.run_at(5, dry_run=True)
        self.assertIn(f"would post the daily brief to main-acme-shop ({rec['summary']})", lines)
        code, lines = self.run_at(5)
        self.assertEqual(code, 0)
        self.assertIn(f"post the daily brief to main-acme-shop ({rec['summary']})", lines)
        self.assertIn(("nudge", "main-acme-shop"), self.spawned)
        self.assertTrue(any("action=daily-brief project=acme-shop" in e for e in self.events()))
        code, lines = self.run_at(10)
        self.assertFalse(any("daily brief" in ln for ln in lines))
