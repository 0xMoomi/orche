"""`orche metrics` on synthetic transcripts: dedupe, context, cost, wake sources, routine share."""
import datetime as dt
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import metrics
from test_registry import load_cli

TRANSCRIPTS = support.FIXTURES / "transcripts"
NOW = dt.datetime(2026, 10, 16, 0, 0, tzinfo=dt.timezone.utc)


class MetricsTest(unittest.TestCase):
    def setUp(self):
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
        row = {"input": 1.0, "output": 0, "cache_read": 0, "cache_write": 0}
        prices = metrics.load_prices(self.prices_file({"claude-mystery-9": row}))
        self.assertAlmostEqual(self.compute(prices=prices)["models"]["claude-mystery-9"]["cost_usd"], 1.0)
        self.assertEqual(metrics.price_row("claude-opus-5-5-20261001", metrics.PRICES)["input"], 4.0)

    def test_prices_rejects_bad_rows(self):
        bad = {
            "non-dict row": {"m": 1},
            "string value": {"m": {"input": "1", "output": 0, "cache_read": 0, "cache_write": 0}},
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
        for text in ("진행상황 알려줘", "상황 알려줘", "멈춘 거야?", "이어서 해", "계속 진행", "컨펌", "승인",
                     "진행해", "status?", "Progress update please", "continue", "go ahead", "Approve"):
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

    def test_missing_root_is_empty(self):
        m = metrics.compute(self.root / "nope", days=30, now=NOW)
        self.assertEqual((m["responses"], m["cost_usd"], m["routine"]["share"]), (0, 0, 0.0))


if __name__ == "__main__":
    unittest.main()
