"""Python guard: bin/orche and every hook refuse old interpreters before importing orche modules."""
import io
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

import support
from orche import pyguard

OLD = (3, 9, 6, "final", 0)


class GuardTest(support.TempHomeCase):
    def test_message_and_cli_exit_with_patched_version(self):
        with mock.patch.object(sys, "version_info", OLD), mock.patch.object(sys, "executable", "/usr/bin/python3"):
            self.assertFalse(pyguard.ok())
            self.assertEqual(pyguard.message(), "ORCHE needs Python 3.11+ (found 3.9 at /usr/bin/python3)")
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                pyguard.check_cli()
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(err.getvalue(), "orche: ORCHE needs Python 3.11+ (found 3.9 at /usr/bin/python3)\n")
            self.assertFalse(pyguard.check_hook("scope_gate"))
        log = (self.home / "logs" / "hooks.log").read_text()
        self.assertIn("scope_gate: ORCHE needs Python 3.11+ (found 3.9 at /usr/bin/python3)", log)
        self.assertTrue(pyguard.ok() and pyguard.check_hook("x"))

    def test_sources_check_before_other_orche_imports(self):
        files = [support.ROOT / "bin" / "orche", *sorted((support.ROOT / "hooks").glob("*.py"))]
        for path in files:
            lines = path.read_text().splitlines()
            orche_imports = [i for i, l in enumerate(lines) if l.startswith("from orche") or "from orche import" in l]
            self.assertTrue(orche_imports, path)
            self.assertIn("pyguard", lines[orche_imports[0]], path)

    @unittest.skipUnless(shutil.which("python3.9") or os.path.exists("/usr/bin/python3"), "no old python")
    def test_real_old_interpreter_if_present(self):
        exe = shutil.which("python3.9") or "/usr/bin/python3"
        ver = subprocess.run([exe, "-c", "import sys;print(sys.version_info[:2] < (3, 11))"],
                             capture_output=True, text=True).stdout.strip()
        if ver != "True":
            self.skipTest("interpreter is new enough")
        env = {**os.environ, "ORCHE_HOME": str(self.home)}
        res = subprocess.run([exe, str(support.ROOT / "bin" / "orche"), "version"], capture_output=True, text=True,
                             env=env)
        self.assertEqual(res.returncode, 1)
        self.assertIn("ORCHE needs Python 3.11+", res.stderr)
        for hook in sorted((support.ROOT / "hooks").glob("*.py")):
            res = subprocess.run([exe, str(hook)], input="{}", capture_output=True, text=True, env=env)
            self.assertEqual((res.returncode, res.stdout), (0, ""), hook)
        self.assertIn("session_end: ORCHE needs Python 3.11+", (self.home / "logs" / "hooks.log").read_text())


if __name__ == "__main__":
    unittest.main()
