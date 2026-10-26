"""The tier ladder (lib/orche/ladder.py): TIER picks the model and effort, the division's model is
the ceiling, a MODEL pin is checked at dispatch, and a mechanical attempt climbs once."""
import json
import unittest
from types import SimpleNamespace

import support  # noqa: F401  (puts lib/ on sys.path)
from orche import brief, config, dispatch, ladder, registry, roster
from test_runner import COMMIT, ROW, RunnerCase

CFG = {"ladder": config.DEFAULTS["ladder"]}
OPUS, SONNET, HAIKU, FABLE = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"


class RungTest(unittest.TestCase):
    def test_tiers_pick_the_rung_on_an_opus_division(self):
        self.assertEqual(ladder.rung(CFG, OPUS, "mechanical")[:2], (HAIKU, "low"))
        self.assertEqual(ladder.rung(CFG, OPUS, "standard")[:2], (OPUS, "medium"))
        self.assertEqual(ladder.rung(CFG, OPUS, "hard")[:2], (OPUS, "high"))
        self.assertEqual(ladder.rung(CFG, OPUS, "bogus")[:2], (OPUS, "medium"))

    def test_codex_keeps_its_division_model_and_climbs_effort(self):
        self.assertEqual(ladder.rung(CFG, "codex", "mechanical")[:2], ("codex", "low"))
        self.assertEqual(ladder.rung(CFG, "codex", "hard")[:2], ("codex", "high"))

    def test_the_division_model_is_the_ceiling(self):
        cfg = {"ladder": {**CFG["ladder"], "claude": {**CFG["ladder"]["claude"],
                                                      "mechanical": {"model": FABLE, "effort": "low"}}}}
        self.assertEqual(ladder.rung(cfg, SONNET, "mechanical"), ladder.Rung(SONNET, "low", "ceiling"))
        self.assertEqual(ladder.rung(CFG, SONNET, "mechanical")[:2], (HAIKU, "low"))
        self.assertEqual(ladder.rung(CFG, HAIKU, "mechanical")[:2], (HAIKU, "low"))

    def test_a_pin_replaces_the_model_never_the_effort(self):
        self.assertEqual(ladder.rung(CFG, OPUS, "hard", "sonnet"), ladder.Rung(SONNET, "high", "MODEL"))

    def test_pin_errors(self):
        self.assertIn("another vendor", ladder.pin_error(CFG, "codex", OPUS, "div-x",
                                                         available=lambda c: SimpleNamespace(state="ok")))
        self.assertIn("above", ladder.pin_error(CFG, FABLE, OPUS, "div-x"))
        self.assertTrue(ladder.pin_error(CFG, "fable", OPUS, "div-x"))
        self.assertEqual(ladder.pin_error(CFG, "sonnet", OPUS, "div-x"), "")
        absent = ladder.pin_error(CFG, "codex", "codex", "div-x", available=lambda c: SimpleNamespace(
            state="missing", detail="not installed", fix="install it"))
        self.assertTrue(absent.startswith("MODEL codex:"), absent)

    def test_escalation_rules(self):
        esc = lambda status, summary="", tier="mechanical", pinned=False, **kw: ladder.escalate(
            CFG, tier, pinned, status, summary, **kw)
        self.assertTrue(esc("failed"))
        self.assertTrue(esc("info", "unverified: no ledger row"))
        self.assertFalse(esc("done"))
        self.assertFalse(esc("blocked"))
        self.assertFalse(esc("info", "needs a decision"))
        self.assertFalse(esc("failed", tier="standard"))
        self.assertFalse(esc("failed", pinned=True))
        for kw in ("stopped", "timed_out", "turn_cap", "dead"):
            self.assertFalse(esc("failed", **{kw: True}), kw)
        self.assertFalse(ladder.escalate({"ladder": {**CFG["ladder"], "escalate": False}}, "mechanical",
                                         False, "failed", ""))

    def test_header_and_doctor_row(self):
        self.assertEqual(ladder.header(CFG, OPUS, "mechanical"), f"{HAIKU} (mechanical rung; ceiling {OPUS})")
        self.assertEqual(ladder.header(CFG, OPUS, "hard", "sonnet"), f"{SONNET} (MODEL)")
        self.assertEqual(ladder.doctor_row(CFG),
                         f"claude: mechanical {HAIKU} low · standard division medium · hard division high · "
                         "codex: low/medium/high · escalate once")

    def test_previous_keeps_only_the_tail(self):
        text = ladder.previous(HAIKU, "low", "failed", "x" * 5000 + "END")
        self.assertIn(f"{HAIKU} at effort low", text)
        self.assertIn("x" * (ladder.TAIL_CHARS - 3) + "END\n</previous>", text)
        self.assertNotIn("x" * (ladder.TAIL_CHARS + 1), text)


class LadderRunTest(RunnerCase):
    def history(self):
        path = self.home / "runs" / "acme" / "history.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def pinned_brief(self, model, tier="standard"):
        fields = {"TO": "div-acme-build", "GOAL": "Add coupon field", "SCOPE": "src/**", "CONTEXT": "none",
                  "ACCEPT": "works", "VERIFY": "true", "TIMEBOX": "20m", "FORBID": "merge",
                  "REPORT": "status", "TIER": tier, "MODEL": model}
        target = roster.Roster(registry.load()).member("div-acme-build")
        return brief.write(brief.Block("brief", fields), target, "main-acme", "1. Never force-push.",
                           timeout=0).unit

    def test_a_failed_mechanical_attempt_climbs_once_and_files_one_pointer(self):
        unit = self.brief(tier="mechanical")
        self.reply("", is_error=True, subtype="error_during_execution", code=1, cost=0.5, turns=3)
        self.orche("run", unit)
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["status"], "failed")
        rec = self.run_record(unit)
        self.assertEqual([(h["model"], h["effort"], h["kind"]) for h in self.history()],
                         [(HAIKU, "low", "work"), (OPUS, "medium", "escalation")])     # escalated once, from HAIKU
        self.assertEqual((rec["model"], rec["cost_usd"], rec["num_turns"]), (OPUS, 1.0, 6))
        self.assertIn("rung=mechanical:claude-opus-5-5:medium attempts=2 escalated=1\n", self.events())
        self.assertIn("## Previous attempt (one rung down)", (self.fake / "stdin.txt").read_text())

    def test_a_failed_mechanical_attempt_that_climbs_to_done_publishes_once(self):
        """T-ladder + T-publish: only the final attempt's gated done pushes and opens the PR."""
        unit = self.brief(tier="mechanical")
        self.ledger()
        self.reply("", is_error=True, subtype="error_during_execution", code=1)
        done = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                           "result": "STATUS: done\nok", "total_cost_usd": 0.2, "stop_reason": "end_turn"})
        d = self.fake
        self.action(f'if [ -f "{d}/second" ]; then\n{COMMIT}{ROW.format(verdict="unit-test-verified")}'
                    f"printf '%s' '{done}' > \"{d}/reply.json\"\necho 0 > \"{d}/exit\"\n"
                    f'else touch "{d}/second"; fi\n')
        code, out, err = self.orche("run", unit)
        self.assertEqual(code, 0, out + err)
        rec = self.run_record(unit)
        self.assertEqual([(h["model"], h["status"]) for h in self.history()], [(HAIKU, "failed"), (OPUS, "done")])
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"], ptr["pr"]), ("done", self.published("ok"), 1))
        self.assertEqual(rec["pr"]["number"], 1)
        self.assertEqual([c["argv"][:2] for c in support.gh_calls(self.gh)].count(["pr", "create"]), 1)

    def test_a_done_mechanical_attempt_does_not_climb(self):
        unit = self.brief(tier="mechanical")
        self.reply("STATUS: blocked — needs a key", cost=0.1, turns=2)
        self.orche("run", unit)
        rec = self.run_record(unit)
        self.assertEqual(rec["model"], HAIKU)
        self.assertEqual(len(self.history()), 1)

    def test_a_pinned_sonnet_runs_sonnet_on_an_opus_division_and_never_climbs(self):
        unit = self.pinned_brief("sonnet", tier="mechanical")
        _, out, err = self.orche("--dry-run", "--json", "run", unit)
        argv = json.loads(out)["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], SONNET, err)
        self.reply("", is_error=True, subtype="error_during_execution", code=1)
        self.orche("run", unit)
        self.assertEqual(len(self.history()), 1)

    def test_dispatch_refuses_a_pin_the_division_cannot_run(self):
        reg = registry.load()
        target = roster.Roster(reg).member("div-acme-build")
        r = SimpleNamespace(reg=reg)
        for pin in ("codex", "claude-fable-5-1", "fable"):
            block = SimpleNamespace(fields={"MODEL": pin})
            with self.assertRaises(brief.Refusal, msg=pin):
                dispatch._check_pin(r, target, block, {**CFG, "codex": {"enabled": True}})
        dispatch._check_pin(r, target, SimpleNamespace(fields={"MODEL": "sonnet"}), CFG)


class AuditTest(unittest.TestCase):
    def test_a_confident_disagreement_is_one_events_line(self):
        from unittest import mock
        lines = []
        with mock.patch("orche.log.log", lambda name, text: lines.append((name, text))):
            ask = lambda text, qs: {"tier": {"value": "hard", "confidence": 0.9}}
            self.assertEqual(ladder.audit("u-1", "mechanical", "brief", {"jev": {"enabled": True}}, ask), ("hard", 0.9))
            self.assertIsNone(ladder.audit("u-1", "mechanical", "brief", {"jev": {"enabled": False}}, ask))
            ladder.audit("u-1", "mechanical", "brief", {"jev": {"enabled": True}},
                         lambda t, q: {"tier": {"value": "hard", "confidence": 0.5}})
            ladder.audit("u-1", "hard", "brief", {"jev": {"enabled": True}}, ask)
        self.assertEqual(lines, [("events", "tier-audit unit=u-1 tier=mechanical jev=hard conf=0.90")])


if __name__ == "__main__":
    unittest.main()
