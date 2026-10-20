"""`orche decide`: escaping, unit resolution."""
import io
import os
import re
import stat
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from orche import cmd_decide
from test_ledger import make_registry
from test_registry import load_cli

TS =re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
HEADER = "ts\tphase\tdecision\twhy\tevidence\tresult"
CELLS = ("plan", "=SUM(A1:A2)", "line one\nline\ttwo\r\nend", "-flag", "@mention")


class DecideTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.root = self.home / "wt"
        make_registry(self.root)
        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)

    def orche(self, *argv, cwd=None):
        os.chdir(cwd or self._cwd)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_escaping_and_single_line(self):
        self.assertEqual(cmd_decide.escape("=1+1"), "'=1+1")
        self.assertEqual(cmd_decide.escape("+x"), "'+x")
        self.assertEqual(cmd_decide.escape("a=b"), "a=b")
        self.assertEqual(cmd_decide.escape("a\tb\nc\rd"), "a b c d")
        target = self.home / "DECISIONS.tsv"
        code, out, err = self.orche("decide", "--file", str(target), "--", *CELLS)
        self.assertEqual((code, err), (0, ""))
        lines = target.read_text().split("\n")
        self.assertEqual(lines[0], HEADER)
        self.assertEqual(lines[2:], [""])
        cells = lines[1].split("\t")
        self.assertRegex(cells[0], TS)
        self.assertEqual(cells[1:], ["plan", "'=SUM(A1:A2)", "line one line two  end", "'-flag", "'@mention"])
        self.assertEqual(out, lines[1] + "\n")
        self.assertNotEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.orche("decide", "a", "b", "c", "d", "e", "--file", str(target))
        self.assertEqual(target.read_text().count(HEADER), 1)

    def test_resolves_unit_file_from_cwd(self):
        feat = self.root / "acme-checkout" / "src"
        feat.mkdir(parents=True)
        self.assertEqual(self.orche("decide", "p", "d", "w", "e", "r", cwd=feat)[0], 0)
        self.assertTrue((self.root / "acme-checkout" / ".orche" / "feat" / "checkout" / "DECISIONS.tsv").is_file())
        self.assertEqual(self.orche("decide", "p", "d", "w", "e", "r", cwd=self.root / "acme-build")[0], 0)
        self.assertTrue((self.root / "acme-build" / ".orche" / "div" / "build" / "DECISIONS.tsv").is_file())
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": "main-acme"}):
            self.assertEqual(self.orche("decide", "p", "d", "w", "e", "r", cwd=self.root / "acme")[0], 0)
        self.assertTrue((self.root / "acme" / ".orche" / "DECISIONS.tsv").is_file())
        code, _, err = self.orche("decide", "p", "d", "w", "e", "r", cwd=self.home)
        self.assertEqual(code, 1)
        self.assertIn("--file", err)

    def test_writes_the_sessions_own_file(self):
        """L14: ORCHE_SESSION picks the member's memory dir; no session in the main checkout is refused."""
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": "liaison-acme"}):
            self.assertEqual(self.orche("decide", "p", "d", "w", "e", "r", cwd=self.root / "acme")[0], 0)
        self.assertTrue((self.root / "acme" / ".orche" / "liaison" / "DECISIONS.tsv").is_file())
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": "main-acme"}):
            self.assertEqual(self.orche("decide", "p", "d", "w", "e", "r", cwd=self.root / "acme-build")[0], 0)
        self.assertEqual(len((self.root / "acme" / ".orche" / "DECISIONS.tsv").read_text().splitlines()), 2)
        self.assertFalse((self.root / "acme-build" / ".orche").exists())
        code, _, err = self.orche("decide", "p", "d", "w", "e", "r", cwd=self.root / "acme")
        self.assertEqual(code, 1)
        self.assertIn("--file", err)
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": "ghost-acme"}):
            code, _, err = self.orche("decide", "p", "d", "w", "e", "r", cwd=self.root / "acme-build")
        self.assertEqual(code, 1)
        self.assertIn("--file", err)
        self.assertFalse((self.root / "acme-build" / ".orche").exists())

    def test_dry_run_writes_nothing(self):
        target = self.home / "DECISIONS.tsv"
        self.assertEqual(self.orche("--dry-run", "decide", "a", "b", "c", "d", "e", "--file", str(target))[0], 0)
        self.assertFalse(target.exists())
