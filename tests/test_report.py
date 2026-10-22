"""`orche report`: a worker files one inbox pointer (defaults from session, brief, git and ledger)."""
import copy
import io
import json
import os
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import registry, shell
from test_registry import load_cli

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
SHA = "a" * 40


class ReportTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self._repo = tempfile.TemporaryDirectory()
        self.addCleanup(self._repo.cleanup)
        self.repo = Path(os.path.realpath(self._repo.name))
        reg = copy.deepcopy(SAMPLE)
        reg["projects"]["acme-shop"]["path"] = str(self.repo)
        registry.save(reg)
        old = os.environ.get("ORCHE_SESSION")
        self.addCleanup(lambda: os.environ.pop("ORCHE_SESSION", None) if old is None
                        else os.environ.__setitem__("ORCHE_SESSION", old))
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self._cwd)
        self.calls = []
        self._runner, shell.RUNNER = shell.RUNNER, self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", self._runner)

    def fake_runner(self, argv, *, timeout, input):
        self.calls.append(argv)
        if argv[:1] == ["git"] and argv[-2:] == ["rev-parse", "HEAD"]:
            return shell.Result(0, SHA + "\n", "", argv)
        return shell.Result(1, "", "unexpected", argv)

    def as_session(self, session):
        os.environ["ORCHE_SESSION"] = session

    def brief(self, unit, to, rev=1, age=0, frm=None):
        folder = self.repo / ".orche" / "units" / unit
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "BRIEF.md"
        header = f"unit: {unit}\nto: {to}   from: {frm}\n" if frm else ""
        path.write_text(f"rev: {rev}\n{header}\n1. Never force-push.\n\npersona: .claude/agents/{to}.md\n\n"
                        f"## Brief {unit}\nTO        {to}\nGOAL      Do the thing\n")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return folder

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def pointers(self, session):
        folder = self.home / "inbox" / session
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.exists() else []

    def test_division_defaults_resolved(self):
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-old-20261015-1", "div-acme-shop-build", age=100)
        folder = self.brief("acme-shop-coupon-20261016-1", "div-acme-shop-build", rev=3)
        self.brief("acme-shop-other-20261016-2", "div-acme-shop-rnd")
        (folder / "REPORT.md").write_text("STATUS: done\n")
        ledger = self.home / "ledger" / "acme-shop.tsv"
        ledger.parent.mkdir()
        ledger.write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n"
                          f"7\t{SHA}\tunit-test-verified\tlog\tverifier-1\t2026-10-16T00:00:00Z\n")
        code, out, err = self.orche("report", "done", "--summary", "coupon field landed")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("reported acme-shop-coupon-20261016-1 done -> main-acme-shop", out)
        [ptr] = self.pointers("main-acme-shop")
        self.assertEqual((ptr["unit"], ptr["from"], ptr["to"], ptr["status"], ptr["brief_rev"], ptr["head_sha"],
                          ptr["ledger_row"], ptr["urgent"], ptr["summary"], ptr["report_path"]),
                         ("acme-shop-coupon-20261016-1", "div-acme-shop-build", "main-acme-shop", "done", 3, SHA,
                          True, False, "coupon field landed", str(folder / "REPORT.md")))

    def test_newest_skips_folders_that_are_not_unit_ids(self):
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-real-20261016-1", "div-acme-shop-build", age=100)
        self.brief("scratch", "div-acme-shop-build")
        code, out, err = self.orche("report", "info", "--summary", "progress")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("reported acme-shop-real-20261016-1 info", out)

    def test_feature_reports_to_build_and_overrides(self):
        self.as_session("feat-acme-shop-search")
        self.brief("acme-shop-search-20261016-1", "feat-acme-shop-search", rev=2)
        summary = "\n".join(f"line {i}" for i in range(1, 9))
        code, out, _ = self.orche("--json", "report", "blocked", "--summary", summary, "--urgent")
        self.assertEqual(code, 0)
        ptr = json.loads(out)
        self.assertEqual((ptr["to"], ptr["brief_rev"], ptr["urgent"], ptr["ledger_row"], ptr["report_path"]),
                         ("div-acme-shop-build", 2, True, False, ""))
        self.assertEqual(ptr["summary"].splitlines(), ["line 1", "line 2", "line 3", "line 4", "[…truncated]"])
        self.assertEqual(len(self.pointers("div-acme-shop-build")), 1)
        code, _, _ = self.orche("report", "stop", "--summary", "halted", "--to", "main-acme-shop",
                                "--unit", "acme-shop-search-20261016-1", "--report-path", "notes/R.md")
        self.assertEqual(code, 0)
        [ptr] = self.pointers("main-acme-shop")
        self.assertEqual((ptr["status"], ptr["report_path"]), ("stop", str(self.repo / "notes" / "R.md")))

    def test_validation_errors(self):
        self.as_session("div-acme-shop-build")
        cases = [(("report", "stopped", "--summary", "x"), "status must be one of"),
                 (("report", "done", "--summary", "x"), "no brief addressed to div-acme-shop-build; pass --unit"),
                 (("report", "done", "--summary", "x", "--unit", "../etc"), "bad unit id"),
                 (("report", "done", "--summary", "x", "--unit", "acme-shop-none-20261016-1"), "unknown unit"),
                 (("report", "done", "--summary", "  "), "--summary is empty")]
        self.brief("acme-shop-x-20261016-9", "div-acme-shop-rnd")
        for argv, message in cases:
            if message == "--summary is empty":
                self.brief("acme-shop-y-20261016-1", "div-acme-shop-build")
            code, _, err = self.orche(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(message, err, argv)
        self.brief("acme-shop-z-20261016-1", "div-acme-shop-build")
        code, _, err = self.orche("report", "done", "--summary", "x", "--to", "main-acme-shpo")
        self.assertEqual(code, 1)
        self.assertIn("unknown target session 'main-acme-shpo'", err)
        self.as_session("nobody")
        code, _, err = self.orche("report", "done", "--summary", "x")
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which session is reporting", err)
        self.assertFalse((self.home / "inbox").exists())

    def test_done_from_build_needs_ledger_row(self):
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-gate-20261016-1", "div-acme-shop-build")
        code, _, err = self.orche("report", "done", "--summary", "landed")
        self.assertEqual(code, 3)
        self.assertIn("[orche] completion blocked: missing LEDGER row for HEAD aaaaaaaaaaaa", err)
        self.assertFalse((self.home / "inbox").exists())
        ledger = self.home / "ledger" / "acme-shop.tsv"
        ledger.parent.mkdir()
        ledger.write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n"
                          f"7\t{SHA}\ttype-check-only\tlog\tverifier-1\t2026-10-16T00:00:00Z\n")
        code, out, err = self.orche("--json", "report", "done", "--summary", "landed")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(json.loads(out)["ledger_row"])

    def test_done_from_build_in_the_main_checkout_proves_builds_worktree_head(self):
        """D11 (Q5): Build reporting from the main checkout is judged on its own worktree's HEAD."""
        build_sha = "b" * 40
        real = self.fake_runner

        def per_path(argv, *, timeout, input):
            if argv[:3] == ["git", "-C", "/srv/orca/acme-shop-build"] and argv[-2:] == ["rev-parse", "HEAD"]:
                self.calls.append(argv)
                return shell.Result(0, build_sha + "\n", "", argv)
            return real(argv, timeout=timeout, input=input)
        shell.RUNNER = per_path
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-gate-20261016-2", "div-acme-shop-build")
        ledger = self.home / "ledger" / "acme-shop.tsv"
        ledger.parent.mkdir()
        ledger.write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n"
                          f"7\t{build_sha}\ttype-check-only\tlog\tverifier-1\t2026-10-16T00:00:00Z\n")
        code, out, err = self.orche("--json", "report", "done", "--summary", "landed")
        self.assertEqual((code, err), (0, ""))
        ptr = json.loads(out)
        self.assertEqual((ptr["head_sha"], ptr["ledger_row"]), (build_sha, True))
        ledger.write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n"
                          f"7\t{SHA}\ttype-check-only\tlog\tverifier-1\t2026-10-16T00:00:00Z\n")
        code, _, err = self.orche("report", "done", "--summary", "landed")
        self.assertEqual(code, 3)
        self.assertIn("LEDGER row for HEAD bbbbbbbbbbbb", err)

    def test_done_from_main_is_not_gated(self):
        self.as_session("main-acme-shop")
        self.brief("acme-shop-plan-20261016-1", "main-acme-shop")
        self.assertEqual(self.orche("report", "done", "--summary", "x")[0], 0)

    def test_adhoc_unit_for_requests_questions_and_info(self):
        self.as_session("div-acme-shop-rnd")
        code, out, err = self.orche("--json", "report", "external-request", "--summary", "add a date filter")
        self.assertEqual((code, err), (0, ""))
        ptr = json.loads(out)
        self.assertRegex(ptr["unit"], r"^adhoc-div-acme-shop-rnd-\d{8}$")
        self.assertEqual((ptr["to"], ptr["brief_rev"], ptr["report_path"]), ("main-acme-shop", 1, ""))
        self.assertEqual(self.orche("report", "question", "--summary", "which API?")[0], 0)
        code, _, err = self.orche("report", "blocked", "--summary", "x")
        self.assertEqual(code, 1)
        self.assertIn("no brief addressed to div-acme-shop-rnd; pass --unit", err)

    def test_retired_target_rejected(self):
        reg = registry.load()
        feat = reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]
        feat["status"] = "retired"
        registry.save(reg)
        self.as_session("main-acme-shop")
        self.brief("acme-shop-plan-20261016-1", "main-acme-shop")
        code, _, err = self.orche("report", "info", "--summary", "x", "--to", feat["session"])
        self.assertEqual(code, 1)
        self.assertIn(f"target session {feat['session']!r} is retired", err)

    def test_feature_reports_to_the_sender_of_its_brief(self):
        self.as_session("feat-acme-shop-search")
        self.brief("acme-shop-search-20261016-1", "feat-acme-shop-search", frm="main-acme-shop")
        code, out, err = self.orche("report", "blocked", "--summary", "stuck")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("-> main-acme-shop", out)
        self.assertEqual((len(self.pointers("main-acme-shop")), self.pointers("div-acme-shop-build")), (1, []))

    def test_feature_of_sessionless_division_reports_to_main(self):
        reg = registry.load()
        reg["projects"]["acme-shop"]["divisions"]["build"]["session"] = ""
        registry.save(reg)
        self.as_session("feat-acme-shop-search")
        self.brief("acme-shop-search-20261016-1", "feat-acme-shop-search")
        code, out, err = self.orche("report", "blocked", "--summary", "stuck")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("-> main-acme-shop", out)
        self.assertEqual(len(self.pointers("main-acme-shop")), 1)

    def test_project_without_main_has_no_default_recipient(self):
        reg = registry.load()
        reg["projects"]["acme-shop"]["main_session"] = ""
        registry.save(reg)
        self.as_session("div-acme-shop-rnd")
        self.brief("acme-shop-probe-20261016-1", "div-acme-shop-rnd")
        code, _, err = self.orche("report", "info", "--summary", "x")
        self.assertEqual(code, 1)
        self.assertIn("no recipient for div-acme-shop-rnd; pass --to", err)
        code, out, _ = self.orche("report", "info", "--summary", "x", "--to", "div-acme-shop-build")
        self.assertEqual(code, 0)
        self.assertIn("-> div-acme-shop-build", out)

    def test_unidentified_main_checkout_refused(self):
        os.environ.pop("ORCHE_SESSION", None)
        self.brief("acme-shop-plan-20261016-1", "main-acme-shop")
        code, _, err = self.orche("report", "info", "--summary", "x")
        self.assertEqual(code, 1)
        self.assertIn("set ORCHE_SESSION=main-acme-shop", err)
        self.assertFalse((self.home / "inbox").exists())

    def test_summary_is_masked(self):
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-key-20261016-1", "div-acme-shop-build")
        canary = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        self.assertEqual(self.orche("report", "blocked", "--summary", f"rotated key {canary}")[0], 0)
        [path] = list((self.home / "inbox" / "main-acme-shop").glob("*.json"))
        text = path.read_text()
        self.assertNotIn(canary, text)
        self.assertIn("rotated key", text)

    def test_dry_run_writes_nothing(self):
        self.as_session("main-acme-shop")
        self.brief("acme-shop-plan-20261016-1", "main-acme-shop")
        code, out, _ = self.orche("--dry-run", "report", "info", "--summary", "plan ready")
        self.assertEqual(code, 0)
        self.assertIn("[dry-run] would write pointer acme-shop-plan-20261016-1 info -> main-acme-shop", out)
        self.assertFalse((self.home / "inbox").exists())
