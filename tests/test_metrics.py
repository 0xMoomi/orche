"""`orche metrics` on synthetic transcripts: dedupe, context, cost, wake sources, routine share."""
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

import support
from orche import metrics
from test_registry import load_cli

TRANSCRIPTS = support.FIXTURES / "transcripts"
NOW = dt.datetime(2026, 10, 16, 0, 0, tzinfo=dt.timezone.utc)


class MetricsTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()   # the CLI reads the registry for the Runner section: a temp ORCHE_HOME
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "projects"
        session = self.root / "-srv-orca-acme-shop"
        (session / "sess-1" / "subagents").mkdir(parents=True)
        shutil.copy(TRANSCRIPTS / "metrics_main.jsonl", session / "sess-1.jsonl")
        shutil.copy(TRANSCRIPTS / "metrics_subagent.jsonl", session / "sess-1" / "subagents" / "agent-a1.jsonl")

    def compute(self, **kw):
        return metrics.compute(self.root, days=30, now=NOW, **kw)

    def test_known_counts(self):
        m = self.compute()
        self.assertEqual(m["files"], {"main": 1, "subagent": 1})
        self.assertEqual(m["responses"], 5)  # msg_1..msg_5; duplicate ids and the August line dropped
        self.assertEqual({k: v["responses"] for k, v in m["models"].items()},
                         {"claude-fable-5-1": 2, "claude-mystery-9": 1, "claude-opus-5-5": 1,
                          "claude-sonnet-5-5": 1})
        self.assertEqual(m["models"]["claude-fable-5-1"]["avg_context"], 1655)  # (1300 + 2010) / 2
        self.assertEqual(m["avg_context"], 201003)  # (1300 + 2010 + 505 + 1000000 + 1200) / 5
        self.assertEqual(m["models"]["claude-fable-5-1"]["per_day"], 0.07)
        self.assertEqual(m["by_day"], {"2026-10-14": {"claude-fable-5-1": 2, "claude-opus-5-5": 1,
                                                      "claude-sonnet-5-5": 1},
                                       "2026-10-15": {"claude-mystery-9": 1}})
        self.assertEqual(m["wake"], {"user": 2, "notification": 1})
        self.assertEqual(m["routine"], {"messages": 2, "routine": 1, "share": 0.5})
        self.assertEqual(m["ask_user_question"], 1)

    def test_cost_by_model(self):
        models = self.compute()["models"]
        self.assertAlmostEqual(models["claude-fable-5-1"]["cost_usd"], 0.00785)
        self.assertAlmostEqual(models["claude-opus-5-5"]["cost_usd"], 0.00022)
        self.assertAlmostEqual(models["claude-sonnet-5-5"]["cost_usd"], 0.0039)
        self.assertAlmostEqual(models["claude-mystery-9"]["cost_usd"], 5.0)  # opus-5 fallback row
        self.assertAlmostEqual(self.compute()["cost_usd"], 5.01197)

    def prices_file(self, data):
        path = self.root.parent / "prices.json"
        path.write_text(json.dumps(data))
        return path

    def test_fable_5_2_has_its_own_row(self):
        row = metrics.PRICES["claude-fable-5-2"]
        self.assertEqual(row, {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.0,
                               "cache_write_1h": 16.0})
        self.assertIs(metrics.price_row("claude-fable-5-2", metrics.PRICES), row)
        self.assertEqual(metrics.PRICES["claude-fable-5-1"]["output"], 50.0)
        self.assertEqual(metrics.PRICES["claude-sonnet-5-5"]["cache_read"], 0.10)
        self.assertIsNone(metrics.known_row("claude-mystery-9", metrics.PRICES))

    def test_an_unpriced_model_is_named_once(self):
        m = self.compute()
        self.assertEqual(m["unpriced"], ["claude-mystery-9"])
        self.assertEqual([ln for ln in metrics.render(m) if ln.startswith("unpriced")],
                         ["unpriced models (counted at claude-opus-5's price): claude-mystery-9"])
        self.assertFalse(any(ln.startswith("unpriced") for ln in metrics.render({**m, "unpriced": []})))

    def test_prices_override_and_dated_ids(self):
        row = {"input": 1.0, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0}
        prices = metrics.load_prices(self.prices_file({"claude-mystery-9": row}))
        self.assertAlmostEqual(self.compute(prices=prices)["models"]["claude-mystery-9"]["cost_usd"], 1.0)
        self.assertEqual(metrics.price_row("claude-opus-5-5-20261001", metrics.PRICES)["input"], 4.0)

    def test_cache_writes_priced_by_ttl(self):
        row = metrics.PRICES["claude-opus-5-5"]
        self.assertEqual((row["cache_write"], row["cache_write_1h"]), (row["input"] * 1.25, row["input"] * 2))
        split = {"cache_creation_input_tokens": 3_000_000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 2_000_000}}
        self.assertAlmostEqual(metrics.cost(split, row), 5.0 + 16.0)
        self.assertAlmostEqual(metrics.cost({"cache_creation_input_tokens": 3_000_000}, row), 15.0)  # no split: 5m

    def test_one_hour_writes_from_transcript_lines(self):
        session = self.root / "-srv-orca-acme-shop"
        line = {"type": "assistant", "timestamp": "2026-10-15T23:00:00.000Z",
                "message": {"id": "msg_1h", "model": "claude-haiku-5-5", "role": "assistant", "content": [],
                            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 1_000_000,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                         "ephemeral_1h_input_tokens": 1_000_000}}}}
        (session / "sess-2.jsonl").write_text(json.dumps(line) + "\n")
        self.assertAlmostEqual(self.compute()["models"]["claude-haiku-5-5"]["cost_usd"], 0.2)
        since = dt.datetime(2026, 10, 15, 22, 0, tzinfo=dt.timezone.utc)
        self.assertAlmostEqual(metrics.spent_since(self.root, ["-srv-orca-acme-shop"], since, now=NOW), 0.2)

    def test_prices_rejects_bad_rows(self):
        bad = {
            "non-dict row": {"m": 1},
            "string value": {"m": {"input": "1", "output": 0, "cache_read": 0, "cache_write": 0,
                                     "cache_write_1h": 0}},
            "missing key": {"m": {"input": 1, "output": 0, "cache_read": 0}},
            "not an object": [1, 2],
        }
        for label, data in bad.items():
            with self.subTest(label), self.assertRaises(ValueError) as ctx:
                metrics.load_prices(self.prices_file(data))
            if label != "not an object":
                self.assertIn("model 'm'", str(ctx.exception))

    def test_cli_bad_prices_exits_1_with_one_line(self):
        path = self.prices_file({"m": 1})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = load_cli().main(["metrics", "--root", str(self.root), "--prices", str(path)])
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("model 'm'", err.getvalue())

    def test_routine_regex(self):
        for text in ("status?", "Progress update please", "continue", "go ahead", "Approve"):
            self.assertTrue(metrics.ROUTINE_RE.search(text), text)
        self.assertIsNone(metrics.ROUTINE_RE.search("build the statusline widget"))

    def test_cli_json_and_table(self):
        cli = load_cli()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["metrics", "--root", str(self.root), "--days", "36500", "--json"])
        data = json.loads(out.getvalue())
        self.assertEqual((code, data["responses"], data["ask_user_question"]), (0, 6, 1))
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["metrics", "--root", str(self.root), "--days", "36500"])
        self.assertEqual(code, 0)
        self.assertIn("main wake-ups: user 3, notification 1", out.getvalue())
        self.assertIn("AskUserQuestion: 1", out.getvalue())

    def run_lines(self, now):
        """Worker attempts in acme's run history: three in the window (one failed; two Opus medium, one Sonnet
        without an effort; the failed one escalated: its escalation line counts too), one older than 30 days and
        one that never started (no cost figure: not an attempt)."""
        from orche.runs import history
        opus, sonnet = ("claude-opus-5-5", "medium"), ("claude-sonnet-5-5", None)
        for i, (status, cost, age, (model, effort), kind) in enumerate((
                ("done", 0.2, 1, opus, "work"), ("failed", 1.0, 2, opus, "work"),
                ("failed", 0.3, 2, opus, "escalation"), ("info", 0.4, 3, sonnet, "work"),
                ("done", 9.0, 40, opus, "work"), ("info", None, 1, opus, None))):
            history.append("acme", {"unit": f"acme-u-20261016-{i + 1}", "status": status, "cost_usd": cost,
                                    "model": model, "effort": effort, "kind": kind,
                                    "finished": (now - dt.timedelta(days=age)).isoformat()}, 1)

    def test_runner_summary_counts_every_attempt_in_the_window(self):
        self.run_lines(NOW)
        self.assertEqual(metrics.runner_summary(metrics.attempts(["acme"]), days=30, now=NOW),
                         {"attempts": 4, "cost_usd": 1.9, "median_cost_usd": 0.35, "failures": 2,
                          "by_status": {"done": 1, "failed": 2, "info": 1},
                          "by_model_effort": {
                              "claude-opus-5-5 medium": {"attempts": 3, "cost_usd": 1.5, "median_cost_usd": 0.3,
                                                         "failures": 2},
                              "claude-sonnet-5-5 ?": {"attempts": 1, "cost_usd": 0.4, "median_cost_usd": 0.4,
                                                      "failures": 0}}})
        self.assertEqual(metrics.runner_summary(metrics.attempts(["nope"]), days=30, now=NOW)["attempts"], 0)

    def registry(self):
        from orche import registry
        reg = registry.empty()
        reg["projects"]["acme"] = json.loads((support.FIXTURES / "hooks" / "registry.json").read_text()
                                             .replace("@ROOT@", str(self.root.parent)))["projects"]["acme"]
        registry.save(reg)
        return reg

    def freeze(self):
        """The CLI takes no `now`: metrics reads NOW as the clock, so the dated fixture transcripts stay in
        its 30-day window whatever the real date is."""
        class Frozen(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz) if tz is not None else NOW.astimezone().replace(tzinfo=None)

        clock = mock.patch.object(metrics, "_dt", mock.Mock(datetime=Frozen, timedelta=dt.timedelta,
                                                             timezone=dt.timezone))
        clock.start()
        self.addCleanup(clock.stop)

    def test_cli_runner_section_from_registered_projects(self):
        self.registry()
        self.freeze()
        self.run_lines(NOW)
        cli, out = load_cli(), io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["metrics", "--root", str(self.root), "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["runner"]["attempts"], 4)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["metrics", "--root", str(self.root)])
        lines = out.getvalue().splitlines()
        at = lines.index("Runner (orche run, history.jsonl):")
        self.assertEqual(lines[at:at + 5], [
            "Runner (orche run, history.jsonl):",
            "  every attempt counts: re-runs, escalations and finishing calls (no longer a unit's last run)",
            "  attempts 4, cost $1.90, median $0.35/attempt, failures 2",
            "  claude-opus-5-5 medium: attempts 3, cost $1.50, median $0.30/attempt, failures 2",
            "  claude-sonnet-5-5 ?: attempts 1, cost $0.40, median $0.40/attempt, failures 0"])
        self.assertEqual(lines[at + 5], "Rework (last 30 days):")
        self.assertEqual(lines[-3:], ["Spend by role (list price, $5.01):", "  ROLE               COST_USD  SHARE",
                                      "  other (not Orche)  5.01      100%"])

    def rework_fixture(self):
        """Three units in the last 7 days: A ran three times (a `defect` AMEND with a finishing call, then an
        `owner` AMEND whose run was killed) and landed, B re-ran its unchanged revision (`retry`), C landed at
        once; the queue merged two, re-tested one green and returned two; five review rows; Main spent $14."""
        from orche import ledger, units as units_mod
        from orche.queue import store
        from orche.runs import history
        reg = self.registry()
        root = units_mod.project_root(reg, "acme")
        day = lambda d, h=0: (NOW - dt.timedelta(days=d, hours=h)).isoformat()   # noqa: E731
        notes = {"acme-a-20261010-1": {2: "rev 1: defect: the cap is off by one tier", 3: "rev 2: owner: new copy"}}
        for unit in ("acme-a-20261010-1", "acme-b-20261010-1", "acme-c-20261010-1"):
            folder = root / ".orche" / "units" / unit
            folder.mkdir(parents=True)
            for rev, note in notes.get(unit, {}).items():
                path = folder / f"BRIEF.rev{rev}.md"
                path.write_text(f"rev: {rev}\nsupersedes: rev {rev - 1}\nsupersedes-note: {note}\nunit: {unit}\n")
                os.utime(path, (NOW.timestamp() - 86400 * (4 - rev),) * 2)
        lines = (("acme-a-20261010-1", 1, "work", 1.0, None, 5), ("acme-a-20261010-1", 2, "work", 2.0, None, 4),
                 ("acme-a-20261010-1", 2, "finish", 1.0, None, 4), ("acme-a-20261010-1", 3, None, None, "busy", 3),
                 ("acme-a-20261010-1", 3, "work", 1.0, "stopped", 2), ("acme-b-20261010-1", 1, "work", 1.0, None, 3),
                 ("acme-b-20261010-1", 1, "work", 1.0, "end_turn", 2), ("acme-c-20261010-1", 1, "work", 3.0, None, 1),
                 ("acme-c-20261010-1", 1, "review", 0.5, None, 1))
        for unit, rev, kind, cost, stop, age in lines:
            history.append("acme", {"unit": unit, "kind": kind, "cost_usd": cost, "stop_reason": stop,
                                    "started": day(age), "finished": day(age, -1)}, rev)
        for event, eid, unit, extra in (("merged", "e1", "acme-c-20261010-1", {}), ("retested", "e2", "acme-a-20261010-1", {}),
                                        ("merged", "e2", "acme-a-20261010-1", {}),
                                        ("returned", "e3", "acme-b-20261010-1", {"reason": "red"}),
                                        ("returned", "e4", "acme-b-20261010-1", {"reason": "conflict"})):
            with mock.patch("orche.util.now_iso", return_value=day(1)):
                store.log("acme", event, {"id": eid, "pr": int(eid[1:]), "unit": unit}, **extra)
        for kind, verdict, reviewer, model, note in (
                ("build", "pass", "div-acme-build (review)", "claude-opus-5-5", ""),
                ("build", "pass", "div-acme-build (pane)", "claude-opus-5-5", ""),
                ("build", "return", "div-acme-build (review)", "facts", "route: pane review: no unit brief"),
                ("build", "return", "div-acme-build (review)", "claude-opus-5-5", ""),
                ("pre", "return", "codex (pre)", "gpt-5.5-codex", "")):
            ledger.append_review("acme", ledger.make_review_row(9, "abc1234", kind, verdict, reviewer, model,
                                                                "feat-acme-checkout", note=note, evidence="/x",
                                                                ts=day(1), second_vendor=False))
        return reg

    def test_rework_block_from_a_fixture(self):
        reg = self.rework_fixture()
        w = metrics.rework_summary(reg, days=7, now=NOW, named={"main-acme": 14.0})
        self.assertEqual((w["units"], w["runs"], w["landed"], w["runs_per_landed"], w["returns"]), (3, 6, 2, 2.0, 5))
        self.assertEqual((w["reruns"], w["rerun_usd"], w["worker_usd"], w["killed"], w["killed_usd"]), (3, 5.0, 10.0, 1, 1.0))
        self.assertEqual(w["by_cause"]["defect"], {"reruns": 1, "usd": 3.0})     # its finishing call included
        self.assertEqual(metrics.rework_lines(w, 7), [
            "Rework (last 7 days):",
            "  units 3 · runs 6 · 2.0 runs/unit · landed 2 (2.0 runs/landed unit) · 1.67 returns/unit",
            "  re-runs 3 · $5 of $10 worker spend (50%) · without owner revisions 2 · $4 (40%)",
            "  killed mid-way 1 runs · $1",
            "  AMENDs by cause (re-runs · $): owner 1·$1 defect 1·$3 retry 1·$1 untagged 0",
            "  queue: merged 2 · re-tested 1 (green 1) · returned 2 (conflict 1, red 1)",
            "  head review: pass 1 · routed 1 · returned 1 · pane pass 1 · pre-review 1 rounds, 1 returned",
            "  Main spend: acme $2.00/day (claude-opus-5-5)"])
        self.assertEqual(metrics.rework_lines(metrics.rework_summary(reg, days=7, now=NOW), 7)[-1],
                         "  Main spend: n/a (transcripts cannot be read)")
        self.assertEqual(metrics.rework_lines(None, 7), ["Rework (last 7 days):", "  n/a (registry unreadable)"])

    def test_missing_root_is_na_not_zero(self):
        m = metrics.compute(self.root / "nope", days=30, now=NOW)
        self.assertEqual((m["readable"], m["responses"], m["cost_usd"], m["routine"]["share"]),
                         (False, None, None, None))
        lines = metrics.render({**m, "triage": None, "runner": None, "rework": None})
        self.assertIn("responses: n/a (avg context n/a)", lines)
        self.assertIn("estimated cost: n/a (list price)", lines)
        self.assertIn("  n/a (no samples file)", lines)
        self.assertIn("  n/a (registry unreadable)", lines)
        self.assertNotIn("$0.00", "\n".join(lines))

    def test_an_unreadable_transcript_makes_the_total_na(self):
        locked = self.root / "-srv-orca-acme-shop" / "sess-2.jsonl"
        shutil.copy(TRANSCRIPTS / "metrics_main.jsonl", locked)
        os.chmod(locked, 0)
        try:
            if os.access(locked, os.R_OK):
                self.skipTest("running as a user who reads mode-0 files")
            m = self.compute()
        finally:
            os.chmod(locked, 0o644)
        self.assertEqual((m["unreadable"], m["cost_usd"], m["responses"]), (1, None, 5))
        lines = metrics.render(m)
        self.assertIn("1 unreadable", lines[0])
        self.assertIn("estimated cost: n/a (list price)", lines)

    def role_setup(self):
        """A registry for acme and transcripts of each role, priced on Opus 5.5 ($20/MTok output)."""
        from orche import registry
        from orche.janitor import discover
        base = self.root.parent
        reg = registry.empty()
        reg["projects"]["acme"] = json.loads((support.FIXTURES / "hooks" / "registry.json").read_text()
                                             .replace("@ROOT@", str(base)))["projects"]["acme"]
        registry.save(reg)
        root = base / "roles"
        slug = lambda name: discover.claude_slug(os.path.realpath(base / name))  # noqa: E731
        n = iter(range(100))

        def write(folder, rel, usd, title=None):
            path = root / folder / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = [{"type": "custom-title", "customTitle": title}] if title else []
            rows.append({"type": "assistant", "timestamp": "2026-10-15T10:00:00Z",
                         "message": {"id": f"msg_{next(n)}", "model": "claude-opus-5-5",
                                     "usage": {"output_tokens": int(usd * 50_000)}}})
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        write(slug("acme"), "m1.jsonl", 1.0, "main-acme")
        write(slug("acme"), "m1/subagents/agent-a.jsonl", 1.0)          # Main's subagent: Main
        write(slug("acme-staff"), "s1.jsonl", 1.0, "staff-acme")
        write(slug("acme-build"), "d1.jsonl", 1.0, "div-acme-build")
        write(slug("acme-build"), "w1.jsonl", 1.0)                       # unnamed in a unit worktree: a worker
        write(slug("acme-checkout"), "f1.jsonl", 1.0, "feat-acme-checkout")
        write(slug("acme-checkout"), "w2.jsonl", 0.5)
        write("-elsewhere", "x.jsonl", 1.0)
        from orche import paths
        from orche.runs import history
        day = (NOW - dt.timedelta(days=1)).isoformat()
        for kind, vendor, usd in (("review", "claude", 0.3), ("build-review", "codex", 0.1), ("work", "codex", 0.2),
                                  ("work", "claude", 9.0), ("finish", "claude", 9.0)):
            history.append("acme", {"unit": "u", "kind": kind, "vendor": vendor, "cost_usd": usd, "finished": day}, 1)
        history.append("acme", {"unit": "u", "kind": "review", "vendor": "claude", "cost_usd": 7.0,
                                "finished": (NOW - dt.timedelta(days=40)).isoformat()}, 1)
        events = paths.orche_home() / "logs" / "events.log"
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text("".join(f"{day}\tevents\t{msg}\n" for msg in (
            "intake project=acme head=abc1234 model=claude-sonnet-5-5 cost=0.4 turns=3 divisions=2",
            "ask project=acme by=main-acme head=abc1234 model=claude-sonnet-5-5 cost=0.1 turns=2 chars=40",
            "card project=acme refreshed base=none head=abc1234 sections=none model=m cost=0.05 turns=1",
            "ask project=acme failed: boom",
            "dispatch kind=brief unit=u cost=5")))
        return reg, root

    def test_spend_by_role(self):
        reg, root = self.role_setup()
        m = metrics.compute(root, days=30, now=NOW, classify=metrics.classifier(reg))
        self.assertEqual(m["roles"], {"Main": 2.0, "Staff": 1.0, "build pane": 1.0, "feature panes": 1.0,
                                      "other (not Orche)": 1.0, "workers": 1.5})
        m["headless"] = metrics.headless_spend(["acme"], days=30, now=NOW)
        self.assertEqual(m["headless"], {"ask": 0.15, "intake": 0.4, "review": 0.4, "workers": 0.2})
        samples = root.parent / "samples.jsonl"
        samples.write_text("".join(json.dumps({"at": (NOW - dt.timedelta(days=d)).isoformat(), "decision": "main",
                                               "backend": "haiku", "ms": 120, "model_ms": 100, "cost_usd": usd}) + "\n"
                                   for d, usd in ((1, 0.25), (40, 3.0))))
        m["triage"] = metrics.triage_summary(samples, days=30, now=NOW)
        self.assertEqual(m["triage"]["cost_usd"], 0.25)
        rows = metrics.role_rows(m)
        self.assertEqual([(r, round(usd, 2)) for r, usd, _share in rows],
                         [("Main", 2.0), ("Staff", 1.0), ("build pane", 1.0), ("feature panes", 1.0),
                          ("workers", 1.7), ("review", 0.4), ("intake", 0.4), ("ask", 0.15), ("triage", 0.25),
                          ("other (not Orche)", 1.0)])
        self.assertAlmostEqual(sum(share for *_r, share in rows), 1.0)
        lines = metrics.render({**m, "runner": None})
        at = lines.index("Spend by role (list price, $8.90):")
        self.assertEqual(lines[at + 1:at + 4], ["  ROLE               COST_USD  SHARE",
                                                "  Main               2.00      22%",
                                                "  Staff              1.00      11%"])
        self.assertEqual(lines[-1], "  other (not Orche)  1.00      11%")

    def test_spend_by_role_is_na_without_its_sources(self):
        reg, root = self.role_setup()
        m = metrics.compute(root / "nope", days=30, now=NOW, classify=metrics.classifier(reg))
        m["headless"] = {}
        self.assertIsNone(metrics.role_rows(m))
        self.assertEqual(metrics.render(m)[-2:], ["Spend by role:", "  n/a (transcripts cannot be read)"])
        m = metrics.compute(root, days=30, now=NOW, classify=metrics.classifier(None))
        self.assertEqual(m["roles"], {"other (not Orche)": 7.5})
        self.assertEqual(metrics.render({**m, "headless": None})[-1], "  n/a (registry unreadable)")
        self.assertNotIn("roles", metrics.compute(root, days=30, now=NOW))   # without a classifier

    def test_no_runs_median_is_na(self):
        self.assertIsNone(metrics.runner_summary([], days=30, now=NOW)["median_cost_usd"])


if __name__ == "__main__":
    unittest.main()
