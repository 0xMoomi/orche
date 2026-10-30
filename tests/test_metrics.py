"""`orche metrics` on synthetic transcripts: dedupe, context, cost, wake sources, routine share."""
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import metrics, runs
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

    def run_records(self, repo, now):
        """Three RUN.json records in the window (one failed; two Opus medium, one Sonnet without an
        effort) and one older than 30 days."""
        opus, sonnet = ("claude-opus-5-5", "medium"), ("claude-sonnet-5-5", None)
        for i, (status, cost, age, (model, effort)) in enumerate((
                ("done", 0.2, 1, opus), ("failed", 1.0, 2, opus), ("info", 0.4, 3, sonnet),
                ("done", 9.0, 40, opus))):
            folder = repo / ".orche" / "units" / f"acme-u-20261016-{i + 1}"
            folder.mkdir(parents=True)
            (folder / "RUN.json").write_text(json.dumps({
                "status": status, "cost_usd": cost, "model": model, "effort": effort,
                "finished": (now - dt.timedelta(days=age)).isoformat()}))

    def test_runner_summary_sums_run_records_in_the_window(self):
        repo = self.root.parent / "acme"
        self.run_records(repo, NOW)
        self.assertEqual(metrics.runner_summary(runs.records([repo]), days=30, now=NOW),
                         {"units": 3, "cost_usd": 1.6, "median_cost_usd": 0.4, "failures": 1,
                          "by_status": {"done": 1, "failed": 1, "info": 1},
                          "by_model_effort": {
                              "claude-opus-5-5 medium": {"units": 2, "cost_usd": 1.2, "median_cost_usd": 0.6,
                                                         "failures": 1},
                              "claude-sonnet-5-5 ?": {"units": 1, "cost_usd": 0.4, "median_cost_usd": 0.4,
                                                      "failures": 0}}})
        self.assertEqual(metrics.runner_summary(runs.records([repo / "nope"]), days=30, now=NOW)["units"], 0)

    def test_records_skips_an_unreadable_run_json(self):
        """A RUN.json that is a directory or cannot be read is skipped, as the old metrics scan did."""
        repo = self.root.parent / "acme"
        self.run_records(repo, NOW)
        units = repo / ".orche" / "units"
        (units / "acme-u-20261016-8" / "RUN.json").mkdir(parents=True)
        unreadable = units / "acme-u-20261016-9" / "RUN.json"
        unreadable.parent.mkdir()
        unreadable.write_text(json.dumps({"status": "done", "cost_usd": 5.0, "finished": NOW.isoformat()}))
        os.chmod(unreadable, 0)
        try:
            found = runs.records([repo])
            blocked = not os.access(unreadable, os.R_OK)
        finally:
            os.chmod(unreadable, 0o644)
        self.assertEqual(sorted(rec["cost_usd"] for rec in found),
                         [0.2, 0.4, 1.0, 9.0] if blocked else [0.2, 0.4, 1.0, 5.0, 9.0])

    def test_cli_runner_section_from_registered_projects(self):
        from orche import registry
        repo = self.root.parent / "acme"
        self.run_records(repo, dt.datetime.now(dt.timezone.utc))
        reg = registry.empty()
        reg["projects"]["acme"] = json.loads((support.FIXTURES / "hooks" / "registry.json").read_text()
                                             .replace("@ROOT@", str(self.root.parent)))["projects"]["acme"]
        registry.save(reg)
        cli, out = load_cli(), io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["metrics", "--root", str(self.root), "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["runner"]["units"], 3)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["metrics", "--root", str(self.root)])
        self.assertEqual(out.getvalue().splitlines()[-4:], [
            "Runner (orche run, RUN.json):", "  units 3, cost $1.60, median $0.40/unit, failures 1",
            "  claude-opus-5-5 medium: units 2, cost $1.20, median $0.60/unit, failures 1",
            "  claude-sonnet-5-5 ?: units 1, cost $0.40, median $0.40/unit, failures 0"])

    def test_missing_root_is_na_not_zero(self):
        m = metrics.compute(self.root / "nope", days=30, now=NOW)
        self.assertEqual((m["readable"], m["responses"], m["cost_usd"], m["routine"]["share"]),
                         (False, None, None, None))
        lines = metrics.render({**m, "triage": None, "runner": None})
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

    def test_no_runs_median_is_na(self):
        self.assertIsNone(metrics.runner_summary([], days=30, now=NOW)["median_cost_usd"])


if __name__ == "__main__":
    unittest.main()
