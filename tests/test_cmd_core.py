"""CLI core: status, depts, statusline, hr, sleep/wake, card (golden strings on a two-project registry)."""
import copy
import datetime as dt
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import registry, shell, units
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
            "acme-shop stage=building awake=2/12 divisions: rnd○ research○ build●1 gtm○ ops○"
            " · new:1 · keys:1 · unverified:1\n"
            "docs-site stage=public awake=0/12 divisions: rnd○ research○ build○ gtm○ ops○"
            " · new:0 · keys:1 · unverified:0\n"))

    def test_status_without_counter_dirs(self):
        code, out, _ = self.orche("status", "shop")
        self.assertEqual(code, 0)
        self.assertEqual(out, "acme-shop stage=building awake=2/12 divisions: rnd○ research○ build●1"
                              " gtm○ ops○ · new:1 · keys:0 · unverified:0\n")

    def test_status_json_and_unknown(self):
        code, out, _ = self.orche("--json", "status", "acme")
        row = json.loads(out)[0]
        self.assertEqual((code, row["project"], row["awake"], row["divisions"]["build"]),
                         (0, "acme-shop", 2, "active"))
        self.assertEqual(self.orche("status", "nope")[0], 2)


class DeptsTest(CoreCase):
    def test_depts_golden(self):
        code, out, err = self.orche("depts")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "PROJECT    UNIT            STATUS   SESSION                  BRANCH         LAST_ACTIVITY              IDLE",
            "acme-shop  rnd             dormant  div-acme-shop-rnd        div/rnd        2026-10-01T09:00:00+09:00  15d",
            "acme-shop  build           active   div-acme-shop-build      div/build      2026-10-15T18:30:00+09:00  0d",
            "acme-shop  build/checkout  active   feat-acme-shop-checkout  feat/checkout  2026-10-15T18:00:00+09:00  0d",
            "acme-shop  build/search    paused   feat-acme-shop-search    feat/search    2026-10-03T10:00:00+09:00  12d",
        ])

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
        line = "R&D○ RS○ BUILD●1 GTM○ OPS○ · new1 · 🔑1 · unverified1"
        self.assertEqual((code, out, err), (0, line + "\n", ""))
        self.assertEqual((self.home / "status" / "acme-shop.line").read_text(), line + "\n")

    def test_marker_and_zero_cells_omitted(self):
        self.counters()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".orche").mkdir()
            (Path(tmp) / ".orche" / "project").write_text("docs-site\n")
            (Path(tmp) / "sub").mkdir()
            code, out, _ = self.orche("statusline", "--cwd", str(Path(tmp) / "sub"))
        self.assertEqual((code, out), (0, "R&D○ RS○ BUILD○ GTM○ OPS○ · 🔑1\n"))

    def test_unit_worktree_resolves_project(self):
        out = self.orche("statusline", "--cwd", "/srv/orca/acme-shop-checkout")[1]
        self.assertEqual(out, "R&D○ RS○ BUILD●1 GTM○ OPS○ · new1\n")

    def test_missing_registry_is_silent(self):
        (self.home / "registry.json").unlink()
        self.assertEqual(self.orche("statusline", "--cwd", "/srv/orca/acme-shop"), (0, "", ""))

    def test_fast_without_orca(self):
        start = time.perf_counter()
        self.orche("statusline", "--cwd", "/srv/orca/acme-shop")
        self.assertLess(time.perf_counter() - start, 0.05)
        self.assertEqual(self.calls, [])


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
        self.assertFalse(units.hr_log_path().exists())

    def test_log_creates_missing_file(self):
        self.assertEqual(self.orche("hr", "log")[:2], (0, "# HR log\n\n"))
        self.assertTrue(units.hr_log_path().exists())


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
        self.assertIn("sleep div-acme-shop-build by user", units.hr_log_path().read_text())

    def test_sleep_without_pane_just_records(self):
        self.fresh_handoff()
        self.assertEqual(self.orche("sleep", "div-acme-shop-build")[:2],
                         (0, "div-acme-shop-build paused (no open pane)\n"))

    def test_wake_dry_run_prints_orca_argv(self):
        code, out, err = self.orche("wake", "feat-acme-shop-search", "--dry-run")
        self.assertEqual((code, out), (0, "would wake feat-acme-shop-search (dry-run)\n"))
        self.assertEqual(err, "[dry-run] orca terminal create --worktree id:repo-acme::/srv/orca/acme-shop-search"
                              " --title feat-acme-shop-search --command ORCHE_SESSION=feat-acme-shop-search claude --resume feat-acme-shop-search"
                              " --json\n")
        self.assertEqual(registry.load(), self.reg)

    def test_wake_creates_pane_and_records(self):
        code, out, _ = self.orche("wake", "feat-acme-shop-search")
        self.assertEqual((code, out), (0, "feat-acme-shop-search active\n"))
        self.assertEqual(self.calls[-1][:3], ["orca", "terminal", "create"])
        self.assertIn("ORCHE_SESSION=feat-acme-shop-search claude --resume feat-acme-shop-search", self.calls[-1])
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"]["divisions"]["build"]["features"][1]["status"], "active")
        self.assertEqual((reg["events"][-1]["type"], reg["events"][-1]["unit"]), ("wake", "build/search"))

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
        self.assertEqual(self.orche("wake", "nonsense")[0], 2)


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


if __name__ == "__main__":
    unittest.main()
