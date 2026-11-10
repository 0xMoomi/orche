"""Running work made visible (runs/progress.py): the runner reads the worker's stream line by line, keeps one
progress record per running unit, `orche status` and the status line show it, and the heartbeat notices a
headless worker that went silent. Fake streams only: no test starts a real claude.

The fixture tests/fixtures/stream-haiku-2.1.322.ndjson is a real `stream-json --verbose` capture of a
five-tool-call Haiku run (claude 2.1.322, 2026-11-10) with every prose and path replaced by placeholders.
"""
import datetime as _dt
import json
import os
import time
from pathlib import Path
from unittest import mock

import support
from orche import headless, reconcile, registry, runs, statusline, units, util
from orche.runs import progress
from test_runner import RunnerCase

FIXTURE = Path(__file__).parent / "fixtures" / "stream-haiku-2.1.322.ndjson"


def stream_lines():
    return FIXTURE.read_text().splitlines(keepends=True)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def tool_use(name, inp, tid="t1", mid="m1"):
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": "claude-haiku-5-5",
                                                        "usage": {"input_tokens": 1000, "output_tokens": 100},
                                                        "content": [{"type": "tool_use", "id": tid, "name": name,
                                                                     "input": inp}]}}) + "\n"


def tool_result(tid="t1"):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                                "content": "x"}]}}) + "\n"


class RecorderTest(support.TempHomeCase):
    def recorder(self, clock=None, **kw):
        self.path = self.home / "PROGRESS.json"
        return progress.Recorder(self.path, "u@1", "2026-11-10T10:00:00+00:00", 1800, "claude-haiku-5-5",
                                 clock=clock or Clock(), **kw)

    def test_a_normal_stream_ends_with_the_result_events_turns_and_cost(self):
        clock = Clock()
        rec = self.recorder(clock)
        for line in stream_lines():
            clock.t += 10           # every event past the throttle: each is written
            rec.feed(line)
        data = progress.read(self.path)
        self.assertEqual((data["turns"], data["usd"], data["tool"], data["target"], data["tool_open_since"]),
                         (8, 0.0046, "Bash", "git", None))
        self.assertGreater(data["tokens"], 100000)
        self.assertIsNone(data["ended"])
        rec.close()
        self.assertIsNotNone(progress.read(self.path)["ended"])
        # what the runner files comes from the same stream's final result event
        res = headless._parse_output("".join(stream_lines()))
        self.assertEqual((res["type"], res["num_turns"], res["total_cost_usd"], res["session_id"], res["is_error"]),
                         ("result", 8, 0.00456563, "sid-fixture", False))

    def test_the_record_holds_facts_never_conversation_text(self):
        rec = self.recorder()
        for line in stream_lines():
            rec.feed(line)
        rec.close()
        text = self.path.read_text()
        for word in ("<text>", "<thinking>", "<output>", "<report text>", "<args>", "/repo/", "STATUS"):
            self.assertNotIn(word, text)
        self.assertEqual(set(json.loads(text)), {"run_id", "started", "timebox_s", "last_event", "turns", "tool",
                                                 "target", "tool_open_since", "tokens", "usd", "ended"})

    def test_unknown_and_broken_lines_never_break_a_run(self):
        clock = Clock()
        rec = self.recorder(clock)
        for line in ("not json at all\n", "{broken\n", "[1, 2]\n", "\n", "\x00\xff garbage\n",
                     json.dumps({"type": "mystery", "message": "x"}) + "\n",
                     json.dumps({"type": "assistant", "message": "not a dict"}) + "\n",
                     json.dumps({"type": "user", "message": {"content": "plain text"}}) + "\n",
                     json.dumps({"type": "assistant", "message": {"id": 5, "content": 7, "usage": {"input_tokens": "x"}}}) + "\n",
                     json.dumps({"type": "assistant", "message": {"content": [None, 3, {"type": "tool_use"}]}}) + "\n",
                     json.dumps({"type": "result", "num_turns": "x", "total_cost_usd": True}) + "\n"):
            clock.t += 10
            rec.feed(line)
        data = progress.read(self.path)
        self.assertIsNotNone(data["last_event"])      # the JSON events counted as events
        self.assertEqual((data["turns"], data["tool"], data["usd"], data["tool_open_since"]), (2, "?", None, None))

    def test_a_tool_call_stays_open_until_its_result(self):
        clock = Clock()
        rec = self.recorder(clock)
        clock.t += 10
        rec.feed(tool_use("Bash", {"command": "python3 -m unittest discover -s tests"}))
        data = progress.read(self.path)
        self.assertEqual((data["tool"], data["target"]), ("Bash", "python3"))
        self.assertIsNotNone(data["tool_open_since"])
        clock.t += 10
        rec.feed(tool_result())
        self.assertIsNone(progress.read(self.path)["tool_open_since"])

    def test_targets_are_base_names_or_first_words_and_masked(self):
        self.assertEqual(progress.target_of("Edit", {"file_path": "/a/b/ladder.py"}), "ladder.py")
        self.assertEqual(progress.target_of("Bash", {"command": "/usr/bin/git commit -m x"}), "git")
        self.assertEqual(progress.target_of("Bash", {"command": "sk-" + "a" * 30 + " run"}), "[REDACTED]")
        self.assertEqual(progress.target_of("Grep", {"pattern": "secret"}), "")
        self.assertEqual(progress.target_of("Read", None), "")

    def test_a_shell_target_is_the_program_never_an_assignment(self):
        """Environment assignments, wrappers and their options are skipped: no `NAME=value` (a password, a token)
        reaches PROGRESS.json, `orche status`, the status card or the daily brief."""
        cases = [("PGPASSWORD=hunter2xyz psql -h db", "psql"),
                 ("PGPASSWORD='hunter2 xyz' TOKEN=\"a b\" /usr/bin/psql -c 'select 1'", "psql"),
                 ("env VAR=x cmd --flag", "cmd"),
                 ("sudo -E env -i KEY=secret nohup time python3 job.py", "python3"),
                 ("command git status", "git"),
                 ("A=1 B=2", ""),
                 ("FOO='unbalanced", ""),
                 ("env", ""),
                 ("--password=hunter2 run", "")]
        for command, target in cases:
            with self.subTest(command=command):
                got = progress.target_of("Bash", {"command": command})
                self.assertEqual(got, target)
                self.assertNotIn("=", got)
                self.assertNotIn("hunter2", got)
        self.assertEqual(progress.target_of("WebFetch", {"url": "https://u:p@x.io/?token=abc"}), "")

    def test_writes_are_throttled_and_the_last_event_still_lands(self):
        clock = Clock()
        with mock.patch.object(progress.util, "write_json", wraps=util.write_json) as write:
            rec = self.recorder(clock)
            self.assertEqual(write.call_count, 1)          # the start
            for _ in range(5):                             # inside 5 s: none written now
                clock.t += 0.5
                rec.feed(tool_use("Read", {"file_path": "/x/a.py"}))
            self.assertEqual(write.call_count, 1)
            clock.t += 5
            rec.feed(tool_result())                        # 5 s after the last write: written
            self.assertEqual(write.call_count, 2)
            rec.close()                                    # once at the end
            self.assertEqual(write.call_count, 3)
        with mock.patch.object(progress, "WRITE_EVERY_S", 0.2):
            rec = self.recorder()                          # a real clock: the timer writes the last event
            rec.feed(tool_use("Edit", {"file_path": "/x/b.py"}, mid="m9"))
            self.assertIsNone(progress.read(self.path)["tool"])
            time.sleep(0.5)
            self.assertEqual(progress.read(self.path)["tool"], "Edit")
            rec.close()

    def test_a_later_attempt_adds_to_the_run(self):
        clock = Clock()
        rec = self.recorder(clock)
        for line in stream_lines():
            clock.t += 10
            rec.feed(line)
        rec.attempt("claude-haiku-5-5", prior=0.00456563)   # a finishing continuation of that session
        clock.t += 10
        rec.feed(tool_use("Bash", {"command": "git log"}, mid="m-new"))
        clock.t += 10
        rec.feed(json.dumps({"type": "result", "num_turns": 10, "total_cost_usd": 0.01}) + "\n")
        data = progress.read(self.path)
        self.assertEqual((data["turns"], data["usd"]), (9, 0.01))

    def test_an_unpriced_model_leaves_dollars_empty_until_the_result(self):
        clock = Clock()
        rec = progress.Recorder(self.home / "P.json", "u@1", "s", 60, "m-x", clock=clock)
        clock.t += 10
        rec.feed(json.dumps({"type": "assistant", "message": {"id": "a", "model": "m-x",
                                                              "usage": {"input_tokens": 5}, "content": []}}) + "\n")
        self.assertEqual((progress.read(self.home / "P.json")["usd"], progress.read(self.home / "P.json")["tokens"]),
                         (None, 5))


class StreamReadTest(support.TempHomeCase):
    def test_spawn_hands_each_line_over_as_it_arrives_and_keeps_stdout_whole(self):
        script = self.home / "s.sh"
        script.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"system\"}'\nsleep 1\n"
                          "echo '{\"type\":\"result\",\"num_turns\":1}'\necho err >&2\n")
        script.chmod(0o755)
        seen = []
        code, out, err, error, killed = headless.spawn([str(script)], dict(os.environ), self.home, "x" * 200000,
                                                       timeout=20, on_line=lambda ln: seen.append((time.monotonic(), ln)))
        self.assertEqual((code, error, killed, err), (0, None, False, "err\n"))
        self.assertEqual([ln for _, ln in seen], out.splitlines(keepends=True))
        self.assertGreater(seen[1][0] - seen[0][0], 0.5)       # the first line came before the sleep ended

    def test_a_failing_reader_and_a_timeout_keep_the_run_whole(self):
        script = self.home / "s.sh"
        script.write_text("#!/bin/sh\necho '{\"type\":\"assistant\"}'\nsleep 30\n")
        script.chmod(0o755)

        def boom(line):
            raise RuntimeError("reader broke")
        code, out, err, error, killed = headless.spawn([str(script)], dict(os.environ), self.home, "",
                                                       timeout=1, on_line=boom)
        self.assertEqual(error, "timeout after 1s")
        self.assertEqual(out, '{"type":"assistant"}\n')
        self.assertEqual(headless._parse_output(out), {})     # a stream cut short is no result

    def test_project_calls_stream_and_the_others_do_not(self):
        project = headless.Call("project", "claude-haiku-5-5", 5, 60, cwd=self.home)
        sealed = headless.Call("sealed", "claude-haiku-5-5", 5, 60, cwd=self.home, allowed="Read")
        self.assertEqual(headless.argv(project, {})[4:8], ["--output-format", "stream-json", "--verbose", "--max-turns"])
        self.assertEqual(headless.argv(sealed, {})[4:7], ["--output-format", "json", "--max-turns"])


class LiveRunCase(RunnerCase):
    """A live run of a unit: this process holds its lock (pid and child_pid are alive) and a progress record."""

    def live(self, unit, *, quiet_min=0.0, elapsed_min=12.0, tool_open_min=None, turns=14, tool="Edit",
             target="ladder.py", usd=1.2, ended=None, timebox_s=1800):
        now = units.now()
        ago = lambda m: (now - _dt.timedelta(minutes=m)).isoformat(timespec="seconds")
        run_id = f"{unit}@1"
        folder = self.units_dir() / unit
        lock = {"pid": os.getpid(), "child_pid": os.getpid(), "unit": unit, "to": "div-acme-build", "rev": 1,
                "started": ago(elapsed_min), "run_id": run_id, "timeout_s": timebox_s + 60,
                "progress": str(folder / progress.FILE)}
        util.write_json(runs.run_dir("acme") / f"{unit}.lock", lock)
        util.write_json(folder / progress.FILE, {
            "run_id": run_id, "started": ago(elapsed_min), "timebox_s": timebox_s, "last_event": ago(quiet_min),
            "turns": turns, "tool": tool, "target": target,
            "tool_open_since": ago(tool_open_min) if tool_open_min is not None else None,
            "tokens": 1000, "usd": usd, "ended": ended})
        self.addCleanup(lambda: (runs.run_dir("acme") / f"{unit}.lock").unlink(missing_ok=True))
        return lock


class StatusTest(LiveRunCase):
    def test_the_status_line_and_formats(self):
        unit = self.brief()
        self.live(unit, elapsed_min=22.5)
        view = progress.views("acme")[0]
        self.assertEqual(progress.line(view), f"{unit}  22m of 30m · turn 14 · Edit ladder.py · $1.20")
        reg = registry.load()
        self.assertIn("▶ running 1 (oldest 22m/30m)", statusline.plain(statusline.segment(reg, "acme")))
        self.assertNotIn("stalled", statusline.plain(statusline.segment(reg, "acme")))
        self.live(unit, elapsed_min=25, quiet_min=11)     # no event for 11 min, no tool call open
        self.assertIn("▶ running 1 (oldest 25m/30m) · ⚠ stalled 1", statusline.plain(statusline.segment(reg, "acme")))
        (self.home / "status").mkdir(exist_ok=True)
        (self.home / "status" / "lang").write_text("ko\n")
        ko = statusline.labels("ko")
        self.assertIn(f"▶ {ko['running_oldest'].format(n=1, age='25m', box='30m')} · ⚠ {ko['stalled'].format(n=1)}",
                      statusline.plain(statusline.segment(reg, "acme")))
        self.assertIn(f"stalled: no event for 11m", progress.line(progress.views("acme")[0]))

    def test_a_long_tool_call_is_not_stalled(self):
        unit = self.brief()
        self.live(unit, elapsed_min=20, quiet_min=14, tool="Bash", target="python3", tool_open_min=14)
        v = progress.views("acme")[0]
        self.assertFalse(v["stalled"])
        self.assertEqual(progress.line(v), f"{unit}  20m of 30m · turn 14 · in Bash python3 14m · $1.20")

    def test_the_card_and_the_status_lines_list_each_running_unit(self):
        unit = self.brief()
        self.live(unit, elapsed_min=12)
        self.enterContext(mock.patch.object(runs, "reconcile_cli", return_value=[]))   # not an `orche run` process
        code, out, err = self.orche("status", "acme")
        self.assertEqual(code, 0, err)
        self.assertIn(f"  ▶ {unit}  12m of 30m · turn 14 · Edit ladder.py · $1.20", out)
        code, out, err = self.orche("status")
        self.assertIn(f"  running: {unit}  12m of 30m · turn 14 · Edit ladder.py · $1.20", out)

    def test_an_ended_or_foreign_record_is_never_shown_as_live(self):
        unit = self.brief()
        self.live(unit, ended="2026-11-10T10:00:00+00:00", quiet_min=30)
        v = progress.views("acme")[0]
        self.assertEqual((v["live_record"], v["turns"], v["stalled"]), (False, None, False))
        self.assertEqual(progress.line(v), f"{unit}  12m of 30m")
        lock = self.live(unit, quiet_min=30)
        util.write_json(runs.run_dir("acme") / f"{unit}.lock", {**lock, "run_id": f"{unit}@2"})   # another run
        self.assertFalse(progress.views("acme")[0]["live_record"])
        (runs.run_dir("acme") / f"{unit}.lock").unlink()
        self.assertEqual(progress.views("acme"), [])


class SilentRunTest(LiveRunCase):
    def conds(self):
        return [c for c in reconcile.observe(registry.load(), units.now(), terms=None) if c.cls == "silent"]

    def test_a_quiet_run_is_noticed_and_its_sender_hears_once(self):
        unit = self.brief(sender="main-acme")
        self.live(unit, quiet_min=5)
        self.assertEqual(self.conds(), [])                       # quiet 5 min: nothing
        self.live(unit, quiet_min=12)
        c, = self.conds()                                        # stalled: seen, the note not due yet
        self.assertEqual((c.target, c.ready, c.info["headless"]), (unit, False, True))
        actions, state = reconcile.plan([c], {}, units.now())
        self.assertEqual(actions, [])
        self.live(unit, quiet_min=21, tool="Bash", target="pytest", tool_open_min=21)   # no event at all
        c, = self.conds()
        self.assertTrue(c.ready)
        actions, state = reconcile.plan([c], state, units.now())
        self.assertEqual([(a.kind, a.cond.target) for a in actions], [("escalate", unit)])
        with mock.patch.object(reconcile, "_post") as post:
            reconcile.apply(registry.load(), actions[0], units.now())
        (_unit, to, summary), kw = post.call_args
        self.assertEqual((to, kw["rev"]), ("main-acme", 1))
        self.assertIn(f"silent: {unit} rev 1 in div-acme-build: its headless run is alive but printed no event "
                      "for 21 min", summary)
        self.assertNotRegex(summary, "[–—]")
        for a in actions:
            reconcile._record(state, a, units.now())
        again, state = reconcile.plan(self.conds(), state, units.now())
        self.assertEqual(again, [])                               # once per run
        util.write_json(reconcile.state_path(), state)
        self.assertEqual([(s["cls"], s["escalated"]) for s in reconcile.stalled("acme")], [("silent", True)])

    def test_a_dead_worker_or_a_finished_run_is_not_silent(self):
        unit = self.brief()
        lock = self.live(unit, quiet_min=30)
        util.write_json(runs.run_dir("acme") / f"{unit}.lock", {**lock, "child_pid": 999999})
        self.assertEqual(self.conds(), [])
        self.live(unit, quiet_min=30, ended="2026-11-10T10:00:00+00:00")
        self.assertEqual(self.conds(), [])

    def test_the_episode_ends_with_its_run(self):
        unit = self.brief()
        self.live(unit, quiet_min=25)
        c, = self.conds()
        actions, state = reconcile.plan([c], {}, units.now())
        for a in actions:
            reconcile._record(state, a, units.now())
        self.assertEqual(reconcile.resolved(registry.load(), state), set())
        (runs.run_dir("acme") / f"{unit}.lock").unlink()
        self.assertEqual(reconcile.resolved(registry.load(), state), {c.key})


class RunnerProgressTest(RunnerCase):
    def test_a_streamed_run_files_from_its_result_event_and_ends_its_record(self):
        unit = self.brief()
        self.commit_action()
        (self.fake / "reply.json").write_text("noise before the stream\n" + FIXTURE.read_text())
        code, out, err = self.orche("run", unit)
        self.assertEqual((code, err), (0, ""), out)
        rec = self.run_record(unit)
        self.assertEqual((rec["cost_usd"], rec["num_turns"], rec["status"], rec["stop_reason"]),
                         (0.00456563, 8, "done", "end_turn"))
        data = progress.read(self.units_dir() / unit / progress.FILE)
        self.assertEqual((data["turns"], data["usd"], data["run_id"].split("@")[0]), (8, 0.0046, unit))
        self.assertIsNotNone(data["ended"])
        self.assertEqual(progress.views("acme"), [])
        argv = (self.fake / "argv.txt").read_text().split("\n")
        self.assertEqual(argv[argv.index("--output-format") + 1:argv.index("--output-format") + 3],
                         ["stream-json", "--verbose"])
