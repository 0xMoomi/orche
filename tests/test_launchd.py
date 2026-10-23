"""C10 T2: the reconcile LaunchAgent (plist, install/uninstall, doctor row) and the hook kick."""
import json
import os
import plistlib
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import support
from orche import procs, reconcile, shell, units
from orche.install import doctor, launchd
from test_install import InstallCase

UID = os.getuid()


class PlistTest(support.TempHomeCase):
    def test_reconcile_plist_fields(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            data = plistlib.loads(launchd.reconcile_plist(300).encode())
        self.assertEqual(data["Label"], "com.orche.reconcile")
        self.assertTrue(data["ProgramArguments"][0].endswith("/bin/orche"))
        self.assertEqual(data["ProgramArguments"][1:], ["reconcile"])
        self.assertEqual(data["StartInterval"], 300)
        self.assertIs(data["RunAtLoad"], True)
        self.assertEqual(set(data["EnvironmentVariables"]), {"ORCHE_HOME", "PATH", "ORCHE_RECONCILE_JOB"})
        self.assertEqual(data["EnvironmentVariables"]["ORCHE_HOME"], str(self.home))
        self.assertEqual(data["EnvironmentVariables"]["ORCHE_RECONCILE_JOB"], "1")   # reconcile.is_job()
        self.assertEqual(data["StandardOutPath"], f"{self.home}/logs/reconcile.log")
        self.assertEqual(data["StandardErrorPath"], f"{self.home}/logs/reconcile.log")

    def test_claude_config_dir_joins_only_when_set(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/x/claude"}):
            data = plistlib.loads(launchd.reconcile_plist(120).encode())
        self.assertEqual(data["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], "/x/claude")
        self.assertEqual(data["StartInterval"], 120)

    def test_template_has_no_user_paths(self):
        text = (Path(support.ROOT) / "templates/launchd/com.orche.reconcile.plist").read_text()
        self.assertNotIn("/" + "Users/", text)
        plistlib.loads(text.replace("{{EVERY_S}}", "300").replace("{{", "").replace("}}", "").encode())


class HelperTest(support.TempHomeCase):
    """The helper with the real home patched to a temp dir: launchctl goes through a stub runner."""

    def setUp(self):
        super().setUp()
        self.h = self.home / "userhome"
        self.calls = []

        def runner(argv, *, timeout, input):
            self.calls.append(argv)
            return shell.Result(0, "", "", argv)
        patches = [mock.patch.object(shell, "RUNNER", runner),
                   mock.patch.object(launchd.Path, "home", classmethod(lambda cls: self.h))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_install_boots_out_then_bootstraps(self):
        res = launchd.install(launchd.RECONCILE, launchd.reconcile_plist(300), self.h)
        plist = self.h / "Library/LaunchAgents/com.orche.reconcile.plist"
        self.assertEqual(res["plist"], str(plist))
        self.assertTrue(res["loaded"])
        self.assertIsNone(res["error"])
        # enable before bootstrap: a job disabled by 0.6.x's `unload -w` would not bootstrap
        self.assertEqual(self.calls, [["launchctl", "bootout", f"gui/{UID}/com.orche.reconcile"],
                                      ["launchctl", "enable", f"gui/{UID}/com.orche.reconcile"],
                                      ["launchctl", "bootstrap", f"gui/{UID}", str(plist)]])
        self.assertEqual(oct(plist.stat().st_mode & 0o777), "0o644")
        res = launchd.install(launchd.RECONCILE, launchd.reconcile_plist(300), self.h)   # idempotent
        self.assertTrue(res["loaded"])
        self.assertEqual(self.calls[3:], self.calls[:3])
        res = launchd.uninstall(launchd.RECONCILE, self.h)
        self.assertEqual(self.calls[-1], ["launchctl", "bootout", f"gui/{UID}/com.orche.reconcile"])
        self.assertFalse(plist.exists())
        self.assertTrue(launchd.uninstall(launchd.RECONCILE, self.h)["missing"])

    def test_dry_run_writes_and_runs_nothing(self):
        res = launchd.install(launchd.RECONCILE, launchd.reconcile_plist(300), self.h, dry_run=True)
        self.assertEqual([a[1] for a in res["argv"]], ["bootout", "enable", "bootstrap"])
        self.assertFalse(Path(res["plist"]).exists())
        self.assertEqual(self.calls, [])


@unittest.skipUnless(sys.platform == "darwin", "the LaunchAgent is macOS only")
class InstallStepTest(InstallCase):
    def plist(self):
        return self.h / "Library/LaunchAgents/com.orche.reconcile.plist"

    def test_install_with_home_writes_but_never_loads(self):
        out = self.install("--mode", "settings", "--yes")
        self.assertIn("reconcile job", out)
        self.assertEqual(plistlib.loads(self.plist().read_bytes())["StartInterval"], 300)
        self.assertFalse([c for c in self.calls if c and c[0] == "launchctl"])
        log = self.home / support.LAUNCHCTL_CALLS
        self.assertFalse(log.exists() and "bootstrap" in log.read_text())
        self.assertEqual(self.orche("uninstall", "--home", str(self.h))[0], 0)
        self.assertFalse(self.plist().exists())

    def test_no_reconcile_skips(self):
        self.install("--mode", "settings", "--yes", "--no-reconcile")
        self.assertFalse(self.plist().exists())

    def test_doctor_rows(self):
        self.assertEqual(doctor.reconcile_job_rows(self.h)[0][0:2], ("WARN", "reconcile job"))
        self.install("--mode", "settings", "--yes")
        level, _, detail = doctor.reconcile_job_rows(self.h)[0]  # the stub launchctl exits 113
        self.assertEqual(level, "WARN")
        self.assertTrue(detail.startswith(f"not loaded: launchctl bootstrap gui/{UID} "), detail)
        with mock.patch.object(launchd, "loaded", lambda label: True):
            self.assertEqual(doctor.reconcile_job_rows(self.h)[0], ("OK", "reconcile job", "loaded, every 300s"))
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["reconcile"] = {"enabled": True, "every_s": 120}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.assertIn("every 300s, reconcile.every_s is 120", doctor.reconcile_job_rows(self.h)[0][2])
        cfg["reconcile"] = {"enabled": False}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(doctor.reconcile_job_rows(self.h)[0][0], "OK")


class KickTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        reconcile.kick_path().unlink()
        self.spawns = []
        p = mock.patch.object(procs, "spawn_detached", lambda argv, log, **kw: self.spawns.append((argv, log)))
        p.start()
        self.addCleanup(p.stop)

    def heartbeat(self, age_s):
        reconcile.write_heartbeat(units.now() - timedelta(seconds=age_s), reconcile.settings(), 0.1, 0)

    def test_missing_heartbeat_spawns_once(self):
        self.assertTrue(reconcile.kick())
        self.assertFalse(reconcile.kick())  # .kicked is now fresh
        self.assertEqual(len(self.spawns), 1)
        argv, log = self.spawns[0]
        self.assertEqual(argv[-1], "reconcile")
        self.assertEqual(log, self.home / "logs" / "reconcile.log")

    def test_stale_heartbeat_spawns(self):
        self.heartbeat(3 * 300 + 60)
        self.assertTrue(reconcile.kick())
        self.assertEqual(len(self.spawns), 1)

    def test_fresh_heartbeat_does_not_spawn(self):
        self.heartbeat(60)
        self.assertFalse(reconcile.kick())
        self.assertEqual(self.spawns, [])

    def test_fresh_marker_does_not_spawn(self):
        reconcile.kick_path().touch()
        self.assertFalse(reconcile.kick())
        self.assertEqual(self.spawns, [])

    def test_disabled_does_not_spawn(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["reconcile"] = {"enabled": False}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.assertFalse(reconcile.kick())
        self.assertEqual(self.spawns, [])


if __name__ == "__main__":
    unittest.main()
