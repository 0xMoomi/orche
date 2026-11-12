"""Staff errands (lib/orche/errand.py, lib/orche/cmd_errand.py) and the runner, filing and shape changes they need:
the Staff pseudo-member, start / answer / hand over / stop and their refusals, the errand's state, the runner's
errand job (fake claude through config `claude_bin`), Main-reply forwarding, Staff-to-Main forwarding and the sweep."""
import datetime as dt
import json
import os
import time
from pathlib import Path
from unittest import mock

import test_runner
from orche import brief, config, errand, inbox, registry, roster, runs, shell, templates, units
from orche.flow import guard, lineage, shape, usage
from orche.runs import filing
from orche.runs import job as runner

STAFF, MAIN = "staff-acme", "main-acme"


class ErrandCase(test_runner.RunnerCase):
    def setUp(self):
        super().setUp()
        self.git_init(self.repo, "README")
        self.started = []
        patcher = mock.patch.object(runs, "start", side_effect=lambda unit: self.started.append(unit) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def errand(self, *argv, session=STAFF, **env):
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": session, **env}):
            return self.orche("errand", *argv)

    def start(self, goal="List open PRs that touch the payment page", **kw):
        return errand.start(registry.load(), STAFF, goal, "Owner's words: which PRs?", **kw)

    def brief_of(self, unit):
        return brief.find(registry.load(), unit, project="acme")

    def result(self, unit, status, summary="the answer", via="runner", rev=None, minutes=0, frm=STAFF, to=STAFF,
               **extra):
        """A pointer filed to Staff (pending), `minutes` old."""
        b = self.brief_of(unit)
        at = (units.now() - dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
        ptr = inbox.make(unit, frm, to, status, brief_rev=rev or b.rev, summary=summary, via=via,
                         lineage=b.lineage, **extra)
        ptr["at"] = at
        return inbox.write(ptr)

    def newest(self, session, drained=False):
        """The pointer written last to `session`'s inbox (file names of one second do not sort by time)."""
        paths = self.inbox(session, drained)
        return json.loads(max(paths, key=lambda p: p.stat().st_mtime_ns).read_text())

    def state(self, unit, now=None):
        return errand.state(registry.load(), self.brief_of(unit), now)


class MemberTest(ErrandCase):
    def test_staff_of_a_registered_project_is_the_pseudo_member(self):
        reg = registry.load()
        m = errand.member(reg, STAFF)
        self.assertEqual((m.session, m.project, m.kind, m.name, m.division, m.entry, m.home),
                         (STAFF, "acme", "staff", "staff", None, {}, self.repo))
        self.assertFalse(m.owns_worktree)
        for other in ("staff-nope", MAIN, "div-acme-build", "", None):
            self.assertIsNone(errand.member(reg, other), other)


class StartTest(ErrandCase):
    def test_start_writes_the_brief_and_starts_the_run(self):
        code, out, err = self.errand("--goal", "List open PRs that touch the payment page",
                                     "--context", "Owner's words: which PRs?")
        unit = self.only_unit()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"errand {unit} started (up to 10m); the result comes back here\n")
        self.assertTrue(unit.startswith(f"acme-list-open-prs-that-{dt.date.today():%Y%m%d}-"))
        text = (self.units_dir() / unit / "BRIEF.md").read_text()
        self.assertTrue(text.startswith(f"rev: 1\nunit: {unit}\nto: {STAFF}   from: {STAFF}\n"
                                        f"lineage: {unit}   parent: -\nworktree: {self.repo}   branch: "), text)
        b = self.brief_of(unit)
        self.assertEqual((b.to, b.sender, b.lineage, b.tier, b.fields["TIMEBOX"]), (STAFF, STAFF, unit, "standard", "10m"))
        self.assertEqual(b.fields["CONTEXT"], "Owner's words: which PRs?")
        self.assertEqual(self.started, [unit])
        self.assertIn(f"errand start unit={unit} project=acme timebox=600s", self.events())
        self.assertEqual(self.state(unit), "waiting")
        self.assertEqual([x.unit for x in errand.open_errands(registry.load(), "acme")], [unit])

    def test_json_and_dry_run(self):
        code, out, err = self.errand("--json", "--dry-run", "--goal", "x")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        unit = f"acme-x-{dt.date.today():%Y%m%d}-1"
        self.assertEqual(data, {"unit": unit, "mode": "start", "state": "running", "dry_run": True,
                                "brief": str(self.repo / ".orche" / "units" / unit / "BRIEF.md")})
        code, out, _ = self.errand("--dry-run", "--goal", "x")
        self.assertEqual(out, f"[dry-run] would write {self.repo}/.orche/units/{unit}/BRIEF.md and start the errand\n")
        self.assertFalse(self.units_dir().exists() and any(self.units_dir().iterdir()))
        self.assertEqual(self.started, [])

    def refused(self, *argv, session=STAFF, **env):
        code, out, err = self.errand(*argv, session=session, **env)
        self.assertEqual((code, out), (2, ""), err)
        self.assertTrue(err.startswith("orche: errand refused: "), err)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertEqual(self.started, [])
        return err

    def test_refused_when_not_staff(self):
        self.assertIn("only a project's CEO Staff runs errands", self.refused("--goal", "x", session=MAIN))
        self.assertIn("only a project's CEO Staff", self.refused("--goal", "x", session="staff-nope"))

    def test_refused_under_the_runner(self):
        self.assertIn("an errand cannot start errands", self.refused("--goal", "x", ORCHE_RUNNER="1"))

    def test_refused_in_orches_staff_turn(self):
        lineage.write_turn(STAFF, False, [])
        self.assertIn("this turn is Orche's", self.refused("--goal", "x"))
        lineage.write_turn(STAFF, True, [])
        self.assertEqual(self.errand("--goal", "x")[0], 0)

    def test_refused_while_paused(self):
        guard.pause("acme", "user", "travel")
        err = self.refused("--goal", "x")
        self.assertRegex(err, r"acme is paused by the owner since \d\d:\d\d \(travel\): nothing starts until he "
                              r"resumes it in Main\n$")

    def test_refused_at_the_spend_cap(self):
        self.config({"claude_bin": str(self.claude), "flow": {"max_project_cost_usd": 0}})
        err = self.refused("--goal", "x")
        self.assertIn("acme spent $0.00 on headless runs since", err)
        self.assertIn("One word from the owner to Main lifts it.", err)
        self.assertNotIn("Ask the owner", err)
        self.assertIn("errand refused session=staff-acme unit=- rule=G4", self.events())

    def test_refused_with_two_open(self):
        a, b = self.start("first errand"), self.start("second errand")
        self.started.clear()
        err = self.refused("--goal", "third errand")
        self.assertIn(f"2 errands are open ({', '.join(sorted([a, b]))})", err)

    def test_refused_over_twenty_minutes(self):
        self.assertIn("TIMEBOX 21m is over 20m", self.refused("--goal", "x", "--timebox", "21m"))
        self.assertEqual(self.errand("--goal", "x", "--timebox", "20m")[0], 0)

    def test_usage_errors(self):
        for argv in ((), ("--goal", "x", "--stop"), ("u", "--goal", "x"), ("--answer", "y"), ("--main",),
                     ("--goal", "x", "--timebox", "soon")):
            code, _out, err = self.errand(*argv)
            self.assertEqual(code, 1, (argv, err))
        code, _out, err = self.errand("acme-nope-20261112-1", "--stop")
        self.assertEqual(code, 1, err)
        self.assertIn("unknown unit", err)


class StartFailureTest(ErrandCase):
    def check_failed(self, unit, why):
        marker = self.newest(STAFF, drained=True)
        self.assertEqual((marker["unit"], marker["status"], marker["via"], marker["summary"]),
                         (unit, "failed", "staff", f"could not start: {why}"))
        self.assertFalse([p for p in self.pointers(STAFF) if p["status"] == "failed"])    # drained: no wake
        self.assertEqual(self.state(unit), "failed")
        self.assertEqual(errand.open_errands(registry.load(), "acme"), [])
        self.assertIn(f"errand refused session=staff-acme unit={unit} rule=not-started", self.events())

    def test_a_start_whose_run_is_not_spawned_is_failed_and_refused(self):
        with mock.patch.object(runs, "start", return_value=False):
            with self.assertRaises(errand.Refused) as cm:
                self.start()
        self.assertEqual(str(cm.exception), "could not start: the run was not spawned")
        self.check_failed(self.only_unit(), "the run was not spawned")

    def test_a_start_whose_spawn_raises_is_failed_and_refused(self):
        with mock.patch.object(runs, "start", side_effect=OSError("no orche on PATH")):
            code, out, err = self.errand("--goal", "List open PRs")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "orche: errand refused: could not start: OSError: no orche on PATH\n")
        self.check_failed(self.only_unit(), "OSError: no orche on PATH")

    def test_an_answer_whose_run_is_not_spawned_is_failed(self):
        unit = self.start()
        self.result(unit, "question", "which repo?")
        with mock.patch.object(runs, "start", return_value=False), self.assertRaises(errand.Refused):
            errand.answer(registry.load(), STAFF, unit, "the web repo")
        self.assertEqual(self.brief_of(unit).rev, 2)
        self.check_failed(unit, "the run was not spawned")


class AnswerTest(ErrandCase):
    def test_answer_needs_a_question(self):
        unit = self.start()
        code, _out, err = self.errand(unit, "--answer", "yes")
        self.assertEqual(code, 2)
        self.assertIn(f"{unit} asks nothing", err)
        self.result(unit, "done", "the answer")
        self.assertEqual(self.errand(unit, "--answer", "yes")[0], 2)

    def test_answer_amends_with_the_owners_words_and_runs_again(self):
        unit = self.start()
        self.started.clear()
        self.result(unit, "blocked", "which repo?", owner="which repo? recommend web; default web")
        self.assertEqual(self.state(unit), "needs-owner")
        code, out, err = self.errand(unit, "--answer", "the web repo")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"errand {unit} rev 2 started with the owner's answer; the result comes back here\n")
        b = self.brief_of(unit)
        self.assertEqual(b.rev, 2)
        self.assertRegex(b.fields["CONTEXT"], r"Owner's words: which PRs\?\nOwner's answer \d\d:\d\d: the web repo$")
        self.assertEqual(brief.causes(b.folder), {2: "owner"})
        self.assertEqual((b.to, b.lineage), (STAFF, unit))
        self.assertEqual(self.started, [unit])
        self.assertIn(f"errand answer unit={unit} project=acme rev=2", self.events())
        self.assertEqual(self.state(unit), "waiting")      # no result at rev 2 yet


class HandOverTest(ErrandCase):
    def test_a_new_request_goes_to_main_and_the_errand_ends_handed(self):
        code, out, err = self.errand("--main", "--goal", "Fix the typo on the pricing page",
                                     "--context", "Owner's words: fix it")
        unit = self.only_unit()
        self.assertEqual((code, err, out), (0, "", f"handed to Main: {unit}; Main's answer comes back here\n"))
        [ptr] = self.pointers(MAIN)
        b = self.brief_of(unit)
        self.assertEqual({k: ptr[k] for k in ("unit", "from", "to", "status", "via", "lineage", "report_path",
                                                "brief_rev")},
                         {"unit": unit, "from": STAFF, "to": MAIN, "status": "external-request", "via": "staff",
                          "lineage": unit, "report_path": str(b.path), "brief_rev": 1})
        self.assertEqual(ptr["summary"], "The owner asks Main (via Staff): Fix the typo on the pricing page\n"
                                         "Owner's words: fix it")
        self.assertEqual(self.inbox(STAFF), [])
        [marker] = [json.loads(p.read_text()) for p in self.inbox(STAFF, drained=True)]
        self.assertEqual((marker["status"], marker["summary"], marker["via"], marker["from"], marker["to"]),
                         ("done", "handed to Main", "staff", STAFF, STAFF))
        self.assertEqual(self.started, [])
        self.assertEqual(self.state(unit), "handed")
        self.assertIn(f"errand handover unit={unit} project=acme", self.events())

    def test_an_errand_goes_to_main_once_until_main_replies(self):
        unit = self.start()
        self.result(unit, "done", "NEEDS MAIN: change the copy in web/pay.tsx")
        report = self.units_dir() / unit / "REPORT.md"
        report.write_text("STATUS: done\nNEEDS MAIN: change the copy\n")
        self.assertEqual(self.state(unit), "needs-main")
        code, out, err = self.errand(unit, "--main", "--context", "Owner: yes, go ahead")
        self.assertEqual((code, err), (0, ""))
        [ptr] = self.pointers(MAIN)
        self.assertEqual((ptr["status"], ptr["via"], ptr["report_path"], ptr["lineage"]),
                         ("external-request", "staff", str(report), unit))
        self.assertTrue(ptr["summary"].startswith("The owner asks Main (via Staff): List open PRs"), ptr["summary"])
        self.assertTrue(ptr["summary"].endswith("\nOwner: yes, go ahead"))
        self.assertEqual(self.state(unit), "handed")
        code, _out, err = self.errand(unit, "--main", "--context", "again")
        self.assertEqual(code, 2)
        self.assertIn("already with Main", err)
        time.sleep(1.1)
        self.result(unit, "question", "Which color?", via="main", frm=MAIN)
        code, _out, err = self.errand(unit, "--main", "--context", "blue")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.pointers(MAIN)), 2)

    def test_a_handover_main_has_seen_can_be_sent_again(self):
        unit = errand.hand_over(registry.load(), STAFF, goal="Fix the typo")
        code, _out, err = self.errand(unit, "--main", "--context", "again")
        self.assertEqual(code, 2)                                   # still pending: Main has not seen it
        self.assertIn("already with Main", err)
        inbox.drain([p for p in self.inbox(MAIN)])                  # Main's turn took it, then was cut short
        code, _out, err = self.errand(unit, "--main", "--context", "Owner: blue, and go on")
        self.assertEqual((code, err), (0, ""))
        ptr = self.newest(MAIN)
        self.assertTrue(ptr["summary"].endswith("\nOwner: blue, and go on"), ptr["summary"])


class StopTest(ErrandCase):
    def test_a_running_errand_is_stopped(self):
        unit = self.start()
        with mock.patch.object(runs, "running", return_value={"pid": 1}), \
                mock.patch.object(runs, "stop", return_value=True) as stop:
            self.assertEqual(self.state(unit), "running")
            code, out, err = self.errand(unit, "--stop")
            self.assertEqual((code, out, err), (0, f"errand {unit} stopped\n", ""))
            stop.assert_called_once_with("acme", unit, reg=mock.ANY)
            code, out, err = self.errand(unit, "--stop", session=MAIN)      # Main may stop a running one
            self.assertEqual((code, err), (0, ""))
            code, _out, err = self.errand(unit, "--stop", session="div-acme-build")
            self.assertEqual(code, 2)
            self.assertIn("only the project's CEO Staff or its Main", err)
        self.assertIn(f"errand stop unit={unit} project=acme by=staff-acme state=stopped", self.events())

    def test_a_waiting_errand_without_a_run_is_dropped_at_once(self):
        a, b = self.start("one thing"), self.start("two thing")
        with mock.patch.object(runs, "stop", return_value=False) as stop:
            code, out, err = self.errand(a, "--stop")
        self.assertEqual((code, out, err), (0, f"errand {a} stopped\n", ""))
        stop.assert_called_once_with("acme", a, reg=mock.ANY)
        marker = self.newest(STAFF, drained=True)
        self.assertEqual((marker["unit"], marker["status"], marker["summary"]), (a, "stop", "dropped by the owner"))
        self.assertEqual(self.state(a), "dropped")
        self.assertEqual([x.unit for x in errand.open_errands(registry.load(), "acme")], [b])
        self.start("three thing")                                   # one slot is free again

    def test_a_running_errand_keeps_its_run_to_file_the_stop(self):
        unit = self.start()
        with mock.patch.object(runs, "running", return_value={"pid": 1}), \
                mock.patch.object(runs, "stop", return_value=True):
            self.assertEqual(errand.stop(registry.load(), STAFF, unit), "stopped")
        self.assertEqual(self.inbox(STAFF, drained=True), [])        # the run files "stopped by the owner"

    def test_an_errand_needing_the_owner_is_dropped(self):
        unit = self.start()
        self.result(unit, "blocked", "which repo?")
        self.assertEqual(self.errand(unit, "--stop", session=MAIN)[0], 2)     # Main stops only a running one
        code, out, _err = self.errand(unit, "--stop")
        self.assertEqual((code, out), (0, f"errand {unit} dropped\n"))
        marker = self.newest(STAFF, drained=True)
        self.assertEqual((marker["status"], marker["summary"], marker["via"]), ("stop", "dropped by the owner", "staff"))
        self.assertEqual(self.state(unit), "dropped")
        code, _out, err = self.errand(unit, "--stop")
        self.assertEqual(code, 2)
        self.assertIn("nothing to stop", err)

    def test_an_errand_with_main_asks_main_to_stop(self):
        self.errand("--main", "--goal", "Fix the typo")
        unit = self.only_unit()
        code, out, _err = self.errand(unit, "--stop")
        self.assertEqual((code, out), (0, f"asked Main to stop {unit} and its units\n"))
        ptr = self.newest(MAIN)
        self.assertEqual((ptr["status"], ptr["via"]), ("external-request", "staff"))
        self.assertTrue(ptr["summary"].startswith("The owner asks to stop this request and its units"))

    def test_stop_works_while_paused(self):
        unit = self.start()
        self.result(unit, "question", "which?")
        guard.pause("acme", "user")
        self.assertEqual(self.errand(unit, "--stop")[0], 0)


class StateTest(ErrandCase):
    def test_each_state(self):
        unit = self.start()
        self.assertEqual(self.state(unit), "waiting")
        old = time.time() - 61 * 60
        for name in ("BRIEF.rev1.md", "BRIEF.md"):
            os.utime(self.units_dir() / unit / name, (old, old))
        self.assertEqual(self.state(unit), "lost")
        cases = [("done", "the answer", "runner", "answered"), ("done", "x\nNEEDS MAIN: a change", "runner", "needs-main"),
                 ("blocked", "?", "runner", "needs-owner"), ("question", "?", "runner", "needs-owner"),
                 ("failed", "run failed", "runner", "failed"), ("info", "stopped by the owner", "runner", "dropped"),
                 ("info", "unverified: no STATUS line\nthe answer", "runner", "answered"),
                 ("stop", "dropped by the owner", "staff", "dropped"), ("done", "handed to Main", "staff", "handed")]
        for i, (status, summary, via, want) in enumerate(cases):
            time.sleep(0.01)
            self.result(unit, status, summary, via=via, minutes=-i)
            self.assertEqual(self.state(unit), want, (status, summary))
        self.result(unit, "done", "an old rev", rev=7, minutes=-20)       # another rev is no result of this one
        self.assertEqual(self.state(unit), "handed")
        with mock.patch.object(runs, "running", return_value={"pid": 1}):
            self.assertEqual(self.state(unit), "running")

    def old(self, unit):
        old = time.time() - 61 * 60
        for name in ("BRIEF.rev1.md", "BRIEF.md"):
            os.utime(self.units_dir() / unit / name, (old, old))

    def test_a_queued_run_waits_never_lost(self):
        unit = self.start()
        self.old(unit)
        table = shell.Result(0, f"/usr/bin/python3 /x/bin/orche run {unit}\nother\n", "", [])
        with mock.patch.object(shell, "run", return_value=table) as ps:
            self.assertEqual(self.state(unit), "waiting")
            self.assertEqual(ps.call_args[0][0][0], "ps")
            self.assertEqual(errand.sweep(registry.load()), [])
        other = shell.Result(0, f"/x/bin/orche run {unit}2\n", "", [])
        with mock.patch.object(shell, "run", return_value=other):
            self.assertEqual(self.state(unit), "lost")
        with mock.patch.object(shell, "run", return_value=shell.Result(1, "", "boom", [])):
            self.assertEqual(self.state(unit), "lost")

    def test_a_held_run_waits_never_lost(self):
        unit = self.start()
        self.old(unit)
        until = (units.now() + dt.timedelta(hours=1)).isoformat(timespec="seconds")
        hold = {"vendor": "claude", "until": until, "units": [{"project": "acme", "unit": unit, "stage": "work"}]}
        usage.path("claude").parent.mkdir(parents=True, exist_ok=True)
        usage.path("claude").write_text(json.dumps(hold))
        self.assertEqual(self.state(unit), "waiting")
        self.result(unit, "info", "held: claude usage limit until 12:00; this unit runs then")
        self.assertEqual(self.state(unit), "waiting")
        usage.path("claude").write_text(json.dumps({**hold, "until": "2020-01-01T00:00:00+00:00"}))
        self.assertEqual(self.state(unit), "dropped")

    def test_a_real_result_after_lost_wins(self):
        unit = self.start()
        self.old(unit)
        self.assertEqual(errand.sweep(registry.load()), [f"lost {unit}"])
        self.assertEqual(self.state(unit), "failed")
        time.sleep(1.1)
        self.result(unit, "done", "the answer after all")
        self.assertEqual(self.state(unit), "answered")


class RunnerTest(ErrandCase):
    def test_resolve_call_and_prompt(self):
        unit = self.start()
        reg = registry.load()
        job = runner.resolve(reg, unit)
        self.assertEqual((job.worktree, job.root, job.model, job.effort, job.kind, job.persona, job.to, job.gated,
                          job.timebox_s, job.branch),
                         (self.repo, self.repo, "claude-opus-5-5", "medium", "staff", errand.PERSONA, STAFF, False,
                          600, "-"))
        call = runner.call_for(job, {"runner": {}})
        self.assertEqual((call.allowed, call.denied, call.append_file, call.max_turns, call.cwd),
                         ("Read,Write,Edit,Glob,Grep,Bash,WebFetch,WebSearch", "", errand.PERSONA, 30, self.repo))
        self.assertEqual(call.env, {"ORCHE_SESSION": STAFF, "ORCHE_RUNNER": "1"})
        text = runner.prompt(job, {})
        footer = templates.load(errand.FOOTER, UNIT=job.unit_dir)
        self.assertTrue(text.endswith("\n## Runner\n" + footer + "\n"))
        self.assertIn(f"Your unit folder is {self.units_dir() / unit}:", footer)
        self.assertNotIn("{{", footer)
        self.assertNotIn("Work only inside", footer)
        self.assertEqual(runner.stopped_text(job), "stopped by the owner")
        self.assertEqual(runner.stopped_text(job, before=True), "stopped by the owner before start")

    def test_a_run_files_its_result_to_staff_and_wakes_it(self):
        unit = self.start()
        self.reply("Three PRs touch it.\nSTATUS: done\nThree open PRs touch the payment page: #12, #14, #15.")
        out = runner.execute(registry.load(), unit, config.load())
        self.assertEqual(out.code, 0, out.stderr)
        self.assertEqual((self.fake / "session.txt").read_text().strip(), STAFF)
        self.assertEqual(os.path.realpath((self.fake / "cwd.txt").read_text().strip()), str(self.repo))
        [ptr] = self.pointers(STAFF)
        self.assertEqual((ptr["from"], ptr["to"], ptr["status"], ptr["via"], ptr["lineage"]),
                         (STAFF, STAFF, "done", "runner", unit))
        self.assertNotIn("quiet", ptr)
        self.assertTrue(inbox.wakes(ptr))
        self.assertEqual(ptr["report_path"], str(self.units_dir() / unit / "REPORT.md"))
        self.assertEqual(self.pointers(MAIN), [])
        self.assertEqual(self.state(unit), "answered")
        self.assertEqual(self.run_record(unit)["to"], STAFF)
        self.assertIn(f"run unit={unit} to={STAFF} ", self.events())

    def test_a_run_that_changed_project_files_says_so_first(self):
        unit = self.start()
        names = ["README"] + [f"new{i}.txt" for i in range(6)]
        self.action("".join(f"echo x >> {self.repo / n}\n" for n in names))
        self.reply("Done.\nSTATUS: done\nThe answer is 42.")
        out = runner.execute(registry.load(), unit, config.load())
        self.assertEqual(out.code, 0, out.stderr)
        [ptr] = self.pointers(STAFF)
        first = ptr["summary"].splitlines()[0]
        self.assertEqual(first, "CHANGED PROJECT FILES: README, new0.txt, new1.txt, new2.txt, new3.txt and 2 more")
        self.assertIn("The answer is 42.", ptr["summary"])
        self.assertIn(f"errand changed unit={unit} files=7", self.events())
        self.assertTrue((self.repo / "new5.txt").exists())                 # nothing is reverted

    def test_a_run_that_changed_nothing_has_no_changed_line(self):
        (self.repo / "dirty.txt").write_text("before the run")             # already dirty: not the errand's
        unit = self.start()
        self.action(f"echo note > {self.units_dir() / unit / 'notes.md'}\n")
        self.reply("Done.\nSTATUS: done\nThe answer is 42.")
        self.assertEqual(runner.execute(registry.load(), unit, config.load()).code, 0)
        [ptr] = self.pointers(STAFF)
        self.assertNotIn("CHANGED PROJECT FILES", ptr["summary"])
        self.assertNotIn("errand changed", self.events())

    def test_no_git_repository_no_check(self):
        unit = self.start()
        job = runner.resolve(registry.load(), unit)
        with mock.patch.object(shell, "git", return_value=shell.Result(128, "", "not a git repository", [])):
            self.assertIsNone(runner._porcelain(job.worktree))
        job.porcelain = None
        self.assertEqual(runner._changed(job), "")

    def test_a_capped_start_wakes_staff(self):
        unit = self.start()
        self.config({"claude_bin": str(self.claude), "flow": {"max_project_cost_usd": 0}})
        with mock.patch("orche.flow.owner.parked") as parked:
            out = runner.execute(registry.load(), unit, config.load())
        self.assertEqual(out.code, runner.EXIT_BLOCKED)
        parked.assert_not_called()
        [ptr] = self.pointers(STAFF)
        self.assertEqual((ptr["status"], ptr["via"]), ("info", "runner"))
        self.assertTrue(ptr["summary"].startswith("not started: "))
        self.assertNotIn("quiet", ptr)
        self.assertNotIn("retry", ptr)
        self.assertEqual(self.state(unit), "dropped")

    def test_filing_routes_a_staff_target_only_to_its_own_staff(self):
        unit = self.start()
        reg = registry.load()
        self.assertEqual(filing.recipient(reg, {"unit": unit, "project": "acme", "to": STAFF}), STAFF)
        self.assertIsNone(filing.recipient(reg, {"unit": unit, "project": "other", "to": STAFF}))
        self.assertIsNone(filing.recipient(reg, {"unit": unit, "project": "acme", "to": "staff-nope"}))
        who = {"unit": unit, "project": "acme", "to": STAFF, "worktree": str(self.repo), "branch": "-",
               "tier": "standard", "model": "claude-opus-5-5", "vendor": "claude", "effort": "medium"}
        filing.unrun(reg, who, 1, self.units_dir() / unit, "info", "runner busy", "busy", "busy")
        [ptr] = self.pointers(STAFF)
        self.assertNotIn("quiet", ptr)
        self.assertNotIn("retry", ptr)


class ShapeTest(ErrandCase):
    def test_a_pointer_to_staff_and_an_errand_item_are_never_quiet(self):
        unit = self.start()
        for _ in range(2):
            ptr = inbox.make(unit, STAFF, STAFF, "done", summary="x", lineage=unit)
            shape.shape(ptr)
            self.assertNotIn("quiet", ptr)
            inbox.write(ptr)
        for via in ("staff", "main"):
            self.assertFalse(shape._report({"from": MAIN, "to": "div-acme-build", "via": via, "unit": unit,
                                            "status": "info"}), via)
        self.assertTrue(shape._report({"from": "div-acme-build", "to": MAIN, "unit": unit, "status": "done"}))


class ForwardTest(ErrandCase):
    def test_forward_copies_to_main_and_drains(self):
        unit = self.start()
        self.result(unit, "done", "the answer")
        self.result(unit, "info", "a reply", via="main", frm=MAIN)
        n = errand.forward(registry.load(), "acme", inbox.pending(STAFF), "undelivered")
        self.assertEqual(n, 2)
        self.assertEqual(self.inbox(STAFF), [])
        [ptr] = [p for p in self.pointers(MAIN) if p["summary"].startswith(errand.FORWARD_PREFIX)]
        self.assertEqual((ptr["status"], ptr["via"], ptr["forwarded"], ptr["from"], ptr["lineage"]),
                         ("info", "staff", "undelivered", STAFF, unit))
        self.assertEqual(ptr["summary"], "Errand result for the owner: the answer")
        self.assertNotIn("quiet", ptr)
        self.result(unit, "done", "another")
        errand.forward(registry.load(), "acme", inbox.pending(STAFF), "asleep")
        self.assertIs(self.newest(MAIN)["quiet"], True)
        self.assertIn(f"errand forwarded unit={unit} project=acme reason=asleep", self.events())
        with self.assertRaises(ValueError):
            errand.forward(registry.load(), "acme", [], "lost")

    def test_mains_own_reply_goes_back_to_main_never_dropped(self):
        unit = self.start()
        reply = self.units_dir() / unit / "REPLY.md"
        reply.write_text("Which color?\nOWNER: blue or red; default blue\n")
        self.result(unit, "question", "Which color?", via="main", frm=MAIN, report_path=str(reply))
        for reason, quiet in (("undelivered", None), ("asleep", True)):
            self.assertEqual(errand.forward(registry.load(), "acme", inbox.pending(STAFF), reason), 1)
            self.assertEqual(self.inbox(STAFF), [])
            ptr = self.newest(MAIN)
            self.assertEqual({k: ptr.get(k) for k in ("unit", "from", "to", "status", "via", "forwarded", "lineage",
                                                       "report_path", "quiet")},
                             {"unit": unit, "from": STAFF, "to": MAIN, "status": "info", "via": "staff",
                              "forwarded": "undelivered", "lineage": unit, "report_path": "",
                              "quiet": quiet})
            self.assertTrue(ptr["summary"].startswith(
                f"Your reply about {unit} did not reach the owner (his Staff window could not be opened): give it "
                "to him here. Your reply: "), ptr["summary"])
            self.result(unit, "info", "again", via="main", frm=MAIN, report_path=str(reply))
        self.assertEqual(errand.turn_errands(registry.load(), "acme", [self.newest(MAIN)]), {})

    def test_forward_skips_an_item_the_staff_prompt_took(self):
        unit = self.start()
        taken = self.result(unit, "done", "the answer")
        left = self.result(unit, "done", "another answer", minutes=-1)
        items = inbox.pending(STAFF)
        inbox.drain([taken])                       # Staff's prompt drained it between the read and the claim
        self.assertEqual(errand.forward(registry.load(), "acme", items, "undelivered"), 1)
        [ptr] = self.pointers(MAIN)
        self.assertEqual(ptr["summary"], "Errand result for the owner: another answer")
        self.assertFalse(left.exists())
        self.assertEqual(len(self.inbox(STAFF, drained=True)), 2)

    def test_the_staff_prompt_skips_an_item_forward_took(self):
        from orche import staff
        unit = self.start()
        taken = self.result(unit, "done", "taken by the forward")
        self.result(unit, "done", "shown to Staff", minutes=-1)
        real = inbox.fit

        def racing(items, *a, **k):          # errand.forward claims one between Staff's read and its drain
            if taken.exists():
                errand.forward(registry.load(), "acme", [(taken, json.loads(taken.read_text()))], "undelivered")
            return real(items, *a, **k)
        with mock.patch.object(inbox, "fit", side_effect=racing):
            text = staff._items(STAFF, False, 9000)
        self.assertIn("shown to Staff", text)
        self.assertNotIn("taken by the forward", text)
        self.assertEqual(self.inbox(STAFF), [])
        [ptr] = self.pointers(MAIN)
        self.assertEqual(ptr["summary"], "Errand result for the owner: taken by the forward")

    def test_two_handovers_in_one_turn_both_get_the_close(self):
        a = errand.hand_over(registry.load(), STAFF, goal="Fix typo A")
        b = errand.hand_over(registry.load(), STAFF, goal="Fix typo B")
        first, second = sorted([a, b])
        reg = registry.load()
        errand.forward_reply(reg, MAIN, "Sent both to build.\nRUNNING: build", {"errands": {a: "handover",
                                                                                         b: "handover"}})
        self.assertEqual((self.units_dir() / second / errand.JOINED).read_text().strip(), first)
        self.assertFalse((self.units_dir() / first / errand.JOINED).exists())
        out = errand.forward_reply(reg, MAIN, "Both typos fixed.\nDONE: merged", {"errands": {first: "close"}})
        self.assertEqual(out, [first, second])
        done = [p for p in self.pointers(STAFF) if p.get("via") == "main" and p["status"] == "done"]
        self.assertEqual(sorted(p["unit"] for p in done), [first, second])
        self.assertTrue((self.units_dir() / second / "REPLY.md").read_text().startswith("Both typos fixed."))
        self.assertFalse((self.units_dir() / second / errand.JOINED).exists())
        self.assertIn(f"errand forward unit={second} project=acme kind=joined status=done", self.events())
        mine = lambda: [p for p in self.pointers(STAFF) if p["unit"] == second and p.get("via") == "main"]  # noqa: E731
        n = len(mine())
        errand.forward_reply(reg, MAIN, "x", {"errands": {first: "item"}})      # no close: nothing joins
        self.assertEqual(len(mine()), n)

    def test_forward_reply_statuses_and_reply_md(self):
        unit = self.start()
        reg = registry.load()
        cases = (("close", "Done: PR #3 merged.\nDONE: merged", "done"),
                 ("handover", "Which color?\nOWNER: pick blue or red; default blue", "question"),
                 ("item", "Build is on it.\nRUNNING: build changes the copy", "info"))
        for kind, text, status in cases:
            first, last = text.split("\n")
            out = errand.forward_reply(reg, MAIN, f"{first}\nkey sk-{'a' * 30}\n{last}", {"errands": {unit: kind}})
            self.assertEqual(out, [unit])
            ptr = self.newest(STAFF)
            self.assertEqual((ptr["status"], ptr["via"], ptr["from"], ptr["to"], ptr["lineage"]),
                             (status, "main", MAIN, STAFF, unit))
            reply = self.units_dir() / unit / "REPLY.md"
            self.assertEqual(ptr["report_path"], str(reply))
            self.assertTrue(reply.read_text().startswith(text.splitlines()[0]))
            self.assertNotIn("sk-aaaa", reply.read_text() + ptr["summary"])
            self.assertIn(f"errand forward unit={unit} project=acme kind={kind} status={status}", self.events())
        self.assertEqual(errand.forward_reply(reg, MAIN, "x", {"errands": {}}), [])
        self.assertEqual(errand.forward_reply(reg, MAIN, "x", None), [])
        self.assertEqual(errand.forward_reply(reg, "div-acme-build", "x", {"errands": {unit: "item"}}), [])

    def test_forward_reply_skips_an_asleep_staff(self):
        unit = self.start()
        registry.update(lambda r: r["projects"]["acme"].__setitem__("staff", {"status": "asleep"}))
        n = len(self.pointers(STAFF))
        self.assertEqual(errand.forward_reply(registry.load(), MAIN, "x", {"errands": {unit: "item"}}), [])
        self.assertEqual(len(self.pointers(STAFF)), n)

    def test_turn_errands(self):
        unit = self.start()
        reg = registry.load()
        other = brief.write(brief.Block("brief", {"TO": "div-acme-build", "GOAL": "Other work", "SCOPE": "x",
                                                  "CONTEXT": "x", "ACCEPT": "x", "VERIFY": "x", "TIMEBOX": "10m",
                                                  "FORBID": "x", "REPORT": "x", "TIER": "standard"}),
                            roster.Roster(reg).member("div-acme-build"), MAIN, "", timeout=0).unit
        item = {"unit": unit, "lineage": unit, "status": "done", "from": "div-acme-build"}
        hand = {"unit": unit, "lineage": unit, "status": "external-request", "via": "staff"}
        close = {"unit": unit, "lineage": unit, "status": "done", "from": "orche", "lineage_closed": True}
        self.assertEqual(errand.turn_errands(reg, "acme", [item]), {unit: "item"})
        self.assertEqual(errand.turn_errands(reg, "acme", [(Path("x"), item), (Path("y"), hand)]), {unit: "handover"})
        self.assertEqual(errand.turn_errands(reg, "acme", [close, hand, item]), {unit: "close"})
        self.assertEqual(errand.turn_errands(reg, "acme", [{"unit": other, "lineage": other, "status": "done"},
                                                           {"unit": "adhoc-staff-acme-20261112", "status": "info"},
                                                           {**item, "forwarded": "asleep"}]), {})
        self.assertTrue(errand.is_errand_lineage(reg, "acme", unit))
        self.assertFalse(errand.is_errand_lineage(reg, "acme", other))


class SweepTest(ErrandCase):
    def test_renudge_forward_and_lost(self):
        unit = self.start()
        reg = registry.load()
        self.result(unit, "done", "fresh", minutes=2)
        with mock.patch.object(inbox, "wake") as wake:
            self.assertEqual(errand.sweep(reg), [])
            wake.assert_not_called()
            self.result(unit, "done", "older", minutes=15)
            self.assertEqual(errand.sweep(reg), [f"renudge {STAFF}"])
            wake.assert_called_once_with(STAFF)
            (inbox.box(STAFF) / inbox.NUDGED).touch()        # a nudge in flight: no second one
            self.assertEqual(errand.sweep(reg), [])
            self.result(unit, "done", "oldest", minutes=35)
            self.assertEqual(errand.sweep(reg), [f"forward {STAFF} 1"])
        [ptr] = self.pointers(MAIN)
        self.assertEqual((ptr["forwarded"], ptr["summary"]), ("undelivered", "Errand result for the owner: oldest"))
        self.assertEqual(len(self.inbox(STAFF)), 2)

    def test_a_lost_errand_is_filed_failed_once(self):
        unit = self.start()
        old = time.time() - 61 * 60
        for name in ("BRIEF.rev1.md", "BRIEF.md"):
            os.utime(self.units_dir() / unit / name, (old, old))
        self.assertEqual(errand.sweep(registry.load()), [f"lost {unit}"])
        [ptr] = self.pointers(STAFF)
        self.assertEqual((ptr["status"], ptr["summary"], ptr["from"], ptr["to"]),
                         ("failed", "errand lost: no result 60 minutes after it started", STAFF, STAFF))
        self.assertIn(f"errand lost unit={unit} project=acme", self.events())
        self.assertEqual(self.state(unit), "failed")
        self.assertEqual(errand.sweep(registry.load()), [])

    def test_sweep_never_raises(self):
        with mock.patch.object(inbox, "pending", side_effect=RuntimeError("boom")):
            self.assertEqual(errand.sweep(registry.load()), [])
