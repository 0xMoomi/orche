"""Headless-claude (SPEC §14.10): the one `claude -p` call shape, the environment rule, check()."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, TempHomeCase, write_fake_claude

from orche import headless, paths, procs
from orche.headless import Call
from orche.install import settings as install_settings

CWD = Path("/w/proj")
PERSONA = Path("/w/persona.md")
DENIED = "Bash(rm:*),WebFetch"


def project(**kw):
    return Call(**{"context": "project", "model": "claude-opus-5-5", "max_turns": 50, "timeout": 600.0,
                   "cwd": CWD, **kw})


def sealed(**kw):
    return Call(**{"context": "sealed", "model": "claude-sonnet-5-5", "max_turns": 30, "timeout": 300.0,
                   "cwd": CWD, "allowed": "Read,Glob,Grep", **kw})


def blank(**kw):
    return Call(**{"context": "blank", "model": "claude-haiku-5-5", "max_turns": 1, "timeout": 30.0,
                   "system": "SYS", **kw})


def gates_pair():
    return ["--settings", headless.gates()]


class ArgvTest(TempHomeCase):
    def test_shapes(self):
        mk = str(headless.marker_path("r-1"))
        rows = [
            ("project minimal", project(), {},
             ["claude", "-p", "--model", "claude-opus-5-5", "--output-format", "json", "--max-turns", "50",
              "--setting-sources", "project", "--permission-mode", "bypassPermissions",
              "--strict-mcp-config"] + gates_pair()),
            ("project full", project(effort="high", tag="r-1", allowed="Read,Edit", denied=DENIED,
                                     append_file=PERSONA), {},
             ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high", "--output-format", "json",
              "--max-turns", "50", "--setting-sources", "project", "--permission-mode", "bypassPermissions",
              "--strict-mcp-config",
              "--mcp-config", mk, "--allowedTools", "Read,Edit", "--disallowedTools", DENIED,
              "--append-system-prompt-file", str(PERSONA)] + gates_pair()),
            ("sealed intake", sealed(tag="r-1", denied=DENIED, append_file=PERSONA), {},
             ["claude", "-p", "--model", "claude-sonnet-5-5", "--output-format", "json", "--max-turns", "30",
              "--restricted", "--tools", "Read,Glob,Grep", "--strict-mcp-config", "--mcp-config", mk,
              "--allowedTools", "Read,Glob,Grep", "--disallowedTools", DENIED, "--no-session-persistence",
              "--append-system-prompt-file", str(PERSONA)]),
            ("sealed rules", sealed(allowed="Read,Bash(git status:*),Bash(ls:*)"), {},
             ["claude", "-p", "--model", "claude-sonnet-5-5", "--output-format", "json", "--max-turns", "30",
              "--restricted", "--tools", "Read,Bash", "--strict-mcp-config",
              "--allowedTools", "Read,Bash(git status:*),Bash(ls:*)", "--no-session-persistence"]),
            ("blank triage", blank(), {},
             ["claude", "-p", "--model", "claude-haiku-5-5", "--output-format", "json", "--max-turns", "1",
              "--setting-sources", "project", "--tools", "", "--strict-mcp-config",
              "--no-session-persistence", "--system-prompt", "SYS"]),
            ("claude_bin from cfg", project(), {"claude_bin": "/opt/x/claude"},
             ["/opt/x/claude", "-p", "--model", "claude-opus-5-5", "--output-format", "json", "--max-turns",
              "50", "--setting-sources", "project", "--permission-mode", "bypassPermissions",
              "--strict-mcp-config"] + gates_pair()),
            ("empty claude_bin is the default", project(), {"claude_bin": ""},
             ["claude", "-p", "--model", "claude-opus-5-5", "--output-format", "json", "--max-turns", "50",
              "--setting-sources", "project", "--permission-mode", "bypassPermissions",
              "--strict-mcp-config"] + gates_pair()),
        ]
        for name, call, cfg, want in rows:
            with self.subTest(name):
                self.assertEqual(headless.argv(call, cfg), want)

    def test_project_gates_are_the_plugin_pretooluse_hooks(self):
        """D8: the --settings JSON is compact, holds only PreToolUse, and flattens to exactly the
        PreToolUse hooks a pane gets from hooks/hooks.json (env_read_gate and scope_gate today)."""
        got = headless.argv(project(effort="high", tag="r-1", allowed="Read,Edit", append_file=PERSONA), {})
        self.assertEqual(got[-2], "--settings")
        self.assertEqual(got.count("--settings"), 1)
        value = got[-1]
        data = json.loads(value)
        self.assertEqual(json.dumps(data, separators=(",", ":")), value)
        self.assertEqual(list(data), ["hooks"])
        self.assertEqual(list(data["hooks"]), ["PreToolUse"])
        flat = [("PreToolUse", e.get("matcher"), h["command"])
                for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
        want = [h for h in install_settings.plugin_hooks(paths.PLUGIN_ROOT) if h[0] == "PreToolUse"]
        self.assertEqual(flat, want)
        root = str(paths.PLUGIN_ROOT)
        self.assertEqual([c for _, _, c in flat],
                         [f'"{root}/hooks/run.sh" env_read_gate', f'"{root}/hooks/run.sh" scope_gate'])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", value)

    def test_sealed_and_blank_get_no_settings(self):
        for name, call in (("sealed", sealed(tag="r-1", denied=DENIED, append_file=PERSONA)),
                           ("sealed rules", sealed(allowed="Read,Bash(ls:*)")), ("blank", blank())):
            with self.subTest(name):
                self.assertNotIn("--settings", headless.argv(call, {}))

    def test_hooks_json_is_read_on_every_call(self):
        root = self.home / "plugin"
        (root / "hooks").mkdir(parents=True)
        hooks = root / "hooks" / "hooks.json"

        def write(pre):
            hooks.write_text(json.dumps({"hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/stop"}]}],
                **pre}}))

        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            write({"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/a\" x ${CLAUDE_PLUGIN_ROOT}"}]}]})
            self.assertEqual(json.loads(headless.argv(project(), {})[-1]), {"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": f'"{root}/a" x {root}'}]}]}})
            write({"PreToolUse": [{"hooks": [{"type": "command", "command": "b"}]}]})
            self.assertEqual(json.loads(headless.argv(project(), {})[-1]),
                             {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "b"}]}]}})

    def test_project_call_without_readable_gates_raises(self):
        """A project worker without its gates must not start silently; sealed and blank do not read."""
        root = self.home / "plugin"
        (root / "hooks").mkdir(parents=True)
        hooks = root / "hooks" / "hooks.json"
        with mock.patch.object(paths, "PLUGIN_ROOT", root):
            with self.assertRaises(OSError):
                headless.argv(project(), {})
            for name, text in (("not JSON", "{"), ("no PreToolUse", '{"hooks": {"Stop": []}}'),
                               ("no hooks", "{}")):
                with self.subTest(name):
                    hooks.write_text(text)
                    with self.assertRaises((ValueError, KeyError)):
                        headless.argv(project(), {})
            hooks.unlink()
            self.assertNotIn("--settings", headless.argv(sealed(), {}))
            self.assertNotIn("--settings", headless.argv(blank(), {}))

    def test_effort_only_when_set(self):
        for effort in (None,) + tuple(headless.registry.EFFORTS):
            with self.subTest(effort=effort):
                got = headless.argv(project(effort=effort), {})
                self.assertEqual("--effort" in got, effort is not None)
                if effort:
                    self.assertEqual(got[got.index("--effort") + 1], effort)

    def test_invalid(self):
        rows = [
            ("unknown context", lambda: Call("open", "m", 1, 1.0, cwd=CWD)),
            ("unknown effort", lambda: project(effort="huge")),
            ("max_turns 0", lambda: project(max_turns=0)),
            ("timeout 0", lambda: project(timeout=0)),
            ("timeout negative", lambda: project(timeout=-1.0)),
            ("project without cwd", lambda: project(cwd=None)),
            ("sealed without cwd", lambda: sealed(cwd=None)),
            ("blank with cwd", lambda: blank(cwd=CWD)),
            ("blank with tag", lambda: blank(tag="r-1")),
            ("blank with allowed", lambda: blank(allowed="Read")),
            ("blank with denied", lambda: blank(denied="Read")),
            ("blank with append_file", lambda: blank(append_file=PERSONA)),
            ("blank without system", lambda: blank(system=None)),
            ("blank with empty system", lambda: blank(system="")),
            ("project with system", lambda: project(system="S")),
            ("sealed with system", lambda: sealed(system="S")),
            ("sealed without allowed", lambda: sealed(allowed="")),
            ("ORCHE_RUN_ID in env", lambda: project(env={procs.ENV: "x"})),
        ]
        for name, make in rows:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    headless.argv(make(), {})

    def test_call_is_frozen(self):
        with self.assertRaises(Exception):
            project().model = "other"


class EnvTest(unittest.TestCase):
    PASSING = ("HOME", "LANG", "ANTHROPIC_BASE_URL", "AWS_PROFILE", "OTHER_VAR") + headless.PASS_ENV \
        + headless.AUTH_ENV
    DROPPED = ("ORCHE_SESSION", "ORCHE_RUNNER", "ORCHE_RUN_ID", "ORCHE_RUN_SINCE", "ORCHE_KEYMASTER",
               "ORCHE_DECISIONS_FILE", "CLAUDECODE", "CLAUDE_PROJECT_DIR", "CLAUDE_EFFORT", "CLAUDE_PID",
               "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SUBAGENT_MODEL",
               "ORCA_TERMINAL_HANDLE", "ORCA_AGENT_LAUNCH_TOKEN", "ORCA_PANE_KEY", "JEV_API_KEY")

    def environ(self, **extra):
        base = {name: f"v-{name}" for name in self.PASSING + self.DROPPED}
        base["PATH"] = "/usr/bin:/bin"
        base.update(extra)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_passing(self):
        with self.environ():
            got = headless.env(project(), {})
        for name in self.PASSING:
            with self.subTest(name):
                self.assertEqual(got.get(name), f"v-{name}")

    def test_dropped(self):
        with self.environ():
            got = headless.env(project(), {})
        for name in self.DROPPED:
            with self.subTest(name):
                self.assertNotIn(name, got)

    def test_configured_jev_key(self):
        cfg = {"jev": {"api_key_env": "MY_JEV_KEY"}}
        with self.environ(MY_JEV_KEY="secret"):
            got = headless.env(project(), cfg)
        self.assertNotIn("MY_JEV_KEY", got)
        self.assertNotIn("JEV_API_KEY", got)

    def test_path_starts_with_plugin_bin(self):
        with self.environ():
            got = headless.env(blank(), {})
        self.assertEqual(got["PATH"], f"{paths.PLUGIN_ROOT / 'bin'}{os.pathsep}/usr/bin:/bin")

    def test_call_env_applies_last(self):
        with self.environ():
            got = headless.env(project(env={"HOME": "/h2", "ORCHE_SESSION": "s", "PATH": "/p"}), {})
        self.assertEqual((got["HOME"], got["ORCHE_SESSION"], got["PATH"]), ("/h2", "s", "/p"))

    def test_tag_sets_run_id(self):
        rows = [("tagged", project(tag="r-9"), "r-9"), ("untagged", project(), None),
                ("blank", blank(), None)]
        for name, call, want in rows:
            with self.subTest(name):
                with self.environ():
                    self.assertEqual(headless.env(call, {}).get(procs.ENV), want)

    def test_reads_environ_at_call_time(self):
        with self.environ():
            os.environ["LATE_VAR"] = "1"
            self.assertEqual(headless.env(project(), {}).get("LATE_VAR"), "1")


ALL_FLAGS = "Usage: claude [options]\n" + "".join(f"  {flag} <x>\n" for flag in headless.FLAGS)


def help_without(*flags):
    return "Usage: claude [options]\n" + "".join(f"  {f} <x>\n" for f in headless.FLAGS if f not in flags)


class CheckTest(TempHomeCase):
    def fake(self, help_text):
        return str(write_fake_claude(self.home / "fake", help_text))

    def test_flags(self):
        bracket = help_without("--append-system-prompt-file") + "  --append-system-prompt[-file] <x>\n"
        alone = help_without("--system-prompt") + "  --append-system-prompt <x>\n"
        rows = [
            ("all listed", ALL_FLAGS, ("OK", "claude flags", "all 15 listed in claude --help")),
            ("effort and restricted missing", help_without("--restricted", "--effort"),
             ("WARN", "claude flags", "not listed in claude --help: --effort, --restricted; headless "
                                      "calls that use them fail (update Claude Code)")),
            ("bracket form counts", bracket, ("OK", "claude flags", "all 15 listed in claude --help")),
            ("append alone is not system", alone,
             ("WARN", "claude flags", "not listed in claude --help: --system-prompt; headless "
                                      "calls that use them fail (update Claude Code)")),
        ]
        for name, text, want in rows:
            with self.subTest(name):
                bin_ = self.fake(text)
                got = headless.check({"claude_bin": bin_})
                self.assertEqual(got, [("OK", "claude", f"{bin_} 9.9.9 (Claude Code)"), want])

    def test_not_found(self):
        missing = str(self.home / "nope" / "claude")
        self.assertEqual(headless.check({"claude_bin": missing}),
                         [("WARN", "claude", f"{missing} not found (config claude_bin); "
                                             "orche run, intake and triage cannot start claude")])

    def test_version_fails(self):
        folder = self.home / "bad"
        folder.mkdir()
        script = folder / "claude"
        script.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo boom >&2; exit 1; fi\n'
                          f'cat <<EOF\n{ALL_FLAGS}EOF\n')
        script.chmod(0o755)
        got = headless.check({"claude_bin": str(script)})
        self.assertEqual(got, [("WARN", "claude", f"{script} --version failed: boom"),
                               ("OK", "claude flags", "all 15 listed in claude --help")])

    def test_found_on_path(self):
        bin_ = self.fake(ALL_FLAGS)
        os.environ["PATH"] = f"{Path(bin_).parent}{os.pathsep}{os.environ.get('PATH', '')}"
        got = headless.check({})
        self.assertEqual(got[0], ("OK", "claude", f"{bin_} 9.9.9 (Claude Code)"))


class MarkerTest(TempHomeCase):
    def test_marker(self):
        tag = "r-42"
        self.assertEqual(headless.marker(tag), "--mcp-config " + str(headless.marker_path(tag)))
        self.assertEqual(headless.marker_dir(), self.home / "runner" / "mcp")
        self.assertEqual(headless.marker_path(tag), self.home / "runner" / "mcp" / "r-42.json")


class ResultTest(unittest.TestCase):
    def test_problem_names_why_a_call_failed(self):
        ok = {"type": "result", "subtype": "success", "is_error": False, "result": "hi"}
        self.assertEqual(headless.Result(0, ok, "", "", False).problem, "")
        self.assertEqual(headless.Result(3, {}, "", "exit 3: no output", False).problem, "exit 3: no output")
        err = {"type": "result", "subtype": "error_max_turns", "is_error": True}
        self.assertEqual(headless.Result(0, err, "", "", False).problem, "claude reported an error (error_max_turns)")
        self.assertEqual(headless.Result(2, ok, "", "", False).problem, "claude exited 2")


class ImportTest(unittest.TestCase):
    def test_loads_only_its_modules(self):
        code = ("import sys; sys.path.insert(0, 'lib'); import orche.headless; "
                "print(sorted(m for m in sys.modules if m.startswith('orche')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        self.assertEqual(out, str(['orche', 'orche.headless', 'orche.log', 'orche.paths', 'orche.procs',
                                   'orche.registry', 'orche.shell', 'orche.util']))


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid, seconds=3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


REPLY = {"type": "result", "result": "hi", "is_error": False, "total_cost_usd": 0.25, "num_turns": 2,
         "usage": {"input_tokens": 3}}


class RunTest(TempHomeCase):
    """run() against the fake claude (support.FAKE_CLAUDE): never the real one."""

    def setUp(self):
        super().setUp()
        self.fake_dir = self.home / "fake"
        self.bin = write_fake_claude(self.fake_dir)
        self.cfg = {"claude_bin": str(self.bin)}
        self.cwd = self.home / "proj"
        self.cwd.mkdir()

    def put(self, name, text):
        (self.fake_dir / name).write_text(text)

    def read(self, name):
        return (self.fake_dir / name).read_text()

    def call(self, **kw):
        return project(cwd=self.cwd, **kw)

    def test_success(self):
        self.put("reply.json", json.dumps(REPLY))
        call = self.call(env={"ORCHE_SESSION": "s-1"}, timeout=10.0)
        res = headless.run(call, "the prompt\nline 2", self.cfg)
        self.assertEqual((res.code, res.error, res.failure, res.killed), (0, None, None, False))
        self.assertEqual(res.data, REPLY)
        self.assertEqual((res.text, res.is_error, res.cost_usd, res.turns, res.usage),
                         ("hi", False, 0.25, 2, {"input_tokens": 3}))
        self.assertEqual(self.read("stdin.txt"), "the prompt\nline 2")
        self.assertEqual(self.read("argv.txt").splitlines(), headless.argv(call, self.cfg)[1:])
        self.assertEqual(self.read("cwd.txt").strip(), os.path.realpath(self.cwd))
        self.assertEqual(self.read("session.txt").strip(), "s-1")

    def test_session_only_from_call_env(self):
        os.environ["ORCHE_SESSION"] = "outer"
        self.put("reply.json", json.dumps(REPLY))
        headless.run(self.call(timeout=10.0), "", self.cfg)
        self.assertEqual(self.read("session.txt").strip(), "")

    def test_noise(self):
        obj = json.dumps(REPLY)
        cases = [
            ("banner before", "Update available: 9.9.10\n" + obj + "\n"),
            ("pretty printed", "banner\n" + json.dumps(REPLY, indent=2) + "\n"),
            ("trailing noise", obj + "\nhook said bye\n"),
            ("a list", json.dumps([{"type": "system"}, REPLY])),
        ]
        for name, out in cases:
            with self.subTest(name):
                self.put("reply.json", out)
                res = headless.run(self.call(timeout=10.0), "", self.cfg)
                self.assertEqual(res.data, REPLY)
                self.assertIsNone(res.failure)
        with self.subTest("no json"):
            self.put("reply.json", "just prose\n")
            res = headless.run(self.call(timeout=10.0), "", self.cfg)
            self.assertEqual((res.data, res.failure), ({}, "exit 0: just prose"))

    def test_nonzero_exit_with_stderr(self):
        self.put("action.sh", "echo first >&2; echo 'boom: it broke' >&2\n")
        self.put("exit", "3")
        res = headless.run(self.call(timeout=10.0), "", self.cfg)
        self.assertEqual((res.code, res.data, res.error, res.killed), (3, {}, None, False))
        self.assertEqual(res.failure, "exit 3: boom: it broke")

    def test_no_output(self):
        res = headless.run(self.call(timeout=10.0), "", self.cfg)
        self.assertEqual(res.failure, "exit 0: no output")

    def test_is_error_is_not_a_failure(self):
        reply = {"type": "result", "result": 5, "is_error": True, "cost_usd": 1.0}
        self.put("reply.json", json.dumps(reply))
        res = headless.run(self.call(timeout=10.0), "", self.cfg)
        self.assertIsNone(res.failure)
        self.assertTrue(res.is_error)
        self.assertEqual((res.text, res.cost_usd, res.turns, res.usage), ("", None, None, None))

    def test_non_utf8_is_replaced(self):
        (self.fake_dir / "reply.json").write_bytes(b'{"type": "result", "result": "a\xffb"}')
        res = headless.run(self.call(timeout=10.0), "", self.cfg)
        self.assertEqual(res.text, "a�b")

    def test_marker(self):
        tag = "u1@r-7"
        marker = headless.marker_path(tag)
        self.put("action.sh", f"cp '{marker}' '{self.fake_dir / 'seen.json'}'\n")
        self.put("reply.json", json.dumps(REPLY))
        res = headless.run(self.call(timeout=10.0, tag=tag), "", self.cfg)
        self.assertIsNone(res.failure)
        self.assertEqual(json.loads(self.read("seen.json")), {"mcpServers": {}})
        self.assertFalse(marker.exists())
        self.assertIn(f"--mcp-config\n{marker}\n", self.read("argv.txt"))

    def test_untagged_has_no_marker(self):
        self.put("reply.json", json.dumps(REPLY))
        headless.run(self.call(timeout=10.0), "", self.cfg)
        self.assertNotIn("--mcp-config", self.read("argv.txt"))
        mdir = headless.marker_dir()
        self.assertEqual(list(mdir.iterdir()) if mdir.exists() else [], [])

    def check_blank_cwd(self):
        self.put("reply.json", json.dumps(REPLY))
        res = headless.run(blank(timeout=10.0), "", self.cfg)
        self.assertIsNone(res.failure)
        cwd = self.read("cwd.txt").strip()
        home = os.path.realpath(str(Path.home()))
        self.assertTrue(os.path.basename(cwd).startswith("orche-blank-"), cwd)
        self.assertFalse(cwd == home or cwd.startswith(home + os.sep), cwd)
        self.assertFalse(os.path.exists(cwd))

    def test_blank_cwd(self):
        self.check_blank_cwd()

    def test_blank_cwd_when_tempdir_is_under_home(self):
        under = Path.home() / "orche-test-tmp-never-used"
        with mock.patch.object(headless.tempfile, "gettempdir", return_value=str(under)):
            self.check_blank_cwd()
        self.assertFalse(under.exists())

    def slow_cfg(self, ignore_term=False):
        """A fake claude that writes grandchild and pid (atomically, pid last) before reading stdin
        or sleeping, so on_start can wait for both before the timeout clock starts."""
        script = self.fake_dir / "slow-claude"
        script.write_text("#!/bin/sh\n" + 'd=$(dirname "$0")\n' + ("trap '' TERM\n" if ignore_term else "")
                          + 'sleep 30 & echo $! > "$d/grandchild.tmp"; mv "$d/grandchild.tmp" "$d/grandchild"\n'
                          + 'echo $$ > "$d/pid.tmp"; mv "$d/pid.tmp" "$d/pid"\n'
                          + "sleep 30\n")
        script.chmod(0o755)
        return {"claude_bin": str(script)}

    def wait_for_pid(self, pid):
        """on_start: block (up to 5 s) until the fake wrote its files; run() starts the timeout after."""
        deadline = time.monotonic() + 5
        while not (self.fake_dir / "pid").exists():
            self.assertLess(time.monotonic(), deadline, "the fake never wrote its pid file")
            time.sleep(0.02)
        self.assertEqual(int(self.read("pid")), pid)

    def test_project_timeout_kills_the_group(self):
        with mock.patch.object(headless, "KILL_WAIT_S", 0.3):
            res = headless.run(self.call(timeout=0.5), "", self.slow_cfg(ignore_term=True),
                               on_start=self.wait_for_pid)
        self.assertEqual(res.error, "timeout after 0.5s")
        self.assertEqual(res.failure, "timeout after 0.5s")
        self.assertFalse(res.killed)
        self.assertEqual(res.code, -9)
        self.assertTrue(wait_dead(int(self.read("grandchild"))))

    def test_blank_timeout_is_quick(self):
        real_killpg = headless._killpg
        for timeout, error in ((0.5, "timeout after 0.5s"), (1, "timeout after 1s")):
            with self.subTest(timeout=timeout):
                (self.fake_dir / "pid").unlink(missing_ok=True)
                sent = []

                def killpg(pgid, sig):
                    sent.append(sig)
                    real_killpg(pgid, sig)

                with mock.patch.object(headless, "_killpg", killpg):
                    res = headless.run(blank(timeout=timeout), "", self.slow_cfg(), on_start=self.wait_for_pid)
                self.assertEqual((res.error, res.failure), (error, error))
                self.assertIn(signal.SIGKILL, sent)
                self.assertNotIn(signal.SIGTERM, sent)
                self.assertTrue(wait_dead(int(self.read("pid"))))
                self.assertTrue(wait_dead(int(self.read("grandchild"))))

    def test_should_stop(self):
        self.put("sleep", "30")
        pids = []
        with mock.patch.object(headless, "POLL_S", 0.05):
            res = headless.run(self.call(timeout=20.0), "", self.cfg, should_stop=lambda: True,
                               on_start=pids.append)
        self.assertTrue(res.killed)
        self.assertEqual((res.error, res.failure), ("stopped by STOP marker", "stopped by STOP marker"))
        self.assertTrue(wait_dead(pids[0]))

    def test_should_stop_that_raises_is_logged_and_polled_again(self):
        self.put("sleep", "30")
        calls = []

        def check():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk gone")
            return True

        with mock.patch.object(headless, "POLL_S", 0.05):
            res = headless.run(self.call(timeout=20.0), "", self.cfg, should_stop=check)
        self.assertTrue(res.killed)
        self.assertGreaterEqual(len(calls), 2)
        hooks_log = (self.home / "logs" / "hooks.log").read_text()
        self.assertIn("runner: STOP marker check failed: disk gone", hooks_log)

    def test_missing_binary(self):
        missing = str(self.home / "no-such-claude")
        res = headless.run(self.call(timeout=10.0), "", {"claude_bin": missing})
        self.assertEqual((res.code, res.data, res.killed), (127, {}, False))
        self.assertTrue(res.error.startswith(f"cannot start {missing}: "), res.error)
        self.assertEqual(res.failure, res.error)

    def test_on_start_gets_the_pid(self):
        self.put("sleep", "0.3")
        self.put("reply.json", json.dumps(REPLY))
        seen = []
        res = headless.run(self.call(timeout=10.0), "", self.cfg, on_start=seen.append)
        self.assertIsNone(res.failure)
        self.assertEqual(seen, [int(self.read("pid"))])

    def test_on_start_interrupt_kills_claude(self):
        self.put("sleep", "30")

        pids = []

        def interrupt(pid):
            pids.append(pid)
            raise KeyboardInterrupt

        with mock.patch.object(headless, "KILL_WAIT_S", 0.3), self.assertRaises(KeyboardInterrupt):
            headless.run(self.call(timeout=20.0), "", self.cfg, on_start=interrupt)
        self.assertTrue(wait_dead(pids[0]))

    def test_watcher_start_failure_kills_claude(self):
        self.put("sleep", "30")
        real_start = threading.Thread.start

        def start(thread):
            if getattr(thread, "_target", None) is headless._watch:
                raise RuntimeError("can't start new thread")
            return real_start(thread)

        pids = []
        with mock.patch.object(headless.threading.Thread, "start", start), \
                mock.patch.object(headless, "KILL_WAIT_S", 0.3):
            with self.assertRaises(RuntimeError) as cm:
                headless.run(self.call(timeout=20.0), "", self.cfg, should_stop=lambda: False,
                             on_start=pids.append)
        self.assertEqual(str(cm.exception), "can't start new thread")
        self.assertTrue(wait_dead(pids[0]))


class StopTest(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.fake_dir = self.home / "fake"
        self.cfg = {"claude_bin": str(write_fake_claude(self.fake_dir))}
        (self.fake_dir / "sleep").write_text("30")
        self.cwd = self.home / "proj"
        self.cwd.mkdir()

    def test_sigterm_kills_the_call(self):
        def sigterm():   # to the main thread: it wakes from communicate() and runs the handler
            signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)

        for name in ("from on_start (before _attach)", "from should_stop (after _attach)"):
            with self.subTest(name):
                pids, sent = [], []

                def on_start(pid):
                    pids.append(pid)
                    if name.startswith("from on_start"):
                        sigterm()

                def should_stop():
                    if name.startswith("from should_stop") and not sent:
                        sent.append(1)
                        sigterm()
                    return False

                with headless.Stop() as stop, mock.patch.object(headless, "POLL_S", 0.05):
                    res = headless.run(project(cwd=self.cwd, timeout=20.0), "", self.cfg, stop=stop,
                                       on_start=on_start, should_stop=should_stop)
                self.assertTrue(res.killed)
                self.assertEqual(res.error, "terminated by SIGTERM")
                self.assertEqual(stop.reason, "terminated by SIGTERM")
                self.assertEqual(stop.signum, signal.SIGTERM)
                self.assertTrue(wait_dead(pids[0]))

    def test_signal_before_start_kills_at_once(self):
        with headless.Stop() as stop:
            stop.signum = signal.SIGTERM
            start = time.monotonic()
            res = headless.run(blank(timeout=20.0), "", self.cfg, stop=stop)
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(res.killed)
        self.assertEqual(res.error, "terminated by SIGTERM")

    def test_previous_handler_restored(self):
        def mine(signum, frame):
            pass

        previous = signal.signal(signal.SIGTERM, mine)
        try:
            with headless.Stop() as stop:
                self.assertEqual(signal.getsignal(signal.SIGTERM), stop._on_signal)
            self.assertIs(signal.getsignal(signal.SIGTERM), mine)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_not_main_thread_installs_nothing(self):
        before = signal.getsignal(signal.SIGTERM)
        seen = []

        def body():
            with headless.Stop() as stop:
                seen.append((signal.getsignal(signal.SIGTERM), stop._installed))

        thread = threading.Thread(target=body)
        thread.start()
        thread.join()
        self.assertEqual(seen, [(before, False)])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


if __name__ == "__main__":
    unittest.main()
