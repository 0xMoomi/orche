"""Shared test helpers: put lib/ on sys.path and isolate ORCHE_HOME per test."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))


class TempHomeCase(unittest.TestCase):
    """Runs each test with ORCHE_HOME pointing at a fresh temporary directory."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # An ORCHE pane exports ORCHE_SESSION / ORCA_TERMINAL_HANDLE (and maybe more): every test
        # starts without any ORCHE_* / ORCA_* variable, and the caller's values come back after.
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith(("ORCHE_", "ORCA_"))}
        for key in self._saved_env:
            del os.environ[key]
        os.environ["ORCHE_HOME"] = str(self.home)

    def tearDown(self):
        for key in [k for k in os.environ if k.startswith(("ORCHE_", "ORCA_"))]:
            del os.environ[key]
        os.environ.update(self._saved_env)
        self._tmp.cleanup()
        super().tearDown()
