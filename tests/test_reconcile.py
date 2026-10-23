"""The heartbeat pass (`orche reconcile`, C10 T1): observe -> plan -> bounded repairs, the heartbeat file,
the lock, dry-run, off, and the status / doctor wording. No process is started: procs.spawn_detached is
replaced by a recorder (nudger, deliverer and the escalation's wake all go through it)."""
import copy
import datetime as dt
import fcntl
import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support
from unittest import mock

from orche import brief, deliver, hr, inbox, notify, orca, procs, reconcile, registry, roster, runs, shell, units, util
from test_registry import load_cli

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
T0 = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
WAKE = {"inbox": {"wake": True, "quiet_s": 0}}


class ReconcileCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        registry.save(copy.deepcopy(SAMPLE))
        (self.home / "config.json").write_text(json.dumps(WAKE))
        self.now = T0
        self._orig_now, units.now = units.now, lambda: self.now
        self.spawned = []
        self._orig_spawn, procs.spawn_detached = procs.spawn_detached, lambda argv, log, **kw: self.spawned.append(
            (argv[2], argv[-1]))
        os.environ["ORCHE_FAKE_ORCA"] = "1"
        self.set_panes("main-acme-shop", "div-acme-shop-build", "main-docs-site")

    def set_panes(self, *titles, output_min_ago=10):
        at = time.time() * 1000 - output_min_ago * 60 * 1000
        (self.home / "fake-orca.json").write_text(json.dumps(
            {"terminal list": {"terminals": [{"title": t, "lastOutputAt": at} for t in titles]}}))

    def tearDown(self):
        units.now, procs.spawn_detached = self._orig_now, self._orig_spawn
        super().tearDown()

    def pointer(self, session, minutes_ago, **ptr):
        p = {"unit": "u1", "from": "div-acme-shop-build", "to": session, "status": "done", "brief_rev": 1,
             "at": (T0 - dt.timedelta(minutes=minutes_ago)).isoformat(), **ptr}
        util.write_json(inbox.box(session) / f"{ptr.get('unit', 'u1')}-{minutes_ago}.json", p)

    def run_at(self, minutes, dry_run=False):
        self.now = T0 + dt.timedelta(minutes=minutes)
        self.spawned.clear()
        lines = []
        code = reconcile.run_pass(dry_run=dry_run, out=lines.append)
        return code, lines

    def events(self):
        path = self.home / "logs" / "events.log"
        return [l for l in path.read_text().splitlines() if "reconcile" in l] if path.exists() else []


class PassTest(ReconcileCase):
    def test_unread_report_gets_one_nudge_and_the_heartbeat_is_written(self):
        self.pointer("main-acme-shop", 12)
        code, lines = self.run_at(0)
        self.assertEqual(code, 0)
        self.assertEqual(self.spawned, [("nudge", "main-acme-shop")])
        self.assertEqual(len(lines), 1)
        self.assertIn("nudge main-acme-shop (1 report(s) unread, 12 min; try 1/3)", lines[0])
        hb = json.loads(reconcile.heartbeat_path().read_text())
        self.assertEqual((hb["actions"], hb["enabled"], hb["repairs_today"], hb["every_s"]), (1, True, 1, 300))
        self.assertEqual(hb["at"], T0.isoformat(timespec="seconds"))
        self.assertTrue(any("action=nudge" in e and "target=main-acme-shop" in e for e in self.events()))

    def test_an_abort_mid_apply_still_spends_the_attempt(self):
        # 0.7.2 review #6: recorded before apply(), so a watchdog abort inside it cannot hand out fresh tries
        self.pointer("main-acme-shop", 12)

        def hang(*_a):
            raise reconcile.Abort(None)
        with mock.patch.object(reconcile, "apply", hang), redirect_stderr(io.StringIO()):
            self.assertEqual(reconcile.run_pass(out=lambda *_: None), 3)
        state = json.loads(reconcile.state_path().read_text())
        self.assertEqual([len(e["attempts"]) for e in state.values() if isinstance(e, dict) and "attempts" in e], [1])
        self.run_at(3)                       # 15 min: try 2 waits for 20 min, as after a completed try
        self.assertEqual(self.spawned, [])

    def test_a_fresh_report_is_left_alone(self):
        self.pointer("main-acme-shop", 5)
        _code, lines = self.run_at(0)
        self.assertEqual((self.spawned, lines), ([], []))
        self.assertEqual(json.loads(reconcile.heartbeat_path().read_text())["actions"], 0)

    def test_backoff_then_cap_then_one_escalation_to_main(self):
        self.pointer("div-acme-shop-build", 12, unit="u3", **{"from": "main-acme-shop"})
        self.assertEqual(self.run_at(0)[1][0][:6], "nudge ")       # 12 min >= 10: try 1
        self.assertEqual(self.spawned, [("nudge", "div-acme-shop-build")])
        self.run_at(3)                                             # 15 min < 20: none
        self.assertEqual(self.spawned, [])
        self.run_at(9)                                             # 21 min >= 20: try 2
        self.assertEqual(self.spawned, [("nudge", "div-acme-shop-build")])
        self.run_at(29)                                            # 41 min >= 40: try 3
        self.assertEqual(self.spawned, [("nudge", "div-acme-shop-build")])
        self.run_at(60)                                            # 72 min < 80: capped, none
        self.assertEqual(self.spawned, [])
        _code, lines = self.run_at(70)                             # 82 min >= 80: escalate once
        self.assertEqual(lines, ["escalate unread div-acme-shop-build (after 3 tries, 82 min)"])
        ptrs = [p for _x, p in inbox.pending("main-acme-shop")]
        self.assertEqual(len(ptrs), 1)
        self.assertEqual((ptrs[0]["status"], ptrs[0]["urgent"], ptrs[0]["from"]), ("info", True, "orche"))
        self.assertIn("unread after 3 nudges: div-acme-shop-build has 1 report(s) waiting", ptrs[0]["summary"])
        self.assertEqual(self.spawned, [("nudge", "main-acme-shop")])   # the escalation's own wake
        self.run_at(200)                                           # escalated: never again
        self.assertNotIn(("nudge", "div-acme-shop-build"), self.spawned)
        self.assertEqual(len(inbox.pending("main-acme-shop")), 1)

    def test_a_gone_condition_drops_its_state(self):
        self.pointer("main-acme-shop", 12)
        self.run_at(0)
        self.assertIn("unread:main-acme-shop:u1-12.json", json.loads(reconcile.state_path().read_text()))
        for path, _p in inbox.pending("main-acme-shop"):
            path.unlink()
        self.run_at(1)
        self.assertEqual(json.loads(reconcile.state_path().read_text()), {})

    def test_a_new_unread_episode_after_resolution_gets_fresh_attempts(self):
        self.pointer("main-acme-shop", 12)
        self.run_at(0)
        self.run_at(9)                                             # try 2/3
        inbox.drain([path for path, _p in inbox.pending("main-acme-shop")])     # read: resolved
        self.run_at(10)
        self.assertEqual(util.read_json(reconcile.state_path()), {})
        self.pointer("main-acme-shop", -15)                        # a new report at T0 + 15 min
        self.assertEqual(self.run_at(27)[1], ["nudge main-acme-shop (1 report(s) unread, 12 min; try 1/3)"])

    def test_an_orca_outage_keeps_attempts_and_the_escalation(self):
        self.pointer("div-acme-shop-build", 12, unit="u3", **{"from": "main-acme-shop"})
        for m in (0, 9, 29, 70):
            self.run_at(m)
        before = util.read_json(reconcile.state_path())
        self.assertTrue(before["unread:div-acme-shop-build:u3-12.json"]["escalated"])
        with mock.patch.object(orca, "terminal", side_effect=orca.OrcaError("down")):
            self.run_at(75)
        self.assertEqual(util.read_json(reconcile.state_path()), before)
        nudges = 0
        for m in (80, 85, 90, 95):
            self.run_at(m)
            nudges += self.spawned.count(("nudge", "div-acme-shop-build"))
        esc = [p for _x, p in inbox.pending("main-acme-shop") if p.get("from") == "orche"]
        self.assertEqual((len(esc), nudges), (1, 0))

    def test_project_filter_and_json(self):
        self.pointer("main-acme-shop", 12)
        self.pointer("main-docs-site", 12)
        out = []
        self.now = T0
        self.assertEqual(reconcile.run_pass(out=out.append, keys={"docs-site"}, as_json=True), 0)
        doc = json.loads(out[0])
        self.assertEqual([(a["action"], a["target"]) for a in doc["actions"]], [("nudge", "main-docs-site")])
        self.assertEqual(self.spawned, [("nudge", "main-docs-site")])
        self.assertEqual(list(util.read_json(reconcile.state_path())), ["unread:main-docs-site:u1-12.json"])
        with redirect_stdout(io.StringIO()) as got, redirect_stderr(io.StringIO()):
            self.assertEqual(load_cli().main(["reconcile", "--project", "acme-shop", "--json"]), 0)
        self.assertEqual([a["target"] for a in json.loads(got.getvalue())["actions"]], ["main-acme-shop"])
        self.assertEqual(sorted(util.read_json(reconcile.state_path())),
                         ["unread:main-acme-shop:u1-12.json", "unread:main-docs-site:u1-12.json"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(load_cli().main(["reconcile", "--project", "nope"]), 2)

    def test_a_nudged_session_waits(self):
        self.pointer("main-acme-shop", 12)
        (inbox.box("main-acme-shop") / inbox.NUDGED).touch()
        _code, lines = self.run_at(0)
        self.assertEqual((self.spawned, lines), ([], []))

    def test_undelivered_dispatch_is_redelivered_newest_revision(self):
        self.pointer("div-acme-shop-build", 30, unit="u7", via="dispatch", brief_rev=1,
                     status="info", **{"from": "main-acme-shop"})
        self.pointer("div-acme-shop-build", 11, unit="u7", via="dispatch", brief_rev=2,
                     status="info", **{"from": "main-acme-shop"})
        _code, lines = self.run_at(0)
        self.assertEqual(self.spawned, [("deliver", "u7")])
        self.assertEqual(lines, ["deliver u7 → div-acme-shop-build (rev 2, 11 min; try 1/3)"])
        self.assertIn("undelivered:u7:2", json.loads(reconcile.state_path().read_text()))

    def test_dry_run_writes_nothing(self):
        self.pointer("main-acme-shop", 12)
        code, lines = self.run_at(0, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["would nudge main-acme-shop (1 report(s) unread, 12 min; try 1/3)"])
        self.assertEqual(self.spawned, [])
        self.assertFalse(reconcile.heartbeat_path().exists())
        self.assertFalse(reconcile.state_path().exists())

    def test_dry_run_with_nothing_due(self):
        self.assertEqual(self.run_at(0, dry_run=True)[1], ["reconcile: nothing to repair"])

    def test_a_held_lock_exits_zero_without_a_pass(self):
        self.pointer("main-acme-shop", 12)
        reconcile.folder().mkdir(parents=True, exist_ok=True)
        with open(reconcile.folder() / ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, lines = self.run_at(0)
        self.assertEqual((code, lines, self.spawned), (0, ["reconcile: another pass is running"], []))
        self.assertFalse(reconcile.heartbeat_path().exists())

    def test_off_writes_a_disabled_heartbeat_and_repairs_nothing(self):
        (self.home / "config.json").write_text(json.dumps({**WAKE, "reconcile": {"enabled": False}}))
        self.pointer("main-acme-shop", 12)
        _code, lines = self.run_at(0)
        self.assertEqual((self.spawned, lines), ([], ["reconcile: off (reconcile.enabled false)"]))
        self.assertFalse(json.loads(reconcile.heartbeat_path().read_text())["enabled"])

    def test_wake_off_skips_unread(self):
        (self.home / "config.json").write_text(json.dumps(support.NO_WAKE))
        self.pointer("main-acme-shop", 12)
        self.assertEqual(self.run_at(0)[1], [])

    def test_cli_runs_one_pass(self):
        self.pointer("main-acme-shop", 12)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = load_cli().main(["reconcile"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("nudge main-acme-shop", out.getvalue())
        self.assertEqual(self.spawned, [("nudge", "main-acme-shop")])


class RepairCase(ReconcileCase):
    """acme-shop's root in the temp home, so briefs and RUN.json can be written."""

    def setUp(self):
        super().setUp()
        reg = registry.load()
        reg["projects"]["acme-shop"]["path"] = str(self.home / "acme-shop")
        registry.save(reg)

    def brief(self, to="div-acme-shop-build", sender="main-acme-shop", **over):
        fields = {"TO": to, "GOAL": "Add coupon field", "SCOPE": "src/**", "CONTEXT": "none", "ACCEPT": "works",
                  "VERIFY": "true", "TIMEBOX": "20m", "FORBID": "merge", "REPORT": "status", "TIER": "standard",
                  **over}
        target = roster.Roster(registry.load()).member(to)
        return brief.write(brief.Block("brief", fields), target, sender, "1. Never force-push.", timeout=0)

    def dispatched(self, minutes_ago=12):
        b = self.brief()
        self.pointer(b.to, minutes_ago, unit=b.unit, via="dispatch", brief_rev=b.rev, status="info",
                     **{"from": b.sender})
        return b

    def run_json(self, b, **rec):
        path = b.folder / "RUN.json"
        util.write_json(path, {"unit": b.unit, "project": b.project, "to": b.to, **rec})
        later = b.path.stat().st_mtime + 1
        os.utime(path, (later, later))
        return path


class DelivererRepairTest(RepairCase):
    def test_a_dispatch_whose_revision_ran_is_drained_not_redelivered(self):
        b = self.dispatched()
        self.run_json(b, started=util.now_iso(), stop_reason="done")
        _code, lines = self.run_at(0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(lines, [f"drain {b.unit} in div-acme-shop-build (rev 1 already ran or reported)"])
        self.assertEqual(inbox.pending("div-acme-shop-build"), [])
        self.assertTrue(any("action=drain-stale" in e for e in self.events()))

    def test_a_stop_pointer_is_never_drained_as_settled(self):
        b = self.brief()
        inbox.write(inbox.make(b.unit, "div-acme-shop-build", "main-acme-shop", "blocked", brief_rev=1))
        self.pointer(b.to, 12, unit=b.unit, via="dispatch", brief_rev=1, status="stop", **{"from": b.sender})
        argv = []
        with mock.patch.object(procs, "spawn_detached", lambda a, log, **kw: argv.append(a)):
            _code, lines = self.run_at(0)
        self.assertEqual([a[2:] for a in argv], [["deliver", "--retry", "--", b.unit]])
        self.assertEqual(len(inbox.pending("div-acme-shop-build")), 1)

    def test_the_undelivered_escalation_names_the_last_delivery(self):
        self.pointer("div-acme-shop-build", 12, unit="u7", via="dispatch", brief_rev=1, status="info",
                     **{"from": "main-acme-shop"})
        for m in (0, 9, 29):
            self.run_at(m)
        util.write_json(inbox.box("div-acme-shop-build") / f"{deliver.LAST_DELIVER}u7",
                        {"state": "dialog", "rev": 1, "at": T0.isoformat()})
        self.assertEqual(self.run_at(70)[1], ["escalate undelivered u7 (after 3 tries, 82 min)"])
        [esc] = [p["summary"] for _x, p in inbox.pending("main-acme-shop") if p.get("from") == "orche"]
        self.assertEqual(esc, "not delivered after 3 tries: u7 → div-acme-shop-build (dialog, pending 82 min) — "
                              "the pane shows a dialog; answer or STOP it, then `orche deliver u7`")
        self.run_at(200)
        self.assertEqual(len([p for _x, p in inbox.pending("main-acme-shop") if p.get("from") == "orche"]), 1)

    def test_a_reported_revision_is_drained(self):
        b = self.dispatched()
        inbox.write(inbox.make(b.unit, "div-acme-shop-build", "main-acme-shop", "done", brief_rev=1, via="runner"))
        self.run_at(0)
        self.assertEqual([p for _x, p in inbox.pending("div-acme-shop-build")], [])

    def test_a_drain_waits_for_a_held_deliver_lock(self):
        self.dispatched()
        box = inbox.box("div-acme-shop-build")
        box.mkdir(parents=True, exist_ok=True)
        with open(box / deliver.DELIVER_LOCK, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_json(brief.find(registry.load(), self.only_unit()), stop_reason="done", started=util.now_iso())
            _code, lines = self.run_at(0)
        self.assertEqual(lines, [])
        self.assertEqual(len(inbox.pending("div-acme-shop-build")), 1)

    def only_unit(self):
        return next((self.home / "acme-shop" / brief.UNITS).iterdir()).name

    def test_an_unsettled_dispatch_is_redelivered_with_retry(self):
        b = self.dispatched()
        argv = []
        with mock.patch.object(procs, "spawn_detached", lambda a, log, **kw: argv.append(a)):
            self.run_at(0)
        self.assertEqual(len(argv), 1)
        self.assertEqual(argv[0][2:], ["deliver", "--retry", "--", b.unit])

    def test_a_busy_stop_restarts_the_run_once(self):
        b = self.brief()
        self.run_json(b, stop_reason="busy", finished=(T0 - dt.timedelta(minutes=11)).isoformat())
        started = []
        with mock.patch.object(runs, "start", started.append), mock.patch.object(runs, "running", lambda *a: False):
            _code, lines = self.run_at(0)
            self.assertEqual(started, [b.unit])
            self.assertEqual(lines, [f"restart {b.unit} (rev 1, target was busy 11 min ago; try 1/1)"])
            self.run_at(100)
            self.run_at(300)
        self.assertEqual(started, [b.unit])                        # cap 1, never escalated
        self.assertFalse(any("action=escalate" in e for e in self.events()))

    def test_a_busy_stop_older_than_its_brief_is_ignored(self):
        b = self.brief()
        path = self.run_json(b, stop_reason="busy", finished=(T0 - dt.timedelta(minutes=30)).isoformat())
        earlier = b.path.stat().st_mtime - 5
        os.utime(path, (earlier, earlier))
        with mock.patch.object(runs, "start", side_effect=AssertionError("started")):
            self.assertEqual(self.run_at(0)[1], [])


class PaneGateTest(ReconcileCase):
    def test_unread_waits_while_the_pane_has_recent_output(self):
        self.set_panes("main-acme-shop", output_min_ago=1)
        self.pointer("main-acme-shop", 12)
        self.assertEqual(self.run_at(0)[1], [])
        self.assertEqual(self.spawned, [])

    def test_unread_needs_the_pane(self):
        self.set_panes("div-acme-shop-build")
        self.pointer("main-acme-shop", 12)
        self.assertFalse([l for l in self.run_at(0)[1] if l.startswith("nudge")])   # closed: a reopen instead

    def test_orca_unreachable_skips_unread_and_logs_hourly(self):
        self.pointer("main-acme-shop", 12)
        with mock.patch.object(orca, "terminal", side_effect=orca.OrcaError("down")):
            self.assertEqual(self.run_at(0)[1], [])
            self.run_at(5)
        self.assertEqual(sum("orca-unreachable" in e for e in self.events()), 1)

    def test_main_unread_escalation_notifies_the_owner_with_the_last_nudge(self):
        self.pointer("main-acme-shop", 12)
        util.write_json(inbox.box("main-acme-shop") / deliver.LAST_NUDGE,
                        {"state": "user-typing", "at": "2026-10-16T00:30:00+00:00"})
        calls = []

        def run(argv, **kw):            # osascript shows it; launchctl: no Main waker loaded
            calls.append(argv)
            return shell.Result(0 if argv[0] == "osascript" else 113, "", "", argv)
        with mock.patch.object(notify, "PLATFORM", "darwin"), mock.patch.object(shell, "run", run):
            for m in (0, 10, 30, 70):
                self.run_at(m)
        notes = [c for c in calls if c[0] == "osascript"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][:3], ["osascript", "-e", "on run argv"])
        self.assertIn("last nudge: user-typing at", notes[0][-2])           # an argument, never script
        self.assertEqual(notes[0][-1], "ORCHE acme-shop")
        self.assertTrue(any("owner-notified project=acme-shop" in e for e in self.events()))


class StatusHintTest(ReconcileCase):
    """§6: `unread:`/`undelivered:` show ORCHE's live repair while the heartbeat is fresh, else the
    command; a pane the last pass found closed names `orche wake`, not `orche nudge`."""

    def status(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            load_cli().main(["status", "acme-shop"])
        return out.getvalue()

    def test_unread_lines(self):
        self.pointer("main-acme-shop", 12)
        self.assertIn("— run `orche nudge main-acme-shop`", self.status())           # no pass on file
        self.run_at(0)
        self.assertIn(f"— ORCHE retrying (1/3, next {reconcile._hhmm(T0 + dt.timedelta(minutes=8))})", self.status())
        self.set_panes("div-acme-shop-build", "main-docs-site")                      # Main's pane closes
        self.run_at(1)
        self.assertIn(f"— pane closed: ORCHE reopens it at {reconcile._hhmm(T0 + dt.timedelta(minutes=11))}",
                      self.status())
        self.now = T0 + dt.timedelta(hours=1)                                         # the heartbeat is stale
        self.assertIn("— pane closed: run `orche wake main-acme-shop`", self.status())

    def test_undelivered_lines(self):
        self.pointer("div-acme-shop-build", 12, unit="u7", via="dispatch", brief_rev=1, status="info",
                     **{"from": "main-acme-shop"})
        self.run_at(0)
        self.assertIn(f"undelivered: u7 → div-acme-shop-build (rev 1, 12 min) — ORCHE retrying (1/3, next "
                      f"{reconcile._hhmm(T0 + dt.timedelta(minutes=8))})", self.status())
        (self.home / "config.json").write_text(json.dumps({**WAKE, "reconcile": {"enabled": False}}))
        self.assertIn("— run `orche deliver u7`", self.status())                     # heartbeat off


class OwnerNoticeTest(ReconcileCase):
    """S6: the owner notification goes through notify.send, obeys `notify` (`inbox.notify` is gone) and
    has no sound in quiet hours; the waker class: a loaded com.orche.main-waker is told once a day."""

    def notes(self, cfg=None, quiet=False):
        self.now += dt.timedelta(minutes=10)          # past the per-project gap (flow.owner.NOTIFY_GAP_S)
        cfg = {**WAKE, **(cfg or {}), **({"flow": {"quiet_hours": "08:00-10:00"}} if quiet else {})}
        (self.home / "config.json").write_text(json.dumps(cfg))
        calls = []
        with mock.patch.object(notify, "PLATFORM", "darwin"), \
                mock.patch.object(shell, "run",
                                  lambda argv, **kw: calls.append(argv) or shell.Result(0, "", "", argv)):
            reconcile._tell_owner("acme-shop", 'a "quoted" text; do shell script', self.now)
        return calls

    def test_gates_and_quiet_hours(self):
        [loud] = self.notes()
        self.assertEqual(loud[-2:], ['a "quoted" text; do shell script', "ORCHE acme-shop"])
        self.assertIn('sound name "Glass"', loud[4])
        [hushed] = self.notes(quiet=True)
        self.assertNotIn("sound", " ".join(hushed[:-2]))
        self.assertEqual(self.notes({"notify": False}), [])
        self.assertEqual(len(self.notes({"inbox": {"wake": True, "quiet_s": 0, "notify": False}})), 1)  # ignored

    def test_a_loaded_main_waker_is_told_once_a_day(self):
        calls = []
        with mock.patch.object(notify, "PLATFORM", "darwin"), \
                mock.patch.object(shell, "run",
                                  lambda argv, **kw: calls.append(argv) or shell.Result(0, "", "", argv)):
            got = [self.run_at(m)[1] for m in (0, 5, 10)] + [self.run_at(24 * 60)[1]]
        notes = [c for c in calls if c[0] == "osascript"]
        self.assertEqual(len(notes), 2)                            # day 1 once, day 2 once
        self.assertIn("com.orche.main-waker is still loaded", notes[0][-2])
        self.assertEqual(got[0], ["notify the owner: com.orche.main-waker is loaded (once a day)"])
        self.assertEqual(got[1:3], [[], []])
        self.assertTrue(any("action=notify" in e for e in self.events()))


class ClosedMainTest(ReconcileCase):
    """C10 T4: a Main with waiting reports and no pane is reopened in place, REOPEN_CAP a day."""

    def orca_calls(self):
        path = self.home / "logs" / "fake-orca.jsonl"
        return [json.loads(l)["argv"] if l.startswith("{") else l for l in path.read_text().splitlines()] \
            if path.exists() else []

    def setUp(self):
        super().setUp()
        self.set_panes("div-acme-shop-build", "main-docs-site")      # main-acme-shop's pane is closed
        self.pointer("main-acme-shop", 12)

    def test_a_closed_main_is_reopened_once_without_a_switch(self):
        _code, lines = self.run_at(0)
        self.assertEqual(lines, ["reopen main-acme-shop pane (1 report(s) waiting, 12 min; reopen 1/2 today)"])
        calls = json.dumps(self.orca_calls())
        self.assertIn("create", calls)
        self.assertNotIn("switch", calls)
        self.assertEqual(self.spawned, [("nudge", "main-acme-shop")])
        state = util.read_json(reconcile.state_path())
        self.assertEqual(state["reopens:main-acme-shop"]["n"], 1)
        self.assertEqual(self.run_at(1)[1], [])                       # stall_s apart

    def test_two_reopens_a_day_then_one_owner_escalation(self):
        notes = []
        with mock.patch.object(reconcile, "_tell_owner", lambda p, t, now: notes.append(t)):
            got = [self.run_at(m)[1] for m in (0, 15, 30, 45, 60)]
        self.assertEqual(sum(1 for g in got for l in g if l.startswith("reopen ")), 2)
        self.assertEqual(len(notes), 1)
        self.assertIn("decide: `orche wake main-acme-shop`", notes[0])
        self.assertIn("default:", notes[0])

    def test_a_reopen_that_raises_still_counts_and_escalates_once(self):     # S2 (probe P5)
        calls, notes = [], []

        def boom(*a, **k):
            calls.append(1)
            raise orca.OrcaError("create failed")
        with mock.patch.object(hr, "project_pane", boom), \
                mock.patch.object(reconcile, "_tell_owner", lambda p, t, now: notes.append(t)):
            for m in range(0, 240, 5):
                self.run_at(m)
        self.assertEqual((len(calls), len(notes)), (2, 1))
        self.assertEqual(sum("action=reopen" in e and "failed=OrcaError" in e for e in self.events()), 2)

    def test_no_reopen_while_the_pane_exists(self):
        self.set_panes("main-acme-shop", "div-acme-shop-build", output_min_ago=1)
        self.assertEqual(self.run_at(0)[1], [])
        self.assertNotIn("create", json.dumps(self.orca_calls()))

    def test_quiet_hours_hold_the_reopen(self):
        cfg = {**WAKE, "flow": {"quiet_hours": "08:00-10:00"}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(self.run_at(0)[1], [])
        self.assertEqual(self.spawned, [])

    def test_dry_run_writes_nothing(self):
        _code, lines = self.run_at(0, dry_run=True)
        self.assertEqual(lines, ["would reopen main-acme-shop pane (1 report(s) waiting, 12 min; reopen 1/2 today)"])
        self.assertFalse(reconcile.state_path().exists())
        self.assertFalse(reconcile.heartbeat_path().exists())
        self.assertNotIn("create", json.dumps(self.orca_calls()))


class SilentUnitTest(RepairCase):
    """A delivered pane unit, its target's newest, not reported on at this rev, with no open child: one
    reminder when its pane idled SILENT_IDLE_S or the rev is 1.5 × TIMEBOX old, then one urgent info to
    the sender 30 min later or at 2 × TIMEBOX, whichever first (design §2.2; review B1, S4)."""

    def setUp(self):
        super().setUp()
        (self.home / "config.json").write_text(json.dumps({**WAKE, "delivery": "pane"}))
        self.b = self.brief()                       # TIMEBOX 20m: due at 30 min, escalated by 40 min
        self.age(25)
        self.idle = {"s": 60.0}                     # the pane works (output a minute ago)
        self.argv = []
        self._patches = [mock.patch.object(reconcile, "probe", lambda term: self.idle["s"]),
                         mock.patch.object(procs, "spawn_detached", lambda a, log, **kw: self.argv.append(a))]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def age(self, minutes, b=None):
        then = (T0 - dt.timedelta(minutes=minutes)).timestamp()
        os.utime((b or self.b).path, (then, then))

    def at(self, m, idle=None):
        if idle is not None:
            self.idle["s"] = float(idle)
        return self.run_at(m)[1]

    def reminders(self):
        return [a[a.index("--remind") + 1] for a in self.argv if "--remind" in a]

    def escalations(self):
        return [p for _x, p in inbox.pending("main-acme-shop") if p.get("from") == "orche"]

    def test_the_idle_boundary(self):
        self.assertEqual(self.at(0, reconcile.SILENT_IDLE_S - 1), [])         # 25 min old, idle 19:59
        self.assertEqual(self.at(1, reconcile.SILENT_IDLE_S),
                         [f"remind {self.b.unit} in div-acme-shop-build (rev 1, idle 20 min; try 1/1)"])
        self.assertEqual(self.reminders(), ["idle 20 min"])
        self.assertEqual(self.argv[0][-2:], ["--", self.b.unit])

    def test_the_timebox_boundary_with_a_working_pane(self):
        self.assertEqual(self.at(4), [])                                       # 29 min < 1.5 × 20 min
        self.assertEqual(self.at(5), [f"remind {self.b.unit} in div-acme-shop-build (rev 1, TIMEBOX 0h20m passed; "
                                      f"try 1/1)"])
        self.assertTrue(any("action=remind" in e for e in self.events()))

    def test_a_long_timebox_is_not_capped_and_an_unreadable_one_is_the_default(self):
        self.b.path.write_text(self.b.path.read_text().replace("20m", "8h"))
        self.age(7 * 60)
        self.assertEqual(self.at(0), [])                                       # 7 h < 1.5 × 8 h: no 4 h cap
        self.b.path.write_text(self.b.path.read_text().replace("8h", "soon"))
        self.age(44)
        self.assertEqual(self.at(0), [])                                       # the default 30 min: due at 45
        self.assertEqual(len(self.at(1)), 1)

    def test_one_reminder_per_rev_even_after_the_pane_answers_it(self):          # B1 (probe P1)
        for m, idle in ((0, 1300), (1, 30), (2, 1250), (3, 5), (4, 1900)):
            self.at(m, idle)
        with mock.patch.object(orca, "terminal", side_effect=orca.OrcaError("down")):
            self.at(6, 1900)                                                   # an outage resolves nothing
        self.at(8, 1300)
        self.assertEqual((self.reminders(), self.escalations()), (["idle 21 min"], []))
        for m in (15, 20, 60):                 # 2 × TIMEBOX (since + 40 min) comes before reminder + 30 min
            self.at(m, 1300)
        [esc] = self.escalations()
        self.assertTrue(esc["urgent"])
        self.assertEqual(esc["summary"], f"silent: {self.b.unit} rev 1 in div-acme-shop-build since "
                                         f"{reconcile._hhmm(T0 - dt.timedelta(minutes=25))}, reminded "
                                         f"{reconcile._hhmm(T0)} — STOP, AMEND or let it run")
        self.assertEqual(len(self.reminders()), 1)

    def test_the_escalation_comes_30_min_after_the_reminder_when_that_is_first(self):
        self.b.path.write_text(self.b.path.read_text().replace("20m", "8h"))
        self.age(25)
        self.at(0, 1300)                                                       # idle: reminded
        self.at(29, 1300)
        self.assertEqual(self.escalations(), [])
        self.at(30, 1300)
        self.assertEqual(len(self.escalations()), 1)

    def test_a_stop_report_settles_it(self):                                  # probe P6
        inbox.write(inbox.make(self.b.unit, "div-acme-shop-build", "main-acme-shop", "stop", brief_rev=1))
        self.assertEqual(self.at(10, 1300), [])

    def test_only_the_targets_newest_unit(self):
        newer = self.brief()
        self.age(25, newer)                       # same mtime: the later unit id is the newest
        self.assertEqual([line.split()[1] for line in self.at(1, 1300)], [newer.unit])

    def test_an_open_child_holds_it(self):
        self.brief(to="div-acme-shop-rnd", sender="div-acme-shop-build")       # the division's own dispatch
        self.assertEqual(self.at(1, 1300), [])

    def test_a_pending_dispatch_is_undelivered_not_silent(self):
        self.pointer(self.b.to, 12, unit=self.b.unit, via="dispatch", brief_rev=1, status="info",
                     **{"from": "main-acme-shop"})
        self.assertEqual(self.reminders(), [])
        self.assertTrue(all(not line.startswith("remind") for line in self.at(10, 1300)))

    def test_a_main_target_is_never_reminded(self):
        m = self.brief(to="main-acme-shop", sender="main-acme-shop")           # (sent by the division: a child)
        self.age(25, m)
        self.assertEqual([line.split()[1] for line in self.at(10, 1300)], [self.b.unit])

    def test_status_shows_stalled_lines(self):
        self.at(5)
        rows = reconcile.stalled("acme-shop")
        self.assertEqual([(r["cls"], r["target"], r["rev"], r["tries"], r["cap"], r["escalated"]) for r in rows],
                         [("silent", self.b.unit, 1, 1, 1, False)])
        self.assertEqual(reconcile.stalled_line(rows[0]),
                         f"  stalled: silent {self.b.unit} rev 1 in div-acme-shop-build (idle 1 min, TIMEBOX 0h20m) "
                         f"— reminded {reconcile._hhmm(T0 + dt.timedelta(minutes=5))}")
        out = io.StringIO()
        with redirect_stdout(out):
            load_cli().main(["status"])
        self.assertIn(f"stalled: silent {self.b.unit}", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            load_cli().main(["status", "--json"])
        row = next(r for r in json.loads(out.getvalue()) if r["project"] == "acme-shop")
        self.assertEqual(row["stalled"][0]["cls"], "silent")
        self.assertTrue(any("action=remind" in e for e in self.events()))


class PlanTest(unittest.TestCase):
    def test_plan_is_pure_and_resets_old_entries(self):
        c = reconcile.Condition("unread", "unread:s:p.json", "p", "s", 700)
        old = {"unread:s:p.json": {"first": (T0 - dt.timedelta(hours=49)).isoformat(), "attempts": ["a", "b", "c"],
                                   "escalated": True}, "gone": {"first": T0.isoformat(), "attempts": []}}
        snapshot = copy.deepcopy(old)
        actions, new = reconcile.plan([c], old, T0)
        self.assertEqual(old, snapshot)
        self.assertEqual([(a.kind, a.attempt) for a in actions], [("nudge", 1)])
        self.assertEqual(list(new), ["unread:s:p.json"])

    def test_not_ready_waits(self):
        c = reconcile.Condition("unread", "unread:s:p.json", "p", "s", 7000, ready=False)
        self.assertEqual(reconcile.plan([c], {}, T0)[0], [])

    def test_unseen_entries_are_carried_until_resolved_or_old(self):
        e = {"first": T0.isoformat(), "attempts": [T0.isoformat()], "escalated": False}
        old = {"unread:s:p.json": dict(e), "silent:u:1": dict(e, attempts=[], escalated=True),
               "undelivered:u:1": dict(e), "fresh:x": {"first": T0.isoformat(), "attempts": []}}
        _a, new = reconcile.plan([], old, T0 + dt.timedelta(hours=1), resolved={"undelivered:u:1"})
        self.assertEqual(sorted(new), ["silent:u:1", "unread:s:p.json"])
        self.assertEqual(reconcile.plan([], old, T0 + dt.timedelta(hours=48))[1], {})


class ClosedUnitTest(ReconcileCase):
    """S9: an active division without a pane is woken (hr.wake), twice a day, then Main hears once; a
    dormant one is never woken: one urgent info to Main."""

    def setUp(self):
        super().setUp()
        self.set_panes("main-acme-shop", "main-docs-site")
        self.woke = []
        p = mock.patch.object(hr, "wake", lambda reg, m, **kw: self.woke.append(m.session))
        p.start()
        self.addCleanup(p.stop)

    def to_main(self):
        return [p["summary"] for _x, p in inbox.pending("main-acme-shop") if p.get("from") == "orche"]

    def test_an_active_division_is_woken_twice_then_main_hears_once(self):
        self.pointer("div-acme-shop-build", 12, unit="u3", **{"from": "main-acme-shop"})
        got = [self.run_at(m)[1] for m in (0, 5, 10, 20, 30, 40)]
        self.assertEqual(got[0], ["wake div-acme-shop-build pane (1 report(s) waiting, 12 min; wake 1/2 today)"])
        self.assertEqual(self.woke, ["div-acme-shop-build"] * 2)
        [esc] = self.to_main()
        self.assertTrue(esc.startswith("closed: div-acme-shop-build has 1 report(s) waiting (32 min) after 2 wakes "
                                       "today — run `orche wake div-acme-shop-build`"), esc)

    def test_a_dormant_division_is_never_woken(self):
        self.pointer("div-acme-shop-rnd", 12, unit="u4", **{"from": "main-acme-shop"})
        got = [self.run_at(m)[1] for m in (0, 30, 90)]
        self.assertEqual(got, [["escalate closed div-acme-shop-rnd (dormant, 12 min)"], [], []])
        self.assertEqual(self.woke, [])
        [esc] = self.to_main()
        self.assertIn("closed: div-acme-shop-rnd has 1 report(s) waiting (12 min) while dormant", esc)

    def test_quiet_hours_hold_it(self):
        (self.home / "config.json").write_text(json.dumps({**WAKE, "flow": {"quiet_hours": "08:00-10:00"}}))
        self.pointer("div-acme-shop-build", 12, unit="u3", **{"from": "main-acme-shop"})
        self.assertEqual(self.run_at(0)[1], [])
        self.assertEqual(self.woke, [])


class HeartbeatWordingTest(ReconcileCase):
    def beat(self, minutes_ago, **extra):
        util.write_json(reconcile.heartbeat_path(), {"at": (T0 - dt.timedelta(minutes=minutes_ago)).isoformat(),
                                                     "day": T0.date().isoformat(), "repairs_today": 2, **extra})

    def test_never(self):
        self.assertEqual(reconcile.heartbeat_line(T0), "heartbeat: never — install the job with orche install")
        self.assertEqual(reconcile.doctor_row(T0)[0], "WARN")
        self.assertTrue(reconcile.heartbeat_json(T0)["stale"])

    def test_fresh(self):
        self.beat(3)
        self.assertEqual(reconcile.heartbeat_line(T0), "heartbeat: last pass 3 min ago (every 5 min, 2 repairs today)")
        self.assertEqual(reconcile.doctor_row(T0), ("OK", "heartbeat", "last pass 3 min ago (every 5 min)"))
        self.assertFalse(reconcile.heartbeat_json(T0)["stale"])

    def test_stale(self):
        self.beat(11)
        self.assertEqual(reconcile.heartbeat_line(T0),
                         "heartbeat: STALE — last pass 11 min ago (every 5 min); run orche doctor")
        self.assertEqual(reconcile.doctor_row(T0)[0], "WARN")

    def test_off(self):
        cfg = {"reconcile": {"enabled": False, "every_s": 300}}
        self.assertEqual(reconcile.heartbeat_line(T0, cfg), "heartbeat: off (reconcile.enabled false)")
        self.assertEqual(reconcile.doctor_row(T0, cfg)[0], "OK")

    def test_status_prints_the_line(self):
        self.beat(0)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            load_cli().main(["status"])
        self.assertIn("heartbeat: last pass 0 min ago (every 5 min, 2 repairs today)", out.getvalue())


class LineageCloseRepairTest(RepairCase):
    """C10 T8: the heartbeat posts a lineage close a crash missed, once; a stale lineage is left alone."""

    def lineage(self, n):
        made = [self.brief() for _ in range(n)]
        for b in made:
            text = b.path.read_text().splitlines()
            b.path.write_text("\n".join(f"lineage: {made[0].unit}   parent: -" if ln.startswith("lineage: ") else ln
                                        for ln in text) + "\n")
        return made

    def closes(self):
        return [x for _p, x in inbox.pending("main-acme-shop") if x.get("lineage_closed")]

    def test_the_pass_closes_a_final_lineage_once(self):
        a, b = self.lineage(2)
        self.pointer("main-acme-shop", 5, unit=a.unit, lineage=a.unit)
        self.pointer("main-acme-shop", 4, unit=b.unit, lineage=a.unit, status="failed")
        _code, lines = self.run_at(0, dry_run=True)
        self.assertIn(f"would close lineage {a.unit} (acme-shop)", lines)
        self.assertEqual(self.closes(), [])
        _code, lines = self.run_at(0)
        self.assertIn(f"closed lineage {a.unit} (acme-shop)", lines)
        [close] = self.closes()
        self.assertIn("1 done, 1 failed, 0 stopped", close["summary"])
        _code, lines = self.run_at(1)
        self.assertNotIn(f"closed lineage {a.unit} (acme-shop)", lines)
        self.assertEqual(len(self.closes()), 1)

    def test_a_lineage_older_than_a_day_is_not_closed(self):
        a, b = self.lineage(2)
        self.pointer("main-acme-shop", 60 * 25, unit=a.unit, lineage=a.unit)
        self.pointer("main-acme-shop", 60 * 25 + 1, unit=b.unit, lineage=a.unit)
        self.run_at(0)
        self.assertEqual(self.closes(), [])

    def test_project_filter_json_events_count_and_a_dry_run_without_a_lock(self):
        from orche.flow import lineage
        a, b = self.lineage(2)
        self.pointer("main-acme-shop", 5, unit=a.unit, lineage=a.unit)
        self.pointer("main-acme-shop", 4, unit=b.unit, lineage=a.unit)
        out = []
        self.assertEqual(reconcile.run_pass(dry_run=True, out=out.append, keys=["acme-shop"], as_json=True), 0)
        self.assertFalse((lineage.folder("acme-shop") / ".lock").exists())
        [doc] = [json.loads(x) for x in out]
        self.assertEqual([(r["action"], r["target"]) for r in doc["actions"]], [("lineage-close", a.unit)])
        self.assertEqual(reconcile.run_pass(out=out.append, keys=["docs-site"]), 0)    # another project
        self.assertEqual(self.closes(), [])
        out.clear()
        self.assertEqual(reconcile.run_pass(out=out.append, as_json=True), 0)
        [doc] = [json.loads(x) for x in out]                            # one JSON document, no plain line
        self.assertEqual([r["line"] for r in doc["actions"]], [f"closed lineage {a.unit} (acme-shop)"])
        self.assertEqual(len(self.closes()), 1)
        self.assertIn(f"reconcile action=lineage-close project=acme-shop lineage={a.unit}", "\n".join(self.events()))
        self.assertTrue(any("reconcile pass took=" in e and e.endswith("actions=1") for e in self.events()))
        self.assertEqual(json.loads(reconcile.heartbeat_path().read_text())["actions"], 1)


if __name__ == "__main__":
    unittest.main()
