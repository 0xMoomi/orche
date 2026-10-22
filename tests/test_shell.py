import io
import json
import os
import unittest
from contextlib import redirect_stderr

import support
from orche import orca, shell


class FakeRunner:
    """Records argv and returns queued Results (or a default success)."""

    def __init__(self, *results):
        self.calls = []
        self.results = list(results)

    def __call__(self, argv, *, timeout, input):
        self.calls.append({"argv": argv, "timeout": timeout, "input": input})
        if self.results:
            code, out, err = self.results.pop(0)
            return shell.Result(code, out, err, argv)
        return shell.Result(0, "", "", argv)


class ShellCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self._orig = shell.RUNNER
        self.fake = FakeRunner()
        shell.RUNNER = self.fake

    def tearDown(self):
        shell.RUNNER = self._orig
        super().tearDown()

    def queue(self, *results):
        self.fake.results.extend(results)


class RunTest(ShellCase):
    def test_run_uses_runner(self):
        self.queue((0, "hi\n", ""))
        res = shell.run(["git", "status", 3], timeout=5, input="x")
        self.assertEqual((res.code, res.out, res.argv), (0, "hi\n", ["git", "status", "3"]))
        self.assertTrue(res.ok)
        self.assertEqual(self.fake.calls, [{"argv": ["git", "status", "3"], "timeout": 5, "input": "x"}])

    def test_dry_run_skips_runner(self):
        res = shell.run(["git", "push"], dry_run=True)
        self.assertEqual((res.code, res.argv), (0, ["git", "push"]))
        self.assertEqual(self.fake.calls, [])

    def test_check_raises_on_failure(self):
        self.queue((1, "", "boom"))
        with self.assertRaises(shell.ShellError) as ctx:
            shell.run(["false"], check=True)
        self.assertEqual(ctx.exception.result.code, 1)
        self.queue((1, "", "boom"))
        self.assertEqual(shell.run(["false"]).code, 1)

    def test_default_timeout(self):
        shell.run(["true"])
        self.assertEqual(self.fake.calls[0]["timeout"], 20)


class SubprocessRunnerTest(unittest.TestCase):
    def test_missing_binary_is_127(self):
        res = shell._subprocess_runner(["orche-definitely-missing-binary"], timeout=5, input=None)
        self.assertEqual(res.code, 127)

    def test_which(self):
        self.assertIsNone(shell.which("orche-definitely-missing-binary"))


def ok(result):
    return (0, json.dumps({"id": "x", "ok": True, "result": result}), "")


def fail(code, message):
    return (1, json.dumps({"id": "x", "ok": False, "error": {"code": code, "message": message}}), "")


TERMS = {"terminals": [{"handle": "term_1", "title": "div-acme-build"},
                       {"handle": "term_2", "title": "main-acme"}]}


class OrcaAdapterTest(ShellCase):
    def test_call_returns_result_and_appends_json(self):
        self.queue(ok({"repos": []}))
        self.assertEqual(orca.repo_list(), {"repos": []})
        self.assertEqual(self.fake.calls[0]["argv"], ["orca", "repo", "list", "--json"])

    def test_orca_bin_from_config(self):
        (self.home / "config.json").write_text(json.dumps({"orca_bin": "/opt/orca"}))
        self.queue(ok({}))
        orca.status()
        self.assertEqual(self.fake.calls[0]["argv"], ["/opt/orca", "status", "--json"])

    def test_error_envelope_raises(self):
        self.queue(fail("invalid_argument", "bad flag"))
        with self.assertRaises(orca.OrcaError) as ctx:
            orca.worktree("list")
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_unparseable_output_raises(self):
        self.queue((127, "", "not found"))
        with self.assertRaises(orca.OrcaError) as ctx:
            orca.status()
        self.assertEqual(ctx.exception.code, "bad_output")

    def test_unsupported_command_rejected(self):
        with self.assertRaises(orca.OrcaError):
            orca.terminal("resize")
        with self.assertRaises(orca.OrcaError):
            orca.call("search", None)
        self.assertEqual(self.fake.calls, [])

    def test_dry_run_prints_argv_and_skips_runner(self):
        err = io.StringIO()
        with redirect_stderr(err):
            res = orca.worktree("rm", "--worktree", "id:r::/p", dry_run=True)
        self.assertTrue(res["dryRun"])
        self.assertIn("orca worktree rm --worktree id:r::/p --json", err.getvalue())
        self.assertEqual(self.fake.calls, [])

    def test_find_terminal_by_title(self):
        self.queue(ok(TERMS), ok(TERMS))
        self.assertEqual(orca.find_terminal_by_title("main-acme")["handle"], "term_2")
        self.assertIsNone(orca.find_terminal_by_title("main"))

    def test_hook_budget_defaults_to_three_seconds(self):
        self.assertEqual(orca.DEFAULT_HOOK_BUDGET_S, 3.0)
        os.environ.pop("ORCHE_HOOK_BUDGET_S", None)
        self.assertAlmostEqual(orca.hook_deadline(100.0), 103.0)
        os.environ["ORCHE_HOOK_BUDGET_S"] = "0.5"
        self.assertAlmostEqual(orca.hook_deadline(100.0), 100.5)
        os.environ["ORCHE_HOOK_BUDGET_S"] = "junk"
        self.assertAlmostEqual(orca.hook_deadline(100.0), 103.0)


if __name__ == "__main__":
    unittest.main()
