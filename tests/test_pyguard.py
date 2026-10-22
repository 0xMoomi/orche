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
        files = [support.ROOT / "bin" / "orche.py", *sorted((support.ROOT / "hooks").glob("*.py"))]
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
        res = subprocess.run([exe, str(support.ROOT / "bin" / "orche.py"), "version"], capture_output=True, text=True,
                             env=env)
        self.assertEqual(res.returncode, 1)
        self.assertIn("ORCHE needs Python 3.11+", res.stderr)
        for hook in sorted((support.ROOT / "hooks").glob("*.py")):
            res = subprocess.run([exe, str(hook)], input="{}", capture_output=True, text=True, env=env)
            self.assertEqual((res.returncode, res.stdout), (0, ""), hook)
        self.assertIn("session_end: ORCHE needs Python 3.11+", (self.home / "logs" / "hooks.log").read_text())


class LauncherTest(support.TempHomeCase):
    """R2: bin/orche is a sh launcher that finds a Python 3.11+ the way hooks/run.sh does."""

    def path_dir(self, with_312):
        bin_dir = self.home / "path"
        bin_dir.mkdir()
        old = bin_dir / "python3"   # Apple's 3.9: fails the probe, and would print the guard if run
        old.write_text("#!/bin/sh\n"
                       'if [ "$1" = "-c" ]; then exit 1; fi\n'
                       'echo "orche: ORCHE needs Python 3.11+ (found 3.9 at fake)" >&2; exit 1\n')
        old.chmod(0o755)
        if with_312:
            (bin_dir / "python3.12").symlink_to(os.path.realpath(sys.executable))
        return bin_dir

    def run_cli(self, launcher, bin_dir):
        env = {"PATH": str(bin_dir), "HOME": str(self.home), "ORCHE_HOME": str(self.home)}
        return subprocess.run([str(launcher), "version"], env=env, capture_output=True, text=True, timeout=30)

    def test_launcher_skips_old_python3_and_runs_under_312(self):
        res = self.run_cli(support.ROOT / "bin" / "orche", self.path_dir(with_312=True))
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "orche 0.6.2\n", ""))

    def test_launcher_without_suitable_python_is_loud(self):
        plugin = support.plugin_copy(self.home / "plugin", "bin/orche", "bin/orche.py", "lib/find_python.sh")
        res = self.run_cli(plugin / "bin" / "orche", self.path_dir(with_312=False))
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertEqual(res.stderr.count("\n"), 1, res.stderr)
        self.assertTrue(res.stderr.startswith("orche: ORCHE needs Python 3.11+ (none found; tried python3.14"),
                        res.stderr)

    def test_launcher_and_run_sh_share_one_finder(self):
        finder = "lib/find_python.sh"
        for rel in ("bin/orche", "hooks/run.sh"):
            text = (support.ROOT / rel).read_text()
            self.assertIn(f'. "$dir/../{finder}"', text, rel)
            self.assertNotIn("python3.14", text, rel)
            self.assertTrue(text.startswith("#!/bin/sh\n"), rel)
            self.assertTrue(os.access(support.ROOT / rel, os.X_OK), rel)
        self.assertFalse((support.ROOT / "bin" / "orche.py").read_text().startswith("#!"))


if __name__ == "__main__":
    unittest.main()
