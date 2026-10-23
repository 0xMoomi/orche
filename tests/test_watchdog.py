"""0.7.2: the reconcile pass watchdog (SIGALRM, then a last-resort thread), last-abort.json and the
heartbeat's `job_at` (a pass the LaunchAgent ran)."""
import fcntl
import io
import json
import os
import signal
import sys
import time
from contextlib import redirect_stderr
from unittest import mock

import support  # noqa: F401  (lib/ on sys.path)
from orche import privacy, reconcile, registry
from orche.install import doctor
from test_reconcile import WAKE, ReconcileCase


class WatchdogCase(ReconcileCase):
    def setUp(self):
        super().setUp()
        self.userhome = self.home / "userhome"
        p = mock.patch.object(privacy.Path, "home", classmethod(lambda cls: self.userhome))
        p.start()
        self.addCleanup(p.stop)

    def run_pass(self, **kw):
        err = io.StringIO()
        started = time.monotonic()
        with redirect_stderr(err):
            code = reconcile.run_pass(out=lambda *_: None, **kw)
        return code, err.getvalue(), time.monotonic() - started

    def abort(self):
        return json.loads((self.home / "reconcile" / "last-abort.json").read_text())

    def events(self):
        path = self.home / "logs" / "events.log"
        return [ln for ln in path.read_text().splitlines() if "reconcile abort" in ln] if path.exists() else []

    def lock_free(self) -> bool:
        with open(self.home / "reconcile" / ".lock", "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fh, fcntl.LOCK_UN)
            return True


class WatchdogTest(WatchdogCase):
    def test_a_pass_stuck_in_a_protected_folder_is_aborted_with_the_privacy_hint(self):
        def observe(*_a, **_kw):   # a scandir blocked by macOS privacy, as on 2026-10-23
            units_dir = self.userhome / "Downloads" / "acme" / ".orche" / "units"
            time.sleep(5)
            return [units_dir]
        before = signal.getsignal(signal.SIGALRM)
        with mock.patch.object(reconcile, "observe", observe):
            code, err, took = self.run_pass(watchdog_s=1)
        self.assertEqual(code, 3)
        self.assertLess(took, 4)
        where = str(self.userhome / "Downloads" / "acme" / ".orche" / "units")
        self.assertEqual(self.abort(), {"at": "2026-10-16T09:00:00+09:00", "phase": "observe", "path": where,
                                        "hint": "privacy"})
        self.assertEqual(err, f"orche: reconcile: aborted after 1 s in phase observe at {where} "
                              "(macOS privacy: run orche install --grant)\n")
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.events()[0].endswith("reconcile abort phase=observe hint=privacy"))
        self.assertTrue(self.lock_free())
        self.assertFalse(reconcile.heartbeat_path().exists())   # no pass completed
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_a_slow_phase_elsewhere_is_slow(self):
        def heartbeat(*_a, **_kw):
            target = "/srv/orca/acme-shop/.orche/units"     # under a registered project's root
            time.sleep(5)
            return [target]
        with mock.patch.object(reconcile.owner, "heartbeat", heartbeat):
            code, _err, _took = self.run_pass(watchdog_s=1)
        self.assertEqual(code, 3)
        rec = self.abort()
        self.assertEqual((rec["phase"], rec["path"], rec["hint"]), ("owner", "/srv/orca/acme-shop/.orche/units", "slow"))
        self.assertTrue(self.events()[0].endswith("reconcile abort phase=owner hint=slow"))

    def test_the_thread_ends_a_pass_the_signal_cannot_interrupt(self):
        """A call blocked inside the kernel: SIGALRM is blocked in the main thread here, so only the
        last-resort thread can act; it records the abort, frees the lock, then exits 3."""
        seen = []

        def fake_exit(code):
            seen.append((code, self.lock_free(), self.abort()["hint"]))

        def observe(*_a, **_kw):
            folder = self.userhome / "Documents" / "acme"
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            try:
                time.sleep(1)       # the thread acts at 0.3 + 0.3 s
            finally:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
            return [folder]
        with mock.patch.object(reconcile, "observe", observe), mock.patch.object(reconcile, "_exit", fake_exit), \
                mock.patch.object(reconcile, "WATCHDOG_GRACE_S", 0.3):
            code, err, _took = self.run_pass(watchdog_s=0.3)
        self.assertEqual(seen, [(3, True, "privacy")])
        self.assertEqual(self.abort()["path"], str(self.userhome / "Documents" / "acme"))
        # the thread wrote its line (reconcile.log under launchd) before its _exit
        self.assertTrue(err.startswith("orche: reconcile: aborted after 0.3 s in phase observe at "), err)
        self.assertEqual(code, 3)   # in a real process the thread's os._exit(3) came first

    def test_a_normal_pass_disarms_the_watchdog(self):
        before = signal.getsignal(signal.SIGALRM)
        code, _err, _took = self.run_pass(watchdog_s=5)
        self.assertEqual(code, 0)
        self.assertTrue(reconcile.heartbeat_path().exists())
        self.assertFalse((self.home / "reconcile" / "last-abort.json").exists())
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_tests_run_without_the_watchdog(self):
        self.assertEqual(reconcile.WATCHDOG_S, 0)
        self.assertEqual(reconcile.run_pass.__defaults__[-1], None)


class StuckPathTest(WatchdogCase):
    def test_innermost_path_wins_and_none_is_empty(self):
        def inner(name, folder):
            return reconcile.stuck_path(sys._getframe())

        def outer():
            root = self.userhome / "Documents" / "outer"
            return inner("x", root / "inner")
        self.assertEqual(outer(), str(self.userhome / "Documents" / "outer" / "inner"))
        self.assertEqual(reconcile.stuck_path(None), "")
        self.assertEqual(reconcile._abs_path("relative/x"), "")

    def test_only_project_protected_or_volume_paths_count(self):   # review #7
        def apply_like(reg, action):   # the reviewer's case: an Orca call hung, no path-valued locals
            timeout, argv, own = 10, ["orca", "terminal", "read"], "/opt/orche/bin/orche.py"
            return reconcile.stuck_path(sys._getframe())
        self.assertEqual(apply_like({"projects": {}}, object()), "")    # the module's __file__ never counts
        with mock.patch.object(reconcile, "ROOTS", ("/srv/orca/acme-shop",)):
            self.assertEqual((lambda here: reconcile.stuck_path(sys._getframe()))("/srv/orca/other"), "")
            for path in ("/srv/orca/acme-shop/.orche", "/Volumes/USB/p"):
                self.assertEqual((lambda here: reconcile.stuck_path(sys._getframe()))(path), path)

    def test_the_walk_stops_at_run_pass(self):
        seen = []

        def observe(*_a, **_kw):
            seen.append(reconcile.stuck_path(sys._getframe()))
            return []
        here = str(self.userhome / "Downloads" / "x")   # a protected path held by this test's frame: never reached
        with mock.patch.object(reconcile, "observe", observe):
            self.assertEqual(self.run_pass()[0], 0)
        self.assertEqual((seen, here[0]), ([""], "/"))

    def test_protected_folders_are_string_checks_under_the_home(self):
        for rel in ("Downloads", "downloads/a", "Documents/x/y", "Desktop", "Library/Mobile Documents/com~apple~CloudDocs/p",
                    "Library/CloudStorage/GoogleDrive-me/p"):
            self.assertTrue(privacy.protected(self.userhome / rel), rel)
        self.assertTrue(privacy.protected("/Volumes/Work/p"))
        for path in (self.userhome / "DownloadsX", self.userhome / "orca" / "p", "/Users/someone-else/Downloads", "",
                     "/Volumes", "/Volumes/"):
            self.assertFalse(privacy.protected(path), path)

    def test_the_home_is_compared_by_its_real_path(self):   # review #8: a registered path is a real path
        real = self.home / "real-home"
        (real / "Documents").mkdir(parents=True)
        link = self.home / "link-home"
        link.symlink_to(real)
        self.assertTrue(privacy.protected(os.path.realpath(real / "Documents" / "p"), home=link))
        self.assertTrue(privacy.protected(link / "Documents" / "p", home=link))


class JobAtTest(WatchdogCase):
    def test_only_the_launchd_job_stamps_job_at_and_other_passes_carry_it(self):
        with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "com.orche.reconcile"}):
            self.assertEqual(self.run_pass()[0], 0)
        hb = reconcile.read_heartbeat()
        self.assertEqual(hb["job_at"], hb["at"])
        self.now = self.now.replace(minute=30)
        with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "0"}):
            self.assertEqual(self.run_pass()[0], 0)
        hb2 = reconcile.read_heartbeat()
        self.assertEqual((hb2["at"], hb2["job_at"]), ("2026-10-16T09:30:00+09:00", hb["job_at"]))

    def test_the_plist_marker_alone_is_a_job_pass(self):   # review #11: XPC_SERVICE_NAME or ORCHE_RECONCILE_JOB
        with mock.patch.dict(os.environ, {"ORCHE_RECONCILE_JOB": "1", "XPC_SERVICE_NAME": "0"}):
            self.assertTrue(reconcile.is_job())
            self.assertEqual(self.run_pass()[0], 0)
        self.assertEqual(reconcile.read_heartbeat()["job_at"], "2026-10-16T09:00:00+09:00")
        for env in ({"ORCHE_RECONCILE_JOB": "0", "XPC_SERVICE_NAME": "0"},
                    {"ORCHE_RECONCILE_JOB": "", "XPC_SERVICE_NAME": "com.orche.reconcile"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(reconcile.is_job(), env["XPC_SERVICE_NAME"] == "com.orche.reconcile")


class DeniedFolderTest(WatchdogCase):
    """0.7.2 review #1: after "Don't Allow" macOS answers EPERM under a protected folder and a glob there
    returns [] silently, so the job's pass looked healthy and --grant said granted. chmod 000 on the
    project's .orche stands in for the denial (restored in tearDown, before the temp home goes)."""

    def setUp(self):
        super().setUp()
        self.root = self.userhome / "Downloads" / "acme-shop"
        self.dot = self.root / ".orche"
        (self.dot / "units").mkdir(parents=True)
        reg = registry.load()
        reg["projects"]["acme-shop"]["path"] = str(self.root)
        registry.save(reg)
        os.chmod(self.dot, 0)

    def tearDown(self):
        if self.dot.exists():
            os.chmod(self.dot, 0o755)
        super().tearDown()

    def job_pass(self):
        with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "com.orche.reconcile"}):
            return self.run_pass()[0]

    def job(self):
        return {"darwin": True, "loaded": True, "load_at": self.now - reconcile.ABORT_TTL / 48,
                "protected": privacy.protected_projects(registry.load())}

    def test_a_denied_folder_is_unreadable_not_granted(self):
        since = self.now
        self.assertEqual(self.job_pass(), 0)
        hb = reconcile.read_heartbeat()
        self.assertEqual((hb["unreadable"], hb["job_at"]), (["acme-shop"], hb["at"]))
        self.assertIsNone(privacy.job_pass(since))           # --grant and the install keep waiting
        where = f"1 project(s) under a protected folder ({self.root})"
        self.assertEqual(doctor.privacy_rows(self.job()), [
            ("WARN", "privacy", f"{where}; the heartbeat job cannot read acme-shop (macOS privacy) — "
                                "run: orche install --grant")])
        self.assertEqual(reconcile.doctor_row(job=self.job()), (
            "WARN", "heartbeat", "heartbeat job cannot read acme-shop (macOS privacy) — run: orche install --grant"))
        self.assertEqual(reconcile.heartbeat_line(self.now),
                         "heartbeat: blocked by macOS privacy — run orche install --grant")
        self.assertEqual(self.run_pass()[0], 0)               # a hook-kicked pass cannot tell: carried over
        self.assertEqual(reconcile.read_heartbeat()["unreadable"], ["acme-shop"])
        os.chmod(self.dot, 0o755)                             # granted
        self.assertEqual(self.job_pass(), 0)
        self.assertNotIn("unreadable", reconcile.read_heartbeat())
        self.assertIsNotNone(privacy.job_pass(since))
        self.assertEqual(doctor.privacy_rows(self.job())[0][0], "OK")

    def test_a_missing_dot_orche_is_not_unreadable(self):
        os.chmod(self.dot, 0o755)
        (self.dot / "units").rmdir()
        self.dot.rmdir()
        self.assertEqual(self.job_pass(), 0)
        self.assertNotIn("unreadable", reconcile.read_heartbeat())

    def test_an_off_pass_never_stamps_job_at(self):
        (self.home / "config.json").write_text(json.dumps({**WAKE, "reconcile": {"enabled": False}}))
        self.assertEqual(self.job_pass(), 0)
        hb = reconcile.read_heartbeat()
        self.assertEqual(hb["enabled"], False)
        self.assertNotIn("job_at", hb)
        self.assertNotIn("unreadable", hb)
