"""HR transitions: hr.book (record, log, notify), the hr.limits gates, wake and sleep, creating features
and divisions (stubbed Orca)."""
import ast
import contextlib
import datetime
import io
import inspect
import json
import os
import re
import subprocess
import sys
import time
from unittest import mock

import support
import test_hr

from orche import hr, janitor, orca, registry, roster, shell, statusline
from orche.hr import audit, book, limits, onboarding, transitions


class BookTest(test_hr.HrCase):
    def patched_record(self, reg, line, **kw):
        calls = []
        with mock.patch.object(registry, "append_event", lambda r, **ev: calls.append(("event", ev))), \
                mock.patch.object(registry, "save", lambda r: calls.append(("save",))), \
                mock.patch.object(book, "log", lambda text: calls.append(("log", text))), \
                mock.patch.object(statusline, "refresh", lambda r, key: calls.append(("refresh", key))), \
                mock.patch.object(book, "notify", lambda text: calls.append(("notify", text))):
            book.record(reg, line, **kw)
        return calls

    def test_record_runs_every_step_in_order(self):  # B1
        ev = {"type": "sleep", "project": "acme-shop", "unit": "build", "by": "user"}
        self.assertEqual(self.patched_record({}, "slept build", event=ev, note="build asleep"),
                         [("event", ev), ("save",), ("log", "slept build"), ("refresh", "acme-shop"),
                          ("notify", "build asleep")])

    def test_record_star_project_and_no_save(self):  # B2
        ev = {"type": "audit", "project": "*", "unit": "*", "by": "user"}
        self.assertEqual(self.patched_record({}, "audit", event=ev), [("event", ev), ("save",), ("log", "audit")])
        self.assertEqual(self.patched_record({}, "init again", project="p", save=False),
                         [("log", "init again"), ("refresh", "p")])

    def test_log_writes_header_once(self):  # B3
        self.assertFalse(book.log_path().exists())
        book.log("one")
        book.log("two")
        text = book.log_path().read_text()
        self.assertTrue(text.startswith("# HR log\n\n"))
        self.assertEqual(text.count("# HR log"), 1)
        self.assertEqual([re.sub(r"^- \S+ ", "- <ts> ", l) for l in text.splitlines()[2:]],
                         ["- <ts> one", "- <ts> two"])

    def test_notify_on_off_and_platform(self):  # B4
        posted = []

        def runner(argv, *, timeout, input):
            if argv[0] == "osascript":
                posted.append(argv)
                return shell.Result(0, "", "", argv)
            return self.fake_runner(argv, timeout=timeout, input=input)

        shell.RUNNER = runner
        secret = "token sk-ant-api03-" + "A" * 40
        for on in (False, True):
            (self.home / "config.json").write_text(json.dumps({"notify": on}))
            for platform in ("linux", "darwin"):
                with mock.patch.object(book.sys, "platform", platform):
                    book.notify(secret)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][:2], ["osascript", "-e"])
        self.assertNotIn("A" * 40, posted[0][2])
        self.assertIn('with title "ORCHE"', posted[0][2])


class LimitsTest(test_hr.FeatCase):
    def test_need_awake(self):  # L1
        reg = registry.load()
        with mock.patch.object(registry, "awake_sessions", lambda r: ["a", "b"]):
            reg["limits"]["max_awake_sessions"] = 2
            self.assertEqual(limits.awake(reg), (2, 2))
            with self.assertRaises(limits.AwakeLimit) as cm:
                limits.need_awake(reg)
            self.assertEqual(cm.exception.msg, "awake limit reached (2/2); sleep a unit first (orche sleep <unit>)")
            self.assertIsInstance(cm.exception, book.Refused)
            self.assertEqual(cm.exception.code, 3)
            limits.need_awake(reg, 0)
        with mock.patch.object(registry, "awake_sessions", lambda r: []):
            reg["limits"]["max_awake_sessions"] = 1
            limits.need_awake(reg)
            with self.assertRaises(limits.AwakeLimit) as cm:
                limits.need_awake(reg, 2, plan=True)
            self.assertEqual(cm.exception.msg,
                             "awake limit would be exceeded (0+2/1); sleep a unit first (orche sleep <unit>)")
        with mock.patch.object(registry, "awake_sessions", lambda r: ["a", "b", "c"]):
            reg["limits"]["max_awake_sessions"] = 2
            with self.assertRaises(limits.AwakeLimit):
                limits.need_awake(reg, 0)

    def test_stage_gate(self):  # L2
        with self.assertRaises(book.Refused) as cm:
            limits.stage_gate(["rnd", "ops"], "building", "acme-shop")
        self.assertEqual((cm.exception.code, cm.exception.msg), (3, "ops is gated by stage: it stays dormant while "
                         "stage is building (run: orche stage set acme-shop public --yes)"))
        with self.assertRaises(book.Refused) as cm:
            limits.stage_gate(["ops"], "building")
        self.assertEqual(cm.exception.msg, "ops is gated by stage: it stays dormant while stage is building")
        limits.stage_gate(["ops"], "public", "acme-shop")
        limits.stage_gate(["rnd", "build"], "building", "acme-shop")

    def test_unit_gate(self):
        reg = registry.load()
        entry = {"session": "div-acme-shop-ops", "status": "dormant", "gated_by_stage": True}
        ops = roster.Member(session="div-acme-shop-ops", project="acme-shop", kind="div", name="ops",
                            division=None, entry=entry)
        self.assertEqual(reg["projects"]["acme-shop"]["stage"], "building")
        with self.assertRaises(book.Refused) as cm:
            limits.unit_gate(reg, ops)
        self.assertEqual((cm.exception.code, cm.exception.msg), (3, "div-acme-shop-ops is gated by stage and "
                         "acme-shop is building (run: orche stage set acme-shop public --yes)"))
        reg["projects"]["acme-shop"]["stage"] = "public"
        limits.unit_gate(reg, ops)
        reg["projects"]["acme-shop"]["stage"] = "building"
        limits.unit_gate(reg, roster.Member(session="div-acme-shop-build", project="acme-shop", kind="div",
                                            name="build", division=None, entry={"status": "active"}))


class TransitionsCase(test_hr.FeatCase):
    """Runs hr.wake / hr.sleep on the sample registry, counting status-line refreshes and notes."""

    def setUp(self):
        super().setUp()
        self.notes = []
        patches = (mock.patch.object(statusline, "refresh", wraps=statusline.refresh),
                   mock.patch.object(book, "notify", self.notes.append))
        self.refresh = patches[0].start()
        patches[1].start()
        for p in patches:
            self.addCleanup(p.stop)

    def edit(self, fn):
        reg = registry.load()
        fn(reg["projects"]["acme-shop"])
        self.save(reg)

    def run_verb(self, verb, session, **kw):
        reg = registry.load()
        return verb(reg, roster.Roster(reg).member(session), **kw)

    def refused(self, verb, session, **kw):
        before = registry.load()
        with self.assertRaises(book.Refused) as cm:
            self.run_verb(verb, session, **kw)
        self.assertEqual(registry.load(), before)
        return cm.exception

    def entry(self, session):
        return roster.Roster(registry.load()).member(session).entry

    def log_lines(self):
        path = book.log_path()
        return [re.sub(r"^- \S+ ", "", l) for l in path.read_text().splitlines()[2:]] if path.exists() else []

    def create_argv(self):
        (argv,) = self.orca_calls("terminal create")
        return argv


class WakeTest(TransitionsCase):
    def test_wake_paused_feature_resumes(self):  # W1
        data, line = self.run_verb(transitions.wake, "feat-acme-shop-search")
        self.assertEqual((data, line), ({"unit": "feat-acme-shop-search", "status": "active"},
                                        "feat-acme-shop-search active"))
        argv = self.create_argv()
        self.assertEqual(argv[argv.index("--worktree") + 1], "id:repo-acme::/srv/orca/acme-shop-search")
        self.assertTrue(argv[argv.index("--command") + 1].endswith(
            "--permission-mode bypassPermissions --name feat-acme-shop-search"))
        entry = self.entry("feat-acme-shop-search")
        self.assertEqual(entry["status"], "active")
        self.assertNotEqual(entry["last_activity"], self.reg["projects"]["acme-shop"]["divisions"]["build"]["features"][1]["last_activity"])
        reg = registry.load()
        self.assertEqual([(e["type"], e["project"], e["unit"], e["by"]) for e in reg["events"]],
                         [("wake", "acme-shop", "build/search", "user")])
        self.assertEqual(self.log_lines(), ["wake feat-acme-shop-search by user"])
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(self.notes, [])

    def test_wake_dormant_division_starts_fresh(self):  # W2
        self.run_verb(transitions.wake, "div-acme-shop-rnd")
        command = self.create_argv()[self.create_argv().index("--command") + 1]
        self.assertTrue(command.endswith("--permission-mode bypassPermissions --name div-acme-shop-rnd"))
        self.assertEqual(registry.load()["events"][-1]["unit"], "rnd")
        self.assertEqual(self.entry("div-acme-shop-rnd")["status"], "active")

    def test_wake_active_with_pane_is_already_active(self):  # W3
        self.terminals = [{"handle": "t-1", "title": "✳ div-acme-shop-build"}]
        before = registry.load()
        self.assertEqual(self.run_verb(transitions.wake, "div-acme-shop-build"),
                         ({"unit": "div-acme-shop-build", "status": "active", "changed": False},
                          "div-acme-shop-build is already active"))
        self.assertEqual(self.orca_calls("terminal create"), [])
        self.assertEqual(registry.load(), before)
        self.assertEqual(self.log_lines(), [])

    def test_wake_active_without_pane_reopens_it(self):  # W4
        before = registry.load()
        data, line = self.run_verb(transitions.wake, "div-acme-shop-build")
        self.assertEqual((data, line), ({"unit": "div-acme-shop-build", "status": "active", "changed": False,
                                         "pane_reopened": True}, "div-acme-shop-build pane reopened"))
        self.assertIn("--name div-acme-shop-build", self.create_argv()[self.create_argv().index("--command") + 1])
        self.assertEqual(registry.load(), before)
        self.assertEqual(self.log_lines(), ["reopen div-acme-shop-build pane by user"])
        self.assertEqual(self.refresh.call_count, 0)

    def test_wake_retired_refused(self):  # W5
        self.edit(lambda p: p["divisions"]["build"]["features"][1].update(status="retired"))
        exc = self.refused(transitions.wake, "feat-acme-shop-search")
        self.assertEqual((exc.code, exc.msg), (3, "feat-acme-shop-search is retired"))

    def test_wake_onboarding_refused(self):  # W6
        self.edit(lambda p: p["divisions"]["build"]["features"][1].update(status="onboarding"))
        exc = self.refused(transitions.wake, "feat-acme-shop-search")
        self.assertEqual((exc.code, exc.msg),
                         (3, "onboarding unfinished: re-run `orche feat new acme-shop search`"))
        self.assertEqual(self.orca_calls("terminal create"), [])

    def test_wake_gated_ops_refused_while_building(self):  # W7
        self.edit(lambda p: p["divisions"].__setitem__("ops", dict(
            p["divisions"]["rnd"], status="dormant", worktree="repo-acme::/srv/orca/acme-shop-ops",
            branch="div/ops", session="div-acme-shop-ops", gated_by_stage=True)))
        exc = self.refused(transitions.wake, "div-acme-shop-ops")
        self.assertEqual((exc.code, exc.msg), (3, "div-acme-shop-ops is gated by stage and acme-shop is building "
                                                  "(run: orche stage set acme-shop public --yes)"))
        self.assertEqual(self.orca_calls("terminal create"), [])
        self.assertEqual(self.log_lines(), [])

    def test_wake_over_the_awake_limit(self):  # W8
        reg = registry.load()
        reg["limits"]["max_awake_sessions"] = 2
        self.save(reg)
        exc = self.refused(transitions.wake, "feat-acme-shop-search")
        self.assertIsInstance(exc, limits.AwakeLimit)
        self.assertEqual((exc.code, exc.msg),
                         (3, "awake limit reached (2/2); sleep a unit first (orche sleep <unit>)"))
        self.assertEqual(self.orca_calls("terminal create"), [])

    def test_wake_dry_run_changes_nothing(self):  # W9
        before = registry.load()
        data, line = self.run_verb(transitions.wake, "feat-acme-shop-search", dry_run=True)
        self.assertEqual(line, "would wake feat-acme-shop-search (dry-run)")
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["argv"][1:3], ["terminal", "create"])
        self.assertIn("--name feat-acme-shop-search", data["argv"][data["argv"].index("--command") + 1])
        self.assertEqual(self.orca_calls("terminal create"), [])
        self.assertEqual(registry.load(), before)
        self.assertEqual(self.log_lines(), [])

    def test_wake_failed_pane_leaves_unit_unchanged(self):  # W10
        def runner(argv, *, timeout, input):
            if argv[:3] == ["orca", "terminal", "create"]:
                self.calls.append(argv)
                return shell.Result(0, json.dumps({"ok": False, "error": {"code": "no_worktree",
                                                                          "message": "gone"}}), "", argv)
            return self.fake_runner(argv, timeout=timeout, input=input)

        shell.RUNNER = runner
        exc = self.refused(transitions.wake, "feat-acme-shop-search")
        self.assertEqual((exc.code, exc.msg), (1, "could not open a pane for feat-acme-shop-search: no_worktree: gone"))
        self.assertEqual(self.log_lines(), [])


class SleepTest(TransitionsCase):
    def setUp(self):
        super().setUp()
        self.wt = self.home / "wt-checkout"
        self.edit(lambda p: p["divisions"]["build"]["features"][0].update(worktree=f"repo-acme::{self.wt}"))
        self.handoff = self.wt / ".orche" / "feat" / "checkout" / "HANDOFF.md"
        self.handoff.parent.mkdir(parents=True)
        self.handoff.write_text("# Handoff\n")
        self.terminals = [{"handle": "t-3", "title": "feat-acme-shop-checkout"}]

    def test_sleep_closes_pane_and_records(self):  # S1
        self.assertEqual(self.run_verb(transitions.sleep, "feat-acme-shop-checkout"),
                         ({"unit": "feat-acme-shop-checkout", "status": "paused", "pane_closed": True},
                          "feat-acme-shop-checkout paused"))
        self.assertEqual([c[3:5] for c in self.orca_calls("terminal close")], [["--terminal", "t-3"]])
        self.assertEqual(self.entry("feat-acme-shop-checkout")["status"], "paused")
        self.assertEqual([(e["type"], e["unit"]) for e in registry.load()["events"]], [("sleep", "build/checkout")])
        self.assertEqual(self.log_lines(), ["sleep feat-acme-shop-checkout by user"])
        self.assertEqual(self.refresh.call_count, 1)

    def test_sleep_stale_handoff_refused(self):  # S2
        old = time.time() - 31 * 60
        os.utime(self.handoff, (old, old))
        exc = self.refused(transitions.sleep, "feat-acme-shop-checkout")
        self.assertEqual((exc.code, exc.msg), (3, f"HANDOFF.md is stale (modified 31 min ago: {self.handoff}); "
                                                  "update it before sleeping"))
        self.assertEqual(self.orca_calls("terminal close"), [])

    def test_sleep_without_handoff_check(self):  # S3
        self.handoff.unlink()
        _, line = self.run_verb(transitions.sleep, "feat-acme-shop-checkout", require_handoff=False)
        self.assertEqual(line, "feat-acme-shop-checkout paused")
        self.assertEqual(self.entry("feat-acme-shop-checkout")["status"], "paused")

    def test_sleep_paused_is_already_paused(self):  # S4
        before = registry.load()
        self.assertEqual(self.run_verb(transitions.sleep, "feat-acme-shop-search"),
                         ({"unit": "feat-acme-shop-search", "status": "paused", "changed": False},
                          "feat-acme-shop-search is already paused"))
        self.assertEqual(registry.load(), before)

    def test_sleep_retired_refused(self):  # S5
        self.edit(lambda p: p["divisions"]["build"]["features"][0].update(status="retired"))
        exc = self.refused(transitions.sleep, "feat-acme-shop-checkout")
        self.assertEqual((exc.code, exc.msg), (3, "feat-acme-shop-checkout is retired"))

    def test_sleep_without_open_pane(self):  # S6
        self.terminals = []
        self.assertEqual(self.run_verb(transitions.sleep, "feat-acme-shop-checkout"),
                         ({"unit": "feat-acme-shop-checkout", "status": "paused", "pane_closed": False},
                          "feat-acme-shop-checkout paused (no open pane)"))
        self.assertEqual(self.orca_calls("terminal close"), [])


class NamesTest(test_hr.FeatCase):
    def test_session_name_and_owner(self):  # N1
        self.assertEqual([hr.session_name("div", "acme", "build"), hr.session_name("feat", "acme", "pay"),
                          hr.session_name("main", "acme")],
                         ["div-acme-build", "feat-acme-pay", "main-acme"])
        reg = registry.load()
        self.assertEqual(hr.session_owner(reg, "acme", "div-acme-shop-build"), "acme-shop")
        self.assertIsNone(hr.session_owner(reg, "acme-shop", "div-acme-shop-build"))
        self.assertIsNone(hr.session_owner(reg, "acme", "div-acme-build"))


class CreateFeatureTest(TransitionsCase):
    def create(self, name="pay", scope=("src/pay/**",), **kw):
        return hr.create_feature(registry.load(), "acme-shop", name, scope=list(scope), reason="r", **kw)

    def refused_create(self, **kw):
        with self.assertRaises(book.Refused) as cm:
            self.create(**kw)
        return cm.exception

    def test_create_records_in_order(self):  # C1
        order = []
        save = registry.save

        def runner(argv, *, timeout, input):
            if argv[:3] == ["orca", "worktree", "set"]:
                order.append("unread")
            return self.fake_runner(argv, timeout=timeout, input=input)

        shell.RUNNER = runner
        self.refresh.side_effect = lambda reg, key: order.append("refresh")
        with mock.patch.object(registry, "save", lambda reg: (order.append("save"), save(reg))), \
                mock.patch.object(book, "notify", lambda text: order.append(("notify", text))):
            data, line = self.create(by="div-acme-shop-build", batch="b1")
        self.assertEqual(line, "feat-acme-shop-pay active (feat/pay)")
        self.assertEqual(data["feature"]["session"], "feat-acme-shop-pay")
        self.assertEqual(order, ["save", "save", "refresh", ("notify", "New feature agent feat-acme-shop-pay: r"),
                                 "unread"])
        reg = registry.load()
        self.assertEqual(reg["events"][-1] | {"at": None}, {"at": None, "type": "create", "project": "acme-shop",
                                                            "unit": "build/pay", "by": "div-acme-shop-build",
                                                            "batch": "b1"})
        self.assertEqual(self.log_lines()[-1], "create feat-acme-shop-pay by div-acme-shop-build: r")

    def test_frozen_refused(self):  # C2
        reg = registry.load()
        reg["frozen"] = True
        self.save(reg)
        exc = self.refused_create()
        self.assertEqual((exc.code, exc.msg), (3, "feature-agent creation is frozen; run: orche hr unfreeze"))
        self.assertEqual(registry.load()["events"], [])
        self.assertEqual(self.calls, [])

    def test_global_cap_freezes(self):  # C3
        self.creates(20, project="docs-site")
        exc = self.refused_create()
        self.assertEqual(exc.code, 3)
        self.assertIn("global limit reached (20/20 in 24h)", exc.msg)
        reg = registry.load()
        self.assertTrue(reg["frozen"])
        self.assertEqual((reg["events"][-1]["type"], reg["events"][-1]["project"], reg["events"][-1]["by"]),
                         ("freeze", "*", "hr-limits"))
        self.assertEqual(self.notes, ["HR freeze: global feature-agent creation cap reached"])
        self.assertEqual(self.refresh.call_count, 0)
        self.assertEqual(self.calls, [])

    def test_global_cap_dry_run_does_not_freeze(self):  # C4
        before = self.creates(20, project="docs-site")
        exc = self.refused_create(dry_run=True)
        self.assertEqual(exc.code, 3)
        self.assertEqual(registry.load(), before)
        self.assertEqual((self.notes, self.log_lines()), ([], []))

    def test_strict_overlap_refused(self):  # C5
        exc = self.refused_create(scope=("src/checkout/x",), strict=True)
        self.assertEqual((exc.code, exc.msg), (3, "scope src/checkout/x overlaps feature checkout (src/checkout/**)"))
        self.assertEqual(self.calls, [])

    def test_awake_limit_refused(self):  # C6
        reg = registry.load()
        reg["limits"]["max_awake_sessions"] = 2
        self.save(reg)
        exc = self.refused_create()
        self.assertIsInstance(exc, limits.AwakeLimit)
        self.assertEqual((exc.code, exc.msg), (3, "awake limit reached (2/2); sleep a unit first (orche sleep <unit>)"))
        self.assertEqual(self.calls, [])


    def test_onboard_errors_refused(self):  # T3 nit: feat new error codes
        for error, code in ((onboarding.UnsafeTarget("x"), 3), (orca.OrcaError("boom"), 1)):
            with self.subTest(code=code), mock.patch.object(onboarding, "onboard", side_effect=error):
                exc = self.refused_create()
                self.assertEqual((exc.code, exc.msg), (code, f"feat new pay stopped: {error}"))

    def test_unread_failure_warns(self):  # T3 nit: the unread dot is best effort
        def runner(argv, *, timeout, input):
            if argv[:3] == ["orca", "worktree", "set"]:
                return shell.Result(0, json.dumps({"ok": False, "error": {"code": "x", "message": "m"}}), "", argv)
            return self.fake_runner(argv, timeout=timeout, input=input)

        shell.RUNNER = runner
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, line = self.create()
        self.assertEqual(line, "feat-acme-shop-pay active (feat/pay)")
        self.assertIn("orche: warning: could not mark the worktree unread: x: m", err.getvalue())


class DivisionsTest(test_hr.HrCase):
    SETUP = {"divisions": ["build", "gtm"], "custom": {}, "stage": "building", "aliases": []}

    def setUp(self):
        super().setUp()
        self.notes = []
        patches = (mock.patch.object(statusline, "refresh", wraps=statusline.refresh),
                   mock.patch.object(book, "notify", self.notes.append))
        self.refresh = patches[0].start()
        patches[1].start()
        for p in patches:
            self.addCleanup(p.stop)
        self.root = os.path.realpath(self.repo)

    def onboard(self, wake):
        reg = registry.load()
        plan = hr.plan_divisions(reg, "acme-shop", self.SETUP, wake)
        return plan, hr.onboard_project(reg, "acme-shop", self.SETUP, plan, repo_id="repo-acme", root=self.root,
                                        card="~/.claude/orche/cards/acme-shop.md")

    def log_lines(self):
        """The init and create records (onboarding also logs missing persona templates)."""
        return [l for l in TransitionsCase.log_lines(self) if l.startswith(("init ", "create "))]

    def test_plan_gates_ops_while_building(self):  # D1
        with self.assertRaises(book.Refused) as cm:
            hr.plan_divisions(registry.load(), "acme-shop", dict(self.SETUP, divisions=["build", "ops"]), ["ops"])
        self.assertEqual((cm.exception.code, cm.exception.msg), (3, "ops is gated by stage: it stays dormant while "
                         "stage is building (run: orche stage set acme-shop public --yes)"))

    def test_plan_awake_limit(self):  # D2
        reg = registry.empty()
        reg["limits"]["max_awake_sessions"] = 1
        self.save(reg)
        with self.assertRaises(limits.AwakeLimit) as cm:
            hr.plan_divisions(registry.load(), "acme-shop", self.SETUP, ["build", "gtm"])
        self.assertEqual(cm.exception.msg,
                         "awake limit would be exceeded (0+2/1); sleep a unit first (orche sleep <unit>)")

    def test_onboard_project(self):  # D3
        plan, (proj, opened) = self.onboard(["build"])
        self.assertEqual(plan, hr.create.Plan(["build", "gtm"], ["build"], [], []))
        self.assertEqual(opened, ["main-acme-shop"])
        reg = registry.load()
        self.assertEqual(reg["projects"]["acme-shop"], proj)
        self.assertNotIn("liaison_session", proj)
        self.assertEqual((proj["main_session"], proj["path"], proj["repo_id"]),
                         ("main-acme-shop", self.root, "repo-acme"))
        self.assertEqual({d: v["status"] for d, v in proj["divisions"].items()}, {"build": "active", "gtm": "dormant"})
        self.assertEqual([(e["type"], e["project"], e["unit"], e["by"]) for e in reg["events"]],
                         [("create", "acme-shop", "build", "user"), ("create", "acme-shop", "gtm", "user")])
        self.assertEqual(self.log_lines(),
                         ["init acme-shop (repo repo-acme) by user", "create div-acme-shop-build (active) by user",
                          "create div-acme-shop-gtm (dormant) by user"])
        self.assertEqual(self.refresh.call_count, 3)
        self.assertEqual(self.notes, [])

    def test_rerun_resumes_without_saving(self):  # D4
        self.onboard(["build"])
        before = registry.path().read_bytes()
        self.refresh.reset_mock()
        plan, (_, opened) = self.onboard([])
        self.assertEqual(plan, hr.create.Plan([], [], ["build", "gtm"], []))
        self.assertEqual(opened, [])
        self.assertEqual(registry.path().read_bytes(), before)
        self.assertEqual(self.log_lines()[-1], "init acme-shop resumed by user (skipping build,gtm)")
        self.assertEqual(self.refresh.call_count, 1)


    def test_resume_saves_before_logging(self):  # T3 nit: a resumed init with new custom definitions
        self.onboard(["build"])
        reg = registry.load()
        reg["projects"]["acme-shop"]["custom_divisions"] = {"old": {"label": "Old", "charter": "c", "model": "claude-opus-5-5",
                                                                     "effort": "medium"}}
        self.save(reg)
        order, save, log = [], registry.save, book.log
        with mock.patch.object(registry, "save", lambda r: (order.append("save"), save(r))), \
                mock.patch.object(book, "log", lambda text: (order.append(("log", text)), log(text))):
            self.onboard([])
        self.assertEqual(order[:2], ["save", ("log", "init acme-shop resumed by user (skipping build,gtm)")])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["custom_divisions"], {})

    def test_init_errors_refused(self):  # T3 nit: init error codes
        for error, code in ((onboarding.UnsafeTarget("x"), 3), (orca.OrcaError("boom"), 1)):
            with self.subTest(code=code), mock.patch.object(onboarding, "write_project_marker", side_effect=error), \
                    self.assertRaises(book.Refused) as cm:
                self.onboard(["build"])
            self.assertEqual((cm.exception.code, cm.exception.msg),
                             (code, f"init acme-shop stopped: {error} (re-run orche init acme-shop to resume)"))


class RetireGoneTest(TransitionsCase):
    def test_retire_gone_records_without_note(self):  # R1
        reg = registry.load()
        hr.retire_gone(reg, roster.Roster(reg).member("feat-acme-shop-checkout"), by="janitor")
        self.assertEqual(self.entry("feat-acme-shop-checkout")["status"], "retired")
        ev = registry.load()["events"][-1]
        self.assertEqual((ev["type"], ev["project"], ev["unit"], ev["by"]),
                         ("retire", "acme-shop", "build/checkout", "janitor"))
        self.assertEqual(self.log_lines()[-1], "retire feat-acme-shop-checkout by janitor (worktree gone from Orca)")
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(self.notes, [])


class StageSetTest(TransitionsCase):
    """hr.set_stage, hr.set_frozen, book.correct and the audit helpers (C4 T5)."""

    def add_ops(self, **kw):
        self.edit(lambda p: p["divisions"].__setitem__("ops", {
            "status": "dormant", "worktree": "repo-acme::/srv/orca/acme-shop-ops", "branch": "div/ops",
            "session": "div-acme-shop-ops", "model": "claude-opus-5-5",
            "last_activity": test_hr.ago(days=1), "gated_by_stage": True, **kw}))

    def stage(self, stage, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            data, lines = hr.set_stage(registry.load(), "acme-shop", stage, **kw)
        return data, lines, err.getvalue()

    def event_types(self):
        return [e["type"] for e in registry.load()["events"]]

    def test_public_wakes_dormant_ops(self):  # G1
        self.add_ops()
        before = len(self.event_types())
        data, lines, err = self.stage("public")
        self.assertEqual(self.event_types()[before:], ["stage", "wake"])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["stage"], "public")
        self.assertEqual(self.entry("div-acme-shop-ops")["status"], "active")
        self.assertTrue(data["woke"])
        self.assertEqual(lines[0], "set acme-shop stage=public")
        self.assertEqual(lines[1], "div-acme-shop-ops active")
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.notes, ["acme-shop is public — ops woken"])
        self.assertEqual(err, "")

    def test_building_with_stale_ops_warns_and_keeps_stage(self):  # G2
        self.add_ops(status="active")
        self.edit(lambda p: p.__setitem__("stage", "public"))
        data, lines, err = self.stage("building")
        self.assertEqual(registry.load()["projects"]["acme-shop"]["stage"], "building")
        self.assertEqual(self.entry("div-acme-shop-ops")["status"], "active")
        self.assertIn("orche: warning: ops not slept: ", err)
        self.assertTrue(any(l.startswith("ops not slept: ") for l in lines))
        self.assertEqual(self.log_lines(), ["stage acme-shop -> building by user"])

    def test_building_no_change(self):  # G3
        self.add_ops()
        before = registry.load()
        data, lines, err = self.stage("building")
        self.assertEqual(lines, ["already acme-shop stage=building"])
        self.assertFalse(data["changed"])
        self.assertEqual(registry.load(), before)
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.refresh.call_count, 0)
        self.assertEqual((self.notes, err), ([], ""))

    def test_public_with_onboarding_ops(self):  # G4
        self.add_ops(status="onboarding")
        data, lines, err = self.stage("public")
        self.assertIn("onboarding unfinished: re-run `orche init acme-shop`", lines)
        self.assertFalse(data["woke"])
        self.assertEqual(self.notes, ["acme-shop is public"])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["stage"], "public")

    def test_public_over_awake_limit_warns(self):  # G5
        self.add_ops()
        reg = registry.load()
        reg["limits"]["max_awake_sessions"] = 2
        self.save(reg)
        data, lines, err = self.stage("public")
        self.assertIn("orche: warning: ops not woken: awake limit reached (2/2); sleep a unit first "
                      "(orche sleep <unit>)", err)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["stage"], "public")
        self.assertEqual(self.entry("div-acme-shop-ops")["status"], "dormant")
        self.assertEqual(self.notes, ["acme-shop is public"])

    def test_freeze_records_once(self):  # F1
        data, line = hr.set_frozen(registry.load(), True)
        self.assertEqual((data, line), ({"frozen": True, "changed": True, "dry_run": False}, "frozen"))
        ev = registry.load()["events"][-1]
        self.assertEqual((ev["type"], ev["project"]), ("freeze", "*"))
        self.assertEqual(self.notes, ["HR freeze: feature-agent creation stopped"])
        self.assertEqual(self.refresh.call_count, 0)
        before, logged = registry.load(), self.log_lines()
        data, line = hr.set_frozen(registry.load(), True)
        self.assertEqual((data["changed"], line), (False, "already frozen"))
        self.assertEqual((registry.load(), self.log_lines(), len(self.notes)), (before, logged, 1))

    def test_freeze_dry_run(self):  # F2
        before = registry.load()
        data, line = hr.set_frozen(registry.load(), True, dry_run=True)
        self.assertEqual((data, line), ({"frozen": False, "changed": False, "dry_run": True},
                                        "would freeze (dry-run)"))
        self.assertEqual((registry.load(), self.log_lines(), self.notes), (before, [], []))

    def test_correct_appends_and_logs_without_saving(self):  # K1
        path = self.repo / ".orche" / "STANDING.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# STANDING\n\n1. Run the tests\n")
        before = registry.load()
        with mock.patch.object(registry, "save", wraps=registry.save) as save:
            data, number = book.correct(registry.load(), "acme-shop", "Never  push")
        self.assertEqual(number, 2)
        self.assertEqual(path.read_text(), "# STANDING\n\n1. Run the tests\n2. Never push\n")
        self.assertEqual(self.log_lines(), ["correct acme-shop #2 by user: Never push"])
        self.assertEqual(save.call_count, 0)
        self.assertEqual(registry.load(), before)

    def test_active_matches_depts_rows(self):  # A0
        from orche import cmd_depts
        reg, now = registry.load(), test_hr.NOW
        want = [{"session": r["session"], "at": datetime.datetime.fromisoformat(r["last_activity"]),
                 "idle_days": r["idle_days"]}
                for r in cmd_depts.rows(reg, sorted(reg["projects"]), now) if r["status"] == "active"]
        got = [{"session": r["session"], "at": r["at"].replace(microsecond=0), "idle_days": r["idle_days"]}
               for r in audit._active(reg, now)]
        self.assertGreater(len(want), 1)  # the fixture's active build and checkout: the order is checked
        self.assertEqual(got, want)

    def test_corrections_ignore_a_leftover_liaison_folder(self):  # A1
        """C8: the Liaison has no memory folder any more; an old .orche/liaison/DECISIONS.tsv is not read."""
        path = self.repo / ".orche" / "liaison" / "DECISIONS.tsv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ts\tphase\tdecision\twhy\tevidence\tresult\n"
                        f"{test_hr.ago(days=1)}\tx\trev 2 something\twhy\t-\tok\n")
        found = audit.corrections(registry.load(), test_hr.NOW)
        self.assertEqual([f for f in found if f["text"] == "rev 2 something"], [])


class RetireImportTest(test_hr.HrCase):
    def test_janitor_imports_hr_only_inside_retire(self):  # R3
        code = "import sys, orche.janitor; print('orche.hr' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONPATH": str(support.ROOT / "lib")}).stdout
        self.assertEqual(out.strip(), "False")
        tree = ast.parse(inspect.getsource(janitor))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any(a.name == "hr" for n in top for a in n.names))
        self.assertIn("from .. import hr", inspect.getsource(janitor._retire))


class StructureTest(test_hr.HrCase):
    """Design §9 ST1-ST4: HR owns registry writes and the HR log; orche.hr and orche.janitor stay leaves."""
    ALLOWED = set("""orche orche.bip39 orche.brief orche.cli orche.config orche.headless orche.hr orche.hr.audit
        orche.hr.book orche.hr.create orche.hr.limits orche.hr.onboarding orche.hr.panes orche.hr.refresh
        orche.hr.retire orche.hr.transitions orche.janitor orche.janitor.archive orche.janitor.discover orche.janitor.graveyard
        orche.log orche.mask orche.metrics orche.orca orche.paths orche.procs orche.registry orche.roster
        orche.runs orche.setup_flow orche.shell orche.statusline orche.templates orche.units orche.util""".split())
    FORBIDDEN = re.compile(r"orche\.(cmd_\w+|dispatch|runner|inbox|intake|triage|ledger|jev\w*)$")

    def sources(self):
        for top in ("lib", "hooks", "bin"):
            for path in sorted((support.ROOT / top).rglob("*.py")):
                yield path.relative_to(support.ROOT).as_posix(), path.read_text()

    def loaded(self, module):
        code = f"import sys, {module}; print(' '.join(sorted(m for m in sys.modules if m.split('.')[0] == 'orche')))"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONPATH": str(support.ROOT / "lib")}).stdout
        return set(out.split())

    def test_registry_writes_only_in_hr(self):  # ST1
        pattern = re.compile(r"registry\.save\(|append_event\(|from \.+registry import .*\b(save|append_event)\b")
        hits = [rel for rel, text in self.sources()
                if not rel.startswith("lib/orche/hr/") and rel != "lib/orche/registry.py" and pattern.search(text)]
        self.assertEqual(hits, [])

    def test_log_file_name_only_in_book(self):  # ST2
        hits = [rel for rel, text in self.sources() if rel != "lib/orche/hr/book.py" and '"hr-log.md"' in text]
        self.assertEqual(hits, [])

    def test_hr_imports_no_command_layer(self):  # ST3
        loaded = self.loaded("orche.hr")
        self.assertLessEqual(loaded, self.ALLOWED, loaded - self.ALLOWED)
        self.assertEqual([m for m in loaded if self.FORBIDDEN.match(m)], [])

    def test_janitor_imports_no_hr(self):  # ST4
        self.assertEqual([m for m in self.loaded("orche.janitor") if m.startswith("orche.hr")], [])
