"""Verification ledger: record/check/summary/export/grade and the completion-gate API."""
import io
import json
import os
import stat
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import ledger, registry, shell
from test_registry import load_cli

SHA_A = "a" * 8 + "1" * 32
SHA_B = "b" * 40
HEAD = "c0ffee" + "0" * 34


def make_registry(root: Path) -> dict:
    reg = registry.empty()
    reg["projects"]["acme"] = {
        "repo_id": "repo-acme", "path": str(root / "acme"), "stage": "building", "aliases": ["ac"],
        "main_session": "main-acme", "liaison_session": "liaison-acme", "card": "", "standing": "",
        "divisions": {"build": {
            "worktree": f"repo-acme::{root / 'acme-build'}", "branch": "div/build",
            "session": "div-acme-build", "model": "opus", "status": "active",
            "last_activity": "2026-10-16T00:00:00Z",
            "features": [{"name": "checkout", "worktree": f"repo-acme::{root / 'acme-checkout'}",
                          "branch": "feat/checkout", "session": "feat-acme-checkout", "status": "active",
                          "created": "2026-10-16", "last_activity": "2026-10-16", "reason": "x",
                          "scope": ["src/**"]}]}},
    }
    for name in ("acme", "acme-build", "acme-checkout"):
        (root / name).mkdir(parents=True, exist_ok=True)
    registry.save(reg)
    return reg


class LedgerCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.reg = make_registry(self.home / "wt")
        self.calls = []
        self.git = {}
        self._orig_runner, shell.RUNNER = shell.RUNNER, self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", self._orig_runner)
        self._old_session = os.environ.pop("ORCHE_SESSION", None)
        self.addCleanup(self._restore_session)

    def _restore_session(self):
        os.environ.pop("ORCHE_SESSION", None)
        if self._old_session is not None:
            os.environ["ORCHE_SESSION"] = self._old_session

    def fake_runner(self, argv, *, timeout, input):
        self.calls.append(argv)
        for key, out in self.git.items():
            if key in " ".join(argv):
                return shell.Result(0, out, "", argv)
        return shell.Result(128, "", "fatal: not a git repository", argv)

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.cli.main(list(argv))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def record(self, sha=SHA_A, verdict="unit-test-verified", pr="12", verifier="div-acme-build", *extra):
        return self.orche("ledger", "record", "--proj", "acme", "--pr", pr, "--sha", sha, "--verdict", verdict,
                          "--evidence", "logs/verify.txt", "--verifier", verifier, *extra)

    def check(self, *extra):
        return self.orche("ledger", "check", "--proj", "acme", *extra)

    def canonical(self):
        return self.home / "ledger" / "acme.tsv"


class RecordCheckTest(LedgerCase):
    def test_round_trip_verified(self):
        code, out, err = self.record()
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(f"12\t{SHA_A}\tunit-test-verified\tlogs/verify.txt\tdiv-acme-build\t20"))
        lines = self.canonical().read_text().splitlines()
        self.assertEqual(lines[0], "pr\tsha\tverdict\tevidence\tverifier\tts")
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[1].split("\t")[5], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotEqual(stat.S_IMODE(self.canonical().stat().st_mode), 0o600)
        self.assertEqual(self.check("--sha", SHA_A), (0, "VERIFIED unit-test-verified\n", ""))
        self.assertEqual(self.check("--sha", SHA_A, "--min", "unit-test-verified")[:2],
                         (0, "VERIFIED unit-test-verified\n"))
        self.record(SHA_B)
        self.assertEqual(len(self.canonical().read_text().splitlines()), 3)

    def test_unknown_sha_not_verified(self):
        self.record()
        self.assertEqual(self.check("--sha", SHA_B)[:2], (2, "NOT-VERIFIED\n"))
        self.assertEqual(self.check("--sha", "nothex!")[0], 1)

    def test_below_min(self):
        self.record(verdict="type-check-only")
        self.assertEqual(self.check("--sha", SHA_A, "--min", "unit-test-verified")[:2],
                         (2, "BELOW-MIN type-check-only\n"))
        self.assertEqual(self.check("--sha", SHA_A)[:2], (0, "VERIFIED type-check-only\n"))

    def test_newer_row_wins_and_failures_never_satisfy(self):
        self.record(verdict="live-ui-verified")
        self.record(verdict="verifier-failed")
        self.assertEqual(self.check("--sha", SHA_A)[:2], (2, "BELOW-MIN verifier-failed\n"))
        self.record(verdict="unit-test-verified")
        self.assertEqual(self.check("--sha", SHA_A)[:2], (0, "VERIFIED unit-test-verified\n"))

    def test_short_sha_prefix_and_ambiguity(self):
        other = "a" * 8 + "2" * 32
        self.record(SHA_A)
        self.assertEqual(self.check("--sha", SHA_A[:7])[:2], (0, "VERIFIED unit-test-verified\n"))
        self.record(other, "type-check-only", "13")
        code, out, _ = self.check("--sha", "aaaaaaa")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("AMBIGUOUS aaaaaaa"), out)
        self.assertEqual(self.check("--sha", "aaaaaaa", "--pr", "13")[:2], (0, "VERIFIED type-check-only\n"))

    def test_new_head_on_pr_voids_prior_rows(self):
        self.record(SHA_A, pr="12")
        self.record(SHA_B, "type-check-only", pr="12")
        code, out, _ = self.check("--sha", SHA_A, "--pr", "12")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("NOT-VERIFIED\n  (superseded by newer head bbbbbbbbbbbb on PR 12)"), out)
        self.assertEqual(self.check("--sha", SHA_A)[:2], (0, "VERIFIED unit-test-verified\n"))

    def test_pr_none_and_validation(self):
        self.assertEqual(self.record(pr="none")[0], 0)
        self.assertEqual(self.check("--sha", SHA_A, "--pr", "none")[:2], (0, "VERIFIED unit-test-verified\n"))
        self.assertEqual(self.record(pr="twelve")[0], 1)
        self.assertEqual(self.record(sha="abc")[0], 1)
        self.assertEqual(self.record(sha="g" * 40)[0], 1)
        self.assertEqual(self.record(verdict="looks-good")[0], 1)
        self.assertEqual(len(self.canonical().read_text().splitlines()), 2)

    def test_record_refuses_verifier_equal_to_producer(self):
        code, out, err = self.record(SHA_A, "unit-test-verified", "12", "feat-acme-checkout",
                                     "--producer", "feat-acme-checkout")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("writer != judge", err)
        os.environ["ORCHE_SESSION"] = "div-acme-build"
        self.assertEqual(self.record()[0], 3)
        self.assertFalse(self.canonical().exists())
        self.assertEqual(self.record(SHA_A, "unit-test-verified", "12", "div-acme-build",
                                     "--producer", "feat-acme-checkout")[0], 0)

    def test_record_json_and_dry_run(self):
        code, out, _ = self.orche("--json", "ledger", "record", "--proj", "ac", "--pr", "7", "--sha", SHA_B,
                                  "--verdict", "live-ui-verified", "--evidence", "https://ci/x\tlog",
                                  "--verifier", "verify-script", "--dry-run")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["project"], data["dry_run"]), ("acme", True))
        self.assertEqual(data["row"]["evidence"], "https://ci/x log")
        self.assertFalse(self.canonical().exists())

    def test_unknown_project_and_cwd_resolution(self):
        self.assertEqual(self.orche("ledger", "check", "--proj", "nope", "--sha", SHA_A)[0], 2)
        self.record()
        old = os.getcwd()
        os.chdir(self.home / "wt" / "acme-checkout")
        try:
            self.git = {"rev-parse HEAD": SHA_A + "\n"}
            self.assertEqual(self.orche("ledger", "check")[:2], (0, "VERIFIED unit-test-verified\n"))
            self.git = {}
            self.assertEqual(self.orche("ledger", "check")[0], 1)
        finally:
            os.chdir(old)

    def test_gate_api(self):
        self.record(SHA_A[:12], "type-check-only")
        self.assertEqual(ledger.row_for_sha("acme", SHA_A)["verdict"], "type-check-only")
        self.assertIsNone(ledger.row_for_sha("acme", SHA_B))
        self.assertIsNone(ledger.row_for_sha("other", SHA_A))
        self.assertTrue(ledger.satisfies("live-ui-verified", "unit-test-verified"))
        self.assertFalse(ledger.satisfies("type-check-only", "unit-test-verified"))
        self.assertFalse(ledger.satisfies("verifier-blocked", "type-check-only"))


class SummaryExportTest(LedgerCase):
    def write_rows(self, *rows):
        path = self.canonical()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n" + "".join("\t".join(r) + "\n" for r in rows))

    def test_export_sorted_by_ts(self):
        self.write_rows(("2", SHA_B, "type-check-only", "e2", "v", "2026-10-16T03:00:00Z"),
                        ("1", SHA_A, "unit-test-verified", "e1", "v", "2026-10-16T01:00:00Z"),
                        ("3", HEAD, "verifier-blocked", "e3", "v", "2026-10-16T02:00:00Z"))
        repo = self.home / "wt" / "acme"
        code, out, err = self.orche("ledger", "export", "--repo", str(repo))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"3 rows -> {repo / 'docs' / 'LEDGER.tsv'}\n")
        lines = (repo / "docs" / "LEDGER.tsv").read_text().splitlines()
        self.assertEqual(lines[0], ledger.HEADER)
        self.assertEqual([l.split("\t")[0] for l in lines[1:]], ["1", "3", "2"])
        self.write_rows(("1", SHA_A, "unit-test-verified", "e1", "v", "2026-10-16T01:00:00Z"))
        self.assertEqual(self.orche("ledger", "export", "--repo", str(repo))[1].split()[0], "1")
        self.assertEqual(len((repo / "docs" / "LEDGER.tsv").read_text().splitlines()), 2)

    def test_summary_counts_unverified_heads_and_last_rows(self):
        self.write_rows(*[(str(i), f"{i:07x}" + "0" * 33, "type-check-only", "e", "v", f"2026-10-16T00:00:{i:02d}Z")
                          for i in range(11)])
        self.record(SHA_A, "live-ui-verified")
        self.record(SHA_A, "verifier-failed")
        self.git = {"for-each-ref": f"feat/checkout\t{SHA_A}\nfeat/search\t{HEAD}\nfeat/ok\t{'0' * 7}{'0' * 33}\n"}
        code, out, err = self.orche("--json", "ledger", "summary", "--proj", "acme")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["rows"], 13)
        self.assertEqual(data["counts"]["type-check-only"], 11)
        self.assertEqual(data["counts"]["verifier-failed"], 1)
        self.assertEqual(data["counts"]["live-ui-verified"], 0)
        self.assertEqual([u["branch"] for u in data["unverified_heads"]], ["feat/checkout", "feat/search"])
        self.assertEqual(len(data["last"]), 10)
        self.assertEqual(data["last"][-1]["verdict"], "verifier-failed")
        argv = next(a for a in self.calls if "for-each-ref" in a)
        self.assertEqual(argv[1:3], ["-C", str(self.home / "wt" / "acme-build")])
        code, out, _ = self.orche("ledger", "summary", "--proj", "acme")
        self.assertIn("unverified feat/* heads: 2", out)


class GradeTest(LedgerCase):
    def test_grade_increments_and_blocks_at_three(self):
        rubric = self.home / "RUBRIC.md"
        rubric.write_text("- [ ] tests pass\n")
        self.git = {"diff --stat": " src/a.py | 2 +-\n 1 file changed\n", "diff --name-only": "src/a.py\n"}
        for n in (1, 2, 3):
            code, out, err = self.orche("ledger", "grade", "--unit", "acme-coupon-20261016-1",
                                        "--rubric", str(rubric), "--diff", "main..feat/checkout")
            self.assertEqual((code, err), (0, ""))
            package = self.home / "grader" / f"acme-coupon-20261016-1-{n}.md"
            self.assertEqual(out, f"iteration {n}/3\n{package}\n")
            text = package.read_text()
            self.assertIn("- [ ] tests pass", text)
            self.assertIn("src/a.py | 2 +-", text)
            self.assertIn("## Files\n\n- src/a.py\n", text)
        diff_call = next(a for a in self.calls if "--stat" in a)
        self.assertEqual(diff_call, ["git", "diff", "--stat", "main..feat/checkout", "--"])
        state = json.loads((self.home / "grader" / "acme-coupon-20261016-1.json").read_text())
        self.assertEqual(state["iterations"], 3)
        code, out, err = self.orche("ledger", "grade", "--unit", "acme-coupon-20261016-1",
                                    "--rubric", str(rubric), "--diff", "main..feat/checkout")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("max grader iterations reached", err)
        self.assertFalse((self.home / "grader" / "acme-coupon-20261016-1-4.md").exists())

    def test_grade_input_errors(self):
        rubric = self.home / "RUBRIC.md"
        rubric.write_text("x\n")
        args = ("ledger", "grade", "--rubric", str(rubric))
        self.assertEqual(self.orche(*args, "--unit", "u1", "--diff", "main")[0], 1)
        self.assertEqual(self.orche(*args, "--unit", "u1", "--diff", "main...x")[0], 1)
        self.assertEqual(self.orche(*args, "--unit", "../evil", "--diff", "a..b")[0], 1)
        self.assertEqual(self.orche("ledger", "grade", "--rubric", "/nonexistent/r.md", "--unit", "u1",
                                    "--diff", "a..b")[0], 2)
        self.assertEqual(self.orche(*args, "--unit", "u1", "--diff", "a..b")[0], 1)  # git fails
        self.assertFalse((self.home / "grader" / "u1.json").exists())


class ConcurrentAppendTest(support.TempHomeCase):
    def test_twenty_threads_twenty_rows(self):
        import threading
        from orche import cmd_decide
        barrier = threading.Barrier(20)

        def ledger_row(i):
            barrier.wait()
            ledger.append("acme", {"pr": str(i), "sha": f"{i:040x}", "verdict": "type-check-only",
                                   "evidence": "log", "verifier": "v", "ts": "2026-10-16T00:00:00Z"})
        threads = [threading.Thread(target=ledger_row, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = ledger.path("acme").read_text().splitlines()
        self.assertEqual(lines[0], ledger.HEADER)
        self.assertEqual(sorted(int(l.split("\t")[0]) for l in lines[1:]), list(range(20)))
        decisions = self.home / "unit" / "DECISIONS.tsv"
        barrier = threading.Barrier(20)

        def decision(i):
            barrier.wait()
            cmd_decide.append(decisions, f"ts\tbuild\tdecision {i}\twhy\t-\tok")
        threads = [threading.Thread(target=decision, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = decisions.read_text().splitlines()
        self.assertEqual(lines[0], cmd_decide.HEADER)
        self.assertEqual(len(lines), 21)
