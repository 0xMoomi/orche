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
        (self.home / "config.json").write_text(json.dumps(support.no_wake({})))   # the pane's pre-review: test_prereview
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
        # a gated done publishes (publish.publish): branch, origin/HEAD, push, then gh pr list/create
        self.publish_replies = {("rev-parse", "--abbrev-ref"): (0, "div/build\n"),
                                ("symbolic-ref", "--short"): (0, "origin/main\n"),
                                ("push", "-u"): (0, ""), ("pr", "list"): (0, "[]"),
                                ("pr", "create"): (0, "https://github.com/acme/shop/pull/12\n")}
        self._runner, shell.RUNNER = shell.RUNNER, self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", self._runner)

    def fake_runner(self, argv, *, timeout, input, env=None, cwd=None):
        self.calls.append(argv)
        if argv[:1] == ["git"] and argv[-2:] == ["rev-parse", "HEAD"]:
            return shell.Result(0, SHA + "\n", "", argv)
        reply = self.publish_replies.get(tuple(argv[3:5] if argv[0] == "git" else argv[1:3]))
        if reply is not None:
            return shell.Result(*reply, "", argv) if len(reply) == 2 else shell.Result(*reply, argv)
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

    def test_owner_flag_sets_the_pointer_field(self):
        """C10 T9 (§5.1): `--owner` is the pointer's `owner` (masked, one line)."""
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-coupon-20261016-1", "div-acme-shop-build")
        code, _, err = self.orche("report", "blocked", "--summary", "needs the key", "--owner",
                                  "secret STRIPE_KEY\n sk-ant-api03-" + "A" * 40)
        self.assertEqual(code, 0, err)
        self.orche("report", "info", "--summary", "fyi")
        by_status = {p["status"]: p for p in self.pointers("main-acme-shop")}
        self.assertTrue(by_status["blocked"]["owner"].startswith("secret STRIPE_KEY "))
        self.assertNotIn("A" * 40, by_status["blocked"]["owner"])
        self.assertNotIn("owner", by_status["info"])

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
                          True, False, f"coupon field landed\nPR: #12 https://github.com/acme/shop/pull/12 head {SHA}",
                          str(folder / "REPORT.md")))
        self.assertEqual(ptr["pr"], 12)
        push = [c for c in self.calls if "push" in c]
        self.assertEqual(push, [["git", "-C", "/srv/orca/acme-shop-build", "push", "-u", "origin", "HEAD:refs/heads/div/build"]])
        [create] = [c for c in self.calls if c[1:3] == ["pr", "create"]]
        self.assertEqual(create[3:7], ["--base", "main", "--head", "div/build"])

    def ledgered(self):
        ledger = self.home / "ledger" / "acme-shop.tsv"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n"
                          f"7\t{SHA}\tunit-test-verified\tlog\tverifier-1\t2026-10-16T00:00:00Z\n")

    def test_done_publish_gap_blocks_with_owner_step(self):
        """A gated done whose publish fails files nothing and exits 3 with the reason (and OWNER step)."""
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-gap-20261016-1", "div-acme-shop-build")
        self.ledgered()
        self.publish_replies[("pr", "list")] = (1, "", "To get started with GitHub CLI, please run:  gh auth login")
        code, _, err = self.orche("report", "done", "--summary", "landed")
        self.assertEqual(code, 3)
        self.assertIn("done not filed: gh pr list: gh is not logged in", err)
        self.assertIn("OWNER: gh auth login", err)
        self.assertFalse((self.home / "inbox").exists())
        self.publish_replies[("pr", "list")] = (0, "[]")
        self.publish_replies[("push", "-u")] = (1, "", " ! [rejected] HEAD -> div/build (non-fast-forward)")
        code, _, err = self.orche("report", "done", "--summary", "landed")
        self.assertEqual(code, 3)
        self.assertIn("push rejected: origin/div/build has commits this worktree lacks", err)
        self.assertNotIn("OWNER:", err)

    def coordination_done(self, branch):
        """R5: Build's parent unit (nothing against origin/main) reports done."""
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-parent-20261016-1", "div-acme-shop-build")
        self.ledgered()
        self.publish_replies[("rev-parse", "--abbrev-ref")] = (0, branch + "\n")
        self.publish_replies[("diff", "--quiet")] = (0, "")
        code, _, err = self.orche("report", "done", "--summary", "all 3 child units landed")
        self.assertEqual((code, err), (0, ""))
        [ptr] = self.pointers("main-acme-shop")
        self.assertEqual(ptr["status"], "done")
        self.assertIsNone(ptr.get("pr"))
        self.assertEqual(ptr["summary"], "all 3 child units landed\nPR: none (no change against origin/main; "
                                         "nothing published)")
        self.assertNotIn("review", ptr)
        self.assertFalse([c for c in self.calls if "push" in c or c[1:2] == ["pr"]])

    def test_a_coordination_done_on_builds_branch_files_without_a_pr(self):
        self.coordination_done("div/build")

    def test_a_coordination_done_on_main_files_without_a_pr(self):
        self.coordination_done("main")

    def test_done_reuses_the_open_pr(self):
        self.as_session("div-acme-shop-build")
        self.brief("acme-shop-again-20261016-1", "div-acme-shop-build")
        self.ledgered()
        self.publish_replies[("pr", "list")] = (0, json.dumps([{"number": 9, "baseRefName": "main", "isDraft": False,
                                                                "url": "https://github.com/acme/shop/pull/9"}]))
        code, _, err = self.orche("report", "done", "--summary", "again")
        self.assertEqual((code, err), (0, ""))
        [ptr] = self.pointers("main-acme-shop")
        self.assertEqual(ptr["pr"], 9)
        self.assertFalse([c for c in self.calls if c[1:3] == ["pr", "create"]])

    def test_a_team_done_publishes_nothing_and_says_what_the_owner_says_next(self):
        """R-TEAM-2: in a team repository a gated done files `pr: none` and the ready line; no push, no PR, no
        head review."""
        from unittest import mock
        from orche import publish
        from orche.review import build as review_build
        reg = registry.load()
        reg["projects"]["acme-shop"]["team"] = "team-os@acme"
        registry.save(reg)
        self.as_session("div-acme-shop-build")
        unit = "acme-shop-team-20261016-1"
        self.brief(unit, "div-acme-shop-build")
        self.ledgered()
        with mock.patch.object(publish, "publish") as pub, mock.patch.object(review_build, "after_publish") as after:
            code, _, err = self.orche("report", "done", "--summary", "1\n2\n3\n4\n5\n6")
        self.assertEqual((code, err), (0, ""))
        pub.assert_not_called()
        after.assert_not_called()
        [ptr] = self.pointers("main-acme-shop")
        self.assertEqual(ptr["status"], "done")
        self.assertIsNone(ptr.get("pr"))
        self.assertEqual(ptr["summary"], f'1\n2\n3\n4\nready: say "push {unit}" (div/build)')
        self.assertFalse([c for c in self.calls if "push" in c or c[1:2] == ["pr"]])

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

        def per_path(argv, *, timeout, input, env=None, cwd=None):
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

    def test_a_feature_with_no_diff_is_a_gap_never_done_without_a_pr(self):
        """Review B G4: a pane feature whose HEAD changes nothing never files done + PR none (the parent's
        all-landed would wait for it forever): the gap and its reason, like every pane publish gap."""
        self.as_session("feat-acme-shop-search")
        self.brief("acme-shop-search-20261016-1", "feat-acme-shop-search")
        self.ledgered()                                   # HEAD has a row (e.g. the queue's (none, merge) row)
        self.publish_replies[("rev-parse", "--abbrev-ref")] = (0, "feat/search\n")
        self.publish_replies[("diff", "--quiet")] = (0, "")   # nothing against origin/main
        code, _out, err = self.orche("report", "done", "--summary", "nothing needed doing")
        self.assertEqual(code, 3)
        self.assertIn("done not filed: nothing to publish: HEAD changes nothing against origin/main; a feature's "
                      "done needs its commit", err)
        self.assertEqual(self.pointers("div-acme-shop-build"), [])
