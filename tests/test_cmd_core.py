"""CLI core: status, depts, statusline, hr, sleep/wake, card (golden strings on a two-project registry)."""
import copy
import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import hr, registry, shell, units
from test_registry import load_cli

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
NOW = dt.datetime(2026, 10, 16, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


class CoreCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.reg = copy.deepcopy(SAMPLE)
        self.reg["events"].append({"at": "2026-10-15T18:00:00+09:00", "type": "create",
                                   "project": "acme-shop", "unit": "build/search", "by": "div-acme-shop-build"})
        registry.save(self.reg)
        (self.home / "config.json").write_text(json.dumps({"notify": False}))
        self._orig_now, units.now = units.now, lambda: NOW
        self.addCleanup(setattr, units, "now", self._orig_now)
        self.calls = []
        self._orig_runner, shell.RUNNER = shell.RUNNER, self.fake_runner
        self.addCleanup(setattr, shell, "RUNNER", self._orig_runner)
        self.terminals = []

    def fake_runner(self, argv, *, timeout, input):
        self.calls.append(argv)
        result = {"terminals": self.terminals} if argv[1:3] == ["terminal", "list"] else {}
        return shell.Result(0, json.dumps({"ok": True, "result": result}), "", argv)

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write(self, rel, data):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def counters(self):
        self.write("secrets/requests/a.json", {"name": "STRIPE_KEY", "project": "acme-shop", "status": "pending"})
        self.write("secrets/requests/b.json", {"name": "OLD", "project": "acme-shop", "status": "filled"})
        self.write("secrets/requests/c.json", {"name": "ALGOLIA", "project": "docs-site", "status": "pending"})
        self.write("inbox/div-acme-shop-build/p1.json", {"unit": "u1", "status": "done", "ledger_row": False})
        self.write("inbox/div-acme-shop-build/p2.json", {"unit": "u2", "status": "done", "ledger_row": True})
        self.write("inbox/main-docs-site/p3.json", {"unit": "u3", "status": "failed", "ledger_row": False})


class StatusTest(CoreCase):
    def test_status_golden(self):
        self.counters()
        code, out, err = self.orche("status")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (
            "acme-shop stage=building awake=2/12 divisions: rnd○ build●1"
            " · new:1 · keys:1 · unverified:1 · RUNNING 0\n"
            "docs-site stage=public awake=0/12 divisions: -"
            " · new:0 · keys:1 · unverified:0 · RUNNING 0\n"))

    def test_status_without_counter_dirs(self):
        code, out, _ = self.orche("status", "shop")
        self.assertEqual(code, 0)
        self.assertEqual(out, "acme-shop stage=building awake=2/12 divisions: rnd○ build●1"
                              " · new:1 · keys:0 · unverified:0 · RUNNING 0\n")

    def test_status_json_and_unknown(self):
        code, out, _ = self.orche("--json", "status", "acme")
        row = json.loads(out)[0]
        self.assertEqual((code, row["project"], row["awake"], row["divisions"]["build"]),
                         (0, "acme-shop", 2, "active"))
        self.assertEqual(self.orche("status", "nope")[0], 2)


class DeptsTest(CoreCase):
    def setUp(self):
        super().setUp()
        # LAST_ACTIVITY is shown in local time (units.now: "Current local time"); the golden is KST.
        self.addCleanup(time.tzset)
        self.addCleanup(lambda old=os.environ.get("TZ"): os.environ.pop("TZ", None) if old is None
                        else os.environ.__setitem__("TZ", old))
        os.environ["TZ"] = "KST-9"
        time.tzset()

    def test_depts_golden(self):
        code, out, err = self.orche("depts")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "PROJECT    UNIT            STATUS   SESSION                  BRANCH         LAST_ACTIVITY              IDLE  RUNNING",
            "acme-shop  rnd             dormant  div-acme-shop-rnd        div/rnd        2026-10-01T09:00:00+09:00  15d   0",
            "acme-shop  build           active   div-acme-shop-build      div/build      2026-10-15T18:30:00+09:00  0d    0",
            "acme-shop  build/checkout  active   feat-acme-shop-checkout  feat/checkout  2026-10-15T18:00:00+09:00  0d    0",
            "acme-shop  build/search    paused   feat-acme-shop-search    feat/search    2026-10-03T10:00:00+09:00  12d   0",
        ])

    def test_depts_shows_last_activity_in_the_local_zone(self):
        os.environ["TZ"] = "UTC0"
        time.tzset()
        self.assertIn("2026-10-01T00:00:00+00:00", self.orche("depts")[1].splitlines()[1])

    def test_depts_project_without_divisions(self):
        self.assertEqual(self.orche("depts", "docs-site")[:2], (0, "no divisions\n"))

    def test_depts_json(self):
        rows = json.loads(self.orche("depts", "--json")[1])
        self.assertEqual([r["unit"] for r in rows], ["rnd", "build", "build/checkout", "build/search"])
        self.assertEqual(rows[0]["idle_days"], 15)


class StatuslineTest(CoreCase):
    def test_unregistered_cwd_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.orche("statusline", "--cwd", tmp), (0, "", ""))
        self.assertFalse((self.home / "status").exists())

    def test_registered_path_writes_line(self):
        self.counters()
        code, out, err = self.orche("statusline", "--cwd", "/srv/orca/acme-shop/src/app")
        line = "R&D○ BUILD●1 · new1 · 🔑1 · unverified1"
        self.assertEqual((code, out, err), (0, line + "\n", ""))
        self.assertEqual((self.home / "status" / "acme-shop.line").read_text(), line + "\n")

    def test_one_roster_per_refresh(self):
        from orche import roster
        built, orig = [], roster.Roster

        class Counting(orig):
            def __init__(self, *a, **k):
                built.append(1)
                super().__init__(*a, **k)

        roster.Roster = Counting
        self.addCleanup(setattr, roster, "Roster", orig)
        self.counters()
        out = self.orche("statusline", "--cwd", "/srv/orca/acme-shop/src/app")[1]
        self.assertEqual(out, "R&D○ BUILD●1 · new1 · 🔑1 · unverified1\n")
        self.assertEqual(len(built), 1)

    def test_marker_and_zero_cells_omitted(self):
        self.counters()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".orche").mkdir()
            (Path(tmp) / ".orche" / "project").write_text("docs-site\n")
            (Path(tmp) / "sub").mkdir()
            code, out, _ = self.orche("statusline", "--cwd", str(Path(tmp) / "sub"))
        self.assertEqual((code, out), (0, "🔑1\n"))

    def test_unit_worktree_resolves_project(self):
        out = self.orche("statusline", "--cwd", "/srv/orca/acme-shop-checkout")[1]
        self.assertEqual(out, "R&D○ BUILD●1 · new1\n")

    def test_missing_registry_is_silent(self):
        (self.home / "registry.json").unlink()
        self.assertEqual(self.orche("statusline", "--cwd", "/srv/orca/acme-shop"), (0, "", ""))

    def test_fast_without_orca(self):
        start = time.perf_counter()
        self.orche("statusline", "--cwd", "/srv/orca/acme-shop")
        self.assertLess(time.perf_counter() - start, 0.05)
        self.assertEqual(self.calls, [])


class RunningVisibilityTest(CoreCase):
    """Live `orche run` lock files (ORCHE_HOME/runs/<proj>/<unit>.lock) show in status, depts, statusline."""

    def lock(self, unit, to, pid):
        self.write(f"runs/acme-shop/{unit}.lock", {"pid": pid, "unit": unit, "to": to})

    def fake_runner(self, argv, *, timeout, input):
        if argv[0] == "ps":       # reconcile's pid check sees the real process table
            return self._orig_runner(argv, timeout=timeout, input=input)
        return super().fake_runner(argv, timeout=timeout, input=input)

    def holder(self, unit):
        """A live process `ps` shows as `orche run <unit>` (status/depts reconcile other pids away)."""
        script = self.home / "launcher" / "orche"
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n")
        script.chmod(0o755)
        proc = subprocess.Popen([str(script), "run", unit])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        deadline = time.monotonic() + 10
        while f"run {unit}" not in subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(proc.pid)],
                                                  capture_output=True, text=True).stdout:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        return proc.pid

    def setUp(self):
        super().setUp()
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.lock("acme-shop-a-20261016-1", "div-acme-shop-build", self.holder("acme-shop-a-20261016-1"))
        self.lock("acme-shop-b-20261016-2", "feat-acme-shop-checkout", self.holder("acme-shop-b-20261016-2"))
        self.lock("acme-shop-c-20261016-3", "div-acme-shop-build", dead.pid)   # stale: not counted

    def test_status_shows_running_per_project(self):
        out = self.orche("status")[1].splitlines()
        self.assertTrue(out[0].endswith(" · RUNNING 2"), out[0])
        self.assertTrue(out[1].endswith(" · RUNNING 0"), out[1])
        self.assertEqual(json.loads(self.orche("--json", "status", "acme")[1])[0]["running"], 2)

    def test_depts_counts_runs_per_session(self):
        rows = {r["unit"]: r["running"] for r in json.loads(self.orche("--json", "depts")[1])}
        self.assertEqual(rows, {"rnd": 0, "build": 1, "build/checkout": 1, "build/search": 0})
        self.assertTrue(self.orche("depts")[1].splitlines()[2].endswith("0d    1"))

    def test_statusline_run_cell(self):
        out = self.orche("statusline", "--cwd", "/srv/orca/acme-shop")[1]
        self.assertEqual(out, "R&D○ BUILD●1 · run2 · new1\n")


class CustomDivisionTest(CoreCase):
    """A custom division (`design`, label "Design") shows in status, depts and the status line, and
    sleeps/wakes like a standard one."""

    def setUp(self):
        super().setUp()
        proj = self.reg["projects"]["acme-shop"]
        proj["custom_divisions"] = {"design": {"label": "Design", "charter": "Owns the visual system.",
                                               "model": "claude-opus-5-5", "effort": "high"}}
        proj["divisions"]["design"] = {"status": "dormant", "worktree": "repo-acme::/srv/orca/acme-shop-design",
                                       "branch": "div/design", "session": "div-acme-shop-design",
                                       "model": "claude-opus-5-5",
                                       "last_activity": "2026-10-14T09:00:00+09:00"}
        registry.save(self.reg)

    def test_status_lists_the_custom_division_last(self):
        line = self.orche("status", "acme")[1]
        self.assertTrue(line.startswith("acme-shop stage=building awake=2/12 divisions: rnd○ build●1 design○ · "),
                        line)
        row = json.loads(self.orche("--json", "status", "acme")[1])[0]
        self.assertEqual(row["divisions"], {"rnd": "dormant", "build": "active", "design": "dormant"})

    def test_depts_lists_the_custom_division(self):
        rows = json.loads(self.orche("--json", "depts", "acme")[1])
        self.assertEqual([r["unit"] for r in rows], ["rnd", "build", "build/checkout", "build/search", "design"])
        self.assertEqual((rows[-1]["session"], rows[-1]["branch"], rows[-1]["idle_days"]),
                         ("div-acme-shop-design", "div/design", 2))

    def test_statusline_cell_is_the_label_short_form(self):
        self.assertEqual(self.orche("statusline", "--cwd", "/srv/orca/acme-shop-design")[1],
                         "R&D○ BUILD●1 DES○ · new1\n")
        from orche import statusline
        proj = self.reg["projects"]["acme-shop"]
        for label, cell in (("QA", "QA"), ("Brand & Docs", "BRA"), ("디자인 팀", "디자인"), ("--", "DES"),
                            ("Opsy", "OPSY"), ("Builders", "BUI"), ("R2D2", "RD")):
            proj["custom_divisions"]["design"]["label"] = label
            self.assertEqual(statusline.short_label(proj, "design"), cell, label)

    def test_statusline_cells_are_unique_and_never_a_standard_cell(self):
        """M1: 3, then 4, then 5 letters, then the 3-letter form plus a digit; first added keeps its cell."""
        from orche import statusline
        spec = self.reg["projects"]["acme-shop"]["custom_divisions"]["design"]
        labels = {"design": "Design", "designers": "Designers", "design2": "Designer", "design3": "Designers 2",
                  "gtmx": "G.T.M.", "opsx": "Op's", "qa": "QA", "qa2": "Q&A"}
        proj = {"custom_divisions": {n: {**spec, "label": lab} for n, lab in labels.items()}}
        self.assertEqual(statusline.custom_cells(proj),
                         {"design": "DES", "designers": "DESI", "design2": "DESIG", "design3": "DES2",
                          "gtmx": "GTM2", "opsx": "OPS2", "qa": "QA", "qa2": "QA2"})
        cells = list(statusline.custom_cells(proj).values())
        self.assertEqual(len(cells), len(set(cells)))
        self.assertFalse(set(cells) & set(statusline.SHORT.values()))

    def test_wake_dry_run_opens_the_custom_division(self):
        code, out, err = self.orche("--dry-run", "wake", "div-acme-shop-design")
        self.assertEqual((code, out), (0, "would wake div-acme-shop-design (dry-run)\n"), err)
        self.assertIn("--worktree id:repo-acme::/srv/orca/acme-shop-design --title div-acme-shop-design", err)
        self.assertEqual(registry.load(), self.reg)

    def test_hr_audit_reads_the_custom_division_files(self):
        from orche.hr import audit
        files = [str(p) for p in audit._decision_files(self.reg["projects"]["acme-shop"])]
        self.assertIn("/srv/orca/acme-shop-design/.orche/div/design/DECISIONS.tsv", files)


class HrTest(CoreCase):
    def test_freeze_unfreeze_events_and_log(self):
        self.assertEqual(self.orche("hr", "freeze")[:2], (0, "frozen\n"))
        self.assertEqual(self.orche("hr", "freeze")[:2], (0, "already frozen\n"))
        reg = registry.load()
        self.assertTrue(reg["frozen"])
        self.assertEqual(reg["events"][-1]["type"], "freeze")
        self.assertEqual(self.orche("hr", "unfreeze")[:2], (0, "unfrozen\n"))
        self.assertFalse(registry.load()["frozen"])
        code, out, _ = self.orche("hr", "log", "--tail", "2")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(" freeze by user") and lines[1].endswith(" unfreeze by user"), lines)
        self.assertEqual(self.calls, [])  # notify disabled in config

    def test_freeze_dry_run_mutates_nothing(self):
        self.assertEqual(self.orche("--dry-run", "hr", "freeze")[:2], (0, "would freeze (dry-run)\n"))
        self.assertFalse(registry.load()["frozen"])
        self.assertFalse(hr.log_path().exists())

    def test_log_creates_missing_file(self):
        self.assertEqual(self.orche("hr", "log")[:2], (0, "# HR log\n\n"))
        self.assertTrue(hr.log_path().exists())


class SleepWakeTest(CoreCase):
    def setUp(self):
        super().setUp()
        self.wt = self.home / "wt-build"
        self.reg["projects"]["acme-shop"]["divisions"]["build"]["worktree"] = f"repo-acme::{self.wt}"
        registry.save(self.reg)
        self.handoff = self.wt / ".orche" / "div" / "build" / "HANDOFF.md"

    def fresh_handoff(self):
        self.handoff.parent.mkdir(parents=True, exist_ok=True)
        self.handoff.write_text("next: integrate checkout\n")

    def test_sleep_refuses_stale_handoff(self):
        self.fresh_handoff()
        old = time.time() - 45 * 60
        os.utime(self.handoff, (old, old))
        code, out, err = self.orche("sleep", "div-acme-shop-build")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("HANDOFF.md is stale (modified 45 min ago:", err)
        self.assertIn("update it before sleeping", err)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["build"]["status"], "active")

    def test_sleep_refuses_missing_handoff(self):
        code, _, err = self.orche("sleep", "div-acme-shop-build")
        self.assertEqual(code, 3)
        self.assertIn("HANDOFF.md is stale (missing:", err)

    def test_sleep_dry_run_prints_orca_argv(self):
        self.fresh_handoff()
        code, out, err = self.orche("--dry-run", "sleep", "div-acme-shop-build")
        self.assertEqual((code, out), (0, "would sleep div-acme-shop-build (dry-run)\n"))
        self.assertEqual(err, "[dry-run] orca terminal close --terminal <div-acme-shop-build> --json\n")
        self.assertEqual(registry.load(), self.reg)
        self.assertEqual(self.calls, [])

    def test_sleep_closes_pane_and_records(self):
        self.fresh_handoff()
        self.terminals = [{"handle": "t-9", "title": "div-acme-shop-build"}]
        code, out, _ = self.orche("sleep", "div-acme-shop-build")
        self.assertEqual((code, out), (0, "div-acme-shop-build paused\n"))
        self.assertEqual(self.calls[-1], ["orca", "terminal", "close", "--terminal", "t-9", "--json"])
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["status"], "paused")
        self.assertEqual({k: reg["events"][-1][k] for k in ("type", "project", "unit")},
                         {"type": "sleep", "project": "acme-shop", "unit": "build"})
        self.assertIn("sleep div-acme-shop-build by user", hr.log_path().read_text())

    def test_sleep_without_pane_just_records(self):
        self.fresh_handoff()
        self.assertEqual(self.orche("sleep", "div-acme-shop-build")[:2],
                         (0, "div-acme-shop-build paused (no open pane)\n"))

    def test_wake_dry_run_prints_orca_argv(self):
        code, out, err = self.orche("wake", "feat-acme-shop-search", "--dry-run")
        self.assertEqual((code, out), (0, "would wake feat-acme-shop-search (dry-run)\n"))
        self.assertEqual(err, "[dry-run] orca terminal create --worktree id:repo-acme::/srv/orca/acme-shop-search"
                              f" --title feat-acme-shop-search --command PATH={support.ROOT / 'bin'}:$PATH"
                              " ORCHE_SESSION=feat-acme-shop-search claude --permission-mode bypassPermissions"
                              " --resume feat-acme-shop-search --json\n")
        self.assertEqual(registry.load(), self.reg)

    def test_wake_dry_run_json_argv_carries_bypass_permissions(self):
        code, out, _ = self.orche("--json", "wake", "feat-acme-shop-search", "--dry-run")
        self.assertEqual(code, 0)
        command = json.loads(out)["argv"][json.loads(out)["argv"].index("--command") + 1]
        self.assertIn(" --permission-mode bypassPermissions --resume feat-acme-shop-search", command)

    def test_wake_and_sleep_load_the_registry_once(self):
        from unittest import mock
        with mock.patch.object(registry, "load", wraps=registry.load) as load:
            self.assertEqual(self.orche("wake", "feat-acme-shop-search", "--dry-run")[0], 0)
            self.assertEqual(load.call_count, 1)
            load.reset_mock()
            self.orche("sleep", "div-acme-shop-build", "--dry-run")
            self.assertEqual(load.call_count, 1)

    def test_wake_creates_pane_and_records(self):
        code, out, _ = self.orche("wake", "feat-acme-shop-search")
        self.assertEqual((code, out), (0, "feat-acme-shop-search active\n"))
        self.assertEqual(self.calls[-1][:3], ["orca", "terminal", "create"])
        self.assertIn(f"PATH={support.ROOT / 'bin'}:$PATH ORCHE_SESSION=feat-acme-shop-search claude "
                      "--permission-mode bypassPermissions --resume feat-acme-shop-search", self.calls[-1])
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["features"][1]["status"], "active")
        self.assertEqual((reg["events"][-1]["type"], reg["events"][-1]["unit"]), ("wake", "build/search"))

    # -- R3: an active unit whose pane is gone ---------------------------------------------------
    def test_wake_active_unit_without_pane_reopens_it(self):
        self.reg["limits"]["max_awake_sessions"] = 1   # already counted as awake: the cap does not apply
        registry.save(self.reg)
        code, out, err = self.orche("wake", "div-acme-shop-build")
        self.assertEqual((code, out, err), (0, "div-acme-shop-build pane reopened\n", ""))
        self.assertEqual([c[1:3] for c in self.calls], [["terminal", "list"], ["terminal", "create"]])
        create = self.calls[-1]
        self.assertEqual(create[create.index("--worktree") + 1], f"id:repo-acme::{self.wt}")
        self.assertEqual(create[create.index("--title") + 1], "div-acme-shop-build")
        self.assertEqual(create[create.index("--command") + 1],
                         f"PATH={support.ROOT / 'bin'}:$PATH ORCHE_SESSION=div-acme-shop-build "
                         "claude --permission-mode bypassPermissions --resume div-acme-shop-build")
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["status"], "active")
        self.assertEqual(reg["events"], self.reg["events"])
        self.assertIn("reopen div-acme-shop-build pane by user", hr.log_path().read_text())
        self.assertTrue((self.home / "activity" / "div-acme-shop-build").is_file())

    def test_wake_active_unit_with_pane_stays_already_active(self):
        self.terminals = [{"handle": "t-9", "title": "✳ div-acme-shop-build"}]
        code, out, _ = self.orche("wake", "div-acme-shop-build")
        self.assertEqual((code, out), (0, "div-acme-shop-build is already active\n"))
        self.assertEqual([c[1:3] for c in self.calls], [["terminal", "list"]])
        self.assertEqual(registry.load(), self.reg)
        self.assertFalse(hr.log_path().exists())

    def test_wake_active_unit_without_pane_dry_run(self):
        code, out, err = self.orche("--dry-run", "wake", "div-acme-shop-build")
        self.assertEqual((code, out), (0, "would reopen div-acme-shop-build pane (dry-run)\n"))
        self.assertTrue(err.startswith("[dry-run] orca terminal create --worktree"), err)
        self.assertEqual([c[1:3] for c in self.calls], [["terminal", "list"]])
        self.assertEqual(registry.load(), self.reg)

    def test_wake_refuses_over_limit(self):
        self.reg["limits"]["max_awake_sessions"] = 2
        registry.save(self.reg)
        code, _, err = self.orche("wake", "feat-acme-shop-search")
        self.assertEqual(code, 3)
        self.assertIn("awake limit reached (2/2)", err)
        self.assertEqual(self.calls, [])

    def dormant_build(self, limit=12):
        self.reg["projects"]["acme-shop"]["divisions"]["build"]["status"] = "dormant"
        self.reg["limits"]["max_awake_sessions"] = limit
        registry.save(self.reg)

    def test_wake_feature_under_dormant_division_counts_toward_cap(self):
        self.dormant_build(limit=1)  # the active checkout feature alone fills the cap
        code, _, err = self.orche("wake", "feat-acme-shop-search")
        self.assertEqual(code, 3)
        self.assertIn("awake limit reached (1/1)", err)
        self.assertEqual(self.calls, [])

    def test_wake_feature_under_dormant_division_warns_and_proceeds(self):
        self.dormant_build()
        code, out, err = self.orche("wake", "feat-acme-shop-search")
        self.assertEqual((code, out), (0, "feat-acme-shop-search active\n"))
        self.assertEqual(err, "orche: warning: parent division build is dormant\n")
        self.assertEqual(registry.awake_sessions(registry.load()),
                         ["feat-acme-shop-checkout", "feat-acme-shop-search"])

    def test_unknown_unit(self):
        self.assertEqual(self.orche("sleep", "div-acme-shop-gtm")[0], 2)
        code, _, err = self.orche("wake", "nonsense")
        self.assertEqual(code, 2)
        self.assertIn("main-<proj> or liaison-<proj>", err)


class ProjectPaneTest(CoreCase):
    """`orche wake main-<proj>|liaison-<proj>` opens the project pane or switches to it."""
    WT = "id:repo-acme::/srv/orca/acme-shop"
    ENV = f"PATH={support.ROOT / 'bin'}:$PATH "

    def calls_of(self, sub):
        return [c for c in self.calls if c[1:3] == ["terminal", sub]]

    def test_wake_main_opens_a_pane_when_none_is_open(self):
        events = len(registry.load()["events"])
        self.assertEqual(self.orche("wake", "main-acme-shop"), (0, "main-acme-shop pane opened\n", ""))
        self.assertEqual(self.calls_of("create"), [
            ["orca", "terminal", "create", "--worktree", self.WT, "--title", "main-acme-shop", "--command",
             f"{self.ENV}ORCHE_SESSION=main-acme-shop claude --name main-acme-shop", "--json"]])
        self.assertEqual(self.calls_of("switch"), [])
        self.assertEqual(len(registry.load()["events"]), events)
        self.assertNotIn("--permission-mode", " ".join(self.calls_of("create")[0]))

    def test_wake_main_switches_to_an_open_pane(self):
        self.terminals = [{"handle": "t-main", "title": "✳ main-acme-shop"}]
        self.assertEqual(self.orche("wake", "main-acme-shop"), (0, "main-acme-shop pane switched to\n", ""))
        self.assertEqual(self.calls_of("create"), [])
        self.assertEqual(self.calls_of("switch"), [["orca", "terminal", "switch", "--terminal", "t-main", "--json"]])

    def test_wake_liaison_opens_with_the_liaison_agent(self):
        code, out, err = self.orche("--json", "wake", "liaison-acme-shop")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"session": "liaison-acme-shop", "pane": "opened"})
        self.assertEqual(self.calls_of("create")[0][-2],
                         f"{self.ENV}ORCHE_SESSION=liaison-acme-shop claude --agent liaison --name liaison-acme-shop")
        self.assertNotIn("--permission-mode", self.calls_of("create")[0][-2])

    def test_wake_main_dry_run_prints_create_argv_or_switch(self):
        code, out, err = self.orche("--dry-run", "wake", "main-acme-shop")
        self.assertEqual((code, out), (0, "would open main-acme-shop pane (dry-run)\n"))
        self.assertEqual(err, f"[dry-run] orca terminal create --worktree {self.WT} --title main-acme-shop --command "
                              f"{self.ENV}ORCHE_SESSION=main-acme-shop claude --name main-acme-shop --json\n")
        self.terminals = [{"handle": "t-main", "title": "main-acme-shop"}]
        self.assertEqual(self.orche("--dry-run", "wake", "main-acme-shop"), (0, "would switch to main-acme-shop\n", ""))
        self.assertEqual({c[2] for c in self.calls}, {"list"})

    def test_wake_main_dry_run_json_carries_the_create_argv(self):
        code, out, _ = self.orche("--json", "--dry-run", "wake", "main-acme-shop")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "session": "main-acme-shop", "pane": "opened", "dry_run": True,
            "argv": ["orca", "terminal", "create", "--worktree", self.WT, "--title", "main-acme-shop", "--command",
                     f"{self.ENV}ORCHE_SESSION=main-acme-shop claude --name main-acme-shop", "--json"]})

    def test_wake_main_ignores_awake_limit_and_stage(self):
        self.reg["limits"]["max_awake_sessions"] = 0
        self.reg["projects"]["acme-shop"]["stage"] = "building"
        registry.save(self.reg)
        self.assertEqual(self.orche("wake", "main-acme-shop")[0], 0)
        self.assertEqual(len(self.calls_of("create")), 1)

    def _orca_fails_on(self, sub, message):
        def runner(argv, *, timeout, input):
            if argv[1:3] == ["terminal", sub]:
                self.calls.append(argv)
                return shell.Result(1, json.dumps({"ok": False, "error": {"message": message}}), "", argv)
            return CoreCase.fake_runner(self, argv, timeout=timeout, input=input)
        shell.RUNNER = runner

    def test_wake_main_reports_a_failed_terminal_list(self):
        self._orca_fails_on("list", "list broke")
        self.assertEqual(self.orche("wake", "main-acme-shop"),
                         (1, "", "orche: could not open or switch to main-acme-shop: orca_error: list broke\n"))
        self.assertEqual(self.calls_of("create") + self.calls_of("switch"), [])

    def test_wake_main_reports_a_failed_terminal_create(self):
        self._orca_fails_on("create", "create broke")
        self.assertEqual(self.orche("wake", "main-acme-shop"),
                         (1, "", "orche: could not open or switch to main-acme-shop: orca_error: create broke\n"))
        self.assertEqual(len(self.calls_of("create")), 1)

    def test_wake_main_reports_a_failed_terminal_switch(self):
        self.terminals = [{"handle": "t-main", "title": "main-acme-shop"}]
        self._orca_fails_on("switch", "switch broke")
        self.assertEqual(self.orche("wake", "main-acme-shop"),
                         (1, "", "orche: could not open or switch to main-acme-shop: orca_error: switch broke\n"))
        self.assertEqual(self.calls_of("create"), [])

    def test_sleep_main_is_refused(self):
        self.assertEqual(self.orche("sleep", "main-acme-shop"),
                         (1, "", "orche: main-acme-shop is a project pane: close it in Orca\n"))
        self.assertEqual(self.calls, [])


class CardTest(CoreCase):
    def setUp(self):
        super().setUp()
        self.card = self.home / "cards" / "acme-shop.md"
        self.reg["projects"]["acme-shop"]["card"] = str(self.card)
        registry.save(self.reg)

    def test_card_text_and_path(self):
        self.card.parent.mkdir(parents=True)
        self.card.write_text("# Acme Shop\nCheckout-first store.\n")
        self.assertEqual(self.orche("card", "acme")[:2], (0, "# Acme Shop\nCheckout-first store.\n"))
        self.assertEqual(self.orche("card", "acme", "--path")[:2], (0, f"{self.card}\n"))

    def test_card_expands_home_and_missing_is_not_found(self):
        code, out, err = self.orche("card", "docs-site", "--path")
        self.assertEqual(out, str(Path.home() / ".claude/orche/cards/docs-site.md") + "\n")
        self.assertEqual(self.orche("card", "acme")[0], 2)
        self.assertEqual(self.orche("card", "nope")[0], 2)


class PaneCommandTest(support.TempHomeCase):
    """F3: every ORCHE pane command puts the plugin's bin/ on PATH next to ORCHE_SESSION."""

    def test_pane_command_prefixes_plugin_bin(self):
        from orche.hr import panes
        self.assertEqual(panes.pane_command("feat-acme-x", resume=True),
                         f"PATH={support.ROOT / 'bin'}:$PATH ORCHE_SESSION=feat-acme-x claude "
                         "--permission-mode bypassPermissions --resume feat-acme-x")

    def test_unit_panes_start_with_bypass_permissions(self):
        from orche.hr import panes
        for session in ("div-acme-build", "feat-acme-x"):
            for resume, flag in ((True, "--resume"), (False, "--name")):
                self.assertTrue(panes.pane_command(session, resume=resume).endswith(
                    f" claude --permission-mode bypassPermissions {flag} {session}"))

    def test_plugin_root_is_shell_quoted_and_path_left_unexpanded(self):
        from unittest import mock
        from orche import paths
        from orche.hr import panes
        with mock.patch.object(paths, "PLUGIN_ROOT", Path("/opt/my plugins/orche")):
            env = panes.session_env("main-acme")
        self.assertEqual(env, "PATH='/opt/my plugins/orche/bin':$PATH ORCHE_SESSION=main-acme ")

    def test_pane_shell_resolves_orche(self):
        import subprocess
        from orche.hr import panes
        res = subprocess.run(["/bin/sh", "-c", panes.session_env("main-acme")
                              + "sh -c 'command -v orche; echo $ORCHE_SESSION'"],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(res.stdout.split(), [str(support.ROOT / "bin" / "orche"), "main-acme"])


if __name__ == "__main__":
    unittest.main()
