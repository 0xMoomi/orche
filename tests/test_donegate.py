"""The done gate (lib/orche/donegate.py): the claim grammar and the stop / report / run proofs.

Real temp git repos, a temp ORCHE_HOME ledger and the hook registry fixture (HookCase). Each repo
gets its own commit message: identical commits with the same author and time share a SHA.
"""
import os
import shutil
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace

import support
from orche import donegate, registry, roster
from test_hooks import GIT_ENV, PREFIX, SUFFIX, HookCase


class ClaimTest(unittest.TestCase):
    def word(self, text):
        c = donegate.claim(text)
        return c.word if c else None

    def test_ported_matches(self):
        for text, word in [
            ("STATUS: done", "done"),
            ("Status: release-ready", "release-ready"),
            ("Finished.\n**STATUS: done**", "done"),
            ("**STATUS:** done", "done"),
            ("- STATUS: done", "done"),
            ("* STATUS: done", "done"),
            ("+ STATUS: done", "done"),
            ("1. STATUS: done", "done"),
            ("2) STATUS: release-ready", "release-ready"),
            ("> STATUS: done", "done"),
            ("> - **STATUS: done**", "done"),
            ("status: done", "done"),
            ("STATUS: DONE", "done"),
            ("`STATUS: done`", "done"),
            ("_STATUS: done_", "done"),
            ("STATUS: done**", "done"),
            ("STATUS: done.", "done"),
            ("  STATUS:done", "done"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.word(text), word)

    def test_ported_non_matches(self):
        for text in ["STATUS: done-ish", "STATUS: doneness", "STATUS: release-ready-ish",
                     "This is the status: done case", "we fixed the STATUS: done handling",
                     "STATUS: working", "STATUS: blocked", "xSTATUS: done", ""]:
            with self.subTest(text=text):
                c = donegate.claim(text)
                self.assertTrue(c is None or c.word not in donegate.DONE_WORDS)
        self.assertEqual(self.word("STATUS: blocked"), "blocked")
        for text in ["STATUS: working", "STATUS: done-ish", "STATUS: doneness", "STATUS: release-ready-ish"]:
            with self.subTest(text=text):
                self.assertIsNone(donegate.claim(text))

    def test_runner_status_cases(self):
        c = donegate.claim("ok\nstatus: blocked\nneed key")
        self.assertEqual((c.word, c.line), ("blocked", "status: blocked"))
        self.assertEqual(self.word("Status: Done.\nx"), "done")
        self.assertIsNone(donegate.claim("End with `STATUS: done|blocked|failed`"))
        self.assertEqual(self.word("STATUS: done|blocked|failed\nSTATUS: failed\nboom"), "failed")

    def test_new_cases(self):
        for text, word in [
            ("STATUS: done|blocked|failed", None),
            ("STATUS: done | blocked | failed | stop", None),
            ("`STATUS: done|blocked|failed`", None),
            ("STATUS: **done** | **blocked**", None),
            ("| STATUS: done | ok |", "done"),
            ("STATUS: done|completed", "done"),
            ("## STATUS: done", "done"),
            ("✅ STATUS: done", "done"),
            ("→ STATUS: done", "done"),
            ("(STATUS: done", "done"),
            ("STATUS : done", "done"),
            ("STATUS: **done**", "done"),
            ("**STATUS:** done", "done"),
            ("**STATUS: DONE_WITH_CONCERNS**", "done"),
            ("Status: done.\n**Status:** complete", "done"),
            ("STATUS: blocked | failed tests", None),
            ("STATUS: stop", "stop"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.word(text), word)
        self.assertEqual(donegate.claim("STATUS: done\nx\nSTATUS: blocked"),
                         donegate.Claim("blocked", "STATUS: blocked", 2))
        self.assertEqual(donegate.claim("x\n1. STATUS: done\nok"), donegate.Claim("done", "1. STATUS: done", 1))
        self.assertEqual(donegate.claim("STATUS: done\n- Status: 200 OK"), donegate.Claim("done", "STATUS: done", 0))

    def test_speed_guard(self):
        for text in ("*" * 320_000, "1. " * 106_000):
            start = time.monotonic()
            self.assertIsNone(donegate.claim(text))
            self.assertLess(time.monotonic() - start, 1.0)


class GateCase(HookCase):
    def setUp(self):
        super().setUp()
        self.git_init(self.repo, "init acme")
        self.git_init(self.root / "acme-build", "init acme-build")
        self.git_init(self.root / "acme-checkout", "init acme-checkout")
        self.reg = registry.load()
        ros = roster.Roster(self.reg)
        self.main = ros.member("main-acme")
        self.build = ros.member("div-acme-build")
        self.rnd = ros.member("div-acme-rnd")
        self.feat = ros.member("feat-acme-checkout")
        (self.home / "ledger").mkdir(exist_ok=True)
        (self.home / "ledger" / "acme.tsv").write_text("pr\tsha\tverdict\tevidence\tverifier\tts\n")

    def git(self, folder, *args):
        return subprocess.run(["git", "-C", str(folder), "-c", "commit.gpgsign=false", *args], check=True,
                              capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout.strip()

    def git_init(self, folder, message):
        subprocess.run(["git", "init", "-q", str(folder)], check=True, env={**os.environ, **GIT_ENV})
        (folder / "README").write_text(message + "\n")
        self.git(folder, "add", "README")
        self.git(folder, "commit", "-q", "-m", message)

    def commit(self, folder, message):
        (folder / "work.txt").write_text(message + "\n")
        self.git(folder, "add", "work.txt")
        self.git(folder, "commit", "-q", "-m", message)
        return self.rev(folder)

    def rev(self, folder):
        return self.git(folder, "rev-parse", "HEAD")

    def row(self, sha, verdict, verifier="verifier-1"):
        with open(self.home / "ledger" / "acme.tsv", "a") as fh:
            fh.write(f"7\t{sha}\t{verdict}\tlog\t{verifier}\t2026-10-18T00:00:00Z\n")


class StopTest(GateCase):
    def setUp(self):
        super().setUp()
        self.fwt = self.root / "acme-checkout"
        self.fhead = self.rev(self.fwt)
        self.tr = self.transcript("hi")

    def who(self, member, session, unit=None, project="acme"):
        return SimpleNamespace(member=member, session=session, place=SimpleNamespace(unit=unit, project=project))

    def status_md(self, wt, kind, name, stale=False):
        path = wt / ".orche" / kind / name / "STATUS.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n")
        if stale:
            old = time.mktime((2026, 10, 15, 0, 0, 0, 0, 0, -1))
            os.utime(path, (old, old))

    def stop(self, text, who, cwd=None, active=False):
        return donegate.may_stop(text, SimpleNamespace(reg=self.reg), who, str(cwd or self.fwt),
                                 transcript_path=self.tr, stop_hook_active=active)

    def feat_who(self):
        return self.who(self.feat, "feat-acme-checkout", self.feat)

    def ledger_item(self, sha, minimum="type-check-only"):
        return f"LEDGER row for HEAD {sha[:12]} in ORCHE_HOME/ledger/acme.tsv (verdict >= {minimum})"

    def test_feature_claims(self):
        sha = self.fhead[:12]
        # S1: no row, no STATUS.md
        self.assertEqual(self.stop("STATUS: done", self.feat_who()),
                         (2, PREFIX + self.ledger_item(self.fhead) + "; .orche/feat/checkout/STATUS.md (missing)" + SUFFIX))
        # S8: S1 under stop_hook_active
        self.assertEqual(self.stop("STATUS: done", self.feat_who(), active=True), (0, ""))
        # S2: STATUS.md fresh
        self.status_md(self.fwt, "feat", "checkout")
        self.assertEqual(self.stop("STATUS: done", self.feat_who()), (2, PREFIX + self.ledger_item(self.fhead) + SUFFIX))
        # S3: a type-check-only row
        self.row(self.fhead, "type-check-only")
        self.assertEqual(self.stop("STATUS: done", self.feat_who()), (0, ""))
        # S4: release-ready needs unit-test-verified
        self.assertEqual(self.stop("STATUS: release-ready", self.feat_who()),
                         (2, PREFIX + f"LEDGER row for HEAD {sha} has verdict 'type-check-only'; need >= "
                                      "unit-test-verified" + SUFFIX))
        # S5: verified by the claimant itself
        self.row(self.fhead, "unit-test-verified", "feat-acme-checkout")
        self.assertEqual(self.stop("STATUS: release-ready", self.feat_who()),
                         (2, PREFIX + f"independent verifier for HEAD {sha} (LEDGER row verifier is "
                                      "feat-acme-checkout; writer != judge)" + SUFFIX))
        # S6: verified by another session
        self.row(self.fhead, "unit-test-verified", "div-acme-build")
        self.assertEqual(self.stop("STATUS: release-ready", self.feat_who()), (0, ""))
        # S7: STATUS.md last written before the session started
        self.status_md(self.fwt, "feat", "checkout", stale=True)
        self.assertEqual(self.stop("STATUS: done", self.feat_who()),
                         (2, PREFIX + ".orche/feat/checkout/STATUS.md update in this session (last written "
                                      "before the session started)" + SUFFIX))

    def test_ungated_and_unregistered_stop(self):
        # S9: an R&D division is not gated
        self.assertEqual(self.stop("STATUS: done", self.who(self.rnd, "div-acme-rnd", self.rnd)), (0, ""))
        # S10: an unregistered session
        self.assertEqual(self.stop("STATUS: done", self.who(None, "ghost-acme")), (0, ""))

    def test_build_with_its_worktree_gone(self):
        """S11."""
        root = os.path.realpath(self.root / "acme-build")
        shutil.rmtree(self.root / "acme-build")
        self.assertEqual(self.stop("STATUS: done", self.who(self.build, "div-acme-build", None), cwd=self.repo),
                         (2, PREFIX + f"LEDGER row (cannot read git HEAD of {root}); .orche/div/build/STATUS.md "
                                      "(missing)" + SUFFIX))

    def test_night_predicate(self):
        night = self.repo / ".orche" / "night"
        night.mkdir(parents=True)
        pred = night / "main-acme.predicate.sh"
        pred.write_text("#!/bin/sh\necho 'night: 2 open'\nexit 1\n")
        pred.chmod(0o755)
        who = self.who(self.main, "main-acme")
        # S12
        self.assertEqual(self.stop("STATUS: done", who, cwd=self.repo), (2, "night: 2 open"))
        # S13
        self.assertEqual(self.stop("STATUS: done", who, cwd=self.repo, active=True), (0, ""))

        # S14: unidentified: `place` is never read
        class NoPlace:
            member = None
            session = None

            @property
            def place(self):
                raise AssertionError("place read")

        self.assertEqual(self.stop("STATUS: done", NoPlace(), cwd=self.repo), (0, ""))


class ReportTest(GateCase):
    def test_report_proofs(self):
        bhead, mhead = self.rev(self.root / "acme-build"), self.rev(self.repo)
        # P1
        p = donegate.for_report(self.build, str(self.repo), "done")
        self.assertEqual(p.missing, (f"LEDGER row for HEAD {bhead[:12]} in ORCHE_HOME/ledger/acme.tsv "
                                     "(verdict >= type-check-only)",))
        self.assertEqual(p.message, PREFIX + p.missing[0] + SUFFIX)
        # P2
        p = donegate.for_report(self.build, str(self.repo), "info")
        self.assertEqual((p.missing, p.head, p.message), ((), bhead, ""))
        # P3
        p = donegate.for_report(self.main, str(self.repo), "done")
        self.assertEqual((p.missing, p.head), ((), mhead))
        # P4
        self.row(bhead, "type-check-only")
        p = donegate.for_report(self.build, str(self.repo), "done")
        self.assertEqual((p.missing, p.head), ((), bhead))
        self.assertEqual(p.row["sha"], bhead)


class RunTest(GateCase):
    def setUp(self):
        super().setUp()
        self.wt = self.root / "acme-build"
        self.before = self.rev(self.wt)

    def run_(self, word, head_before, head, member=None):
        return donegate.after_run(member or self.build, word, str(self.wt), head_before, head).missing

    def untracked(self, *rels):
        for rel in rels:
            path = self.wt / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")

    def test_commit_and_tree_gaps(self):
        # R1
        self.assertEqual(self.run_("done", self.before, self.before), ("no commit",))
        head = self.commit(self.wt, "work one")
        # R2
        self.assertEqual(self.run_("done", self.before, head), (f"no ledger row for {head[:7]}",))
        # R3
        self.assertEqual(self.run_("done", None, head), ("history rewritten (HEAD before the run unknown)",))
        # R4
        self.assertEqual(self.run_("done", head, self.before),
                         (f"history rewritten ({head[:7]} is not an ancestor of HEAD)",))
        # R6: ORCHE's own untracked files are not uncommitted work
        self.untracked(".orche/div/build/STATUS.md", ".claude/agents/div-acme-build.md", ".claude/settings.local.json")
        self.assertEqual(self.run_("done", self.before, head), (f"no ledger row for {head[:7]}",))
        # R7
        self.untracked(".claude/agents/other.md")
        self.assertEqual(self.run_("done", self.before, head), ("uncommitted changes",))

    def test_a_foreign_untracked_file(self):
        """R5."""
        head = self.commit(self.wt, "work two")
        self.untracked("wip.txt")
        self.assertEqual(self.run_("done", self.before, head), ("uncommitted changes",))

    def test_row_gaps(self):
        head = self.commit(self.wt, "work three")
        sha = head[:7]
        # R8
        self.row(head, "type-check-only")
        self.assertEqual(self.run_("done", self.before, head), ())
        # R9
        self.assertEqual(self.run_("release-ready", self.before, head), (f"ledger verdict type-check-only for {sha}",))
        # R10
        self.row(head, "unit-test-verified", "div-acme-build")
        self.assertEqual(self.run_("release-ready", self.before, head),
                         (f"ledger verifier div-acme-build is the claimant for {sha}",))
        # R11
        self.row(head, "unit-test-verified", "verifier-1")
        self.assertEqual(self.run_("release-ready", self.before, head), ())

    def test_ungated_or_not_a_completion(self):
        # R12
        self.assertEqual(self.run_("done", self.before, self.before, member=self.rnd), ())
        # R13
        self.assertEqual(self.run_("blocked", self.before, self.before), ())


class ImportClosureTest(unittest.TestCase):
    def fresh(self, code):
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                              env={**os.environ, "PYTHONPATH": str(support.ROOT / "lib")}).stdout.strip()

    def test_imports_only_the_allowed_modules(self):
        out = self.fresh("import sys, orche.donegate; print(sorted(m for m in sys.modules if m.startswith('orche')))")
        self.assertEqual(out, str(['orche', 'orche.donegate', 'orche.ledger', 'orche.paths', 'orche.shell',
                                   'orche.transcript', 'orche.units', 'orche.util']))

    def test_units_stays_subprocess_free(self):
        self.assertEqual(self.fresh("import sys, orche.units; print('subprocess' in sys.modules)"), "False")


if __name__ == "__main__":
    unittest.main()
