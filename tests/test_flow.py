"""Relay guards and the owner's pause (T5, lib/orche/flow/guard.py): the autonomy window, G1 revisions,
G2 blocked twice, G3 rate, G7 refusal streak, G8 pause, and where they show (`orche status`, the heartbeat)."""
import datetime as _dt
import json
import os
import time
from unittest import mock

import support
from orche import inbox, procs, units, util
from orche.flow import guard, owner, shape
from orche.runs import history
import test_cmd_dispatch
from test_cmd_dispatch import BODY, GIFT
import test_hooks
from test_hooks import HookCase

from test_reconcile import RepairCase
from test_runner import RunnerCase

THIRD = BODY.replace("Add coupon field", "Add loyalty badge")


def amend(unit, rev, goal="Add coupon field only"):
    return f"AMEND {unit} rev {rev}\nSUPERSEDES rev {rev - 1}: smaller\nGOAL      {goal} {rev}\n"


class FlowCase(HookCase):
    orche = test_cmd_dispatch.CmdDispatchTest.orche
    add_project_b = test_hooks.DispatchRelayTest.add_project_b

    def first(self, body=BODY):
        """The unit folder this dispatch made: the new one, not the newest mtime (a later write into an older
        folder, or a coarse clock, would otherwise name the wrong unit)."""
        before = set(self.units_dir().iterdir()) if self.units_dir().is_dir() else set()
        res = self.orche("dispatch", stdin=body)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        made = [p for p in self.units_dir().iterdir() if p not in before and p.is_dir()]
        return sorted(made, key=lambda p: p.stat().st_mtime)[-1]

    def revs(self, folder, *ns, ago=10):
        t = time.time() - ago
        for n in ns:
            p = folder / f"BRIEF.rev{n}.md"
            p.write_text((folder / "BRIEF.md").read_text())
            os.utime(p, (t, t))

    def owner_spoke(self, later=0):
        path = guard.folder("acme") / "owner-seen"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        t = time.time() + later
        os.utime(path, (t, t))

    def blocked_twice(self, unit):
        for i in range(2):
            ptr = inbox.make(unit, "div-acme-build", "main-acme", "blocked", summary=f"stuck {i}")
            inbox.write(ptr)


class RevisionsTest(FlowCase):
    def test_three_revisions_pass_the_fourth_is_refused_and_the_owner_resets_it(self):
        folder = self.first()
        unit = folder.name
        for rev in (2, 3, 4):                                          # 0, 1, 2 revisions before each: pass
            res = self.orche("dispatch", stdin=amend(unit, rev))
            self.assertEqual(res.returncode, 0, res.stdout)
        res = self.orche("dispatch", stdin=amend(unit, 5))
        self.assertEqual(res.returncode, 2)
        self.assertIn(f"{unit} was revised 3 times since", res.stdout)
        self.assertIn("(rev 2–4). Ask the owner before revising it again: close your reply with OWNER:",
                      res.stdout)
        self.assertIn(f"flow refused rule=G1 by=main-acme unit={unit} n=3", self.events())
        for p in folder.glob("BRIEF.rev*.md"):
            os.utime(p, (time.time() - 30, time.time() - 30))
        self.owner_spoke()
        res = self.orche("dispatch", stdin=amend(unit, 5))
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("delivered", res.stdout)



class ConfigCapsTest(FlowCase):
    """0.9.0 scale: every flow cap is config; a malformed value falls back to the default."""

    def flow(self, **kw):
        cfg = self.home / "config.json"
        cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "flow": kw}))

    def test_revisions_cap_from_config(self):
        self.flow(revisions=1)
        unit = self.first().name
        self.assertEqual(self.orche("dispatch", stdin=amend(unit, 2)).returncode, 0)
        res = self.orche("dispatch", stdin=amend(unit, 3))
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn(f"{unit} was revised 1 times since", res.stdout)

    def test_rate_cap_from_config(self):
        self.flow(briefs_per_hour=5)
        folder = self.first()
        self.revs(folder, *range(2, 5))                                  # rev1 + 3 = 4
        self.assertEqual(self.orche("dispatch", stdin=GIFT).returncode, 0)
        res = self.orche("dispatch", stdin=THIRD)
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("(cap 5 an hour)", res.stdout)

    def test_malformed_cap_uses_the_default(self):
        for bad in ("lots", -1, True, None):
            self.flow(revisions=bad, depth=[1])
            self.assertEqual((guard.cap("revisions"), guard.cap("depth")), (3, 3), bad)
        self.flow(revisions=7, fanout=20.0, runs_per_unit=0)
        self.assertEqual((guard.cap("revisions"), guard.cap("fanout"), guard.cap("runs_per_unit")), (7, 20, 0))

class BlockedTwiceTest(FlowCase):
    def test_amend_and_same_goal_brief_refused_other_blocks_still_relay(self):
        unit = self.first().name
        self.blocked_twice(unit)
        res = self.orche("dispatch", stdin=f"{GIFT}\n---\n{amend(unit, 2)}\n---\n{THIRD}")
        self.assertEqual(res.returncode, 2, res.stdout)
        lines = res.stdout.splitlines()
        self.assertTrue(lines[0].startswith("block 1: delivered"), lines)
        self.assertIn(f"{unit} reported blocked 2 times in a row since", lines[1])
        self.assertIn("Ask the owner before re-sending it", lines[1])
        self.assertTrue(lines[2].startswith("block 3: delivered"), lines)
        res = self.orche("dispatch", stdin=BODY.replace("Add coupon field to checkout form",
                                                        "add coupon  field to checkout form"))
        self.assertEqual(res.returncode, 2, res.stdout)                 # same GOAL, case and spaces aside
        self.assertIn("this brief sends div-acme-build the same GOAL again", res.stdout)

    def test_one_blocked_report_is_not_enough(self):
        unit = self.first().name
        inbox.write(inbox.make(unit, "div-acme-build", "main-acme", "blocked"))
        res = self.orche("dispatch", stdin=amend(unit, 2))
        self.assertEqual(res.returncode, 0, res.stdout)


class RateTest(FlowCase):
    def test_twenty_in_the_hour_refuses_the_next_brief(self):
        folder = self.first()
        self.revs(folder, *range(2, 20))                                 # rev1 + 18 = 19
        res = self.orche("dispatch", stdin=GIFT)
        self.assertEqual(res.returncode, 0, res.stdout)                  # the 20th
        res = self.orche("dispatch", stdin=THIRD)
        self.assertEqual(res.returncode, 2)
        self.assertIn("acme sent 20 briefs and AMENDs since", res.stdout)
        self.assertIn("(cap 20 an hour)", res.stdout)

    def test_flow_disabled_turns_the_counters_off(self):
        folder = self.first()
        self.revs(folder, *range(2, 21))
        cfg = self.home / "config.json"
        cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "flow": {"enabled": False}}))
        res = self.orche("dispatch", stdin=GIFT)
        self.assertEqual(res.returncode, 0, res.stdout)


class PauseTest(FlowCase):
    def test_pause_refuses_briefs_stop_passes_streak_suffix_and_resume_needs_the_owner(self):
        unit = self.first().name
        res = self.orche("flow", "pause", "acme", "--reason", "lunch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("flow pause project=acme by=", self.events())
        res = self.orche("dispatch", stdin=f"{GIFT}\n---\n{THIRD}\n---\n{amend(unit, 2)}")
        self.assertEqual(res.returncode, 2)
        lines = res.stdout.splitlines()
        self.assertIn("acme is paused by the owner since", lines[0])
        self.assertIn("(lunch). Nothing is relayed until the owner resumes it", lines[0])
        self.assertNotIn("in a row", lines[1])
        self.assertIn("(refusal 3 in a row: stop re-sending; tell the user what you tried)", lines[2])
        self.assertIn("flow refused rule=G7 by=main-acme", self.events())
        res = self.orche("dispatch", stdin=f"STOP {unit} owner paused\n")
        self.assertEqual(res.returncode, 0, res.stdout)
        status = self.orche("status")
        self.assertIn("paused: by the owner since", status.stdout)
        self.assertIn("(lunch) — resume on their word: orche flow resume acme", status.stdout)
        res = self.orche("flow", "resume", "acme")
        self.assertEqual(res.returncode, 3)
        self.assertIn("resume needs the owner's word after", res.stderr)
        self.owner_spoke(later=5)
        res = self.orche("flow", "resume", "acme")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIsNone(guard.paused("acme"))
        self.assertIn("flow resume project=acme by=", self.events())

    def test_unknown_project(self):
        self.assertEqual(self.orche("flow", "pause", "nope").returncode, 2)


class OwnerSeenTest(FlowCase):
    def seen(self):
        return (guard.folder("acme") / "owner-seen").exists()

    def test_orche_prompts_and_bare_drain_do_not_count(self):
        for prompt in ("[orche] inbox: 1 report", "[orche] report due: u1", "[drain]", " /drain "):
            self.run_hook("report_inject.py", self.payload("prompt.json", prompt=prompt), ORCHE_SESSION="main-acme")
            self.assertFalse(self.seen(), prompt)
        self.run_hook("report_inject.py", self.payload("prompt.json"), ORCHE_SESSION="main-acme")
        self.assertTrue(self.seen())

    def test_an_owner_prompt_answers_the_open_ask(self):              # T10
        owner.record("acme", "pick a plan", kind="owner", source="main",
                     now=_dt.datetime.now().astimezone() - _dt.timedelta(minutes=5))
        self.run_hook("report_inject.py", self.payload("prompt.json", prompt="[orche] inbox: 1 report"),
                      ORCHE_SESSION="main-acme")
        self.assertIsNotNone(owner.open_ask("acme"))                    # ORCHE's own prompt is not the owner's
        self.run_hook("report_inject.py", self.payload("prompt.json"), ORCHE_SESSION="main-acme")
        self.assertIsNone(owner.open_ask("acme"))
        self.assertIn("answered_at", owner.read_ask("acme"))

    def test_a_division_prompt_does_not_count(self):
        self.run_hook("report_inject.py", self.payload("prompt.json", cwd=str(self.root / "acme-build")),
                      ORCHE_SESSION="div-acme-build")
        self.assertFalse(self.seen())


class LoopingStatusTest(FlowCase):
    def test_looping_line(self):
        folder = self.first()
        self.revs(folder, 2, 3, 4)
        status = self.orche("status")
        self.assertIn(f"looping: {folder.name} — revised 3× without the owner since", status.stdout)
        self.assertIn("the next AMEND needs the owner", status.stdout)

    def test_json_gives_looping_and_paused_one_shape_each(self):
        folder = self.first()
        self.revs(folder, 2, 3, 4)
        for _ in range(3):
            inbox.post(inbox.make("feat-x", "div-acme-build", "main-acme", "failed", brief_rev=1, summary="same"))
        history.append("acme", row("other", 30.0), 1)
        self.assertEqual(self.orche("flow", "pause", "acme", "--reason", "lunch").returncode, 0)
        rows = json.loads(self.orche("status", "--json").stdout)
        acme = next(r for r in (rows if isinstance(rows, list) else [rows]) if r["project"] == "acme")
        self.assertNotIn("spend", acme)
        self.assertEqual([(u["unit"], u["kind"]) for u in acme["looping"]],
                         [(folder.name, "revisions"), ("feat-x", "repeats")])
        for u in acme["looping"]:
            self.assertEqual(set(u), {"unit", "kind", "n", "since", "status", "rev"})
        self.assertEqual([(p["by"], p["reason"]) for p in acme["paused"]], [("spend", None), ("owner", "lunch")])
        for p in acme["paused"]:
            self.assertEqual(set(p), {"by", "since", "spent", "cap", "reason"})


class PausedHeartbeatTest(RepairCase):
    def test_paused_project_gets_no_redelivery_but_drains(self):
        b = self.dispatched()
        guard.pause("acme-shop", "owner")
        self.run_at(0)
        self.assertEqual(self.spawned, [])
        self.run_json(b, started=util.now_iso(), stop_reason="done")    # this revision ran (RUN.json `started`)
        _code, lines = self.run_at(1)
        self.assertEqual(lines, [f"drain {b.unit} in div-acme-shop-build (rev 1 already ran or reported)"])




def row(unit, cost=0.0, stop_reason="end_turn", at=None, rev=1):
    at = at or util.now_iso()
    return {"unit": unit, "rev": rev, "to": "div-acme-build", "model": "m", "effort": "medium", "status": "done",
            "stop_reason": stop_reason, "cost_usd": cost, "num_turns": 3, "started": at, "finished": at}


def backdated(seconds):
    return units.now().astimezone().replace(microsecond=0) - __import__("datetime").timedelta(seconds=seconds)


class HistoryTest(FlowCase):
    def test_append_read_window_sums_and_a_corrupt_line_is_skipped(self):
        old = backdated(10 ** 6).isoformat()
        history.append("acme", row("u1", 4.5, at=old), 1)
        with open(history.path("acme"), "a") as f:
            f.write("{not json\n[1, 2]\n")
        history.append("acme", row("u1", 2.25), 2)
        history.append("acme", row("u2", 1.0), 1)
        history.append("acme", row("u2", 0, stop_reason="busy"), 1)
        rows = history.read("acme")
        self.assertEqual([r["unit"] for r in rows], ["u1", "u1", "u2", "u2"])
        self.assertEqual(set(rows[0]), set(history.KEYS))
        self.assertEqual(rows[1]["rev"], 2)
        since = backdated(60)
        self.assertEqual(history.spend("acme", since), 3.25)
        self.assertEqual(history.spend("acme", since, "u1"), 2.25)
        self.assertEqual(history.spend("acme", backdated(2 * 10 ** 6)), 7.75)
        self.assertEqual(history.runs_of("acme", "u2", since), 1)      # busy never ran
        self.assertEqual(history.read("nope"), [])

    def test_a_line_of_bad_bytes_is_skipped_and_the_rest_still_count(self):
        history.append("acme", row("u1", 1.5), 1)
        with open(history.path("acme"), "ab") as f:
            f.write(b'{"unit": "u1", "cost_usd": 9\xff\xfe}\n')
        history.append("acme", row("u1", 2.0), 2)
        self.assertEqual([r["cost_usd"] for r in history.read("acme")], [1.5, 2.0])
        self.assertEqual(history.spend("acme", backdated(60)), 3.5)

    def test_under_the_caps_a_run_passes(self):
        unit = self.first().name
        history.append("acme", row("other", 29.99), 1)                # $29.99 < $30
        for _ in range(3):                                            # 3 runs < 4
            history.append("acme", row(unit), 1)
        self.assertIsNone(guard.run_refusal("acme", unit))
        self.assertEqual(self.orche("dispatch", stdin=amend(unit, 2)).returncode, 0)

    def test_append_never_raises(self):
        history.path("acme").parent.mkdir(parents=True, exist_ok=True)
        history.path("acme").mkdir()                                  # a directory: os.open fails
        history.append("acme", row("u1"), 1)
        self.assertIn("runner: cannot append history for u1", (self.home / "logs" / "hooks.log").read_text())


class ProjectSpendCapTest(FlowCase):
    def test_cap_refuses_briefs_at_the_relay_stop_passes_status_shows_it_and_the_owner_resets(self):
        unit = self.first().name
        history.append("acme", row("other", 30.0), 1)
        res = self.orche("dispatch", stdin=GIFT)
        self.assertEqual(res.returncode, 2)
        self.assertRegex(res.stdout, r"acme spent \$30\.00 on headless runs since \d\d:\d\d without the owner "
                                     r"\(cap \$30\)\. Ask the owner before spending more: close your reply with OWNER:")
        self.assertIn("flow refused rule=G4 by=main-acme unit=- n=30", self.events())
        res = self.orche("dispatch", stdin=f"STOP {unit} spend\n")
        self.assertEqual(res.returncode, 0, res.stdout)
        status = self.orche("status")
        self.assertRegex(status.stdout, r"paused: autonomous spend \$30\.00 since \d\d:\d\d \(cap \$30\) — "
                                        r"until the owner answers")
        self.owner_spoke(later=5)
        self.assertEqual(self.orche("dispatch", stdin=GIFT).returncode, 0)
        self.assertNotIn("autonomous spend", self.orche("status").stdout)

    def test_unit_cap_refuses_its_amend_only(self):
        unit = self.first().name
        history.append("acme", row(unit, 10.0), 1)
        res = self.orche("dispatch", stdin=amend(unit, 2))
        self.assertEqual(res.returncode, 2)
        self.assertIn(f"{unit} spent $10.00 on headless runs since", res.stdout)
        self.assertIn("(cap $10 a unit)", res.stdout)
        self.assertEqual(self.orche("dispatch", stdin=GIFT).returncode, 0)


class RepeatTest(FlowCase):
    def post(self, unit, status, rev=1):
        return json.loads(inbox.post(inbox.make(unit, "div-acme-build", "main-acme", status, brief_rev=rev,
                                                summary=f"text {time.time_ns()}")).read_text())

    def test_a_repeat_is_quiet_a_new_status_or_rev_wakes_and_status_names_the_loop(self):
        unit = self.first().name
        first = self.post(unit, "failed")
        self.assertNotIn("quiet", first)
        second = self.post(unit, "failed")
        self.assertEqual((second["quiet"], second["repeat"]), (True, 2))
        self.assertNotIn("looping: " + unit, self.orche("status").stdout)
        third = self.post(unit, "failed")
        self.assertEqual(third["repeat"], 3)
        self.assertIn(f"looping: {unit} — same report 3× (failed, rev 1): repeats did not wake Main",
                      self.orche("status").stdout)
        self.assertNotIn("quiet", self.post(unit, "done"))            # a new status wakes
        self.assertNotIn("quiet", self.post(unit, "done", rev=2))     # a new revision wakes
        self.assertNotIn("same report", self.orche("status").stdout)

    def test_a_failing_shape_is_logged_and_the_pointer_still_written(self):
        unit = self.first().name
        with mock.patch.object(shape, "shape", side_effect=RuntimeError("boom")):
            ptr = self.post(unit, "failed")
        self.assertEqual(ptr["status"], "failed")
        self.assertIn(f"inbox: shape failed for {unit}: boom", (self.home / "logs" / "hooks.log").read_text())


    def test_an_urgent_repeat_is_quiet_too(self):
        unit = self.first().name
        woke = []
        with mock.patch.object(inbox, "wake", side_effect=lambda s, urgent=False: woke.append(s)):
            ptrs = [json.loads(inbox.post(inbox.make(unit, "div-acme-build", "main-acme", "info", urgent=True,
                                                     summary=f"note {i}")).read_text()) for i in range(4)]
        self.assertEqual(len(woke), 1)
        self.assertEqual([p.get("quiet") for p in ptrs], [None, True, True, True])

    def test_two_external_requests_or_adhoc_reports_both_wake(self):
        self.add_project_b()
        for summary in ("Need: coupon API", "Unrelated: share design tokens"):
            res = self.orche("report", "external-request", "--to", "main-beta", "--summary", summary)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        ptrs = [json.loads(p.read_text()) for p in inbox.box("main-beta").glob("*.json")]
        self.assertEqual(len(ptrs), 2)
        self.assertEqual([p.get("quiet") for p in ptrs], [None, None])
        for unit in ("adhoc-div-acme-build-20261023", "xreq-acme-x-20261023"):
            self.post(unit, "info")
            self.assertNotIn("quiet", self.post(unit, "info"))

    def test_runner_reports_are_not_shaped(self):
        unit = self.first().name
        posts = [json.loads(inbox.post(inbox.make(unit, "div-acme-build", "main-acme", "failed", via="runner",
                                                  summary=f"run {i}")).read_text()) for i in range(2)]
        self.assertEqual([p.get("quiet") for p in posts], [None, None])

    def test_a_repeat_from_before_the_owner_spoke_does_not_count(self):
        unit = self.first().name
        old = inbox.make(unit, "div-acme-build", "main-acme", "failed", summary="before")
        old["at"] = (units.now() - _dt.timedelta(seconds=60)).isoformat(timespec="seconds")
        inbox.post(old)
        self.owner_spoke(later=-30)
        self.assertNotIn("quiet", self.post(unit, "failed"))
        self.assertEqual(self.post(unit, "failed")["repeat"], 2)


class RunCapTest(RunnerCase):
    def test_run_cap_refuses_with_exit_3_and_one_quiet_pointer(self):
        unit = self.brief()
        for i in range(4):
            history.append("acme", row(unit), 1)
        history.append("acme", row(unit, stop_reason="busy"), 1)
        code, out, err = self.orche("run", unit)
        self.assertEqual(code, 3, out + err)
        self.assertRegex(err, rf"not started: {unit} ran 4 times since \d\d:\d\d without the owner \(cap 4\)")
        self.assertFalse((self.fake / "stdin.txt").exists())          # claude never ran
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr.get("quiet")), ("info", True))
        self.assertIn("(cap 4); ask the owner", ptr["summary"])
        self.assertIn(f"flow refused rule=G5 by=", self.events())
        self.assertEqual(len(history.read("acme")), 5)                # a capped start adds no line
        ask = owner.read_ask("acme")                                  # T9: the parked refusal is the owner's ask
        self.assertEqual((ask["kind"], ask["source"], ask["unit"]), ("guard", "runner", unit))
        self.assertTrue(ask["text"].startswith(f"not started: {unit} ran 4 times since "), ask["text"])

    def test_unit_spend_cap_refuses_the_run(self):
        unit = self.brief()
        history.append("acme", row(unit, 10.0), 1)
        code, out, err = self.orche("run", unit)
        self.assertEqual(code, 3, out + err)
        self.assertIn(f"not started: {unit} spent $10.00 on headless runs since", out + err)
        self.assertEqual(len(self.pointers("main-acme")), 1)

    def test_a_finished_run_appends_its_history_line(self):
        unit = self.brief()
        self.persona()
        self.reply("STATUS: done\nok", cost=0.5)
        code, out, err = self.orche("run", unit)
        self.assertEqual(code, 0, out + err)
        [line] = history.read("acme")
        rec = self.run_record(unit)
        self.assertEqual(line, {**{k: rec.get(k) for k in history.KEYS}, "rev": 1, "kind": "work"})
        self.assertEqual((line["unit"], line["cost_usd"]), (unit, 0.5))


class DrainAfterSpawnTest(RunnerCase):
    """Review S1: the dispatch pointer a run replaces is drained only once the run exists."""

    def pending_dispatch(self, unit):
        inbox.write(inbox.make(unit, "main-acme", "div-acme-build", "info", via="dispatch"))

    def dispatch_left(self):
        return [p for p in self.pointers("div-acme-build") if p.get("via") == "dispatch"]

    def test_a_failed_spawn_leaves_the_pointer_pending(self):
        from orche import runs
        unit = self.brief()
        self.pending_dispatch(unit)
        with mock.patch.object(procs.subprocess, "Popen", side_effect=OSError("no exec")):
            with self.assertRaises(OSError):
                runs.start(unit)
        self.assertEqual(len(self.dispatch_left()), 1)

    def test_a_successful_spawn_drains(self):
        from orche import runs
        unit = self.brief()
        self.pending_dispatch(unit)
        with mock.patch.object(procs.subprocess, "Popen", return_value=mock.Mock(pid=4242)):
            runs.start(unit)
        self.assertEqual(self.dispatch_left(), [])

    def test_a_held_target_lock_is_logged(self):
        from orche import deliver, runs
        unit = self.brief()
        self.pending_dispatch(unit)
        with mock.patch.object(procs.subprocess, "Popen", return_value=mock.Mock(pid=4242)), \
                mock.patch.object(deliver, "lock_free", return_value=False):
            runs.start(unit)
        self.assertEqual(len(self.dispatch_left()), 1)
        self.assertIn(f"run unit={unit} lock-held", self.events())

    def test_a_foreground_run_drains_after_acquire(self):
        unit = self.brief()
        self.persona()
        self.pending_dispatch(unit)
        self.action(f"ls {inbox.box('div-acme-build')} > {self.fake}/seen.txt\n")
        self.reply("STATUS: done\nok")
        code, out, err = self.orche("run", unit)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.dispatch_left(), [])
        self.assertNotIn(".json", (self.fake / "seen.txt").read_text())   # drained before claude ran


TO_FEAT = BODY.replace("TO        build", "TO        feat-acme-checkout")


def header(folder, lineage, parent):
    """Rewrite a unit's BRIEF.md `lineage:` line (tests build parent chains by hand)."""
    p = folder / "BRIEF.md"
    lines = [f"lineage: {lineage}   parent: {parent}" if ln.startswith("lineage: ") else ln
             for ln in p.read_text().splitlines()]
    p.write_text("\n".join(lines) + "\n")


class LineageTest(FlowCase):
    def brief_of(self, folder):
        from orche import brief, registry
        return brief.find(registry.load(), folder.name, project="acme")

    def turn(self, prompt, *ptrs):
        for p in ptrs:
            inbox.write(p)
        self.run_hook("report_inject.py", self.payload("prompt.json", prompt=prompt), ORCHE_SESSION="main-acme")

    def test_owner_turn_starts_a_lineage_its_other_briefs_share(self):
        self.turn("please add a coupon field")
        res = self.orche("dispatch", stdin=f"{BODY}\n---\n{GIFT}")
        self.assertEqual(res.returncode, 0, res.stdout)
        a, b = sorted(self.units_dir().iterdir(), key=lambda p: p.stat().st_mtime)
        self.assertIn(f"lineage: {a.name}   parent: -\n", (a / "BRIEF.md").read_text())
        self.assertEqual(self.brief_of(b).lineage, a.name)
        c = self.first(THIRD)                                       # a later dispatch in the same turn
        self.assertEqual(self.brief_of(c).lineage, a.name)

    def test_a_wake_turn_with_one_lineage_joins_it(self):
        self.turn("[orche] inbox: 1 report", inbox.make("u-old", "div-acme-build", "main-acme", "done",
                                                        lineage="acme-root-20261001-1"))
        self.assertEqual(self.brief_of(self.first()).lineage, "acme-root-20261001-1")

    def test_a_two_lineage_wake_turn_starts_a_new_one(self):
        self.turn("[orche] inbox: 2 reports",
                  inbox.make("u-a", "div-acme-build", "main-acme", "done", lineage="L-a"),
                  inbox.make("u-b", "div-acme-build", "main-acme", "done", lineage="L-b"))
        folder = self.first()
        self.assertEqual(self.brief_of(folder).lineage, folder.name)

    def test_orche_notes_name_their_units_lineage_so_a_chain_of_wake_turns_stays_in_it(self):
        from orche.flow import lineage
        self.turn("please add a coupon field")
        u1 = self.first()
        notes = [x for _p, x in inbox.pending("main-acme") if x.get("from") == "orche"]
        self.assertTrue(notes and not any(x.get("lineage") for x in notes), notes)     # `relayed U1 …`
        with mock.patch.object(inbox, "wake"):
            inbox.post(inbox.make(u1.name, "div-acme-build", "main-acme", "done", summary="d", lineage=u1.name))
        self.turn("[orche] inbox: new reports",              # + a heartbeat note about a session, not a unit
                  inbox.make("div-acme-build", "orche", "main-acme", "info", urgent=True, summary="unread"))
        self.assertEqual(lineage.read_turn("main-acme")["lineages"], [u1.name])
        u2 = self.first(GIFT)
        self.assertEqual(self.brief_of(u2).lineage, u1.name)
        with mock.patch.object(inbox, "wake"):
            inbox.post(inbox.make(u2.name, "div-acme-build", "main-acme", "done", summary="d", lineage=u1.name))
        self.turn("[orche] inbox: new reports")             # `relayed U2 …`, U2's report and the close
        self.assertEqual(lineage.read_turn("main-acme")["lineages"], [u1.name])
        self.assertEqual(self.brief_of(self.first(THIRD)).lineage, u1.name)

    def test_a_prompt_that_fails_before_the_drain_leaves_a_record_that_starts_a_lineage(self):
        from orche.flow import lineage
        self.turn("please add a coupon field")
        u1 = self.first()
        self.turn("[orche] inbox: 1 report", inbox.make(u1.name, "div-acme-build", "main-acme", "done",
                                                        lineage=u1.name))
        self.assertEqual(lineage.read_turn("main-acme")["lineages"], [u1.name])
        drained = inbox.box("main-acme") / inbox.DRAINED
        moved = drained.with_name("drained-moved")
        os.replace(drained, moved)
        drained.write_text("not a folder")                  # drain() raises: the hook fails open
        try:
            self.turn("[orche] inbox: 1 report", inbox.make(u1.name, "div-acme-build", "main-acme", "blocked",
                                                            lineage=u1.name))
        finally:
            drained.unlink()
            os.replace(moved, drained)
        turn = lineage.read_turn("main-acme")
        self.assertEqual((turn["owner"], turn["lineages"]), (False, []))
        u2 = self.first(GIFT)
        self.assertEqual(self.brief_of(u2).lineage, u2.name)

    def test_the_turn_record_carries_the_time_before_the_inbox_was_read(self):
        """A report landing between the inbox read and the record must not count as covered by the turn."""
        from orche import registry, roster
        from orche.flow import lineage
        inbox.write(inbox.make("u-old", "div-acme-build", "main-acme", "done", lineage="L-a"))
        start, ticks = units.now() - _dt.timedelta(hours=1), iter(range(10_000))
        drained_at, real = [], inbox.drain

        def drain(paths_):
            drained_at.append(units.now())
            return real(paths_)
        r = roster.Roster(registry.load())
        who = r.identify(str(self.repo), {"ORCHE_SESSION": "main-acme"})
        with mock.patch.object(units, "now", side_effect=lambda: start + _dt.timedelta(seconds=next(ticks))), \
                mock.patch.object(inbox, "drain", drain):
            inbox.handle_prompt({"prompt": "[orche] inbox: 1 report", "session_id": "s-1"}, time.monotonic(), r, who)
        turn = lineage.read_turn("main-acme")
        self.assertEqual((turn["lineages"], len(drained_at)), (["L-a"], 1))
        self.assertLessEqual(units.parse_iso(turn["at"]), drained_at[0])

    def test_a_division_child_inherits_and_amend_keeps_both(self):
        parent = self.first()
        header(parent, "L-root", "-")
        res = self.orche("dispatch", stdin=TO_FEAT, ORCHE_SESSION="div-acme-build")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        child = sorted(self.units_dir().iterdir(), key=lambda p: p.stat().st_mtime)[-1]
        self.assertIn(f"lineage: L-root   parent: {parent.name}\n", (child / "BRIEF.md").read_text())
        res = self.orche("dispatch", stdin=amend(child.name, 2), ORCHE_SESSION="div-acme-build")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        b = self.brief_of(child)
        self.assertEqual((b.rev, b.lineage, b.parent), (2, "L-root", parent.name))

    def test_pointers_carry_lineage_and_parent(self):
        from orche.runs import filing
        from orche import registry
        parent = self.first()
        header(parent, "L-root", "u-up")
        res = self.orche("report", "question", "--summary", "ok", ORCHE_SESSION="div-acme-build")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        ptr = [x for _p, x in inbox.pending("main-acme") if x.get("status") == "question"][-1]
        self.assertEqual((ptr["lineage"], ptr["parent"]), ("L-root", "u-up"))
        who = {"unit": parent.name, "project": "acme", "to": "div-acme-build", "session": "div-acme-build"}
        with mock.patch.object(filing, "recipient", return_value="main-acme"):
            filing.file_pointer(registry.load(), who, 1, parent, "blocked", "stuck")
        ptr = [x for _p, x in inbox.pending("main-acme") if x.get("status") == "blocked"][-1]
        self.assertEqual((ptr["lineage"], ptr["parent"]), ("L-root", "u-up"))


class ChainTest(FlowCase):
    def chain(self, n):
        """n briefs to div-acme-build, each the parent of the next; the last is the division's current."""
        made = []
        for i in range(n):
            f = self.first(BODY.replace("Add coupon field", f"Add field {i}"))
            header(f, "L", made[-1].name if made else "-")
            made.append(f)
        return made

    def test_depth_four_refused_stop_passes_and_other_blocks_relay(self):
        made = self.chain(3)
        stop = f"STOP {made[0].name} enough"
        res = self.orche("dispatch", stdin=f"{TO_FEAT}\n---\n{stop}", ORCHE_SESSION="div-acme-build")
        lines = res.stdout.splitlines()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(lines[0], f"block 1: refused — {made[-1].name} is already 3 briefs deep (cap 3); a brief "
                         "from div-acme-build would make it 4. Do not dispatch it yourself: name the work in your "
                         "report and ask Main to dispatch it. STOP still works.")
        self.assertTrue(lines[1].startswith("block 2: delivered"), lines)
        self.assertIn(f"flow refused rule=G6 by=div-acme-build unit={made[-1].name} n=4", self.events())

    def test_fan_out_counts_children_in_the_window_the_owner_resets_it_main_is_not_capped(self):
        [p] = self.chain(1)
        self.owner_spoke(later=-600)                                     # W opens well before the children
        for i in range(4):
            f = self.first(TO_FEAT.replace("Add coupon field", f"Add child {i}"))
            header(f, "L", p.name)
            sent = time.time() - 300                                     # inside W, before "now", pinned
            os.utime(f / "BRIEF.rev1.md", (sent, sent))
        os.utime(p / "BRIEF.md", (time.time() + 600, time.time() + 600))   # still the division's current unit

        def child(name):
            return self.orche("dispatch", stdin=TO_FEAT.replace("Add coupon field", name), ORCHE_SESSION="div-acme-build")
        res = child("Add child 4")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)     # 4 children: the fifth passes
        res = child("Add child 5")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)     # 5 children: the sixth is refused
        since = units.hhmm(guard.window("acme"))
        self.assertIn(f"block 1: refused — {p.name} already has 5 child briefs since {since} without the owner "
                      "(cap 5). Do not send another: report it to your sender (`orche report blocked --summary "
                      "\"<what is left>\"`). STOP still works.", res.stdout)
        self.owner_spoke(later=900)                                      # the owner's word: a new window
        self.assertEqual(child("Add child 6").returncode, 0)
        self.assertEqual(self.orche("dispatch", stdin=GIFT).returncode, 0)


class LineageCloseTest(FlowCase):
    """T8: done/stopped members of a multi-unit lineage post quiet; the report that leaves every member
    terminal writes LINEAGE.md and posts one close pointer to Main."""

    def lineage(self, n):
        made = [self.first(BODY.replace("Add coupon field", f"Add field {i}")) for i in range(n)]
        for f in made:
            header(f, made[0].name, "-")
        return made[0].name, [f.name for f in made]

    def post(self, unit, status, lin, **extra):
        return json.loads(inbox.post(inbox.make(unit, "div-acme-build", "main-acme", status, brief_rev=1,
                                                summary=f"{unit} {status}", lineage=lin, **extra)).read_text())

    def closes(self):
        return [x for _p, x in inbox.pending("main-acme") if x.get("lineage_closed")]

    def test_two_quiet_dones_then_the_third_closes_once_with_lineage_md(self):
        lin, (a, b, c) = self.lineage(3)
        with mock.patch.object(inbox, "wake") as wake:
            self.assertTrue(self.post(a, "done", lin)["quiet"])
            self.assertTrue(self.post(b, "done", lin)["quiet"])
            self.assertEqual((wake.call_count, self.closes()), (0, []))
            self.assertTrue(self.post(c, "done", lin)["quiet"])
            self.assertEqual([x.args[0] for x in wake.call_args_list], ["main-acme"])   # the close wakes Main
        [close] = self.closes()
        self.assertEqual((close["from"], close["status"], close["unit"]), ("orche", "done", lin))
        self.assertEqual(close["summary"].splitlines()[0], f"lineage {lin} closed: 3 units: 3 done, 0 failed, 0 stopped")
        md = self.units_dir() / lin / "LINEAGE.md"
        self.assertEqual(close["report_path"], str(md))
        text = md.read_text()
        self.assertIn("| unit | to | rev | status | report |", text)
        for u in (a, b, c):
            self.assertIn(f"| {u} | div-acme-build | 1 | done |", text)
        out = self.run_hook("report_inject.py", self.payload("prompt.json", prompt="[orche] inbox: new reports"),
                            ORCHE_SESSION="main-acme").stdout
        self.assertIn(f"  report: {md}\n````markdown\n# Lineage {lin}\n", out)    # the wake carries LINEAGE.md

    def test_the_close_sends_one_finish_notice_with_what_finished_and_its_cost(self):
        from orche import notify
        from orche.runs import history
        lin, (a, b, c) = self.lineage(3)
        util.write_json(self.units_dir() / b / "RUN.json", {"pr": 41})
        history.append("acme", {"unit": a, "cost_usd": 1.25, "finished": "2026-10-30T10:00:00+09:00"}, 1)
        history.append("acme", {"unit": c, "cost_usd": 0.5, "finished": "2026-10-30T10:00:00+09:00"}, 1)
        with mock.patch.object(inbox, "wake"), mock.patch.object(notify, "send", return_value=True) as send:
            self.post(a, "done", lin, ledger_row=True)
            self.post(b, "done", lin)                                 # done without a ledger row
            self.assertEqual(send.call_count, 0)                      # not before the close
            self.post(c, "failed", lin)
        send.assert_called_once()
        (title, body), kw = send.call_args
        self.assertEqual((title, kw["kind"]), ("acme: finished", "finish"))
        self.assertEqual(body, "Add field 0 to checkout form. 2 done, 1 failed. PRs #41. Runs cost $1.75. "
                               "1 not confirmed (no ledger row).")

    def test_no_finish_notice_when_a_member_waits_on_the_owner_or_notify_is_off(self):
        from orche import notify, shell
        lin, (a, b) = self.lineage(2)
        with mock.patch.object(inbox, "wake"), mock.patch.object(notify, "send") as send:
            self.post(a, "done", lin)
            self.post(b, "question", lin)
        self.assertEqual(len(self.closes()), 1)
        send.assert_not_called()                                  # the owner's ask notifies instead
        calls = []
        (self.home / "config.json").write_text(json.dumps({"notify": False}))
        with mock.patch.object(notify, "PLATFORM", "darwin"), \
                mock.patch.object(shell, "RUNNER", lambda argv, **kw: calls.append(argv)):
            self.assertFalse(notify.finished("acme", "Ship it.", {"done": 2}, [], 0.0, 0))
        self.assertEqual(calls, [])

    def test_a_single_unit_lineage_is_unchanged(self):
        lin, (a,) = self.lineage(1)
        with mock.patch.object(inbox, "wake") as wake:
            self.assertNotIn("quiet", self.post(a, "done", lin))
            self.assertEqual(wake.call_count, 1)
        self.assertEqual(self.closes(), [])

    def test_a_single_unit_lineage_gets_its_line_open_then_closed(self):
        from orche import registry
        from orche.flow import lineage
        lin, (a,) = self.lineage(1)
        self.assertEqual(lineage.line(registry.load(), "acme", lin),
                         f"[orche] lineage {lin}: open — 1 of 1 unit running; waiting on {a} (running)")
        with mock.patch.object(inbox, "wake"):
            self.post(a, "blocked", lin)
        self.assertEqual(lineage.line(registry.load(), "acme", lin),
                         f"[orche] lineage {lin}: closed — 1 unit (1 blocked)")
        with mock.patch.object(inbox, "wake"):
            self.post(a, "done", lin)
        out = self.run_hook("report_inject.py", self.payload("prompt.json", prompt="[orche] inbox: new reports"),
                            ORCHE_SESSION="main-acme").stdout
        lines = out.rstrip("\n").splitlines()
        self.assertEqual(lines.count(f"[orche] lineage {lin}: closed — 1 unit (1 done)"), 1, out)
        self.assertEqual(lines[-1], inbox.REPORT_LINE)

    def test_a_division_wakes_for_its_childrens_done_main_stays_quiet(self):
        lin, (a, b) = self.lineage(2)
        res = self.orche("dispatch", stdin=TO_FEAT, ORCHE_SESSION="div-acme-build")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        child = sorted(self.units_dir().iterdir(), key=lambda p: p.stat().st_mtime)[-1].name
        with mock.patch.object(inbox, "wake") as wake:
            ptr = json.loads(inbox.post(inbox.make(child, "feat-acme-checkout", "div-acme-build", "done",
                                                   summary="c", lineage=lin)).read_text())
            self.assertNotIn("quiet", ptr)
            self.assertEqual([c.args[0] for c in wake.call_args_list], ["div-acme-build"])
            self.assertTrue(self.post(a, "done", lin)["quiet"])
            self.assertEqual(wake.call_count, 1)

    def test_racing_posts_close_once(self):
        import threading
        lin, (a, b) = self.lineage(2)
        go = threading.Barrier(2)

        def one(u):
            go.wait()
            self.post(u, "done", lin)
        with mock.patch.object(inbox, "wake"):
            ts = [threading.Thread(target=one, args=(u,)) for u in (a, b)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(5)
        self.assertEqual(len(self.closes()), 1)

    def test_the_heartbeat_reposts_a_missed_close_once(self):
        from orche import registry
        from orche.flow import lineage
        lin, (a, b) = self.lineage(2)
        with mock.patch.object(inbox, "wake"), mock.patch.object(lineage, "settle", return_value=[]):
            self.post(a, "done", lin)
            self.post(b, "stop", lin)                                       # the close was lost in a crash
        self.assertEqual(self.closes(), [])
        out = []
        with mock.patch.object(inbox, "wake") as wake:
            self.assertEqual(lineage.repair(registry.load(), out=out.append), [("acme", lin)])
            self.assertEqual(lineage.repair(registry.load(), out=out.append), [])
            self.assertEqual(wake.call_count, 1)
        [close] = self.closes()
        self.assertIn("1 done, 0 failed, 1 stopped", close["summary"])
        self.assertEqual(out, [f"closed lineage {lin} (acme)"])

    def test_a_post_that_cannot_take_the_lock_never_closes(self):
        import threading
        from orche.flow import lineage
        lin, (a, b) = self.lineage(2)
        with mock.patch.object(inbox, "wake"):
            self.post(a, "done", lin)
        inside, release, real = threading.Event(), threading.Event(), lineage.close

        def slow(*args):
            inside.set()
            release.wait(5)
            return real(*args)
        with mock.patch.object(lineage, "LOCK_WAIT_S", 0.05), mock.patch.object(lineage, "close", slow), \
                mock.patch.object(inbox, "wake"):
            t = threading.Thread(target=self.post, args=(b, "done", lin))
            t.start()
            self.assertTrue(inside.wait(5))                 # the first post holds the lock, closing
            self.post(b, "done", lin)                       # a second one meanwhile: lock busy → no close
            release.set()
            t.join(5)
        self.assertEqual(len(self.closes()), 1)
        self.assertEqual(self.events().count("lineage closed"), 1)
        self.assertEqual(len(list(self.units_dir().glob("*/LINEAGE.md"))), 1)
        self.assertIn("lock acme busy; the close is left to the heartbeat",
                      (self.home / "logs" / "hooks.log").read_text())

    def test_a_failed_close_wakes_the_recipient_and_is_recorded_only_after_its_pointer(self):
        from orche import registry
        from orche.flow import lineage
        lin, (a, b) = self.lineage(2)
        real = inbox.write

        def write(ptr):
            if ptr.get("lineage_closed"):
                raise OSError("disk full")
            return real(ptr)
        with mock.patch.object(inbox, "wake") as wake:
            self.post(a, "done", lin)
            with mock.patch.object(inbox, "write", write):
                self.assertTrue(self.post(b, "done", lin)["quiet"])
            self.assertEqual([c.args[0] for c in wake.call_args_list], ["main-acme"])   # woken, not silent
        self.assertEqual((self.closes(), lineage._recorded("acme")), ([], {}))
        self.assertIn("lineage close failed", (self.home / "logs" / "hooks.log").read_text())
        with mock.patch.object(inbox, "wake"):
            self.assertEqual(lineage.repair(registry.load()), [("acme", lin)])
        self.assertEqual(len(self.closes()), 1)

    def test_a_lost_or_corrupt_record_reposts_no_close_that_went_out(self):
        from orche import registry
        from orche.flow import lineage
        lin, (a, b) = self.lineage(2)
        with mock.patch.object(inbox, "wake"):
            self.post(a, "done", lin)
            self.post(b, "done", lin)
        rec = lineage.folder("acme") / "closed.json"
        for broken in ("{not json", None, None):
            if broken is None:
                rec.unlink()
                inbox.drain([p for p, _x in inbox.pending("main-acme")])    # read: now in .drained
            else:
                rec.write_text(broken)
            with mock.patch.object(inbox, "wake") as wake:
                self.assertEqual(lineage.repair(registry.load()), [])
                self.assertFalse(wake.called)
            self.assertIn(lin, json.loads(rec.read_text()))                 # recorded again, once
        self.assertEqual(len([x for _p, x in inbox.about_all("main-acme") if x.get("lineage_closed")]), 1)

    def test_a_report_counts_where_it_goes_not_where_the_header_says(self):
        lin, (a, b) = self.lineage(2)
        p = self.units_dir() / b / "BRIEF.md"
        p.write_text(p.read_text().replace("from: main-acme", "from: ghost-acme-x"))   # sender off the roster
        with mock.patch.object(inbox, "wake"):
            self.post(a, "done", lin)
            self.post(b, "done", lin)                       # goes to Main (roster.recipient)
        self.assertEqual(len(self.closes()), 1)

    def test_a_repair_pass_reads_each_inbox_once_and_stays_fast(self):
        from orche import registry
        from orche.flow import lineage
        text = (self.first() / "BRIEF.md").read_text()
        base, rec = self.units_dir(), {}
        drained = inbox.box("main-acme") / inbox.DRAINED
        drained.mkdir(parents=True, exist_ok=True)

        def unit(u, lin):
            (base / u).mkdir()
            (base / u / "BRIEF.md").write_text("\n".join(
                f"lineage: {lin}   parent: -" if ln.startswith("lineage: ") else f"unit: {u}"
                if ln.startswith("unit: ") else ln for ln in text.splitlines()) + "\n")
        for i in range(30):                                 # 30 lineages of 3, closed and recorded
            ids = [f"acme-perf-{i}-20261001-{j + 1}" for j in range(3)]
            for u in ids:
                unit(u, ids[0])
                p = inbox.make(u, "div-acme-build", "main-acme", "done", summary="s", lineage=ids[0])
                p["at"] = "2026-10-20T10:00:00+09:00"
                util.write_json(drained / f"{u}.json", p)
            rec[ids[0]] = {"members": [f"{u}@1:done" for u in ids], "at": "2026-10-20T10:00:00+09:00"}
        for k in range(9):
            unit(f"acme-solo-20261001-{k}", f"acme-solo-20261001-{k}")
        for k in range(410):                                # 500 drained pointers in all
            util.write_json(drained / f"filler-{k}.json", inbox.make(f"acme-filler-20261001-{k}", "div-acme-build",
                                                                     "main-acme", "info", summary="x"))
        util.write_json(lineage.folder("acme") / "closed.json", rec)
        reads, scans, real_about, real_groups = [], [], inbox.about_all, lineage.groups
        now = _dt.datetime.fromisoformat("2026-10-20T12:00:00+09:00")       # every report is recent
        with mock.patch.object(inbox, "about_all", side_effect=lambda s: reads.append(s) or real_about(s)), \
                mock.patch.object(lineage, "groups", side_effect=lambda r, p: scans.append(p) or real_groups(r, p)), \
                mock.patch.object(inbox, "wake"):
            t0 = time.perf_counter()
            self.assertEqual(lineage.repair(registry.load(), now=now), [])
            took = time.perf_counter() - t0
        self.assertEqual((reads, scans.count("acme")), (["main-acme"], 1))
        self.assertLess(took, 1.0)

    def test_status_names_a_lineage_a_blocked_member_holds_open(self):
        from orche import cmd_status, reconcile, registry
        lin, (a, b) = self.lineage(2)
        with mock.patch.object(inbox, "wake"):
            self.post(a, "done", lin)
        ptr = inbox.make(b, "div-acme-build", "main-acme", "blocked", summary="stuck", lineage=lin)
        ptr["at"] = (units.now() - _dt.timedelta(minutes=40)).isoformat(timespec="seconds")
        inbox.write(ptr)
        [s] = [s for s in cmd_status.stalled(registry.load(), "acme") if s["cls"] == "lineage"]
        self.assertEqual(reconcile.stalled_line(s), f"  stalled: lineage {lin} waiting on {b} (blocked 40 min)")

    def test_lineage_lines_follow_the_inbox_block(self):
        lin, (a, b) = self.lineage(2)
        with mock.patch.object(inbox, "wake"):
            self.post(a, "done", lin)
            self.post(b, "failed", lin)
        out = self.run_hook("report_inject.py", self.payload("prompt.json", prompt="[drain]"),
                            ORCHE_SESSION="main-acme").stdout
        lines = out.rstrip("\n").splitlines()
        want = f"[orche] lineage {lin}: closed — 2 units (1 done, 1 failed)"
        self.assertEqual(lines.count(want), 1, out)
        self.assertLess(out.index("## Inbox"), out.index(want))
        self.assertEqual(lines[-1], inbox.REPORT_LINE)


def brief_text(unit, to, frm, lin, parent, rev=1, timebox="30m"):
    return (f"rev: {rev}\nunit: {unit}\nto: {to}   from: {frm}\nlineage: {lin}   parent: {parent}\n\n"
            f"## Brief {unit}\nTO        {to}\nGOAL      g {unit}\nTIMEBOX   {timebox}\n")


class StopSubtreeTest(FlowCase):
    """STOP stops the subtree: every running unit under the named one gets the same STOP; ended ones and
    siblings do not; the queue entries of the subtree that have not landed leave the queue."""

    post = LineageCloseTest.post

    def queued(self, unit, pr):
        from orche import queue
        queue.add("acme", {"id": queue.entry_id(pr, f"{pr:040d}"), "pr": pr, "head": f"{pr:040d}",
                           "branch": unit, "unit": unit, "state": "waiting"})

    def test_a_stop_reaches_the_running_units_under_it_and_drops_their_prs(self):
        from orche.queue import store
        made = [self.first(BODY.replace("Add coupon field", f"Add field {i}")) for i in range(6)]
        h, w1, w2, g, d, s = (f.name for f in made)
        for f, parent in zip(made, ("-", h, h, w1, h, "-")):
            header(f, h, parent)
        with mock.patch.object(inbox, "wake"):
            self.post(d, "done", h)
        self.queued(d, 41)
        self.queued(s, 42)
        res = self.orche("dispatch", stdin=f"STOP {h} owner changed plans\n")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(f"STOP {h}: also stopped 3 units under it ({w1}, {w2}, {g}); PR #41 left the queue", res.stdout)
        events = self.events()
        for u in (h, w1, w2, g):
            self.assertEqual(events.count(f"dispatch kind=stop unit={u} "), 1, u)
        for u in (d, s):
            self.assertNotIn(f"dispatch kind=stop unit={u} ", events)
        self.assertIn(f"dispatch stop-subtree unit={h} stopped=3 dropped=1", events)
        self.assertEqual([e["pr"] for e in store.read("acme")["entries"]], [42])
        dropped = [json.loads(ln) for ln in store.log_path("acme").read_text().splitlines()]
        self.assertEqual([(x.get("event"), x.get("pr"), x.get("reason")) for x in dropped if x.get("event") == "dropped"],
                         [("dropped", 41, "stopped")])
        [fb] = [p for _x, p in inbox.pending("main-acme") if f"STOP {h}:" in p.get("summary", "")]
        self.assertTrue(fb["summary"].startswith(f"relayed {h} → div-acme-build rev 1 (deliverer-started)"), fb)

    def test_a_stop_under_a_leaf_touches_nothing_else(self):
        made = [self.first(BODY.replace("Add coupon field", f"Add field {i}")) for i in range(2)]
        h, w = (f.name for f in made)
        header(made[1], h, h)
        res = self.orche("dispatch", stdin=f"STOP {w}\n")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("also stopped", res.stdout)
        self.assertNotIn(f"dispatch kind=stop unit={h} ", self.events())

    def test_an_amend_does_not_cascade(self):
        made = [self.first(BODY.replace("Add coupon field", f"Add field {i}")) for i in range(2)]
        h, w = (f.name for f in made)
        header(made[1], h, h)
        res = self.orche("dispatch", stdin=amend(h, 2))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn(f"dispatch kind=stop unit={w} ", self.events())


class BatchTest(FlowCase):
    """c89: a session is woken once per lineage batch, after every unit it waits on has settled (a head's
    unit once the head reported after its workers); blocked, question, owner steps and urgent wake at once."""
    L = "acme-h-20261029-1"
    BUILD, MAIN, FEAT, RND = "div-acme-build", "main-acme", "feat-acme-checkout", "div-acme-rnd"

    def setUp(self):
        super().setUp()
        self.t = units.now() - _dt.timedelta(minutes=30)
        patcher = mock.patch.object(inbox, "wake")
        self.wake = patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, unit, to, frm, parent="-", lin=None, rev=1):
        d = self.units_dir() / unit
        d.mkdir(parents=True, exist_ok=True)
        (d / "BRIEF.md").write_text(brief_text(unit, to, frm, lin or self.L, parent, rev))
        return unit

    def post(self, unit, frm, to, status, lin=None, rev=1, **extra):
        self.t += _dt.timedelta(seconds=1)
        ptr = inbox.make(unit, frm, to, status, brief_rev=rev, summary=f"{unit} {status}", lineage=lin or self.L,
                         **extra)
        ptr["at"] = self.t.isoformat(timespec="seconds")
        return json.loads(inbox.post(ptr).read_text())

    def woke(self):
        out = [c.args[0] for c in self.wake.call_args_list]
        self.wake.reset_mock()
        return out

    def closes(self, who=None):
        return [x for _p, x in inbox.pending(who or self.MAIN) if x.get("lineage_closed")]

    def head(self, n):
        """H to Build from Main, and n workers W1..Wn Build staffed under it (to the checkout feature)."""
        h = self.make(self.L, self.BUILD, self.MAIN)
        return h, [self.make(f"acme-w{i}-20261029-{i + 1}", self.FEAT, self.BUILD, h) for i in range(1, n + 1)]

    def two(self, to=None, lin=None, n=2):
        lin = lin or self.L
        tag = lin.split("-")[1]
        return [self.make(lin if i == 0 else f"acme-{tag}{i}-20261029-{i + 10}", to or self.BUILD, self.MAIN, lin=lin)
                for i in range(n)]

    def test_a_heads_workers_wake_it_once_after_the_last_one_ends(self):
        h, (w1, w2, w3, w4) = self.head(4)
        self.assertEqual(self.post(w1, self.FEAT, self.BUILD, "info", via="runner")["quiet"], "batch")
        self.assertEqual(self.post(w2, self.FEAT, self.BUILD, "failed", via="runner")["quiet"], "batch")
        self.assertEqual(self.post(w3, self.FEAT, self.BUILD, "done")["quiet"], "batch")
        self.assertEqual(self.woke(), [])
        self.post(w4, self.FEAT, self.BUILD, "done")
        self.assertEqual(self.woke(), [self.BUILD])
        [close] = self.closes(self.BUILD)
        self.assertEqual((close["from"], close["status"], close["unit"]), ("orche", "done", self.L))
        self.assertEqual(close["summary"].splitlines()[0],
                         f"lineage {self.L}: every unit you wait on ended: 4 units, 2 done, 1 failed, 1 stopped")
        self.assertEqual(self.closes(), [])                                   # Main waits on H's own report

    def test_main_waits_for_the_head_to_report_after_its_workers(self):
        """The c89 flow table: Build 3 wakes (one per batch round, one urgent), Main 1."""
        h, (w1, w2, w3, w4) = self.head(4)
        r = self.make("acme-r-20261029-9", self.RND, self.MAIN)
        wakes = []

        def step(*args, **extra):
            self.post(*args, **extra)
            wakes.extend(self.woke())
        step(h, self.BUILD, self.MAIN, "info", via="runner")                   # min 1: the head run staffed and ended
        step(w1, self.FEAT, self.BUILD, "info", via="runner")                  # min 3: unverified
        step(r, self.RND, self.MAIN, "done")                                   # min 5
        step(w2, self.FEAT, self.BUILD, "failed", via="runner")                # min 7
        step(w3, self.FEAT, self.BUILD, "done", via="runner", quiet=True)      # min 12: review pending
        self.assertEqual(wakes, [])
        step(w4, self.FEAT, self.BUILD, "done")                                # min 40: Build's batch
        self.assertEqual((wakes, self.closes()), ([self.BUILD], []))
        inbox.drain([p for p, _x in inbox.pending(self.BUILD)])                # Build reads it, AMENDs W2
        (self.units_dir() / w2 / "BRIEF.md").write_text(brief_text(w2, self.FEAT, self.BUILD, self.L, h, rev=2))
        step(w2, self.FEAT, self.BUILD, "done", rev=2)                         # min 55: the round's close
        self.assertEqual(wakes, [self.BUILD] * 2)
        inbox.post(inbox.make(h, "orche", self.BUILD, "info", urgent=True, summary="every unit landed"))
        wakes.extend(self.woke())                                              # min 60: urgent, at once
        step(h, self.BUILD, self.MAIN, "done")                                 # min 61: H's own report
        self.assertEqual((wakes.count(self.BUILD), wakes.count(self.MAIN)), (3, 1))
        [close] = self.closes()
        self.assertEqual(close["summary"].splitlines()[0],
                         f"lineage {self.L} closed: 6 units: 5 done, 0 failed, 1 stopped")

    def test_a_heads_runner_filing_after_quick_workers_does_not_settle_it(self):
        h, (w1, w2) = self.head(2)
        r = self.make("acme-r-20261029-9", self.RND, self.MAIN)
        self.post(r, self.RND, self.MAIN, "done")
        self.post(w1, self.FEAT, self.BUILD, "done", via="runner")
        self.post(w2, self.FEAT, self.BUILD, "failed", via="runner")
        self.assertEqual(self.woke(), [self.BUILD])
        self.assertEqual(self.post(h, self.BUILD, self.MAIN, "info", via="runner")["quiet"], "batch")
        self.assertEqual((self.woke(), self.closes()), ([], []))
        from orche import registry
        from orche.flow import lineage
        self.assertEqual(lineage.line(registry.load(), "acme", self.L),
                         f"[orche] lineage {self.L}: open — 0 of 4 units running; waiting on {h} (report due)")
        self.post(h, self.BUILD, self.MAIN, "done")
        self.assertEqual((self.woke(), len(self.closes())), ([self.MAIN], 1))

    def test_failed_joins_the_batch_and_counts_in_the_close(self):
        a, b = self.two()
        self.assertEqual(self.post(a, self.BUILD, self.MAIN, "failed")["quiet"], "batch")
        self.assertEqual(self.woke(), [])
        self.post(b, self.BUILD, self.MAIN, "done")
        self.assertEqual(self.woke(), [self.MAIN])
        [close] = self.closes()
        self.assertIn("1 done, 1 failed, 0 stopped", close["summary"])

    def test_blocked_and_question_wake_at_once_and_count_as_ended(self):
        from orche import registry
        from orche.flow import lineage
        a, b, c = self.two(n=3)
        self.post(a, self.BUILD, self.MAIN, "done")
        self.assertNotIn("quiet", self.post(b, self.BUILD, self.MAIN, "blocked"))
        self.assertEqual((self.woke(), self.closes()), ([self.MAIN], []))
        self.assertNotIn("quiet", self.post(c, self.BUILD, self.MAIN, "question"))
        self.assertEqual(self.woke(), [self.MAIN])                             # the report and its close: one wake
        [close] = self.closes()
        self.assertEqual(close["summary"].splitlines()[0],
                         f"lineage {self.L} closed: 3 units: 1 done, 0 failed, 0 stopped, 1 blocked, 1 question")
        self.assertEqual(lineage.line(registry.load(), "acme", self.L),
                         f"[orche] lineage {self.L}: closed — 3 units (1 done, 1 blocked, 1 question)")

    def test_all_members_blocked_closes_the_batch(self):
        a, b = self.two()
        self.post(a, self.BUILD, self.MAIN, "blocked")
        self.assertEqual(self.closes(), [])
        self.post(b, self.BUILD, self.MAIN, "blocked")
        self.assertEqual(self.woke(), [self.MAIN, self.MAIN])
        [close] = self.closes()
        self.assertIn("0 done, 0 failed, 0 stopped, 2 blocked", close["summary"])

    def test_an_owner_step_or_urgent_report_wakes_at_once(self):
        a, b, c = self.two(n=3)
        self.assertNotIn("quiet", self.post(a, self.BUILD, self.MAIN, "done", owner="approve the paid plan"))
        self.assertEqual(self.woke(), [self.MAIN])
        self.assertNotIn("quiet", self.post(b, self.BUILD, self.MAIN, "done", urgent=True))
        self.assertEqual((self.woke(), self.closes()), ([self.MAIN], []))

    def test_an_older_revs_report_is_quiet_and_never_closes(self):
        a, b = self.two()
        self.make(b, self.BUILD, self.MAIN, rev=2)
        self.assertIs(self.post(b, self.BUILD, self.MAIN, "info", via="runner")["quiet"], True)   # rev 1's run
        self.assertEqual(self.post(a, self.BUILD, self.MAIN, "done")["quiet"], "batch")
        self.assertEqual((self.woke(), self.closes()), ([], []))

    def test_a_crashed_run_filed_by_reconcile_ends_its_unit(self):
        import subprocess
        from orche import registry, runs
        a, b = self.two()
        self.post(a, self.BUILD, self.MAIN, "done")
        proc = subprocess.Popen(["true"])
        proc.wait()
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{b}.lock").write_text(json.dumps({"pid": proc.pid, "unit": b, "to": self.BUILD, "rev": 1,
                                                      "started": util.now_iso()}))
        self.assertEqual(self.woke(), [])
        runs.reconcile(registry.load(), "acme", budget_s=0)
        self.assertEqual(self.woke(), [self.MAIN])
        [close] = self.closes()
        self.assertIn("1 done, 1 failed, 0 stopped", close["summary"])

    def test_a_retired_target_counts_as_gone(self):
        from orche import registry
        a = self.make(self.L, self.BUILD, self.MAIN)
        b = self.make("acme-f-20261029-2", self.FEAT, self.MAIN)
        reg = registry.load()
        reg["projects"]["acme"]["divisions"]["build"]["features"][0]["status"] = "retired"
        registry.save(reg)
        self.post(a, self.BUILD, self.MAIN, "done")
        self.assertEqual(self.woke(), [self.MAIN])
        [close] = self.closes()
        self.assertIn("1 done, 0 failed, 1 stopped", close["summary"])

    def test_a_stop_that_reached_a_pane_ends_its_unit(self):
        a, b = self.two()
        inbox.write(inbox.make(b, self.MAIN, self.BUILD, "stop", brief_rev=1, via="dispatch", summary="STOP"))
        self.post(a, self.BUILD, self.MAIN, "done")
        self.assertEqual(self.woke(), [self.MAIN])
        [close] = self.closes()
        self.assertIn("1 done, 0 failed, 1 stopped", close["summary"])

    def test_an_amends_old_rev_stop_report_does_not_end_the_new_rev(self):
        a, b = self.two()
        inbox.write(inbox.make(b, self.MAIN, self.BUILD, "stop", brief_rev=1, via="dispatch", summary="STOP"))
        self.make(b, self.BUILD, self.MAIN, rev=2)                              # the AMEND
        self.assertIs(self.post(b, self.BUILD, self.MAIN, "stop")["quiet"], True)
        self.post(a, self.BUILD, self.MAIN, "done")
        self.assertEqual((self.woke(), self.closes()), ([], []))

    def test_mains_direct_feature_unit_is_in_mains_batch(self):
        from orche import registry
        h = self.make(self.L, self.BUILD, self.MAIN)
        f = self.make("acme-f-20261029-2", self.FEAT, self.MAIN)
        self.assertEqual(self.post(f, self.FEAT, self.MAIN, "done")["quiet"], "batch")
        self.assertEqual(self.woke(), [])
        self.post(h, self.BUILD, self.MAIN, "done")
        self.assertEqual((self.woke(), len(self.closes())), ([self.MAIN], 1))
        reg = registry.load()                                                   # a division without a session:
        reg["projects"]["acme"]["divisions"]["build"]["session"] = ""            # its feature reports to Main
        registry.save(reg)
        l2 = "acme-g-20261029-3"
        g = self.make(l2, self.FEAT, "ghost-acme-x", lin=l2)
        r = self.make("acme-r-20261029-4", self.RND, self.MAIN, lin=l2)
        self.assertEqual(self.post(g, self.FEAT, self.MAIN, "done", lin=l2)["quiet"], "batch")
        self.assertEqual(self.woke(), [])
        self.post(r, self.RND, self.MAIN, "done", lin=l2)
        self.assertEqual(self.woke(), [self.MAIN])
        self.assertEqual(sorted(c["unit"] for c in self.closes()), sorted([self.L, l2]))

    def test_a_second_lineage_closes_while_the_first_holds(self):
        l2 = "acme-k-20261029-5"
        a1, b1 = self.two()
        a2, b2 = self.two(lin=l2)
        self.post(a1, self.BUILD, self.MAIN, "done")
        self.post(a2, self.BUILD, self.MAIN, "done", lin=l2)
        self.post(b2, self.BUILD, self.MAIN, "done", lin=l2)
        self.assertEqual(self.woke(), [self.MAIN])
        self.assertEqual([c["unit"] for c in self.closes()], [l2])

    def test_the_head_is_not_woken_when_nothing_was_held(self):
        h, (w1, w2) = self.head(2)
        self.post(w1, self.FEAT, self.BUILD, "done", via="runner", quiet=True)     # review pending: quiet
        self.post(w2, self.FEAT, self.BUILD, "done", via="runner", quiet=True)
        self.assertEqual((self.woke(), self.closes(self.BUILD)), ([], []))

    def test_a_single_unit_batch_wakes_directly(self):
        h, (w1,) = self.head(1)
        self.assertNotIn("quiet", self.post(w1, self.FEAT, self.BUILD, "done"))
        self.assertEqual((self.woke(), self.closes(self.BUILD)), ([self.BUILD], []))

    def test_no_head_close_while_a_waking_pointer_is_pending(self):
        h, (w1, w2) = self.head(2)
        self.assertNotIn("quiet", self.post(w1, self.FEAT, self.BUILD, "blocked"))
        self.assertEqual(self.post(w2, self.FEAT, self.BUILD, "done")["quiet"], "batch")
        self.assertEqual((self.woke(), self.closes(self.BUILD)), ([self.BUILD], []))   # the block's wake reads it


class GuardEdgesTest(FlowCase):
    """Review 2: the guards cover external briefs, fail open, and count a batch; G7 follows flow.enabled."""

    def config(self, delivery=None, **flow):
        cfg = self.home / "config.json"
        data = {**json.loads(cfg.read_text()), "flow": flow}
        if delivery:
            data["delivery"] = delivery
        cfg.write_text(json.dumps(data))

    def test_a_paused_project_holds_its_external_requests_too(self):
        self.add_project_b()
        guard.pause("acme", "owner", "lunch")
        res = self.orche("dispatch", stdin=BODY.replace("TO        build", "TO        bee"))
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("acme is paused by the owner", res.stdout)
        (guard.folder("acme") / "pause.json").unlink()
        res = self.orche("dispatch", stdin=BODY.replace("TO        build", "TO        bee"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("delivered", res.stdout)

    def test_a_bad_cap_falls_back_and_a_failing_guard_fails_open(self):
        unit = self.first().name
        self.config(max_project_cost_usd=None, max_unit_cost_usd="lots")
        res = self.orche("dispatch", stdin=f"{amend(unit, 2)}\n---\n{GIFT}")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.count("delivered"), 2, res.stdout)
        self.assertEqual(self.orche("status").returncode, 0)
        history.append("acme", row("other", 30.0), 1)                 # the default cap still holds
        self.assertEqual(self.orche("dispatch", stdin=THIRD).returncode, 2)

    def test_a_guard_error_is_logged_and_the_block_relays(self):
        from orche import dispatch, roster
        unit = self.first().name
        reg = __import__("orche.registry", fromlist=["load"]).load()
        frm = roster.Roster(reg).member("main-acme")
        with mock.patch.object(guard, "check", side_effect=RuntimeError("boom")):
            [plan] = dispatch.plan_blocks(roster.Roster(reg), [amend(unit, 2)], frm)
        self.assertFalse(isinstance(plan, str), plan)
        self.assertIn(f"flow guard-error by=main-acme unit={unit} RuntimeError", self.events())

    def test_a_runner_amend_is_refused_at_the_run_cap(self):
        unit = self.first().name
        self.config(delivery="runner")                                   # the runner runs an AMEND (G5)
        for _ in range(3):
            history.append("acme", row(unit), 1)
        self.assertEqual(self.orche("dispatch", stdin=amend(unit, 2)).returncode, 0)
        history.append("acme", row(unit), 2)
        res = self.orche("dispatch", stdin=amend(unit, 3))
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertRegex(res.stdout, rf"{unit} ran 4 times since \d\d:\d\d without the owner \(cap 4\)\. "
                                     r"Ask the owner before running it again")
        self.assertIn(f"flow refused rule=G5 by=main-acme unit={unit} n=4", self.events())

    def test_the_rate_cap_counts_the_blocks_ahead_in_the_batch(self):
        folder = self.first()
        self.revs(folder, *range(2, 19))                                 # rev1 + 17 = 18
        fourth = BODY.replace("Add coupon field", "Add wishlist")
        res = self.orche("dispatch", stdin=f"{GIFT}\n---\n{THIRD}\n---\n{fourth}")
        self.assertEqual(res.returncode, 2, res.stdout)
        lines = res.stdout.splitlines()
        self.assertEqual([ln.split(":")[1].split()[0] for ln in lines[:3]], ["delivered", "delivered", "refused"])
        self.assertIn("acme sent 20 briefs and AMENDs since", lines[2])

    def test_flow_off_drops_the_streak_suffix(self):
        self.first()
        self.config(enabled=False)
        guard.pause("acme", "owner")
        fourth = BODY.replace("Add coupon field", "Add wishlist")
        res = self.orche("dispatch", stdin=f"{GIFT}\n---\n{THIRD}\n---\n{fourth}")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(res.stdout.count("is paused by the owner"), 3, res.stdout)
        self.assertNotIn("in a row", res.stdout)
        self.assertNotIn("rule=G7", self.events())

    def test_a_quiet_repeat_does_not_count_as_a_second_block(self):
        unit = self.first().name
        for _ in range(2):
            inbox.post(inbox.make(unit, "div-acme-build", "main-acme", "blocked", summary="stuck"))
        res = self.orche("dispatch", stdin=amend(unit, 2))
        self.assertEqual(res.returncode, 0, res.stdout)

    def test_resume_needs_a_word_a_full_second_after_the_pause(self):
        at = units.now().astimezone().replace(microsecond=0) - _dt.timedelta(seconds=60)
        guard.folder("acme").mkdir(parents=True, exist_ok=True)
        util.write_json(guard.folder("acme") / "pause.json", {"by": "owner", "at": at.isoformat(), "reason": ""})
        seen = guard.folder("acme") / "owner-seen"
        seen.touch()
        os.utime(seen, (at.timestamp() + 0.5, at.timestamp() + 0.5))
        self.assertEqual(guard.resume("acme", "main-acme")[0], False)
        os.utime(seen, (at.timestamp() + 1.5, at.timestamp() + 1.5))
        self.assertEqual(guard.resume("acme", "main-acme")[0], True)

    def test_the_refusal_events_line_carries_rules_and_counts_only(self):
        guard.pause("acme", "owner", "secret plans")
        res = self.orche("dispatch", stdin=f"{GIFT}\n---\nAMEND nope rev 2\n")
        self.assertEqual(res.returncode, 2)
        self.assertIn("dispatch refused by=main-acme blocks=2 refused=2 rules=G8", self.events())
        self.assertNotIn("secret plans", self.events())


class RunCapRecheckTest(RunnerCase):
    def test_the_cap_is_checked_again_inside_the_slot(self):
        unit = self.brief()
        capped = guard.Refused(f"{unit} ran 4 times since 10:00 without the owner (cap 4)", "G5", unit, 4)
        with mock.patch.object(guard, "run_refusal", side_effect=[None, capped]):
            code, out, err = self.orche("run", unit)
        self.assertEqual(code, 3, out + err)
        self.assertFalse((self.fake / "stdin.txt").exists())          # claude never ran
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr.get("quiet")), ("info", True))
        from orche import runs
        self.assertIsNone(runs.running("acme", unit))                 # the slot is released

    def test_a_failing_run_guard_is_logged_and_the_run_goes_ahead(self):
        unit = self.brief()
        self.persona()
        self.reply("STATUS: done\nok")
        with mock.patch.object(guard, "run_refusal", side_effect=RuntimeError("boom")):
            code, out, err = self.orche("run", unit)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"flow guard-error by=", self.events())
