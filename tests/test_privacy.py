"""0.7.2: macOS privacy and the heartbeat job — the doctor's heartbeat findings and `privacy` row, the
install's first-pass check and `orche install --grant`, and the Python.app to grant."""
import copy
import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import support
import test_install
from orche import privacy, reconcile, registry, shell, units, util
from orche.install import doctor, launchd

UID = os.getuid()

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 10, 23, 5, 0, tzinfo=KST)


class PrivacyCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.userhome = self.home / "userhome"
        self.now = NOW
        for p in (mock.patch.object(privacy.Path, "home", classmethod(lambda cls: self.userhome)),
                  mock.patch.object(units, "now", lambda: self.now)):
            p.start()
            self.addCleanup(p.stop)
        registry.save(copy.deepcopy(SAMPLE))
        self.units_dir = str(self.userhome / "Downloads" / "acme-shop" / ".orche" / "units")

    def protect(self):
        reg = registry.load()
        reg["projects"]["acme-shop"]["path"] = str(self.userhome / "Downloads" / "acme-shop")
        registry.save(reg)

    def heartbeat(self, min_ago, job_min_ago=None):
        doc = {"at": (self.now - dt.timedelta(minutes=min_ago)).isoformat(timespec="seconds"), "every_s": 300,
               "took_s": 0.31, "actions": 0, "enabled": True, "day": self.now.date().isoformat(), "repairs_today": 0}
        if job_min_ago is not None:
            doc["job_at"] = (self.now - dt.timedelta(minutes=job_min_ago)).isoformat(timespec="seconds")
        util.write_json(reconcile.heartbeat_path(), doc)

    def abort(self, min_ago, hint="privacy"):
        util.write_json(reconcile.abort_path(), {"at": (self.now - dt.timedelta(minutes=min_ago)).isoformat(
            timespec="seconds"), "phase": "observe", "path": self.units_dir, "hint": hint})

    def job(self, loaded=True, load_min_ago=20):
        return {"darwin": True, "loaded": loaded, "load_at": self.now - dt.timedelta(minutes=load_min_ago),
                "protected": privacy.protected_projects(registry.load())}


class DoctorHeartbeatTest(PrivacyCase):
    def test_a_privacy_abort_warns_until_a_job_pass_follows_it(self):
        self.heartbeat(2)
        self.abort(10)
        self.assertEqual(reconcile.doctor_row(), ("WARN", "heartbeat", f"heartbeat job cannot read {self.units_dir} "
                                                                       "(macOS privacy) — run: orche install --grant"))
        self.heartbeat(1, job_min_ago=1)
        self.assertEqual(reconcile.doctor_row()[0], "OK")
        self.heartbeat(2)
        self.abort(25 * 60)        # older than a day
        self.assertEqual(reconcile.doctor_row(), ("OK", "heartbeat", "last pass 2 min ago (every 5 min)"))
        self.abort(10, hint="slow")
        self.assertEqual(reconcile.doctor_row()[0], "OK")

    def test_a_loaded_job_with_no_pass_of_its_own(self):
        self.heartbeat(1)          # a hook-kicked pass: no job_at
        log = self.home / "logs" / "reconcile.log"
        self.assertEqual(reconcile.doctor_row(job=self.job()),
                         ("WARN", "heartbeat", f"heartbeat job loaded, no pass yet — run orche reconcile and read {log}"))
        self.protect()
        self.assertEqual(reconcile.doctor_row(job=self.job()),
                         ("WARN", "heartbeat", "heartbeat job loaded, no pass yet — run: orche install --grant"))
        self.assertEqual(reconcile.doctor_row(job=self.job(load_min_ago=5))[0], "OK")      # within 2 × every_s
        self.assertEqual(reconcile.doctor_row(job=self.job(loaded=False))[0], "OK")
        self.heartbeat(1, job_min_ago=19)                                                    # passed after its load
        self.assertEqual(reconcile.doctor_row(job=self.job())[0], "OK")


class NoPassTest(PrivacyCase):
    """Review #9: no_pass trusts a privacy abort only as job_blocked does (a day old at most, newer
    than the job's last pass); review #4: `orche status` says blocked."""

    def test_an_old_or_superseded_abort_is_not_privacy(self):
        reg = registry.load()                          # no project under a protected folder
        self.abort(10)
        self.assertEqual(privacy.no_pass(reg, live=False)[0], "privacy")
        self.assertEqual(reconcile.heartbeat_line(self.now),
                         "heartbeat: blocked by macOS privacy — run orche install --grant")
        self.heartbeat(1, job_min_ago=5)               # a job pass after it
        self.assertEqual(privacy.no_pass(reg, live=False)[0], "slow")
        self.assertEqual(reconcile.heartbeat_line(self.now), "heartbeat: last pass 1 min ago (every 5 min, "
                                                              "0 repairs today)")
        self.heartbeat(1)
        self.abort(25 * 60)                            # older than a day
        self.assertEqual(privacy.no_pass(reg, live=False)[0], "slow")
        self.protect()                                 # a protected project is enough on its own
        self.assertEqual(privacy.no_pass(registry.load(), live=False)[0], "privacy")


class PrivacyRowTest(PrivacyCase):
    def test_rows(self):
        self.assertEqual(doctor.privacy_rows({**self.job(), "darwin": False}), [("OK", "privacy", "not macOS")])
        self.assertEqual(doctor.privacy_rows(self.job()),
                         [("OK", "privacy", "no project under Desktop, Documents, Downloads, iCloud Drive, cloud "
                                            "storage or /Volumes")])
        self.protect()
        where = f"1 project(s) under a protected folder ({self.userhome / 'Downloads' / 'acme-shop'})"
        self.assertEqual(doctor.privacy_rows(self.job(loaded=False)),
                         [("OK", "privacy", f"{where}; the heartbeat job is not loaded (see reconcile job)")])
        self.heartbeat(1)
        warn = [("WARN", "privacy", f"{where}; no heartbeat job pass since it loaded — run: orche install --grant")]
        self.assertEqual(doctor.privacy_rows(self.job()), warn)
        self.heartbeat(1, job_min_ago=3)
        hhmm = f"{(self.now - dt.timedelta(minutes=3)).astimezone():%H:%M}"
        self.assertEqual(doctor.privacy_rows(self.job()),
                         [("OK", "privacy", f"{where}; the heartbeat job reads them (last pass {hhmm})")])
        self.abort(2)              # a later privacy abort: the grant is gone (e.g. a Python upgrade)
        self.assertEqual(doctor.privacy_rows(self.job()), warn)

    def test_job_state_reads_the_plist_mtime_and_launchctl(self):
        self.protect()
        plist = launchd.plist_path(launchd.RECONCILE, self.userhome)
        plist.parent.mkdir(parents=True)
        plist.write_text("x")
        os.utime(plist, (1_790_000_000.7, 1_790_000_000.7))
        calls = []

        def runner(argv, *, timeout, input, env=None):
            calls.append(argv)
            return shell.Result(0, "", "", argv)
        with mock.patch.object(shell, "RUNNER", runner), mock.patch("sys.platform", "darwin"):
            st = doctor.job_state(self.userhome)
        self.assertEqual(st["load_at"], dt.datetime.fromtimestamp(1_790_000_000).astimezone())
        self.assertTrue(st["loaded"])
        self.assertEqual(calls, [["launchctl", "print", f"gui/{os.getuid()}/com.orche.reconcile"]])
        self.assertEqual(st["protected"], [("acme-shop", str(self.userhome / "Downloads" / "acme-shop"))])
        self.assertFalse(doctor.job_state(self.home / "nowhere")["loaded"])


@unittest.skipUnless(sys.platform == "darwin", "the reconcile LaunchAgent is macOS only")
class FirstPassTest(test_install.InstallCase):
    """`orche install` with the real home patched to the test home: launchctl and open go through a
    recording runner; `launchctl kickstart` stands in for the job's pass once it may read the projects."""

    def setUp(self):
        super().setUp()
        self.now, self.t, self.ran = NOW, 0.0, []
        self.granted, self.grant_on_open, self.job_loaded = False, False, True
        inner = shell.RUNNER

        def runner(argv, *, timeout, input, env=None):
            if argv[0] not in ("launchctl", "open"):
                return inner(argv, timeout=timeout, input=input)
            self.ran.append(argv)
            if argv[:2] == ["launchctl", "print"] and not self.job_loaded:
                return shell.Result(113, "", "Could not find service", argv)
            if argv[:2] == ["launchctl", "kickstart"] and self.granted:   # the job's own pass
                with mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "com.orche.reconcile"}):
                    reconcile.write_heartbeat(self.now, {"every_s": 300}, 0.31, 0)
            if argv[:2] == ["open", "-R"] and self.grant_on_open:              # the owner drags it in
                self.granted = True
            return shell.Result(0, "", "", argv)

        def sleep(s):
            self.t += s
        for p in (mock.patch.object(shell, "RUNNER", runner),
                  mock.patch.object(launchd.Path, "home", classmethod(lambda cls: self.h)),
                  mock.patch.object(units, "now", lambda: self.now),
                  mock.patch.object(privacy, "_clock", lambda: self.t),
                  mock.patch.object(privacy, "_sleep", sleep)):
            p.start()
            self.addCleanup(p.stop)
        registry.save(copy.deepcopy(SAMPLE))
        self.app = privacy.python_app()

    def protect(self):
        reg = registry.load()
        reg["projects"]["acme-shop"]["path"] = str(self.h / "Downloads" / "acme-shop")
        registry.save(reg)

    def opens(self):
        return [a for a in self.ran if a[0] == "open"]

    def kicks(self):
        return [a for a in self.ran if a[:2] == ["launchctl", "kickstart"]]

    def test_first_pass_ok(self):
        self.granted = True
        lines = self.install("--mode", "settings", "--no-jev").splitlines()
        self.assertEqual(lines[-2:], [privacy.FIRST_PASS_WAIT, "reconcile job: first pass OK (0.31 s)"])
        self.assertEqual([a[1] for a in self.ran[:7]],   # the heartbeat, the janitor, then the first pass
                         ["bootout", "enable", "bootstrap", "bootout", "enable", "bootstrap", "kickstart"])
        self.assertTrue(self.ran[5][-1].endswith("com.orche.janitor.plist"), self.ran[5])
        # a plain kickstart (review #6): `-k` would kill the job's own pass started at load
        self.assertEqual(self.kicks(), [["launchctl", "kickstart", f"gui/{UID}/com.orche.reconcile"]])
        self.assertEqual(self.opens(), [])
        self.assertLessEqual(self.t, privacy.POLL_S)

    def test_a_pass_already_done_needs_no_kick(self):
        def bootstrapped(argv, *, timeout, input, env=None):    # RunAtLoad: the job's pass ran before the check
            if argv[:2] == ["launchctl", "bootstrap"]:
                with mock.patch.dict(os.environ, {"ORCHE_RECONCILE_JOB": "1"}):
                    reconcile.write_heartbeat(self.now, {"every_s": 300}, 0.2, 0)
            return runner(argv, timeout=timeout, input=input)
        runner = shell.RUNNER
        with mock.patch.object(shell, "RUNNER", bootstrapped):
            lines = self.install("--mode", "settings", "--no-jev").splitlines()
        self.assertEqual(lines[-1], "reconcile job: first pass OK (0.2 s)")
        self.assertEqual(self.kicks(), [])

    def test_ctrl_c_during_the_wait_is_one_line_and_130(self):
        def interrupt(_s):
            raise KeyboardInterrupt
        with mock.patch.object(privacy, "_sleep", interrupt):
            code, out, err = self.orche("install", "--home", str(self.h), "--mode", "settings", "--no-jev")
            self.assertEqual((code, err), (130, privacy.INTERRUPTED + "\n"))
            self.assertTrue(out.endswith(privacy.FIRST_PASS_WAIT + "\n"), out)
            self.protect()
            code, out, err = self.orche("install", "--home", str(self.h), "--grant")
            self.assertEqual((code, err), (130, privacy.INTERRUPTED + "\n"))

    def test_timeout_with_a_protected_project_prints_the_guidance_and_opens_both(self):
        self.protect()
        lines = self.install("--mode", "settings", "--no-jev").splitlines()
        name = self.app.name
        guidance = [g.format(name=name) for g in privacy.GUIDANCE]
        self.assertEqual(lines[-2:], [*guidance, f"  {name}: {self.app}"])   # last (review #10)
        self.assertTrue(guidance[0].startswith("ORCHE's background job (the heartbeat) cannot read the project folder"))
        self.assertIn(f"selected in Finder ({name})", guidance[0])
        self.assertLess(lines.index("jev: skipped (--no-jev); add later with `orche jev setup`"),
                        lines.index(privacy.FIRST_PASS_WAIT))
        self.assertEqual(self.opens(), [["open", "-R", str(self.app)], ["open", privacy.SETTINGS_URL]])
        self.assertGreaterEqual(self.t, privacy.FIRST_PASS_S)
        code, out, _ = self.orche("--json", "install", "--home", str(self.h), "--mode", "settings", "--no-jev")
        self.assertEqual((code, json.loads(out)["reconcile_first_pass"]), (0, {"ok": False, "took_s": None,
                                                                              "hint": "privacy"}))

    def test_timeout_elsewhere_names_the_log(self):
        lines = self.install("--mode", "settings", "--no-jev").splitlines()
        self.assertIn(f"reconcile job: no pass within 45 s — run orche reconcile and read "
                      f"{self.home / 'logs' / 'reconcile.log'}", lines)
        self.assertEqual(self.opens(), [])

    def test_home_elsewhere_or_dry_run_skips_the_check(self):
        self.install("--mode", "settings", "--no-jev", "--dry-run")
        self.assertEqual(self.kicks(), [])

    def test_grant_opens_both_and_waits_for_the_pass(self):
        self.protect()
        self.grant_on_open = True
        before = test_install.tree_digest(self.h)
        code, out, err = self.orche("install", "--home", str(self.h), "--grant")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(test_install.tree_digest(self.h), before)    # --grant installs nothing
        lines = out.splitlines()
        self.assertEqual(lines, [privacy.grant_wait(privacy.GRANT_QUICK_S), *privacy.steps(self.app),
                                 "checking access… (up to 5 min)", privacy.GRANTED])
        self.assertEqual(lines[0], "checking access… (up to 15 s)")
        self.assertEqual(self.opens(), [["open", "-R", str(self.app)], ["open", privacy.SETTINGS_URL]])
        # the quick check's kick (-k, once), then a plain one once the panes are open
        self.assertEqual(self.kicks(), [["launchctl", "kickstart", "-k", f"gui/{UID}/com.orche.reconcile"],
                                        ["launchctl", "kickstart", f"gui/{UID}/com.orche.reconcile"]])

    def test_grant_still_blocked_after_five_minutes(self):
        self.protect()
        code, out, _ = self.orche("install", "--home", str(self.h), "--grant")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[-1], privacy.blocked(self.app))
        self.assertGreaterEqual(self.t, privacy.GRANT_QUICK_S + privacy.GRANT_S)
        kicks = self.kicks()
        self.assertEqual(len(kicks), 1 + privacy.GRANT_S // privacy.KICK_EVERY_S)   # every 30 s
        self.assertEqual([k for k in kicks if "-k" in k], kicks[:1])                   # -k at most once

    def test_grant_already_granted_opens_nothing(self):
        self.protect()
        self.granted = True
        code, out, _ = self.orche("install", "--home", str(self.h), "--grant")
        self.assertEqual((code, out), (0, f"{privacy.grant_wait(privacy.GRANT_QUICK_S)}\n{privacy.GRANTED}\n"))
        self.assertEqual(self.opens(), [])

    def test_grant_with_nothing_protected_opens_nothing(self):
        code, out, err = self.orche("install", "--home", str(self.h), "--grant")
        self.assertEqual((code, out, err), (0, "nothing to grant — no project is under a protected folder\n", ""))
        self.assertEqual((self.opens(), self.kicks()), ([], []))

    def test_grant_texts(self):
        self.assertEqual(privacy.GRANTED, "granted — heartbeat OK")
        self.assertEqual(privacy.blocked(Path("/x/Python.app")),
                         "still blocked — switch Python.app on in Full Disk Access, then: orche install --grant")
        steps = privacy.steps(Path("/u/.local/bin/python3.12"))
        self.assertTrue(steps[0].startswith("1. Drag the item selected in the Finder window that just opened (python3.12)"))
        self.assertEqual(steps[2], "3. Keep this terminal open. ORCHE checks automatically for up to 5 minutes.")
        self.assertEqual(steps[-1], "  python3.12: /u/.local/bin/python3.12")

    def test_grant_without_the_job_names_the_installed_mode(self):
        self.protect()
        self.job_loaded = False
        code, out, err = self.orche("install", "--home", str(self.h), "--grant")
        self.assertEqual((code, out, err), (1, "", "orche: " + privacy.NOT_LOADED.format(mode="settings") + "\n"))
        self.assertIn("run: orche install --mode settings", err)
        util.write_json(self.home / "install-state.json", {"mode": "plugin"})
        code, out, err = self.orche("install", "--home", str(self.h), "--grant")
        self.assertEqual((code, err), (1, "orche: " + privacy.NOT_LOADED.format(mode="plugin") + "\n"))


class PythonAppTest(support.TempHomeCase):
    def test_homebrew_framework_interpreter_resolves_to_its_python_app(self):
        version = self.home / "Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14"
        (version / "bin").mkdir(parents=True)
        exe = version / "bin" / "python3.14"
        exe.write_text("")
        app = version / "Resources" / "Python.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        link = self.home / "opt-bin" / "python3.14"     # /opt/homebrew/bin/python3.14 is a symlink
        link.parent.mkdir()
        link.symlink_to(exe)
        want = Path(os.path.realpath(app))
        self.assertEqual(privacy.python_app(link), want)
        self.assertEqual(privacy.python_app(app / "Contents" / "MacOS" / "Python"), want)

    def test_plain_interpreter_is_its_real_path(self):
        exe = self.home / "usr" / "bin" / "python3"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        self.assertEqual(privacy.python_app(exe), Path(os.path.realpath(exe)))
        bare = self.home / "Python.framework" / "Versions" / "3.13" / "bin" / "python3.13"   # no Python.app
        bare.parent.mkdir(parents=True)
        bare.write_text("")
        self.assertEqual(privacy.python_app(bare), Path(os.path.realpath(bare)))

    def test_the_jobs_own_interpreter_is_preferred(self):   # review #12: uv/pyenv/conda run a bare python3.x
        uv = self.home / "uv" / "cpython-3.12" / "bin" / "python3.12"
        uv.parent.mkdir(parents=True)
        uv.write_text("")
        with mock.patch.dict(os.environ, {"ORCHE_RECONCILE_JOB": "1"}), mock.patch.object(sys, "executable", str(uv)):
            reconcile.write_heartbeat(NOW, {"every_s": 300}, 0.1, 0)
            self.assertEqual(reconcile.record_abort(None)["interpreter"], os.path.realpath(uv))
        self.assertEqual(reconcile.read_heartbeat()["interpreter"], os.path.realpath(uv))
        reconcile.write_heartbeat(NOW, {"every_s": 300}, 0.1, 0)          # a hook-kicked pass carries it over
        app = privacy.python_app()
        self.assertEqual(app, Path(os.path.realpath(uv)))
        hint, lines = privacy.no_pass({"projects": {"p": {"path": str(Path.home() / "Documents" / "p")}}},
                                      live=False)
        self.assertEqual(hint, "privacy")
        self.assertIn("selected in Finder (python3.12)", lines[0])
        self.assertEqual(lines[-1], f"  python3.12: {app}")
