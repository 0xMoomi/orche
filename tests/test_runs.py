"""The run package: `orche.runs` stays a light store the statusline can import; filing's RUN.json
record and the inbox lookups it files by."""
import io
import json
import os
import shlex
import subprocess
import sys
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support
from orche import brief, codex, config, inbox, mask, paths, procs, registry, runs, units, util
from orche.flow import guard
from orche.runs import filing, history
from orche.runs import job as runner
from test_runner import RunnerCase

STORE = ["orche", "orche.headless", "orche.log", "orche.paths", "orche.procs", "orche.registry", "orche.runs",
         "orche.shell", "orche.util"]


def loaded(module):
    code = (f"import sys; sys.path.insert(0, {str(support.ROOT / 'lib')!r}); import {module}; "
            "print(' '.join(sorted(m for m in sys.modules if m.split('.')[0] == 'orche')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    return out.split()


class ImportTest(unittest.TestCase):
    def test_store_imports_no_submodule(self):
        self.assertEqual(loaded("orche.runs"), STORE)

    def test_statusline_stays_light(self):
        self.assertEqual(loaded("orche.statusline"),
                         sorted(STORE + ["orche.roster", "orche.statusline", "orche.units"]))

    def test_filing_never_loads_the_run_or_its_callers(self):
        mods = loaded("orche.runs.filing")
        for name in ("orche.runs.job", "orche.dispatch", "orche.cmd_report", "orche.transcript",
                     "orche.donegate", "orche.hr"):
            self.assertNotIn(name, mods)

    def test_execute_and_preview_belong_to_the_store(self):
        """Loading job.py and filing.py never rebinds runs.execute / runs.preview: no re-export."""
        from orche.runs import filing as _filing, job as _job  # noqa: F401
        self.assertEqual((runs.execute.__module__, runs.preview.__module__), ("orche.runs", "orche.runs"))


EFFORT = {"mechanical": "low", "standard": "medium", "hard": "high"}


def _masked(value):
    return mask.redact(value)[0] if isinstance(value, str) else value


# Private copies of the three RUN.json literals at 3f47850 (runner.py:396-402, 484-490, 602-610), the
# record before filing.run_record, + `vendor` after `model` (0.8.0, design C11 §7.1), + `pr` last (0.9.0, §5.11); the shapes below
# must serialise byte for byte the same.
def old_unrun(job, now, status, error, stop_reason, routed):
    return {"unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
            "branch": job.branch, "tier": job.tier, "model": job.model, "vendor": "claude", "effort": EFFORT[job.tier],
            "started": now, "finished": now,
            "timeout_s": None, "exit_code": None, "is_error": status == "failed", "stop_reason": stop_reason,
            "cost_usd": None, "num_turns": None, "usage": None, "result_tail": "",
            "error": _masked(error), "head_before": None, "head_sha": None, "status": status,
            "pointer_filed": routed, "pr": None}


def old_lost(job, started, finished, error, recipient):
    return {
        "unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
        "branch": job.branch, "tier": job.tier, "model": job.model, "vendor": "claude", "effort": EFFORT[job.tier],
        "started": started, "finished": finished, "timeout_s": None, "exit_code": None,
        "is_error": True, "stop_reason": "lost", "cost_usd": None, "num_turns": None, "usage": None,
        "result_tail": "", "error": error, "head_before": None, "head_sha": None, "status": "failed",
        "pointer_filed": bool(recipient), "pr": None}


def old_finished(job, call, res, data, model, started, finished, code, error, stopped, result,
                 head_before, head, status, filed):
    return {"unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
            "branch": job.branch, "tier": job.tier, "model": model, "vendor": "claude", "effort": call.effort,
            "started": started, "finished": finished, "timeout_s": call.timeout,
            "exit_code": code, "is_error": res.is_error or (error is not None and not stopped),
            "stop_reason": "stopped" if stopped else _masked(
                data["subtype"] if str(data.get("subtype") or "").startswith("error")       # C9: an error wins
                else data.get("stop_reason") or data.get("subtype") or ("error" if error else None)),
            "cost_usd": res.cost_usd, "num_turns": res.turns, "usage": res.usage,
            "result_tail": mask.redact(result[-2000:])[0], "error": error,
            "head_before": head_before, "head_sha": head, "status": status, "pointer_filed": filed,
            "pr": None}


class RecordTest(unittest.TestCase):
    JOB = SimpleNamespace(unit="u-1", project="acme", to="div-acme-build", worktree=Path("/w/acme-build"),
                          branch="build", tier="standard", model="claude-opus-5-5", effort="medium")
    NOW, LATER = "2026-10-20T09:00:00+00:00", "2026-10-20T09:05:00+00:00"

    def same(self, old, new):
        self.assertEqual(json.dumps(old), json.dumps(new))
        self.assertEqual(list(new), list(filing.IDENTITY + filing.RESULT))

    def unrun(self, status, error, stop_reason, routed):
        return filing.run_record(runner.who(self.JOB), started=self.NOW, finished=self.NOW, status=status,
                                 stop_reason=stop_reason, is_error=status == "failed",
                                 error=filing.masked(error), pointer_filed=routed)

    def test_info_busy_unrun(self):
        old = old_unrun(self.JOB, self.NOW, "info", "runner busy", "busy", True)
        self.same(old, self.unrun("info", "runner busy", "busy", True))

    def test_failed_unrun(self):
        old = old_unrun(self.JOB, self.NOW, "failed", "runner terminated by SIGTERM", "error", False)
        self.same(old, self.unrun("failed", "runner terminated by SIGTERM", "error", False))

    def lost(self, recipient):
        error = "runner process 4242 died without filing"
        old = old_lost(self.JOB, self.NOW, self.LATER, error, recipient)
        new = filing.run_record(runner.who(self.JOB), started=self.NOW, finished=self.LATER, status="failed",
                                stop_reason="lost", is_error=True, error=error, pointer_filed=bool(recipient))
        self.same(old, new)

    def test_lost_with_recipient(self):
        self.lost("main-acme")

    def test_lost_without_recipient(self):
        self.lost(None)

    def test_who_is_the_jobs_identity_with_the_calls_model_and_effort(self):
        j, call = self.JOB, SimpleNamespace(model="claude-sonnet-5-5", effort="high")
        base = {"unit": "u-1", "project": "acme", "to": "div-acme-build", "worktree": "/w/acme-build",
                "branch": "build", "tier": "standard"}
        self.assertEqual(runner.who(j), {**base, "model": "claude-opus-5-5", "vendor": "claude", "effort": "medium"})
        self.assertEqual(runner.who(j, call), {**base, "model": "claude-sonnet-5-5", "vendor": "claude",
                                               "effort": "high"})
        codex_call = SimpleNamespace(model="codex:gpt-6.1-sol", effort="low")
        self.assertEqual(runner.who(j, codex_call), {**base, "model": "codex:gpt-6.1-sol", "vendor": "codex",
                                                     "effort": "low"})
        self.assertEqual(list(runner.who(j)), list(filing.IDENTITY))

    def finished(self, res, data, error, stopped, status):
        """job._run_locked's record against the old literal, for one finished run."""
        call = SimpleNamespace(model="claude-sonnet-5-5", effort="high", timeout=1860)
        result = "x" * 2500 + "\nSTATUS: blocked\nneeds a key"
        old = old_finished(self.JOB, call, res, data, call.model, self.NOW, self.LATER, 0, error, stopped,
                           result, "a" * 40, "b" * 40, status, True)
        new = filing.run_record(
            runner.who(self.JOB, call), started=self.NOW, finished=self.LATER, timeout_s=call.timeout,
            exit_code=0, is_error=res.is_error or (error is not None and not stopped),
            stop_reason=runner.stop_reason_of(data, error, stopped),
            cost_usd=res.cost_usd, num_turns=res.turns, usage=res.usage,
            result_tail=mask.redact(result[-filing.TAIL_CHARS:])[0], error=error,
            head_before="a" * 40, head_sha="b" * 40, status=status, pointer_filed=True)
        self.same(old, new)

    def test_finished_blocked_with_usage_and_stop_reason(self):
        res = SimpleNamespace(is_error=False, cost_usd=0.42, turns=7,
                              usage={"input_tokens": 10, "output_tokens": 20})
        self.finished(res, {"stop_reason": "end_turn", "subtype": "success"}, None, False, "blocked")

    def test_finished_with_an_error(self):
        res = SimpleNamespace(is_error=True, cost_usd=None, turns=None, usage=None)
        self.finished(res, {"subtype": "error_max_turns"}, "claude exited 1", False, "failed")
        self.finished(res, {"subtype": "error_max_turns", "stop_reason": "tool_use"}, "claude exited 1", False,
                      "failed")
        self.assertEqual(runner.stop_reason_of({"subtype": "error_max_turns", "stop_reason": "tool_use"},
                                               "claude exited 1", False), "error_max_turns")
        self.assertEqual(runner.stop_reason_of({"subtype": "success", "stop_reason": "end_turn"}, None, False),
                         "end_turn")
        self.finished(SimpleNamespace(is_error=False, cost_usd=None, turns=None, usage=None), {},
                      "timeout after 1860s", False, "failed")

    def test_a_timed_out_run_stops_with_error_whatever_it_printed(self):
        """A Codex run killed at its timeout after some events parses as error_no_message; RUN.json
        says `error`: the runner ended it."""
        for data in ({"subtype": "error_no_message", "stop_reason": "error_no_message"},
                     {"subtype": "success", "stop_reason": "completed"}, {}):
            with self.subTest(data=data):
                self.assertEqual(runner.stop_reason_of(data, "timeout after 660s", False, timed_out=True), "error")
        self.assertEqual(runner.stop_reason_of({"subtype": "error_no_message"}, "timeout after 660s", True,
                                               timed_out=True), "stopped")

    def test_finished_stopped_by_main(self):
        res = SimpleNamespace(is_error=False, cost_usd=0.1, turns=2, usage=None)
        self.finished(res, {"stop_reason": "end_turn"}, "killed by STOP", True, "info")


class InboxAboutTest(support.TempHomeCase):
    UNIT, FROM, RCPT = "u-1", "div-acme-build", "main-acme"
    STARTED = "2026-10-20T09:00:00+00:00"

    def put(self, name, ptr, drained=False):
        folder = inbox.box(self.RCPT) / (inbox.DRAINED if drained else "")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(ptr if isinstance(ptr, str) else json.dumps(ptr), encoding="utf-8")
        return path

    def ptr(self, at, **extra):
        return {"unit": self.UNIT, "from": self.FROM, "to": self.RCPT, "status": "done", "at": at, **extra}

    def test_about_reads_pending_and_drained_for_the_unit_and_sender_only(self):
        self.put("a.json", self.ptr(self.STARTED))
        self.put("b.json", self.ptr(self.STARTED), drained=True)
        self.put("c.json", {**self.ptr(self.STARTED), "unit": "u-2"})
        self.put("d.json", {**self.ptr(self.STARTED), "from": "feat-acme-x"})
        self.put("e.json", "{not json")
        self.put("f.json", "[1, 2]")
        unreadable = self.put("g.json", self.ptr(self.STARTED))
        os.chmod(unreadable, 0)
        try:
            found = inbox.about(self.RCPT, self.UNIT, self.FROM)
            blocked = not os.access(unreadable, os.R_OK)
        finally:
            os.chmod(unreadable, 0o644)
        names = sorted(path.name for path, _ in found)
        self.assertEqual(names, ["a.json", "b.json"] if blocked
                         else ["a.json", "b.json", "g.json"])
        self.assertEqual(inbox.about("nobody", self.UNIT, self.FROM), [])

    def test_worker_pointers_ignore_the_runners(self):
        self.put("mine.json", self.ptr(self.STARTED))
        self.put("runner.json", self.ptr(self.STARTED, via=filing.VIA), drained=True)
        self.assertEqual(filing.worker_pointers(self.RCPT, self.UNIT, self.FROM), {"mine.json"})
        self.assertEqual(filing.worker_pointers(None, self.UNIT, self.FROM), set())

    def test_filed_since_compares_at_with_started(self):
        self.put("old.json", self.ptr("2026-10-20T08:59:59+00:00", via=filing.VIA))
        self.assertFalse(filing.filed_since(self.RCPT, self.UNIT, self.FROM, self.STARTED))
        self.assertTrue(filing.filed_since(self.RCPT, self.UNIT, self.FROM, "not a time"))
        self.put("new.json", self.ptr(self.STARTED), drained=True)
        self.assertTrue(filing.filed_since(self.RCPT, self.UNIT, self.FROM, self.STARTED))
        self.assertFalse(filing.filed_since(None, self.UNIT, self.FROM, self.STARTED))


class StartRestartTest(support.TempHomeCase):
    """start() spawns `orche run <unit>` detached (procs.spawn_detached); restart() is stop() then
    start(), so the STOP marker predates the fresh run's ORCHE_RUN_SINCE."""
    UNIT = "acme-x-20261020-1"

    def spawn(self, call):
        seen = []
        with mock.patch.object(procs.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw))):
            result = call()
        [(argv, kw)] = seen
        kw["stdout"].close()
        return result, argv, kw

    def test_start_spawns_orche_run_detached(self):
        before = time.time_ns()
        result, argv, kw = self.spawn(lambda: runs.start(self.UNIT))
        self.assertIs(result, True)
        self.assertEqual(argv, [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), "run", self.UNIT])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["stdout"].name, str(runs.logs_dir() / f"{self.UNIT}.log"))
        self.assertEqual(kw["stdout"].mode, "ab")
        self.assertLessEqual(before, int(kw["env"][runs.SINCE_ENV]))
        self.assertLessEqual(int(kw["env"][runs.SINCE_ENV]), time.time_ns())
        self.assertTrue(kw["env"]["PATH"].startswith(str(paths.PLUGIN_ROOT / "bin") + os.pathsep))
        self.assertNotIn(procs.ENV, kw["env"])

    def test_restart_writes_the_marker_before_the_request_time(self):
        stopped, argv, kw = self.spawn(lambda: runs.restart("acme", self.UNIT))
        self.assertFalse(stopped)                       # no live run: started all the same
        self.assertEqual(argv[-2:], ["run", self.UNIT])
        self.assertLess(runs.marker_time("acme", self.UNIT), int(kw["env"][runs.SINCE_ENV]))
        self.assertFalse(runs.stop_requested("acme", self.UNIT, since=int(kw["env"][runs.SINCE_ENV])))

    def test_restart_returns_what_stop_returned(self):
        calls = []
        with mock.patch.object(runs, "stop", lambda proj, unit, timeout, reg: calls.append(unit) or True):
            stopped, argv, _ = self.spawn(lambda: runs.restart("acme", self.UNIT, timeout=1))
        self.assertTrue(stopped)
        self.assertEqual(calls, [self.UNIT])
        self.assertEqual(argv[-1], self.UNIT)

    def test_the_old_spawn_names_are_gone(self):
        from orche import dispatch
        old = ("spawn_detached", "subprocess", "run_" + "argv", "spawn_" + "run")   # split: no grep hit
        for name in old:     # R3: an old patch target fails loudly
            self.assertFalse(hasattr(dispatch, name), name)


class ReservationTest(unittest.TestCase):
    """history.reserve (design C11 §7.3) on fixed times: whole minutes rounded up, at least one,
    UNKNOWN_USD_PER_MIN each, at most the cap."""

    def test_whole_minutes_rounded_up_at_least_one_at_most_the_cap(self):
        at = "2026-10-23T09:00:00+00:00"
        cases = [(at, 10, 0.25), ("2026-10-23T09:00:59+00:00", 10, 0.25), ("2026-10-23T09:01:01+00:00", 10, 0.5),
                 ("2026-10-23T18:30:00+09:00", 10, 7.5), ("2026-10-23T10:00:00+00:00", 10, 10.0),
                 ("2026-10-23T10:00:00+00:00", 30, 15.0), (at, 0, 0.0), (None, 10, 0.25),
                 ("2026-10-23T08:00:00+00:00", 10, 0.25)]
        for finished, cap, want in cases:
            with self.subTest(finished=finished, cap=cap):
                self.assertEqual(history.reserve(at, finished, cap), want)
        self.assertEqual(history.UNKNOWN_USD_PER_MIN, 0.25)

    def test_history_lines_carry_the_vendor(self):
        rec = {k: None for k in filing.IDENTITY + filing.RESULT}
        rec.update(unit="u-1", model="codex", vendor="codex", cost_usd=0.5)
        self.assertEqual({k: v for k, v in history.entry(rec, 2).items() if v is not None},
                         {"unit": "u-1", "rev": 2, "model": "codex", "vendor": "codex", "cost_usd": 0.5})
        self.assertEqual(list(history.KEYS[3:6]), ["model", "vendor", "effort"])


class LockIdentityTest(RunnerCase):
    """The lock records what runs (filing.LOCKED, job.lock_info); a lost run is filed from it, never
    re-resolved from today's BRIEF.md."""

    def put_lock(self, unit, to="div-acme-build", **extra):
        """A dead run's locks (test_resilience's ReconcileTest has its own: subclassing it would rerun
        its tests); `to=None` leaves `to` out."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        record = {"pid": proc.pid, "unit": unit, "to": to, "started": "2026-01-01T09:05:00+00:00",
                  "started_ns": 1, **extra}
        if to is None:
            del record["to"]
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{unit}.lock").write_text(json.dumps(record))
        (folder / "div-acme-build.target.lock").write_text(json.dumps(record))

    def amend(self, unit):
        """BRIEF.md now says rev 2, TIER hard: what the lost run did not run."""
        path = self.units_dir() / unit / "BRIEF.md"
        text = path.read_text()
        path.write_text(text.replace("rev: 1\n", "rev: 2\n", 1).replace("TIER      standard", "TIER      hard"))
        b = brief.find(registry.load(), unit)
        self.assertEqual((b.rev, b.tier), (2, "hard"))

    def lost(self, unit):
        [act] = runs.reconcile(registry.load(), "acme", budget_s=10.0)
        self.assertEqual(act["action"], "reconciled")
        [ptr] = [p for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        rec = self.run_record(unit)
        return ptr["brief_rev"], {k: rec[k] for k in ("tier", "model", "effort", "worktree", "branch")}

    def test_a_lost_codex_run_is_reserved_up_to_its_timeout_not_to_the_reconcile(self):
        """§7.3: the lock's `started` to the earlier of now and `started` + `timeout_s`: this lock is
        months old, its timeout 600 s, so 10 minutes ($2.50) are reserved and G4 does not refuse;
        its `-o` file is removed."""
        unit = self.brief()
        out = codex.out_path(procs.run_id(unit, 1))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("partial")
        self.put_lock(unit, rev=1, tier="standard", model="codex", vendor="codex", effort="medium",
                      worktree=str(self.wt), branch="build", timeout_s=600)
        self.assertIsNone(guard.run_refusal("acme", unit))
        [act] = runs.reconcile(registry.load(), "acme", budget_s=10.0)
        self.assertEqual(act["action"], "reconciled")
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["model"], rec["vendor"], rec["cost_usd"],
                          rec["usage"], rec["timeout_s"]), ("failed", "lost", "codex", "codex", 2.5, None, 600))
        self.assertEqual(units.parse_iso(rec["finished"]), units.parse_iso("2026-01-01T09:15:00+00:00"))
        self.assertFalse(out.exists())
        [ptr] = [p for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        self.assertTrue(ptr["summary"].startswith(f"runner died: {unit}"), ptr["summary"])
        self.assertEqual([(r["vendor"], r["model"], r["cost_usd"]) for r in history.read("acme")],
                         [("codex", "codex", 2.5)])
        self.assertIsNone(guard.run_refusal("acme", unit))

    def test_a_lost_run_without_timeout_s_is_reserved_up_to_the_default_timebox(self):
        """A lock written before `timeout_s`: the default TIMEBOX + grace (31 minutes, $7.75)."""
        self.assertEqual(filing.LOST_TIMEOUT_S, runner.DEFAULT_TIMEBOX_S + runner.GRACE_S)
        unit = self.brief()
        self.put_lock(unit, rev=1, tier="standard", model="claude-opus-5-5", vendor="claude", effort="medium",
                      worktree=str(self.wt), branch="build")
        [act] = runs.reconcile(registry.load(), "acme", budget_s=10.0)
        self.assertEqual(act["action"], "reconciled")
        rec = self.run_record(unit)
        self.assertEqual((rec["cost_usd"], rec["timeout_s"]), (7.75, filing.LOST_TIMEOUT_S))
        self.assertEqual(units.parse_iso(rec["finished"]), units.parse_iso("2026-01-01T09:36:00+00:00"))

    def test_lock_info_is_locked(self):
        unit = self.brief()
        j = runner.resolve(self.reg, unit)
        self.assertEqual(set(runner.lock_info(j, runner.call_for(j, {}))), set(filing.LOCKED))

    def test_a_live_run_locks_what_runs(self):
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        j = runner.resolve(self.reg, unit)
        call = runner.call_for(j, config.load())       # the config the spawned run loaded
        want = {"to": "div-acme-build", "rev": 1, "tier": "standard", "model": call.model, "vendor": "claude",
                "effort": call.effort, "worktree": str(j.worktree), "branch": j.branch, "timeout_s": call.timeout}
        target = json.loads((self.home / "runs" / "acme" / "div-acme-build.target.lock").read_text())
        for lock in (self.lock(unit), target):
            self.assertEqual({k: lock.get(k) for k in filing.LOCKED}, want)
        proc.kill()
        proc.wait()
        self.amend(unit)
        self.assertEqual(self.lost(unit), (1, {k: want[k] for k in ("tier", "model", "effort", "worktree", "branch")}))

    def test_a_lost_run_is_filed_from_its_lock_not_todays_brief(self):
        unit = self.brief()
        self.put_lock(unit, rev=1, tier="standard", model="m-lock", effort="low", worktree="/w/lock", branch="b-lock")
        self.amend(unit)
        self.assertEqual(self.lost(unit), (1, {"tier": "standard", "model": "m-lock", "effort": "low",
                                               "worktree": "/w/lock", "branch": "b-lock"}))

    def test_a_lock_without_identity_gives_nulls_and_the_briefs_revision(self):
        unit = self.brief()
        self.put_lock(unit)
        self.amend(unit)
        self.assertEqual(self.lost(unit), (2, dict.fromkeys(("tier", "model", "effort", "worktree", "branch"))))

    def test_a_lock_without_to_is_filed_to_the_briefs_target(self):
        """Every lock written carries `to`; a hand-made one without it falls back to the brief's TO
        (only that: the other LOCKED keys stay null)."""
        unit = self.brief()
        self.put_lock(unit, to=None)
        self.assertEqual(self.lost(unit), (1, dict.fromkeys(("tier", "model", "effort", "worktree", "branch"))))
        self.assertEqual(self.run_record(unit)["to"], "div-acme-build")


class OnePointerTest(RunnerCase):
    """runs.execute, in process: past resolving the unit, one pointer per outcome, except a
    duplicate (the live run files), the worker's own report and a FilingError (reconcile files)."""

    def count(self, session="main-acme"):
        return len(self.inbox(session)) + len(self.inbox(session, drained=True))

    def execute(self, unit):
        return runs.execute(registry.load(), unit, util.read_json(self.home / "config.json", {}))

    def record(self, unit):
        path = self.units_dir() / unit / "RUN.json"
        return json.loads(path.read_text())["stop_reason"] if path.exists() else None

    def check(self, out, unit, code, pointers, stop_reason, has_record):
        self.assertEqual(out.code, code, out.stderr)
        self.assertEqual(self.count(), pointers)
        if unit is not None:
            self.assertEqual(self.record(unit), stop_reason)
        self.assertEqual(out.record is not None, has_record)

    def test_refused_before_a_unit(self):
        self.check(self.execute("Bad_Id"), None, 1, 0, None, False)
        self.check(self.execute("acme-nothing-20261018-1"), None, 2, 0, None, False)

    def test_retired_target(self):
        reg = registry.load()
        reg["projects"]["acme"]["divisions"]["build"]["status"] = "retired"
        registry.save(reg)
        unit = self.brief()
        out = self.execute(unit)
        self.check(out, unit, 2, 1, "error", False)
        self.assertIn("retired", out.stderr[0])

    def test_call_for_error(self):
        unit = self.brief()
        self.config({"claude_bin": str(self.claude), "delivery": "runner", "runner": {"turns_ceiling": "many"}})
        out = self.execute(unit)
        self.check(out, unit, 1, 1, "error", False)
        self.assertEqual(out.stderr[-1], "orche: runner error: ValueError: invalid literal for int() with "
                                         "base 10: 'many'")
        self.assertFalse((self.fake / "stdin.txt").exists())

    def test_busy(self):
        for i in range(4):
            self.lock_file(f"acme-other-20261018-{i + 10}")
        unit = self.brief()
        self.config({"claude_bin": str(self.claude), "runner": {"queue_wait_s": 0}})
        with mock.patch.object(runs, "POLL_S", 0.05):
            out = self.execute(unit)
        self.check(out, unit, 3, 1, "busy", False)

    def test_duplicate(self):
        unit = self.brief()
        self.lock_file(unit)
        self.check(self.execute(unit), unit, 4, 1, None, False)

    def test_finished_blocked(self):
        unit = self.brief()
        self.reply("STATUS: blocked\nneeds a key")
        out = self.execute(unit)
        self.check(out, unit, 0, 1, "end_turn", True)
        self.assertEqual((out.record["status"], out.stderr), ("blocked", ()))

    def test_the_workers_own_report(self):
        unit = self.brief()
        self.action(f"env -u ORCHE_RUNNER orche report blocked --unit {unit} --summary self-filed >/dev/null\n")
        self.reply("STATUS: blocked\nfrom the runner")
        out = self.execute(unit)
        self.check(out, unit, 0, 1, "end_turn", True)
        self.assertFalse(out.record["pointer_filed"])

    def test_unroutable(self):
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(filing, "recipient", return_value=None):
            out = self.execute(unit)
        self.check(out, unit, 0, 0, "end_turn", True)
        self.assertFalse(out.record["pointer_filed"])

    def test_filing_error_leaves_the_lock_for_reconcile(self):
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(filing, "write_report", side_effect=OSError("disk full")):
            out = self.execute(unit)
        self.check(out, unit, 5, 0, None, False)
        self.assertEqual(out.stderr, (f"orche: cannot file the result of {unit}: REPORT.md: disk full; "
                                      "the run's lock stays for reconcile",))
        self.assertIsNotNone(self.lock(unit))

    def release_fails_then_reconcile(self, pid_of):
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(runs, "release", autospec=True, side_effect=OSError("EIO")):
            out = self.execute(unit)
        self.check(out, unit, 0, 1, "end_turn", True)
        self.assertIn(f"runner: {unit}: release failed: OSError: EIO", self.hooks_log())
        lock = self.lock(unit)
        lock["pid"] = pid_of()          # what reconcile next sees
        (self.home / "runs" / "acme" / f"{unit}.lock").write_text(json.dumps(lock))
        [act] = runs.reconcile(registry.load(), "acme", budget_s=10.0)
        self.assertEqual(act["action"], "stale-lock")
        self.assertIsNone(self.lock(unit))
        self.assertEqual(self.count(), 1)

    def test_release_failing_files_once_and_reconcile_drops_the_lock(self):
        def dead_pid():                 # this process is gone
            dead = subprocess.Popen(["true"])
            dead.wait()
            return dead.pid
        self.release_fails_then_reconcile(dead_pid)

    def test_release_failing_lock_with_a_live_non_run_pid_is_stale(self):
        # alive, but `ps` shows no `orche run <unit>` (a recycled pid): is_run_process is False
        self.release_fails_then_reconcile(os.getpid)

    def test_cleanup_failures_log_the_real_function_names(self):
        unit = self.brief()
        marker = self.home / "runs" / "acme" / f"{unit}.stop"
        self.action(f"echo x > {shlex.quote(str(marker))}\n")     # late STOP: clear_stop runs
        self.reply("STATUS: blocked\nx")
        # autospec keeps __name__: the real _quietly logs the function's name
        with mock.patch.object(runs, "release", autospec=True, side_effect=OSError("EIO")), \
             mock.patch.object(runs, "clear_stop", autospec=True) as clear:
            out = self.execute(unit)
        self.check(out, unit, 0, 1, "end_turn", True)
        hooks = self.hooks_log()
        self.assertIn(f"runner: {unit}: release failed: OSError: EIO", hooks)
        self.assertIn(f"runner: {unit}: STOP marker kept: the lock was not released", hooks)
        clear.assert_not_called()       # the marker outlives the lock (an AMEND never sees it unmarked)

    def test_release_failing_keeps_the_stop_marker_through_reconcile(self):
        unit = self.brief()
        marker = self.home / "runs" / "acme" / f"{unit}.stop"
        self.action(f"echo x > {shlex.quote(str(marker))}\n")     # late STOP: the run consumes it
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(runs, "release", autospec=True, side_effect=OSError("EIO")):
            out = self.execute(unit)
        self.check(out, unit, 0, 1, "end_turn", True)
        self.assertIn(f"runner: {unit}: release failed: OSError: EIO", self.hooks_log())
        self.assertTrue(marker.exists())
        self.assertIsNotNone(self.lock(unit))
        run_json = (self.units_dir() / unit / "RUN.json").read_bytes()
        dead = subprocess.Popen(["true"])
        dead.wait()
        lock = self.lock(unit)
        lock["pid"] = dead.pid          # this process is gone
        (self.home / "runs" / "acme" / f"{unit}.lock").write_text(json.dumps(lock))
        [act] = runs.reconcile(registry.load(), "acme", budget_s=10.0)
        self.assertEqual(act["action"], "stale-lock")
        self.assertIsNone(self.lock(unit))
        self.assertTrue(marker.exists())    # reconcile leaves STOP markers: older than any later request
        self.assertEqual(self.count(), 1)
        self.assertEqual((self.units_dir() / unit / "RUN.json").read_bytes(), run_json)

    def test_clear_stop_failing_files_once_and_keeps_the_marker(self):
        unit = self.brief()
        marker = self.home / "runs" / "acme" / f"{unit}.stop"
        self.action(f"echo x > {shlex.quote(str(marker))}\n")     # STOP after claude finished: late
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(runs, "clear_stop", autospec=True, side_effect=OSError("EACCES")):
            out = self.execute(unit)
        self.check(out, unit, 0, 1, "end_turn", True)
        self.assertIn(f"run unit={unit} to=div-acme-build delivery=runner-stop-late", self.events())
        self.assertIn(f"runner: {unit}: clear_stop failed: OSError: EACCES", self.hooks_log())
        self.assertTrue(marker.exists())
        self.assertIsNone(self.lock(unit))

    def test_runner_error_trace_is_print_exc(self):
        unit = self.brief()
        j = runner.resolve(registry.load(), unit)
        try:
            raise OSError("disk full")
        except OSError as exc:
            printed = io.StringIO()
            traceback.print_exc(file=printed)
            out = runner._runner_error(registry.load(), j, exc)
        self.assertEqual(out.stderr[0] + "\n", printed.getvalue())
        self.assertEqual(out.stderr[1:], ("orche: runner error: OSError: disk full",))
        self.assertEqual(self.count(), 1)

    def hooks_log(self):
        path = self.home / "logs" / "hooks.log"
        return path.read_text() if path.exists() else ""


class GlobalCapTest(support.TempHomeCase):
    """0.9.0 T4 (design §8.3, R21): runner.max_parallel_total across projects and the memory guard."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(runs, "reconcile", return_value=[])   # the locks below are this process
        patcher.start()
        self.addCleanup(patcher.stop)

    def live(self, proj, unit):
        folder = runs.run_dir(proj)
        folder.mkdir(parents=True, exist_ok=True)
        util.write_json(folder / f"{unit}.lock", {"pid": os.getpid(), "unit": unit, "started": util.now_iso(),
                                                  "started_ns": 1})

    def events(self):
        path = self.home / "logs" / "events.log"
        return path.read_text() if path.exists() else ""

    def test_the_global_cap_counts_every_project(self):
        self.live("alpha", "a1")
        self.live("beta", "b1")
        with self.assertRaises(runs.Busy) as ctx:
            runs.acquire("alpha", "a2", {}, 4, wait_s=0, reg={}, total=2)
        self.assertEqual(str(ctx.exception), "runner busy: 2 live runs in all projects (cap 2)")
        self.assertIn("run unit=a2 waiting=global n=2 cap=2\n", self.events())
        self.assertTrue(runs.at_cap("alpha", {"runner": {"max_parallel": 4, "max_parallel_total": 2}}))
        self.assertFalse(runs.at_cap("alpha", {"runner": {"max_parallel": 4, "max_parallel_total": 3}}))
        self.assertEqual(runs.total(), 2)
        lock = runs.acquire("alpha", "a2", {}, 4, wait_s=0, reg={}, total=3)
        self.assertEqual(lock.name, "a2.lock")

    def test_the_project_cap_still_applies_first(self):
        self.live("alpha", "a1")
        with self.assertRaises(runs.Busy) as ctx:
            runs.acquire("alpha", "a2", {}, 1, wait_s=0, reg={}, total=8)
        self.assertEqual(str(ctx.exception), "runner busy: 1 live runs")
        self.assertNotIn("waiting=global", self.events())

    def test_low_memory_waits_and_an_unreadable_reading_is_no_guard(self):
        with mock.patch.object(procs, "free_mb", return_value=1000):
            with self.assertRaises(runs.Busy) as ctx:
                runs.acquire("alpha", "a1", {}, 4, wait_s=0, reg={}, total=8, min_free_mb=2048)
        self.assertEqual(str(ctx.exception), "runner busy: free memory 1000 MB < 2048 MB")
        self.assertIn("run unit=a1 waiting=memory free_mb=1000 min_mb=2048\n", self.events())
        self.assertFalse((runs.run_dir("alpha") / "a1.lock").exists())   # no slot taken while waiting
        with mock.patch.object(procs, "free_mb", return_value=None):
            runs.acquire("alpha", "a1", {}, 4, wait_s=0, reg={}, total=8, min_free_mb=2048)
            runs.acquire("alpha", "a3", {}, 4, wait_s=0, reg={}, total=8, min_free_mb=2048)
        hooks = (self.home / "logs" / "hooks.log").read_text()
        self.assertEqual(hooks.count("free memory is unreadable"), 1)       # once a day

    def test_a_waiting_run_rereads_the_memory_guard_after_each_poll(self):
        def lowered(_s):
            (self.home / "config.json").write_text(json.dumps({"runner": {"min_free_mb": 0}}))
        (self.home / "config.json").write_text(json.dumps({"runner": {"min_free_mb": 2048}}))
        with mock.patch.object(procs, "free_mb", return_value=1000), \
                mock.patch.object(runs.time, "sleep", side_effect=lowered) as sleep:
            lock = runs.acquire("alpha", "a1", {}, 4, wait_s=60, poll_s=0, reg={}, total=8, min_free_mb=2048)
        self.assertTrue(lock.exists())
        self.assertEqual(sleep.call_count, 1)

    def test_test_homes_have_no_memory_guard(self):
        self.assertEqual(json.loads((self.home / "config.json").read_text())["runner"]["min_free_mb"], 0)
        self.assertEqual(support.no_wake({})["runner"]["min_free_mb"], 0)
        self.assertEqual(support.no_wake({"runner": {"min_free_mb": 5}})["runner"]["min_free_mb"], 5)


if __name__ == "__main__":
    unittest.main()
