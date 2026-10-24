"""Codex adapter (design C11 §5.2-5.6, §7.2, A.1): argv, env, parse, cost, check, run() on a fake."""
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from support import TempHomeCase, fake_codex

from orche import codex, headless, jevkey, oauth, paths, procs
from orche.codex import Call

WT = Path("/w/wt")
OUT = Path("/h/runner/codex/t1.md")
ROOTS = ("/w/repo/.git", "/h", "/w/repo/.orche/units")
PRICES = {"gpt-6.1-sol": [2, 0.1, 10], "gpt-6-astra": [10, 1, 50], "codex": [10, 1, 50]}
CFG = {"models": {"prices": PRICES}}


def call(**kw):
    return Call(**{"model": "codex", "effort": "medium", "timeout": 600.0, "cwd": WT, "writable": ROOTS,
                   "out": OUT, **kw})


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child of this process counts as dead
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def wait_dead(pid, seconds=3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def jl(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def turn(inp=100, cached=0, out=10, reasoning=0):
    return {"type": "turn.completed", "usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                                "output_tokens": out, "reasoning_output_tokens": reasoning}}


def message(text):
    return {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": text}}


class ArgvTest(unittest.TestCase):
    def test_workspace_write_default_model(self):
        self.assertEqual(codex.argv(call(), {}), [
            "codex", "exec", "--json", "--color", "never", "--skip-git-repo-check", "--ignore-user-config",
            "-C", "/w/wt", "-s", "workspace-write",
            "-c", 'sandbox_workspace_write.writable_roots=["/w/repo/.git","/h","/w/repo/.orche/units"]',
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", 'approval_policy="never"', "-c", 'shell_environment_policy.inherit="all"',
            "-c", 'model_reasoning_effort="medium"', "-o", "/h/runner/codex/t1.md", "-"])

    def test_danger_full_access_with_model_and_bin(self):
        cfg = {"models": {"codex_bin": "/opt/codex"}}
        self.assertEqual(codex.argv(call(model="codex:gpt-6.1-sol", effort="high", sandbox="danger-full-access"),
                                    cfg), [
            "/opt/codex", "exec", "--json", "--color", "never", "--skip-git-repo-check", "--ignore-user-config",
            "-C", "/w/wt", "-s", "danger-full-access",
            "-c", 'approval_policy="never"', "-c", 'shell_environment_policy.inherit="all"',
            "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "-o", "/h/runner/codex/t1.md", "-"])

    def test_writable_roots_with_space_and_quote_round_trip(self):
        roots = ('/a b/.git', '/h "q"')
        entry = next(a for a in codex.argv(call(writable=roots), {}) if a.startswith("sandbox_workspace_write.w"))
        self.assertEqual(json.loads(entry.split("=", 1)[1]), list(roots))

    def test_invalid_calls_raise(self):
        for bad in ({"model": "claude-opus-5-5"}, {"model": "codex:"}, {"effort": "max"},
                    {"sandbox": "read-only"}, {"timeout": 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                codex.argv(call(**bad), {})


class EnvTest(TempHomeCase):
    def test_no_claude_auth_codex_vars_pass_tag_set(self):
        jevkey.set_key("stored-tok", account=oauth.ENV)
        with mock.patch.dict(os.environ, {oauth.ENV: "inherited-tok", "CODEX_HOME": "/c/home",
                                          "OPENAI_API_KEY": "k", procs.ENV: "stale"}):
            env = codex.env(call(env={"ORCHE_SESSION": "s1", "ORCHE_RUNNER": "1"}, tag="t1"), {})
        self.assertFalse([k for k in env if k.startswith("CLAUDE")])
        self.assertNotIn("stored-tok", env.values())
        self.assertEqual((env["CODEX_HOME"], env["OPENAI_API_KEY"]), ("/c/home", "k"))
        self.assertEqual((env["ORCHE_SESSION"], env["ORCHE_RUNNER"], env[procs.ENV]), ("s1", "1", "t1"))
        self.assertTrue(env["PATH"].startswith(f"{paths.PLUGIN_ROOT / 'bin'}{os.pathsep}"))

    def test_untagged_call_has_no_run_id(self):
        self.assertNotIn(procs.ENV, codex.env(call(env={procs.ENV: "x"}), {}))

    def test_claude_env_still_injects(self):
        jevkey.set_key("stored-tok", account=oauth.ENV)
        self.assertEqual(headless.base_env({})[oauth.ENV], "stored-tok")
        self.assertNotIn(oauth.ENV, headless.base_env({}, claude_auth=False))


class ParseTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "o.md"

    def parse(self, stdout, model="codex:gpt-6.1-sol"):
        return codex.parse(stdout, self.out, model, CFG)

    def test_success_from_out_file_with_two_turns_summed(self):
        self.out.write_text("FINAL")
        data = self.parse("not json\n" + jl({"type": "thread.started"}, turn(100, 20, 10, 5), message("m1"),
                                            turn(50, 0, 5), message("m2")) + "[1]\n")
        self.assertEqual((data["type"], data["vendor"], data["result"], data["is_error"], data["subtype"],
                          data["stop_reason"], data["num_turns"]),
                         ("result", "codex", "FINAL", False, "success", "completed", 2))
        self.assertEqual(data["usage"], {"input_tokens": 150, "cached_input_tokens": 20, "output_tokens": 15,
                                         "reasoning_output_tokens": 5})
        self.assertEqual(data["total_cost_usd"], round((130 * 2 + 20 * 0.1 + 20 * 10) / 1e6, 4))
        self.assertIsNone(data["error_text"])

    def test_last_agent_message_when_out_file_missing_or_blank(self):
        self.assertEqual(self.parse(jl(message("a"), turn(), message("b")))["result"], "b")
        self.out.write_text("  \n")
        self.assertEqual(self.parse(jl(message("a"), turn()))["result"], "a")

    def test_turn_failed(self):
        data = self.parse(jl(turn(), {"type": "turn.failed", "error": {"message": "x" * 500}}))
        self.assertEqual((data["subtype"], data["stop_reason"], data["is_error"]),
                         ("error_turn_failed", "error_turn_failed", True))
        self.assertEqual(len(data["error_text"]), 300)

    def test_error_event_without_completed_turn(self):
        data = self.parse(jl({"type": "error", "message": "stream disconnected"}))
        self.assertEqual((data["subtype"], data["error_text"], data["num_turns"], data["total_cost_usd"]),
                         ("error_event", "stream disconnected", 0, None))

    def test_no_final_message(self):
        data = self.parse(jl({"type": "thread.started"}, turn()))
        self.assertEqual((data["subtype"], data["result"], data["is_error"]), ("error_no_message", "", True))

    def test_no_events_is_empty(self):
        self.assertEqual(self.parse(""), {})
        self.assertEqual(self.parse("junk\n[1,2]\n\"s\"\n"), {})


class CostTest(unittest.TestCase):
    def test_listed_id(self):
        usage = {"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 100_000,
                 "reasoning_output_tokens": 100_000}
        self.assertEqual(codex.cost(usage, "codex:gpt-6.1-sol", CFG), 1.2 + 0.04 + 2.0)

    def test_default_and_unknown_ids_take_the_codex_rate(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        self.assertEqual(codex.cost(usage, "codex", CFG), 10.0)
        self.assertEqual(codex.cost(usage, "codex:gpt-9", CFG), 10.0)

    def test_cached_above_input_counts_input_as_uncached(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 500}
        self.assertEqual(codex.cost(usage, "codex:gpt-6-astra", {"models": {"prices": PRICES}}),
                         round((100 * 10 + 500 * 1) / 1e6, 4))

    def test_no_usage_or_no_price(self):
        self.assertIsNone(codex.cost({}, "codex", CFG))
        self.assertIsNone(codex.cost(None, "codex", CFG))
        self.assertIsNone(codex.cost({"input_tokens": 1}, "codex", {}))

    def test_a_malformed_price_counts_as_unlisted_and_is_logged_once(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        lines = []
        for bad in ("cheap", [1, 2], [1, "x", 3], [True, 0, 0], {"in": 1}, [-1, 0, 0]):
            with self.subTest(bad=bad), mock.patch.object(codex, "_MALFORMED", set()), \
                    mock.patch("orche.log.log", lambda name, line: lines.append((name, line))):
                lines.clear()
                cfg = {"models": {"prices": {"gpt-x": bad, "codex": [10.0, 1.0, 50.0]}}}
                self.assertEqual(codex.cost(usage, "codex:gpt-x", cfg), 10.0)
                self.assertEqual(codex.cost(usage, "codex:gpt-x", cfg), 10.0)
                self.assertEqual(lines, [("events", "codex prices malformed model=gpt-x")])
        with mock.patch.object(codex, "_MALFORMED", set()), mock.patch("orche.log.log", lambda *a: None):
            self.assertIsNone(codex.cost(usage, "codex", {"models": {"prices": {"codex": "x"}}}))


class RunTest(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.home / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit",
                        "-q", "--allow-empty", "-m", "init"], check=True)
        self.fake = self.home / "fake"
        self.out = self.home / "runner" / "codex" / "t1.md"

    def run_fake(self, spec, **kw):
        found = fake_codex(self.fake, spec)
        cfg = {"models": {"codex_bin": str(found), "prices": PRICES}}
        run_kw = {k: kw.pop(k) for k in ("stop", "should_stop", "on_start") if k in kw}
        c = call(cwd=self.repo, out=self.out, writable=(str(self.repo / ".git"),), tag="t1", **kw)
        return codex.run(c, "PROMPT", cfg, **run_kw)

    def record(self):
        (path,) = (self.fake / "calls").iterdir()
        return json.loads(path.read_text())

    def pids(self):
        return json.loads((self.fake / "pids.json").read_text())

    def wait_for_pids(self, _pid):
        deadline = time.monotonic() + 2
        while not (self.fake / "pids.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_no_claude_or_anthropic_auth_reaches_codex(self):
        jevkey.set_key("stored-canary", account=oauth.ENV)
        canaries = {oauth.ENV: "inherited-canary", "ANTHROPIC_API_KEY": "canary-a",
                    "ANTHROPIC_AUTH_TOKEN": "canary-b", "CLAUDE_CODE_USE_BEDROCK": "1"}
        with mock.patch.dict(os.environ, canaries):
            res = self.run_fake({"final": "DONE", "events": [turn(), message("m")]})
        self.assertEqual(res.code, 0)
        rec = self.record()
        self.assertEqual((rec["claude_names"], rec["anthropic_names"], rec["env"][oauth.ENV]), ([], [], False))
        self.assertNotIn("canary", json.dumps(rec))

    def test_success_commits_and_removes_out_file(self):
        jevkey.set_key("stored-tok", account=oauth.ENV)
        with mock.patch.dict(os.environ, {oauth.ENV: "inherited-tok"}):
            res = self.run_fake({"files": {"a.txt": "A"}, "commit": "work", "final": "DONE",
                                 "events": [{"type": "thread.started"}, turn(), message("m")]},
                                env={"ORCHE_SESSION": "s1", "ORCHE_RUNNER": "1"})
        self.assertEqual((res.code, res.text, res.is_error, res.failure, res.killed),
                         (0, "DONE", False, None, False))
        self.assertEqual(res.data["vendor"], "codex")
        self.assertFalse(self.out.exists())
        self.assertTrue(self.out.parent.is_dir())
        log = subprocess.run(["git", "-C", str(self.repo), "log", "--format=%s", "-1"], capture_output=True,
                             text=True).stdout.strip()
        self.assertEqual(log, "work")
        rec = self.record()
        self.assertEqual(rec["stdin_len"], len("PROMPT"))
        self.assertEqual(rec["argv"][1:], codex.argv(call(cwd=self.repo, out=self.out,
                                                          writable=(str(self.repo / ".git"),)), {})[1:])
        self.assertEqual(rec["env"], {oauth.ENV: False, "ORCHE_SESSION": True, "ORCHE_RUNNER": True,
                                      procs.ENV: True, "CODEX_HOME": False})
        self.assertEqual(rec["run_id"], "t1")

    def test_no_events_is_a_failure_with_the_tail(self):
        res = self.run_fake({"code": 3, "events": ["boom: not logged in"]})
        self.assertEqual((res.code, res.data), (3, {}))
        self.assertEqual(res.failure, "codex exited 3: boom: not logged in")

    def test_missing_bin(self):
        c = call(cwd=self.repo, out=self.out)
        res = codex.run(c, "", {"models": {"codex_bin": str(self.home / "none")}})
        self.assertEqual(res.code, 127)
        self.assertIn("cannot start", res.failure)

    def test_timeout_kills_the_group(self):
        with mock.patch.object(headless, "KILL_WAIT_S", 0.3):
            res = self.run_fake({"sleep": 30, "ignore_term": True, "child": True, "final": "partial"},
                                timeout=0.5, on_start=self.wait_for_pids)
        self.assertEqual(res.error, "timeout after 0.5s")
        self.assertEqual(res.failure, "timeout after 0.5s")
        pids = self.pids()
        self.assertTrue(wait_dead(pids["self"]))
        self.assertTrue(wait_dead(pids["child"]))
        self.assertFalse(self.out.exists())

    def test_should_stop_kills_and_tag_sweep_reaches_escaped_child(self):
        with mock.patch.object(headless, "POLL_S", 0.1), mock.patch.object(headless, "KILL_WAIT_S", 0.3):
            res = self.run_fake({"sleep": 30, "escaped": True, "child": True}, timeout=20.0,
                                should_stop=lambda: (self.fake / "pids.json").exists())
        self.assertTrue(res.killed)
        self.assertEqual(res.error, "stopped by STOP marker")
        pids = self.pids()
        for key in ("self", "child", "escaped"):
            self.assertTrue(wait_dead(pids[key]), key)


if __name__ == "__main__":
    unittest.main()
