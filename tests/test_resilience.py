"""Resilience wave: the env sweep (R-C), reconcile of orphaned runs (R-A), log pruning, disk
health and a failed filing (R-D). Real processes against the fake `claude` (test_runner.RunnerCase)."""
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

import support
from orche import headless, inbox, procs, runs, shell
from orche.runs import filing
from orche.runs import job as runner
from test_runner import REAL_POPEN, RunnerCase, alive

LIB = str(support.ROOT / "lib")
REAL_SHELL = shell.RUNNER
SLEEP = ("-c", "import time; time.sleep(60)")
# Linux: a process whose environment the kernel hides from its own user (PR_SET_DUMPABLE = 4).
HIDE_ENV = "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(60)"


def rid(unit="acme-x-20261018-1"):
    """A run id no other test (or a second checkout running this suite) can share (M-7)."""
    return f"{unit}@{uuid.uuid4().int % 10 ** 18}"


def ps_fails(argv, *, timeout, input, env=None):
    """shell.RUNNER whose `ps` always fails (exit 1); everything else runs for real."""
    if argv and argv[0] == "ps":
        return shell.Result(1, "", "ps: unavailable", list(argv))
    return REAL_SHELL(argv, timeout=timeout, input=input)


def untagged_env(**extra):
    return {**{k: v for k, v in os.environ.items() if k != procs.ENV}, **extra}
# A grandchild that leaves claude's process group and session (as `setsid` would) and records its pid.
DETACHED = (f"{shlex.quote(sys.executable)} -c 'import os, sys, time; os.setsid(); "
            "open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)' "
            "\"$(dirname \"$0\")/grandchild\" >/dev/null 2>&1 &\n"
            "while [ ! -s \"$(dirname \"$0\")/grandchild\" ]; do sleep 0.05; done\n")


class SweepCase(RunnerCase):
    def grandchild(self, timeout=10.0):
        """The detached grandchild's pid once it has written it (killed at cleanup if still alive)."""
        path = self.fake / "grandchild"
        self.assertTrue(self.wait_for(lambda: path.exists() and path.read_text().strip(), timeout=timeout))
        pid = int(path.read_text().strip())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        return pid

    def gone(self, pid, timeout=5.0):
        return self.wait_for(lambda: not alive(pid), timeout=timeout)

    def tagged(self, run, argv=SLEEP):
        """A live process (this test's child) whose environment carries ORCHE_RUN_ID=<run>."""
        return self.child([sys.executable, *argv], env=untagged_env(**{procs.ENV: run}))

    def child(self, argv, env=None, **kw):
        """A live process of this test (killed and reaped at cleanup)."""
        proc = REAL_POPEN(argv, env=untagged_env() if env is None else env, **kw)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        return proc

    def execd(self, proc, needle="time.sleep(60)", timeout=10.0):
        """Wait until `ps` shows `proc` exec'd (its command line holds `needle`): right after the
        fork it still shows this test runner's command line and environment."""
        self.assertTrue(self.wait_for(lambda: needle in subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True,
            errors="replace").stdout, timeout=timeout))
        return proc


class EnvSweepTest(SweepCase):
    """R-C: every kill path ends with a sweep of the run's ORCHE_RUN_ID."""

    def test_the_worker_carries_the_locks_run_id_and_never_a_parents(self):
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        seen = []
        real = runs.update

        def update(path, **fields):
            seen.append(runs.run_id_of(path))
            real(path, **fields)
        with mock.patch.dict(os.environ, {procs.ENV: rid("outer")}), mock.patch.object(runs, "update", update):
            self.assertEqual(self.orche("run", unit)[0], 0)
            self.assertNotIn(procs.ENV, os.environ)       # M-3: dropped in-process (no re-exec here)
        env = dict(line.split("=", 1) for line in (self.fake / "env.txt").read_text().splitlines() if "=" in line)
        self.assertRegex(env[procs.ENV], rf"^{unit}@\d+$")
        self.assertEqual(seen, [env[procs.ENV]])

    def test_a_stopped_run_kills_a_grandchild_that_left_claudes_group(self):
        unit = self.brief()
        self.action(DETACHED)
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        pid = self.grandchild()
        self.assertNotEqual(os.getpgid(pid), self.lock(unit)["pgid"])      # outside the group kill
        self.assertTrue(runs.stop("acme", unit))
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        self.assertTrue(self.gone(pid))
        self.assertIn(f"run unit={unit} runner-swept pids=", self.events())

    def test_a_timed_out_run_and_a_finished_run_leave_no_detached_grandchild(self):
        unit = self.brief()
        self.action(DETACHED)
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        # 3 s: the timeout must come after the action wrote the grandchild's pid (a python start).
        with mock.patch.object(runner, "timebox_seconds", return_value=0), mock.patch.object(runner, "GRACE_S", 3):
            self.assertEqual(self.orche("run", unit)[0], 1)
        self.assertEqual(self.run_record(unit)["error"], "timeout after 3s")
        self.assertTrue(self.gone(self.grandchild(timeout=1)))
        (self.fake / "grandchild").unlink()
        (self.fake / "sleep").unlink()                    # claude answers at once; the grandchild stays behind
        self.reply("STATUS: blocked\nx")
        self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertTrue(self.gone(self.grandchild(timeout=1)))

    def test_the_sweep_matches_the_exact_tag_and_never_kills_itself_or_its_parent(self):
        """A tagged parent A starts a tagged child C and a tagged sweeper B: B kills C only. A tag
        that merely starts with the run id, an untagged process and this test runner live on."""
        run = rid()
        sweeper = self.root / "b.py"
        sweeper.write_text(f"import json, sys\nsys.path.insert(0, {LIB!r})\nfrom orche import procs\n"
                           "print(json.dumps(procs.sweep(sys.argv[1], wait_s=0.5)))\n")
        script = self.root / "a.py"
        script.write_text(
            "import json, os, subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(0.3)\n"
            f"b = subprocess.run([sys.executable, {str(sweeper)!r}, os.environ['ORCHE_RUN_ID']], "
            "capture_output=True, text=True)\n"
            "c.wait(timeout=10)\n"
            "print(json.dumps({'b': b.stdout, 'err': b.stderr, 'c': c.pid, 'c_code': c.returncode}))\n")
        longer, plain = self.tagged(run + "0"), self.child([sys.executable, *SLEEP])
        res = subprocess.run([sys.executable, str(script)], env=untagged_env(**{procs.ENV: run}),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)          # A (the sweeper's parent) survived
        out = json.loads(res.stdout)
        self.assertEqual(json.loads(out["b"]), [out["c"]], out["err"])
        self.assertEqual(out["c_code"], -signal.SIGTERM)
        self.assertIsNone(longer.poll())
        self.assertIsNone(plain.poll())

    def test_no_table_no_kill(self):
        run = rid()
        with mock.patch.object(procs, "_linux", return_value=False):
            with mock.patch.object(shell, "RUNNER", lambda argv, *, timeout, input: shell.Result(
                    124, "", "timeout", list(argv))):
                self.assertIsNone(procs.sweep(run))
            # either table missing is no table: never an "all clear" that drops a pending sweep
            for broken in (procs.ARGS_PS, procs.ENV_PS):
                with mock.patch.object(shell, "RUNNER", lambda argv, *, timeout, input, b=broken: (
                        shell.Result(1, "", "x", list(argv)) if list(argv) == b else REAL_SHELL(argv, timeout=timeout, input=input))):
                    self.assertIsNone(procs.find(run))
        self.assertIsNone(procs.find(run, timeout=0))
        self.assertIsNone(procs.find("", timeout=1))

    def test_the_tag_counts_only_in_the_environment(self):
        """I-1: a process (and its child) whose ARGUMENTS hold the tag — `grep ORCHE_RUN_ID=<id>` —
        survives the sweep; the one whose environment carries it dies."""
        run = rid()
        grep = self.execd(self.child([sys.executable, "-c",
                                      "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
                                      "'import time; time.sleep(60)']); time.sleep(60)", f"{procs.ENV}={run}"]),
                          needle=f"{procs.ENV}={run}")
        worker = self.execd(self.tagged(run))
        self.assertEqual(procs.sweep(run, wait_s=0.5), [worker.pid])
        self.assertEqual(worker.wait(timeout=5), -signal.SIGTERM)
        self.assertIsNone(grep.poll())

    def test_the_two_tables_and_the_environment_suffix(self):
        """macOS: a pid's environment is its `ps -E` line after the exact arguments-only prefix; the
        tag must stand there as a whole token. A prefix that does not match, a pid listed twice
        (a line break inside a value), or a pid missing from the -E table is no environment match."""
        run = rid("u")
        tag = f"{procs.ENV}={run}"
        args = {201: "/bin/grep " + tag, 202: "python w.py", 203: "python w.py", 204: "python w.py",
                205: "python w.py", 206: "python w.py", 207: "python w.py"}
        env = {201: f"/bin/grep {tag} HOME=/h", 202: f"python w.py HOME=/h {tag}", 203: f"python other.py {tag}",
               205: f"python w.py X={tag}", 206: f"python w.py {tag}0", 207: f"python w.py {tag}"}
        args_out = "".join(f"{pid} 1 {pid} 01:02 {cmd}\n" for pid, cmd in args.items())
        env_out = "".join(f"{pid} {line}\n" for pid, line in env.items()) + f"207 {tag}\n"

        def fake(argv, *, timeout, input, env=None):
            return shell.Result(0, args_out if list(argv) == procs.ARGS_PS else env_out, "", list(argv))
        with mock.patch.object(shell, "RUNNER", fake), mock.patch.object(procs, "_linux", return_value=False):
            table = {p.pid: p for p in procs.table()}
            self.assertEqual(procs.find(run), {202})
        self.assertEqual((table[201].env, table[202].env), ("HOME=/h", f"HOME=/h {tag}"))
        self.assertEqual([table[p].env for p in (203, 204, 207)], [None, None, None])
        self.assertAlmostEqual(table[202].start, time.time() - 62, delta=2)
        self.assertEqual((procs._etime("1-02:03:04"), procs._etime("03:04"), procs._etime("x")),
                         (93784, 184, None))

    def test_a_start_from_etime_is_never_early(self):
        """macOS `ps` etime is its own whole second minus the start's whole second: a process
        started at 100.9 and read at 101.1 shows `00:01`. Its start reads 101 (never before 100.9;
        `now - etime` gave 100.1, so a hidden claude started just after its lock's `started_ns`
        failed `started at or after`), and is at most 1 s late."""
        def fake(argv, *, timeout, input, env=None):
            out = "301 1 301 00:01 claude --mcp-config m.json\n" if list(argv) == procs.ARGS_PS else "301 claude --mcp-config m.json\n"
            return shell.Result(0, out, "", list(argv))
        with mock.patch.object(shell, "RUNNER", fake), mock.patch.object(procs, "_linux", return_value=False), \
                mock.patch.object(procs.time, "time", return_value=101.1):
            [proc] = procs.table()
        self.assertEqual(proc.start, 101)
        claude = {"pid": 301, "pgid": 301, "marker": "--mcp-config m.json", "started_ns": 100_850_000_000}
        self.assertTrue(procs._is_claude(proc, claude))
        self.assertFalse(procs._is_claude(proc, {**claude, "started_ns": 101_000_000_001}))
        for start in (100.0, 100.001, 100.5, 100.999):
            for read in (start, start + 0.001, 100.999, 101.0, 101.001, 101.5, 101.999, 102.0, 3700.25):
                if read < start:
                    continue
                etime = int(read) - int(start)                       # what macOS `ps` prints
                for now in (read, read + 0.3, read + 1.7):           # `now` is taken after `ps` returned
                    got = procs._etime_start(now, etime)
                    self.assertGreaterEqual(got, start, (start, read, now))
                    self.assertLessEqual(got, start + 1 + (int(now) - int(read)), (start, read, now))

    def test_a_table_with_bytes_that_are_not_utf8_still_sweeps(self):
        """C-1: non-UTF-8 bytes in another process's arguments and environment never stop a sweep."""
        run = rid()
        env = {k.encode(): v.encode() for k, v in untagged_env().items()}
        self.execd(self.child([sys.executable.encode(), b"-c", b"import time; time.sleep(60)", b"caf\xe9-\xff"],
                              env={**env, b"PROBE_LEGACY": b"\xff\xfe"}))
        worker = self.execd(self.tagged(run))
        self.assertEqual(procs.find(run), {worker.pid})
        self.assertEqual(procs.sweep(run, wait_s=0.5), [worker.pid])

    def test_a_nested_run_carries_only_its_own_tag(self):
        """M-3: `orche run` started inside another run's worker (it inherits that run's tag)
        re-execs without it, so the outer run's sweep never matches it or its claude."""
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: blocked\nx")
        outer = rid("outer")
        proc = self.spawn(unit, env=untagged_env(**{procs.ENV: outer}))
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        self.assertEqual(procs.find(outer), set())
        env = dict(line.split("=", 1) for line in (self.fake / "env.txt").read_text().splitlines() if "=" in line)
        self.assertEqual(env[procs.ENV], self.lock(unit)["run_id"])
        self.assertTrue(runs.stop("acme", unit))
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())

    def test_linux_proc_table(self):
        """The /proc walk (Linux): exact environ entry, ppid/pgrp/starttime from stat even with
        `)` in comm, the environment of another user's process unreadable (None)."""
        root = Path(tempfile.mkdtemp(dir=self.root))
        (root / "uptime").write_text("5000.25 1234.00\n")
        rows = {101: (1, 101, b"A=1\0ORCHE_RUN_ID=u@1\0"), 102: (101, 101, b"B=2\0"),
                103: (1, 103, b"ORCHE_RUN_ID=u@10\0"), 104: (1, 104, b""), 105: (1, 105, None)}
        for pid, (ppid, pgid, env) in rows.items():
            (root / str(pid)).mkdir()
            tail = " ".join(["0"] * 16 + [str(pid * 100)] + ["0"] * 5)
            (root / str(pid) / "stat").write_text(f"{pid} (we) ird) S {ppid} {pgid} {tail}\n")
            (root / str(pid) / "cmdline").write_bytes(b"sleep\x0060\x00")
            if env is not None:
                (root / str(pid) / "environ").write_bytes(env)
        (root / "self").mkdir()
        table = sorted(procs._table_proc(time.monotonic() + 2.0, root=root), key=lambda p: p.pid)
        tick = os.sysconf("SC_CLK_TCK")
        self.assertEqual([(p.pid, p.ppid, p.pgid, procs.carries(p.env, "u@1"), p.args) for p in table],
                         [(101, 1, 101, True, "sleep 60"), (102, 101, 101, False, "sleep 60"),
                          (103, 1, 103, False, "sleep 60"), (104, 1, 104, False, "sleep 60"),
                          (105, 1, 105, False, "sleep 60")])
        self.assertEqual((table[3].env, table[4].env), ((), None))
        # boot from /proc/uptime (10 ms steps, not btime's whole seconds); a start is its tick's end
        self.assertAlmostEqual(table[0].start, time.time() - 5000.25 + 10101 / tick, delta=1)
        (root / "uptime").unlink()
        self.assertIsNone(procs._table_proc(time.monotonic() + 2.0, root=root)[0].start)

    def test_a_blocked_proc_walk_keeps_the_ps_rules(self):
        """The /proc walk (Linux) obeys the rules of one `ps`: a walk that blocks (a /proc read of a
        process stuck in the kernel) is NoTime under a deadline shorter than PS_TIMEOUT_S, a real
        failure (None) at the full PS_TIMEOUT_S, and never outlasts either; no time to start: NoTime."""
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(procs, "_walk_proc", lambda root: release.wait(30)), \
                mock.patch.object(procs, "PS_TIMEOUT_S", 0.3):
            started = time.monotonic()
            with self.assertRaises(procs.NoTime):
                procs._table_proc(time.monotonic() + 0.2)
            self.assertLess(time.monotonic() - started, 0.5)
            started = time.monotonic()
            self.assertIsNone(procs._table_proc(time.monotonic() + 5.0))
            self.assertLess(time.monotonic() - started, 0.8)
            with self.assertRaises(procs.NoTime):
                procs._table_proc(time.monotonic())
        with mock.patch.object(procs, "_walk_proc", lambda root: 1 / 0):     # a walk that raises: no table
            self.assertIsNone(procs._table_proc(time.monotonic() + 2.0))

    def test_the_hook_spawn_never_hands_its_run_id_to_a_new_run(self):
        seen = []
        with mock.patch.dict(os.environ, {procs.ENV: rid("outer")}), \
                mock.patch.object(procs.subprocess, "Popen", lambda argv, **kw: seen.append(kw["env"])):
            runs.start("acme-coupon-field-20261018-1")
        self.assertNotIn(procs.ENV, seen[0])


class ReconcileTest(SweepCase):
    """R-A: a run whose `orche run` died (or whose pid is another program now) is filed as lost."""

    def folder(self):
        path = self.home / "runs" / "acme"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def put_lock(self, unit, pid, to="div-acme-build", **extra):
        record = {"pid": pid, "unit": unit, "to": to, "started": "2026-01-01T09:05:00+00:00",
                  "started_ns": 1, **extra}
        (self.folder() / f"{unit}.lock").write_text(json.dumps(record))
        (self.folder() / f"{to}.target.lock").write_text(json.dumps(record))
        return record

    def locks(self):
        return sorted(p.name for p in self.folder().glob("*.lock"))

    def reconcile(self, budget_s=10.0):
        shown = getattr(self, "sip_off", set())
        if not shown:
            return runs.reconcile(runs.registry.load(), "acme", budget_s=budget_s)
        with mock.patch.object(shell, "RUNNER", self.sip_ps(shown)):
            return runs.reconcile(runs.registry.load(), "acme", budget_s=budget_s)

    @staticmethod
    def sip_ps(pids):
        """The real `ps`, except that `ps -axwwE` shows only the arguments of `pids` (Apple binaries
        whose environment macOS shows because SIP is off, as on GitHub's runners): what SIP hides."""
        def run(argv, *, timeout, input, env=None):
            res = REAL_SHELL(argv, timeout=timeout, input=input)
            if list(argv) != procs.ENV_PS or not res.ok:
                return res
            lines = []
            for line in res.out.splitlines():
                pid = line.split(None, 1)[0] if line.strip() else ""
                if pid.isdigit() and int(pid) in pids:
                    line = pid + " " + REAL_SHELL(["ps", "-ww", "-o", "command=", "-p", pid], timeout=timeout,
                                                  input=None).out.strip()
                lines.append(line)
            return shell.Result(res.code, "\n".join(lines) + "\n", res.err, res.argv)
        return run

    def from_target(self):
        return [p for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]

    def test_a_sigkilled_run_files_lost_and_failed_and_kills_what_it_left(self):
        """kill -9 of a real `orche run`: claude (alive, its own session) and a detached grandchild
        are killed, RUN.json says `lost`, Main gets `failed` "runner died: …", the locks go."""
        unit = self.brief()
        self.action(DETACHED)
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        lock, grandchild = self.lock(unit), self.grandchild()
        claude = int((self.fake / "pid").read_text())
        proc.kill()                                   # SIGKILL: no handler, no finally
        proc.wait()
        self.assertTrue(alive(claude) and alive(grandchild))
        [act] = self.reconcile()
        self.assertEqual((act["action"], act["unit"], act["pid"], act["removed"]), ("reconciled", unit, proc.pid, True))
        self.assertTrue(self.gone(claude) and self.gone(grandchild))
        self.assertEqual(self.locks(), [])
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["is_error"], rec["error"], rec["started"]),
                         ("failed", "lost", True, f"runner process {proc.pid} died without filing", lock["started"]))
        [ptr] = self.from_target()
        self.assertEqual((ptr["status"], ptr["via"], ptr["report_path"]), ("failed", "runner", ""))
        self.assertEqual(ptr["summary"], f"runner died: {unit} (pid {proc.pid}, started {runs._clock(lock['started'])}); "
                                         f"re-run with `orche run {unit}`")
        self.assertIn(f"run unit={unit} to=div-acme-build runner-reconciled pid={proc.pid}", self.events())
        self.assertEqual(self.reconcile(), [])        # settled once

    def test_a_dead_run_whose_pointer_is_filed_only_loses_its_locks(self):
        unit = self.brief()
        pid = self.dead_pid()
        self.put_lock(unit,pid, child_pid=pid)
        inbox.write(inbox.make(unit, "div-acme-build", "main-acme", "blocked", summary="real",
                               via="runner"))
        [act] = self.reconcile()
        self.assertEqual((act["action"], act["removed"]), ("stale-lock", True))
        self.assertEqual(self.locks(), [])
        self.assertEqual([p["summary"] for p in self.from_target()], ["real"])
        self.assertFalse((self.units_dir() / unit / "RUN.json").exists())
        self.assertIn(f"run unit={unit} to=div-acme-build runner-stale-lock pid={pid} child_pid={pid}", self.events())

    def test_an_older_runner_pointer_does_not_count(self):
        """A runner pointer from before the lock's `started` belongs to an earlier run."""
        unit = self.brief()
        old = inbox.make(unit, "div-acme-build", "main-acme", "blocked", summary="earlier run", via="runner")
        old["at"] = "2026-01-01T09:04:59+00:00"
        inbox.write(old)
        self.put_lock(unit,self.dead_pid())
        self.assertEqual([a["action"] for a in self.reconcile()], ["reconciled"])
        self.assertEqual(sorted(p["status"] for p in self.from_target()), ["blocked", "failed"])

    def test_a_live_verified_run_is_left_alone_and_another_program_is_lost(self):
        unit, other = self.brief(), self.brief()
        live = self.fake_run(unit)
        self.put_lock(unit,live.pid)
        stranger = self.sleeper()
        (self.folder() / f"{other}.lock").write_text(json.dumps({"pid": stranger.pid, "unit": other,
                                                                 "to": "div-acme-build"}))
        [act] = self.reconcile()
        self.assertEqual((act["unit"], act["action"]), (other, "reconciled"))
        self.assertEqual(self.locks(), [f"{unit}.lock", "div-acme-build.target.lock"])   # the live run's
        self.assertIsNone(live.poll())
        self.assertIsNone(stranger.poll())            # never signalled
        self.assertEqual([p["unit"] for p in self.from_target()], [other])

    def test_with_no_budget_only_kill_0_runs_and_the_sweep_waits_for_a_later_reconcile(self):
        """The prompt hook's cheap path: no `ps` at all. A dead run is filed now (the pointer is what
        Main must see) and frees its slot and target at once; its sweep waits, as `<run_id>.sweep`,
        for a reconcile with time."""
        unit, other = self.brief(), self.brief()
        pid = self.dead_pid()
        run = rid(unit)
        self.put_lock(unit, pid, run_id=run, child_pid=pid)
        stranger = self.sleeper()
        (self.folder() / f"{other}.lock").write_text(json.dumps({"pid": stranger.pid, "unit": other}))
        with mock.patch.object(runs.shell, "RUNNER", mock.Mock(side_effect=AssertionError("ps ran"))):
            [act] = self.reconcile(budget_s=0)
        self.assertEqual((act["unit"], act["action"], act["filed"], act["removed"], act["pending"]),
                         (unit, "reconciled", True, True, True))
        self.assertEqual(self.locks(), [f"{other}.lock"])
        self.assertEqual(json.loads((self.folder() / f"{run}.sweep").read_text())["reconciled"], "reconciled")
        self.assertIn("slot freed, leftovers swept by a later reconcile", runs.describe(act))
        self.assertEqual(len(self.from_target()), 1)
        [act] = self.reconcile()
        self.assertEqual((act["unit"], act["action"], act["removed"]), (other, "reconciled", True))
        self.assertEqual(self.locks(), [])
        self.assertEqual(list(self.folder().glob("*.sweep")), [])      # swept (nothing left alive)
        self.assertEqual(sorted(p["unit"] for p in self.from_target()), [unit, other])   # no second pointer
        self.assertIsNone(stranger.poll())

    def test_an_unreadable_process_table_never_pins_the_project(self):
        """C-1: `ps` keeps failing. The lost run is filed, its slot and target are free at once (a
        unit for the same target starts); the pending sweep counts its failures, and after
        SWEEP_TRIES in a row (logged) it is settled by kill -0: kept while its claude's pid lives
        (doctor warns), dropped once it is gone. Nothing is ever signalled without a table."""
        from orche.install import doctor
        unit, other = self.brief(), self.brief()
        claude = self.sleeper()
        run = rid(unit)
        self.put_lock(unit, self.dead_pid(), run_id=run, child_pid=claude.pid, pgid=claude.pid)
        record = self.folder() / f"{run}.sweep"
        with mock.patch.object(shell, "RUNNER", ps_fails), mock.patch.object(procs, "_linux", return_value=False):
            [act] = self.reconcile()
            self.assertEqual((act["action"], act["removed"], act["pending"]), ("reconciled", True, True))
            self.assertEqual(self.locks(), [])
            self.assertEqual(json.loads(record.read_text())["sweep_failures"], 1)
            lock = runs.acquire("acme", other, {"to": "div-acme-build"}, 1, wait_s=0)   # its reconcile: 2
            runs.release(lock)
            self.assertEqual(self.reconcile(), [])
            self.assertEqual(json.loads(record.read_text())["sweep_failures"], runs.SWEEP_TRIES)
            self.assertIn(f"runner: {unit}: the process table could not be read 3 times in a row",
                          (self.home / "logs" / "hooks.log").read_text())
            rows = doctor.process_checks(runs.registry.load())
            self.assertEqual([(lvl, name) for lvl, name, _ in rows], [("WARN", "process table"), ("WARN", "pending sweep")])
            self.assertIn(f"acme: {unit} (run {run}, claude pid {claude.pid}): leftovers not swept yet (3 ", rows[1][2])
            self.assertIsNone(claude.poll())                    # never signalled
            claude.kill()
            claude.wait()
            [act] = self.reconcile()
        self.assertEqual((act["action"], act["failures"]), ("abandoned", 4))
        self.assertIn("could not be read 4 times in a row and its claude is gone", runs.describe(act))
        self.assertFalse(record.exists())
        self.assertIn(f"run unit={unit} runner-sweep-abandoned run_id={run} failures=4", self.events())
        self.assertEqual(len(self.from_target()), 1)

    def test_a_pending_sweep_kills_once_the_table_reads_again(self):
        unit = self.brief()
        run = rid(unit)
        worker = self.execd(self.tagged(run))
        self.put_lock(unit, self.dead_pid(), run_id=run)
        with mock.patch.object(shell, "RUNNER", ps_fails), mock.patch.object(procs, "_linux", return_value=False):
            self.assertEqual([a["pending"] for a in self.reconcile()], [True])
        self.assertIsNone(worker.poll())
        [act] = self.reconcile()
        self.assertEqual((act["action"], act["killed"]), ("cleared", [worker.pid]))
        self.assertEqual(worker.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(list(self.folder().glob("*.sweep")), [])
        self.assertIn(f"run unit={unit} runner-swept pids={worker.pid}", self.events())

    def test_a_filed_lock_left_behind_holds_no_slot(self):
        """A dead lock already marked filed (its rename failed earlier) never makes a run wait: the
        run settles it (into its pending sweep) and takes the slot and the target."""
        unit, other = self.brief(), self.brief()
        run = rid(unit)
        self.put_lock(unit, self.dead_pid(), run_id=run, reconciled="reconciled")
        with mock.patch.object(runs, "reconcile", return_value=[]):       # only _try_acquire looks
            lock = runs.acquire("acme", other, {"to": "div-acme-build"}, 1, wait_s=0)
        self.assertEqual(self.locks(), [f"{other}.lock", "div-acme-build.target.lock"])
        runs.release(lock)
        self.assertTrue((self.folder() / f"{run}.sweep").exists())

    def test_the_doctor_says_whether_the_process_table_reads(self):
        from orche.install import doctor
        self.assertEqual(doctor.process_checks(runs.registry.load()), [("OK", "process table", "readable")])
        with mock.patch.object(shell, "RUNNER", ps_fails), mock.patch.object(procs, "_linux", return_value=False):
            [(level, name, detail)] = doctor.process_checks(runs.registry.load())
        self.assertEqual((level, name), ("WARN", "process table"))
        self.assertIn("cannot read the process table", detail)

    def hidden_claude(self, mcp_path):
        """An env-hidden stand-in for a runner claude (its own group) whose command line carries
        `--mcp-config <mcp_path>`: macOS an Apple /bin/sh (`ps -E` hides its environment; with SIP
        off it does not, and reconcile() hides it through sip_ps), Linux a non-dumpable process
        (prctl PR_SET_DUMPABLE 0: /proc/<pid>/environ refuses)."""
        if not procs._linux():
            proc = self.execd(self.child(["/bin/sh", "-c", "while :; do sleep 1; done", "claude",
                                          "--mcp-config", str(mcp_path)], start_new_session=True),
                              needle="mcp-config")
            if "PATH=" in subprocess.run(["ps", "-wwE", "-o", "command=", "-p", str(proc.pid)],
                                         capture_output=True, text=True, errors="replace").stdout:
                self.sip_off = getattr(self, "sip_off", set()) | {proc.pid}     # SIP off: hide it as SIP would
            return proc
        proc = self.execd(self.child([sys.executable, "-c", HIDE_ENV, "claude", "--mcp-config", str(mcp_path)],
                                     start_new_session=True), needle="mcp-config")

        def hidden():
            try:
                (procs.PROC / str(proc.pid) / "environ").read_bytes()
            except PermissionError:
                return True
            return False
        if not self.wait_for(hidden, timeout=10):
            self.skipTest("this user reads every environment (root / CAP_SYS_PTRACE): none is hidden")
        return proc

    def test_a_lost_hidden_claude_started_right_after_its_lock_is_still_that_runs_claude(self):
        """A start time is never read early (macOS: etime counted from ps's whole second, see
        test_a_start_from_etime_is_never_early; Linux: /proc/uptime and the end of the
        starttime tick, not btime's whole seconds), so a hidden claude started just after the lock's
        `started_ns` passes `started at or after` and is killed."""
        unit = self.brief()
        run = rid(unit)
        since = time.time_ns()
        claude = self.hidden_claude(headless.marker_path(run))
        self.put_lock(unit, self.dead_pid(), run_id=run, child_pid=claude.pid, pgid=claude.pid, started_ns=since)
        [act] = self.reconcile()
        self.assertIn(claude.pid, act["killed"])
        self.assertIsNotNone(claude.wait(timeout=5))

    def test_a_lost_runs_claude_is_only_killed_when_it_is_that_runs_claude(self):
        """I-2, m-2: the lock's child_pid now belongs to another program. A readable environment
        with another tag: never signalled. An unreadable environment (an Apple binary): signalled
        only when it carries THAT run's own `--mcp-config …/runner/mcp/<run_id>.json` (whole), leads
        the lock's pgid AND started at or after the lock's `started_ns`. The old shared path
        (`runner/empty-mcp.json`) or another run's path, with a matching pgid and start time, is
        never enough; the real lost claude is killed."""
        units = [self.brief() for _ in range(1, 7)]
        rids = [rid(unit) for unit in units]
        mine = [headless.marker_path(r) for r in rids]
        other_run = self.execd(self.child([sys.executable, *SLEEP, *headless.marker(rids[0]).split(" ", 1)],
                                          start_new_session=True, env=untagged_env(**{procs.ENV: rid("other")})))
        older, foreign = self.hidden_claude(mine[1]), self.hidden_claude(mine[2])
        shared = self.hidden_claude(self.home / "runner" / "empty-mcp.json")
        elsewhere = self.hidden_claude(headless.marker_path(rid(units[4])))     # another run of the unit
        cases = [(other_run, {"pgid": other_run.pid, "started_ns": 1}),
                 (older, {"pgid": older.pid, "started_ns": time.time_ns() + 60 * 10 ** 9}),
                 (foreign, {"pgid": foreign.pid + 1, "started_ns": 1}),
                 (shared, {"pgid": shared.pid, "started_ns": 1}),
                 (elsewhere, {"pgid": elsewhere.pid, "started_ns": 1})]
        for unit, run, (proc, extra) in zip(units, rids, cases):
            self.put_lock(unit, self.dead_pid(), run_id=run, child_pid=proc.pid, **extra)
        acts = self.reconcile()
        self.assertEqual([(a["action"], a["killed"]) for a in acts], [("reconciled", [])] * 5)
        self.assertEqual([p.poll() for p in (other_run, older, foreign, shared, elsewhere)], [None] * 5)
        lost = self.hidden_claude(mine[5])
        mine[5].parent.mkdir(parents=True, exist_ok=True)
        mine[5].write_text('{"mcpServers": {}}')
        self.put_lock(units[5], self.dead_pid(), run_id=rids[5], child_pid=lost.pid, pgid=lost.pid, started_ns=1)
        [act] = self.reconcile()
        self.assertIn(lost.pid, act["killed"])        # (and its `sleep 1`, a member of its group)
        self.assertIsNotNone(lost.wait(timeout=5))
        self.assertFalse(mine[5].exists())          # the run's own config goes with its lock

    def test_a_ps_cut_short_by_the_budget_is_no_time_not_a_failure(self):
        """m-1 (the review's probe D2): `ps` works but takes 1.2 s; the prompt hook's 2 s budget
        leaves the second `ps` less than its full PS_TIMEOUT_S and it times out. Three such passes
        never count toward SWEEP_TRIES: the pending sweep stays (its claude is gone) and a later
        reconcile with time kills the tagged leftover. A `ps` that times out with its full
        timeout still counts."""
        slow = 1.2

        def slow_ps(argv, *, timeout, input, env=None):
            if argv and argv[0] == "ps":
                if timeout < slow:
                    time.sleep(timeout)
                    return shell.Result(procs.TIMED_OUT, "", f"timeout after {timeout}s", list(argv))
                time.sleep(slow)
            return REAL_SHELL(argv, timeout=timeout, input=input)
        unit = self.brief()
        run = rid(unit)
        leftover = self.execd(self.tagged(run))
        record = self.folder() / f"{run}.sweep"         # a filed lost run, its claude already gone
        record.write_text(json.dumps({"pid": self.dead_pid(), "unit": unit, "to": "div-acme-build",
                                      "started_ns": 1, "run_id": run, "child_pid": self.dead_pid(),
                                      "reconciled": "reconciled"}))
        with mock.patch.object(procs, "_linux", return_value=False):
            with mock.patch.object(shell, "RUNNER", slow_ps):
                for _ in range(3):
                    self.assertEqual(self.reconcile(budget_s=2.0), [])
            self.assertTrue(record.exists())
            self.assertNotIn("sweep_failures", json.loads(record.read_text()))
            self.assertIsNone(leftover.poll())
            with mock.patch.object(shell, "RUNNER", lambda argv, *, timeout, input: shell.Result(
                    procs.TIMED_OUT, "", "timeout", list(argv))):
                self.assertEqual(self.reconcile(), [])          # a full-timeout `ps` timeout: a failure
            self.assertEqual(json.loads(record.read_text())["sweep_failures"], 1)
        [act] = self.reconcile()
        self.assertEqual((act["action"], act["killed"]), ("cleared", [leftover.pid]))
        self.assertEqual(leftover.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(record.exists())

    def test_a_target_that_routes_nowhere_is_never_reported_filed(self):
        """M-5: the lock's target no longer routes: RUN.json `pointer_filed: false`, no pointer,
        `filed: false`, logged; the line says NOT filed."""
        unit = self.brief()
        pid = self.dead_pid()
        self.put_lock(unit, pid, to="div-nowhere")
        [act] = self.reconcile()
        self.assertEqual((act["action"], act["filed"], act["removed"]), ("unroutable", False, True))
        self.assertIn(f"runner died: {unit} (pid {pid}, started ", runs.describe(act))
        self.assertIn("NOT filed: div-nowhere has no route to a recipient", runs.describe(act))
        self.assertEqual(self.pointers("main-acme"), [])
        rec = self.run_record(unit)
        self.assertEqual((rec["stop_reason"], rec["pointer_filed"]), ("lost", False))
        self.assertIn(f"run unit={unit} to=div-nowhere runner-unroutable pid={pid} filed=false", self.events())
        self.assertIn(f"runner: no recipient for div-nowhere; pointer for {unit} not filed",
                      (self.home / "logs" / "hooks.log").read_text())

    def test_a_run_whose_target_routes_nowhere_records_pointer_filed_false(self):
        """M-5 for a run that executed: nothing is filed, so RUN.json never says it was."""
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(filing, "recipient", return_value=None):
            self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertEqual((self.run_record(unit)["status"], self.run_record(unit)["pointer_filed"]), ("blocked", False))
        self.assertEqual(self.pointers("main-acme"), [])
        self.assertIn(f"runner: no recipient for div-acme-build; pointer for {unit} not filed",
                      (self.home / "logs" / "hooks.log").read_text())

    def test_a_pointer_the_worker_filed_itself_counts(self):
        """M-6: the worker ran `orche report` (no `via: runner`) and then `orche run` was killed:
        Main already has this run's pointer, so no second `failed` "runner died"."""
        unit = self.brief()
        inbox.write(inbox.make(unit, "div-acme-build", "main-acme", "done", summary="mine"))
        self.put_lock(unit, self.dead_pid())
        [act] = self.reconcile()
        self.assertEqual((act["action"], act["filed"]), ("stale-lock", True))
        self.assertEqual([p["summary"] for p in self.from_target()], ["mine"])
        self.assertFalse((self.units_dir() / unit / "RUN.json").exists())

    def test_a_filing_that_fails_keeps_the_lock_for_later(self):
        unit = self.brief()
        self.put_lock(unit,self.dead_pid())
        box = self.home / "inbox" / "main-acme"
        box.mkdir(parents=True)
        box.chmod(0o500)
        try:
            [act] = self.reconcile()
        finally:
            box.chmod(0o700)
        self.assertEqual(act["action"], "unfiled")
        self.assertIn("PermissionError", act["error"])
        self.assertIn(f"{unit}.lock", self.locks())
        self.assertNotIn("reconciled", json.loads((self.folder() / f"{unit}.lock").read_text()))
        self.assertEqual([a["action"] for a in self.reconcile()], ["reconciled"])
        self.assertEqual(self.locks(), [])

    def test_a_dead_lock_met_while_waiting_is_filed_before_its_slot_is_taken(self):
        """acquire(): a run that died while another waited is reconciled, not dropped unfiled."""
        unit, waiting = self.brief(), self.brief()
        holder = self.fake_run(unit)
        self.put_lock(unit,holder.pid)
        polls = []

        def cancel():       # asked after every attempt that found the target taken: the holder dies at the first
            if not polls:
                holder.kill()           # SIGKILL, reaped
                holder.wait()
            polls.append(True)
        with mock.patch.object(runs, "POLL_S", 0.05):
            lock = runs.acquire("acme", waiting, {"to": "div-acme-build"}, 4, wait_s=10, cancel=cancel)
        runs.release(lock)
        self.assertTrue(polls)                         # it waited: the death was met while waiting
        self.assertEqual([p["unit"] for p in self.from_target()], [unit])
        self.assertIn(f"run unit={unit} to=div-acme-build runner-reconciled pid={holder.pid}", self.events())

    def test_locks_nothing_can_be_filed_for_never_hold_a_slot(self):
        """An unreadable lock or a dead one without a unit is dropped at once; a lock whose
        reconcile raises is logged and skipped, never blocking the others."""
        unit = self.brief()
        (self.folder() / "garbage.lock").write_text("{")
        (self.folder() / "nounit.lock").write_text(json.dumps({"pid": self.dead_pid()}))
        lock = runs.acquire("acme", unit, {"to": "div-acme-build"}, 1, wait_s=0)
        runs.release(lock)
        self.assertEqual(self.locks(), [])
        a, b = self.brief(), self.brief()
        self.put_lock(a, self.dead_pid())
        self.put_lock(b, self.dead_pid())
        real = filing.lost

        def lost(reg, proj, data):
            if data["unit"] == a:
                raise RuntimeError("boom")
            return real(reg, proj, data)
        with mock.patch.object(filing, "lost", lost), \
                mock.patch.object(runs, "_drop_target", side_effect=[ValueError("bad"), None]):
            acts = self.reconcile()
        self.assertEqual([(x["unit"], x["action"]) for x in acts], [(b, "reconciled")])
        self.assertIn(f"runner: reconcile {a}.lock: ValueError: bad", (self.home / "logs" / "hooks.log").read_text())

    def test_status_and_the_other_commands_reconcile_but_never_on_a_dry_run(self):
        for n, argv in enumerate((["status"], ["depts"], ["metrics", "--root", str(self.root)],
                                  ["janitor", "run"]), start=1):
            unit = self.brief()
            pid = self.dead_pid()
            self.put_lock(unit,pid)
            code, _, err = self.orche("--dry-run", *argv)
            self.assertIn(f"{unit}.lock", self.locks(), argv)
            code, _, err = self.orche(*argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertIn(f"orche: acme: runner died: {unit} (pid {pid}, started ", err)
            self.assertEqual(self.locks(), [], argv)
        self.assertEqual(len(self.from_target()), 4)

    def test_main_prompt_shows_the_lost_run_within_the_hook_budget(self):
        unit = self.brief()
        pid = self.dead_pid()
        self.put_lock(unit, pid, run_id=rid(unit))
        started = time.monotonic()
        res = self.run_hook("report_inject.py", self.payload("prompt.json"), ORCHE_SESSION="main-acme")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"- unit {unit} · from div-acme-build · status failed", res.stdout)
        self.assertIn(f"  summary: runner died: {unit} (pid {pid}, started ", res.stdout)
        self.assertEqual(self.locks(), [])            # budget left for the sweep: dropped too
        self.assertEqual(list(self.folder().glob("*.sweep")), [])
        # No budget at all: still filed and shown (kill -0 + file writes); the slot is free, the sweep waits.
        other = self.brief()
        run = rid(other)
        self.put_lock(other, self.dead_pid(), run_id=run)
        res = self.run_hook("report_inject.py", self.payload("prompt.json"), ORCHE_SESSION="main-acme",
                            ORCHE_HOOK_BUDGET_S="0")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"  summary: runner died: {other} (pid ", res.stdout)
        self.assertEqual(self.locks(), [])
        self.assertEqual(json.loads((self.folder() / f"{run}.sweep").read_text())["reconciled"], "reconciled")

    def fake_ps(self, body):
        """A `ps` on the hook's PATH running `body` (sh) before (or instead of) the real one."""
        folder = self.root / "fakeps"
        folder.mkdir(exist_ok=True)
        script = folder / "ps"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        return f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"

    def timed_hook(self, path, **env):
        started = time.monotonic()
        res = self.run_hook("report_inject.py", self.payload("prompt.json"), ORCHE_SESSION="main-acme", PATH=path,
                            **env)
        return time.monotonic() - started, res

    def hung_proc_walk(self):
        """The hook's environment for a /proc walk that blocks (Linux's table, its hung `ps`): a
        sitecustomize on PYTHONPATH that patches procs._walk_proc before the hook runs."""
        folder = self.root / "hungproc"
        folder.mkdir(exist_ok=True)
        (folder / "sitecustomize.py").write_text(
            f"import sys, time\nsys.path.insert(0, {LIB!r})\nfrom orche import procs\n"
            "procs._walk_proc = lambda root: time.sleep(30)\n")
        return {"PYTHONPATH": str(folder)}

    def test_a_hung_ps_never_stretches_the_prompt_hook(self):
        """M-1: every `ps` hangs (Linux: and the /proc walk blocks). A live lock (unverifiable: left
        alone) and a dead one (filed, its sweep pending): the hook still answers within its 3 s and
        shows the lost run."""
        unit, other = self.brief(), self.brief()
        live = self.fake_run(other)
        self.put_lock(other, live.pid, to="div-other")
        pid = self.dead_pid()
        run = rid(unit)
        self.put_lock(unit, pid, run_id=run)
        took, res = self.timed_hook(self.fake_ps("exec sleep 30"), **(self.hung_proc_walk() if procs._linux() else {}))
        self.assertLess(took, 3.0)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"  summary: runner died: {unit} (pid {pid}, started ", res.stdout)
        self.assertTrue((self.folder() / f"{run}.sweep").exists())
        self.assertIn(f"{other}.lock", self.locks())
        self.assertIsNone(live.poll())

    def test_a_slow_ps_and_a_child_that_ignores_sigterm_stay_inside_the_hook_budget(self):
        """M-1 (the review's 2.98 s case): `ps` takes 0.8 s, one live lock, one dead lock whose tagged
        leftover ignores SIGTERM. The wait before SIGKILL gets only what the table read left."""
        unit, other = self.brief(), self.brief()
        live = self.fake_run(other)
        self.put_lock(other, live.pid, to="div-other")
        run = rid(unit)
        stubborn = self.execd(self.tagged(run, argv=("-c", "import signal, time; "
                                                     "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")),
                              needle="SIG_IGN")
        self.put_lock(unit, self.dead_pid(), run_id=run)
        took, res = self.timed_hook(self.fake_ps('sleep 0.8\nexec /bin/ps "$@"'))
        self.assertLess(took, 3.0)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"  summary: runner died: {unit} (pid ", res.stdout)
        self.assertIsNone(live.poll())
        if stubborn.poll() is None:            # no time left for the table in the hook: a later reconcile
            self.assertTrue((self.folder() / f"{run}.sweep").exists())
            [act] = self.reconcile()
            self.assertEqual(act["killed"], [stubborn.pid])
        self.assertEqual(stubborn.wait(timeout=5), -signal.SIGKILL)


class DiskTest(RunnerCase):
    """R-D: run-log pruning, doctor's disk rows, and a run that cannot file its result."""

    def logs(self, **ages):
        folder = runs.logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for name, days in ages.items():
            path = folder / name.replace("_", ".")
            path.write_text("x\n")
            at = time.time() - days * 86400
            os.utime(path, (at, at))
        return folder

    def test_a_run_prunes_only_old_run_logs(self):
        folder = self.logs(old_log=20, new_log=2, old_txt=20)
        mcp = {name: headless.marker_path(rid(name)) for name in ("old", "new")}   # leaked by unreconciled runs
        for name, days in (("old", 20), ("new", 2)):
            mcp[name].parent.mkdir(parents=True, exist_ok=True)
            mcp[name].write_text('{"mcpServers": {}}')
            os.utime(mcp[name], (time.time() - days * 86400,) * 2)
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["new.log", "old.txt"])
        self.assertEqual(list(mcp["new"].parent.iterdir()), [mcp["new"]])   # and the run's own is gone
        self.assertTrue((self.units_dir() / unit / "RUN.json").exists())     # records are kept
        self.assertIn("run-logs pruned=1 keep_days=14", self.events())

    def test_janitor_prunes_with_the_configured_days_and_never_on_a_dry_run(self):
        self.config({"claude_bin": str(self.claude), "runner": {"log_keep_days": 3}})
        folder = self.logs(a_log=5, b_log=1)
        self.orche("--dry-run", "janitor", "run")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["a.log", "b.log"])
        self.orche("janitor", "run")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["b.log"])
        self.assertIn("run-logs pruned=1 keep_days=3", self.events())
        self.assertEqual((runs.prune_logs(0), runs.prune_logs("soon")), (0, 0))   # off / not a number
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["b.log"])

    def test_doctor_warns_on_low_disk_and_big_run_logs(self):
        from orche.install import doctor
        self.logs(a_log=0)
        rows = {name: level for level, name, _ in doctor.disk_checks()}
        self.assertEqual(rows, {"disk": rows["disk"], "run logs": "OK"})
        fake = os.statvfs_result((4096, 4096, 100, 10, 10, 0, 0, 0, 0, 255))   # 40 KiB free
        with mock.patch.object(doctor.os, "statvfs", return_value=fake), mock.patch.object(doctor, "MAX_RUN_LOGS", 1):
            rows = doctor.disk_checks()
        self.assertEqual([(level, name) for level, name, _ in rows], [("WARN", "disk"), ("WARN", "run logs")])
        self.assertIn("0.0 GiB free", rows[0][2])
        self.assertIn("lower runner.log_keep_days", rows[1][2])

    def test_doctor_warns_about_a_loaded_main_waker(self):
        """0.6.2: the pre-0.6.0 launchd waker doubles Main's wakes; the stub launchctl (not loaded) gives no row."""
        from orche.install import doctor
        self.assertEqual(doctor.main_waker_rows(), [])
        self.assertIn(f"print gui/{os.getuid()}/com.orche.main-waker",
                      (self.home / support.LAUNCHCTL_CALLS).read_text())
        fake = self.home / "fakebin"
        fake.mkdir()
        (fake / "launchctl").write_text("#!/bin/sh\nexit 0\n")
        (fake / "launchctl").chmod(0o755)
        os.environ["PATH"] = f"{fake}{os.pathsep}{os.environ['PATH']}"   # TempHomeCase restores PATH
        [(level, name, detail)] = doctor.main_waker_rows()
        self.assertEqual((level, name), ("WARN", "main-waker"))
        self.assertIn("run: launchctl bootout gui/$(id -u)/com.orche.main-waker", detail)

    def test_a_run_that_cannot_file_exits_5_and_reconcile_files_it_later(self):
        """The inbox is read-only (as with EACCES/ENOSPC): the run writes RUN.json, fails on the
        pointer, says so on stderr (the run log), exits 5 and leaves its lock; once the inbox takes
        writes again reconcile files `failed` "runner died" and keeps the run's own RUN.json."""
        unit = self.brief()
        self.reply("STATUS: blocked\nreal result")
        box = self.home / "inbox" / "main-acme"
        box.mkdir(parents=True)
        box.chmod(0o500)
        try:
            proc = self.spawn(unit)
            code = proc.wait(timeout=30)
        finally:
            box.chmod(0o700)
        out = (self.root / f"{unit}.out").read_text()
        self.assertEqual(code, 5, out)
        self.assertIn(f"orche: cannot file the result of {unit}: pointer: [Errno 13]", out)
        self.assertIsNotNone(self.lock(unit))
        self.assertEqual(self.pointers("main-acme"), [])
        self.assertEqual(self.run_record(unit)["status"], "blocked")
        [act] = runs.reconcile(runs.registry.load(), "acme", budget_s=10)
        self.assertEqual((act["action"], act["pid"], act["removed"]), ("reconciled", proc.pid, True))
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["status"], "failed")
        self.assertTrue(ptr["summary"].startswith(f"runner died: {unit} (pid {proc.pid}, "), ptr["summary"])
        self.assertEqual(self.run_record(unit)["status"], "blocked")             # the run's own record
        self.assertIsNone(self.lock(unit))


class ScaleDoctorTest(support.TempHomeCase):
    """0.9.0 T4 (Appendix C.5): the gh, queue gate, deploy on merge, queue, usage, memory, scale and ladder
    rows, beside the disk and process-table rows above. gh is a fake on PATH (never the real one)."""

    def fake_gh(self, perm):
        folder = self.home / "fake-gh"
        folder.mkdir(exist_ok=True)
        (folder / "repo.json").write_text(json.dumps({"nameWithOwner": "DESPELL/PIKIT", "viewerPermission": perm,
                                                      "defaultBranchRef": {"name": "main"}}))
        script = folder / "gh"
        script.write_text('#!/bin/sh\ncase "$1 $2" in\n  "api user") echo despell-bot ;;\n'
                          '  "repo view") cat "$(dirname "$0")/repo.json" ;;\n  *) exit 1 ;;\nesac\n')
        script.chmod(0o755)
        os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ['PATH']}"

    def git_repo(self, name, origin=None):
        root = self.home / name
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        if origin:
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True, capture_output=True)
        return root

    def test_gh_rows_write_read_and_missing(self):
        from orche.install import doctor
        root = self.git_repo("pikit", "https://github.com/DESPELL/PIKIT.git")
        reg = {"projects": {"pikit": {"path": str(root)}, "loose": {"path": str(self.git_repo("loose"))}}}
        self.fake_gh("WRITE")
        self.assertEqual(doctor.gh_rows(reg), [
            ("WARN", "gh", "loose: no origin remote — the merge queue lands PRs on GitHub"),
            ("OK", "gh", "pikit: gh as despell-bot · WRITE on DESPELL/PIKIT · base main")])
        self.fake_gh("READ")
        level, name, detail = doctor.gh_rows(reg)[1]
        self.assertEqual((level, name), ("WARN", "gh"))
        self.assertIn("pikit: gh account despell-bot has READ on DESPELL/PIKIT — the queue cannot merge: gh auth "
                      "switch -u <an account with write>, or gh auth login", detail)
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            [(level, _name, detail)] = doctor.gh_rows(reg)
        self.assertEqual(level, "WARN")
        self.assertIn("gh is not installed", detail)

    def test_queue_gate_and_deploy_on_merge(self):
        from orche import config
        from orche.install import doctor
        root = self.git_repo("site")
        (root / "vercel.json").write_text("{}")
        reg = {"projects": {"site": {"path": str(root)}}}
        [no_base, _deploy] = doctor.gate_rows(reg, config.load())          # no origin/HEAD, no publish.base: R12
        self.assertIn("site: no base branch: publish.base is unset and origin/HEAD is not set", no_base[2])
        self.assertIn("OWNER: git -C", no_base[2])
        rows = doctor.gate_rows(reg, {**config.load(), "publish": {"base": "main"}})
        self.assertEqual([r[:2] for r in rows], [("WARN", "queue gate"), ("WARN", "deploy on merge")])
        self.assertIn("site: no scripts/verify/ci.sh on origin/main — queue add refuses", rows[0][2])
        self.assertIn("site: vercel.json — every queue merge deploys main (queue.land auto)", rows[1][2])

    def test_queue_usage_memory_scale_and_ladder_rows(self):
        from orche import config, ladder, util
        from orche.flow import usage
        from orche.install import doctor
        from orche.queue import store
        q = store.empty("pikit")
        q["entries"] = [{"id": "pr3-bbbbbbb", "pr": 3, "head": "b" * 40, "state": "testing", "owner_ok": True}]
        store.queue_path("pikit").parent.mkdir(parents=True, exist_ok=True)
        util.write_json(store.queue_path("pikit"), q)
        reg = {"projects": {"pikit": {"path": str(self.home)}, "other": {"path": str(self.home)}}, "limits": {}}
        [(level, _n, detail)] = doctor.queue_rows(reg)                    # `other` has no queue: no row
        self.assertEqual(level, "WARN")
        self.assertIn("pikit: open · 0 waiting · 1 testing · integrator idle — an entry is testing with no "
                      "integrator (lost)", detail)
        self.assertEqual(doctor.usage_rows(), [("OK", "usage", "no usage hold")])
        usage.hit("claude", "API Error: 429", {}, project="pikit", unit="u1", stage="work")
        [(level, _n, detail)] = doctor.usage_rows()
        self.assertEqual(level, "WARN")
        self.assertTrue(detail.startswith("claude held until ") and detail.endswith("(1 units) — they re-run then"))
        cfg = config.load()
        cfg["runner"]["min_free_mb"] = 2048                     # test homes turn the guard off (support.NO_WAKE)
        self.assertEqual(doctor.memory_rows(cfg, free=14499, total=24576),
                         [("OK", "memory", "14.2 GiB free (58 %) ≥ 2048 MB")])
        self.assertEqual(doctor.memory_rows(cfg, free=1500, total=24576),
                         [("WARN", "memory", "1.5 GiB free (6 %) < 2048 MB: new runs wait for memory (runner.min_free_mb)")])
        with mock.patch.object(procs, "free_mb", return_value=None):
            self.assertEqual(doctor.memory_rows(cfg)[0][0], "WARN")
        self.assertEqual(doctor.scale_rows(cfg, reg), [
            ("OK", "scale", "preset default: runs 4/project, 8 total · features headless · spend cap $30 · "
                            "min free 2048 MB"),
            ("OK", "ladder", ladder.doctor_row(cfg))])
