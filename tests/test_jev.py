"""Jev gate client (fake HTTP opener only — no network) and the offline evaluation harness."""
import datetime as dt
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import cmd_jev, inbox, jev, mask
from test_registry import load_cli

TRANSCRIPTS = support.FIXTURES / "transcripts"
NOW = dt.datetime(2026, 10, 16, 0, 0, tzinfo=dt.timezone.utc)
# Secret-shaped canaries are assembled at runtime so the repository never contains such a literal.
CANARY_KEY = "sk-" + "CANARY" + "abcdefghijklmnop" + "0123456789"
FIXTURE_KEY = "sk-" + "ABCDEFGHIJKLM" + "NOPQRSTUVWX12"
CANARY_SEED = " ".join(["abandon", "ability", "able", "about", "above", "absent",
                        "absorb", "abstract", "absurd", "abuse", "access", "accident"])
CANARY_CODE = "def leak_canary_fn():\n    return 42"
CANARY_DIFF = ("diff --git a/app.py b/app.py\nindex 1111..2222 100644\n--- a/app.py\n+++ b/app.py\n"
               "@@ -1,2 +1,2 @@\n-old_canary_line()\n+new_canary_line()\n context_canary\n")
CANARY_BLOB = "QkxPQkNBTkFSWQ" + "A" * 500
CODE_TEXT = (f"please check this\n```python\n{CANARY_CODE}\n```\nand the patch\n{CANARY_DIFF}"
             f"\nblob:\n{CANARY_BLOB}\nthanks")
CODE_CANARIES = ("leak_canary_fn", "old_canary_line", "new_canary_line", "context_canary", "QkxPQkNBTkFSWQ")
PEM = ("-----BEGIN " + "RSA PRIVATE " + "KEY-----\n" + "\n".join(["MIIEow" + "B" * 58] * 78)
       + "\n-----END " + "RSA PRIVATE " + "KEY-----")


def install_fixture(dest):
    """Copy jev_main.jsonl to `dest`, substituting the runtime-built canary key."""
    text = (TRANSCRIPTS / "jev_main.jsonl").read_text().replace("@CANARY_KEY@", FIXTURE_KEY)
    Path(dest).write_text(text)


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body, self.closed = status, body, False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class FakeOpener:
    """Records every request; answers with `reply(questions)` or raises `exc`."""

    def __init__(self, reply=None, status=200, raw=None, exc=None):
        self.reply, self.status, self.raw, self.exc = reply, status, raw, exc
        self.requests = []

    def __call__(self, request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append({"url": request.full_url, "headers": dict(request.header_items()),
                              "body": body, "raw": request.data.decode("utf-8"), "timeout": timeout})
        if self.exc:
            raise self.exc
        if self.raw is not None:
            return FakeResponse(self.status, self.raw)
        answers = self.reply(body["questions"]) if self.reply else {}
        return FakeResponse(self.status, json.dumps({"model": "jev-1.13.0", "answers": answers,
                                                     "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())


def wire_answer(q, value, confidence=0.95):
    """A TypeSafe answer (docs.typesafe.ai/api, "Answer types") to wire question `q`."""
    if q["type"] == "noul":
        return {"type": "noul", "noul": value}
    if q["type"] == "choice":
        return {"type": "choice", "choice": value, "confidence": confidence,
                "probabilities": {k: (1.0 if k == value else 0.0) for k in q["criteria"]}}
    legend = {str(i): label for i, label in enumerate(q["criteria"])}
    return {"type": "score", "score": value, "legend": legend, "confidence": confidence,
            "probabilities": {k: (1.0 if int(k) == round(value) else 0.0) for k in legend}}


def answer_all(value_for, confidence=0.95):
    """reply(questions map) -> answers map; `value_for(wire question)` gives noul p, choice key or
    0-based score."""
    def reply(questions):
        return {qid: wire_answer(q, value_for(q), confidence) for qid, q in questions.items()}
    return reply


def default_value(q):
    return {"noul": 0.97, "score": 1, "choice": next(iter(q.get("criteria") or {"x": None}))}[q["type"]]


class JevCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self._env = os.environ.get("JEV_API_KEY")
        os.environ["JEV_API_KEY"] = "test-key"
        self.addCleanup(self._restore_env)
        self.addCleanup(setattr, jev, "OPENER", jev.OPENER)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("JEV_API_KEY", None)
        else:
            os.environ["JEV_API_KEY"] = self._env

    def configure(self, enabled=True, base_url="https://api.jev.test"):
        (self.home / "config.json").write_text(json.dumps({"jev": {"enabled": enabled, "base_url": base_url}}))

    def log_lines(self):
        path = self.home / "logs" / "jev.log"
        return path.read_text().splitlines() if path.exists() else []


class ClientTest(JevCase):
    def client(self, opener, base_url="https://api.jev.test", **kw):
        return jev.JevClient(base_url, opener=opener, **kw)

    def test_timeout_returns_none_and_logs(self):
        opener = FakeOpener(exc=TimeoutError("timed out"))
        self.assertIsNone(self.client(opener).ask("state", [jev.noul("a", "q?")]))
        self.assertEqual(opener.requests[0]["timeout"], 1.0)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"\tjev\tms=\d+ q=1 timeout$")
        self.assertNotIn("state", lines[0])

    def test_http_500_and_bad_json_return_none(self):
        self.assertIsNone(self.client(FakeOpener(status=500, raw=b"{}")).ask("s", [jev.noul("a", "q")]))
        self.assertIsNone(self.client(FakeOpener(raw=b"not json")).ask("s", [jev.noul("a", "q")]))
        self.assertIsNone(self.client(FakeOpener(raw=b'{"answers": "x"}')).ask("s", [jev.noul("a", "q")]))
        self.assertIsNone(self.client(FakeOpener(exc=OSError("boom"))).ask("s", [jev.noul("a", "q")]))
        lines = self.log_lines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].endswith("error:HTTP500"))
        self.assertTrue(lines[1].endswith("error:JSONDecodeError"))
        self.assertTrue(lines[3].endswith("error:OSError"))

    def test_well_formed_response_parsed_with_bands(self):
        def reply(qs):
            return {"a": {"type": "noul", "noul": 0.05},
                    "b": wire_answer(qs["b"], "status", 0.7),
                    "c": wire_answer(qs["c"], 2.0, 0.2),
                    "zz": {"type": "noul", "noul": 0.99}}
        qs = [jev.noul("a", "q"), jev.choice("b", "q", ["status", "confirm"]), jev.score("c", "q")]
        got = self.client(FakeOpener(reply)).ask("s", qs)
        self.assertEqual(got, {"a": {"value": 0.05, "confidence": 0.95, "band": "act"},
                               "b": {"value": "status", "confidence": 0.7, "band": "confirm",
                                     "probabilities": {"status": 1.0, "confirm": 0.0}},
                               "c": {"value": 3.0, "confidence": 0.2, "band": "defer"}})
        self.assertTrue(self.log_lines()[0].endswith("q=3 ok"))

    def test_unusable_answers_are_dropped(self):
        def reply(qs):
            return {"a": {"type": "choice", "noul": 0.9},                # wrong type
                    "b": {"type": "choice", "choice": "status"},          # no confidence
                    "c": wire_answer(qs["c"], 4.5, 0.9),                  # beyond the last level (0-4)
                    "d": {"type": "noul", "noul": 1.5},                   # not a probability
                    "e": {"type": "noul", "noul": 0.5}}
        qs = [jev.noul("a", "q"), jev.choice("b", "q", ["status"]), jev.score("c", "q"),
              jev.noul("d", "q"), jev.noul("e", "q")]
        self.assertEqual(self.client(FakeOpener(reply)).ask("s", qs),
                         {"e": {"value": 0.5, "confidence": 0.5, "band": "defer"}})
        self.assertIsNone(self.client(FakeOpener(raw=b'{"answers": [{"id": "e", "value": 1}]}')).ask(
            "s", [jev.noul("e", "q")]))  # the old list shape is not accepted

    def test_bands(self):
        self.assertEqual([jev.band(c) for c in (0.95, 0.9, 0.89, 0.6, 0.59, None)],
                         ["act", "act", "confirm", "confirm", "defer", "defer"])
        self.assertEqual((jev.ACT, jev.CONFIRM), (0.9, 0.6))

    def test_canary_secrets_never_reach_request_body(self):
        opener = FakeOpener(answer_all(default_value))
        state = f"deploy with {CANARY_KEY}\nseed: {CANARY_SEED}\nthanks"
        qs = [jev.noul("a", f"is {CANARY_KEY} live?"), jev.choice("b", "pick", [CANARY_KEY, "other"])]
        self.assertIsNotNone(self.client(opener).ask(state, qs))
        raw = opener.requests[0]["raw"]
        self.assertNotIn(CANARY_KEY, raw)
        self.assertNotIn(" ".join(CANARY_SEED.split()[:3]), raw)
        self.assertIn("[REDACTED]", opener.requests[0]["body"]["state"])
        self.assertNotIn(CANARY_KEY, "\n".join(self.log_lines()))

    def test_direct_vs_gateway_naming(self):
        direct = FakeOpener(answer_all(default_value))
        self.client(direct, base_url="https://api.typesafe.test/").ask("s", [jev.noul("a", "q")])
        req = direct.requests[0]
        self.assertEqual(req["url"], "https://api.typesafe.test/v1/systemone")
        self.assertEqual(req["body"]["model"], "jev-1.13.0")
        self.assertEqual(req["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(set(req["body"]), {"model", "state", "questions"})
        gw = FakeOpener(answer_all(default_value))
        self.client(gw, base_url=jev.GATEWAY_BASE_URL, model="jev").ask("s", [jev.noul("a", "q")])
        self.assertEqual(gw.requests[0]["url"], "https://ai-gateway.vercel.sh/typesafe/v1/systemone")
        self.assertEqual(gw.requests[0]["body"]["model"], "typesafe-ai/jev")
        self.assertEqual(jev.KINDS, {"gateway": ("https://ai-gateway.vercel.sh/typesafe", "jev"),
                                     "direct": ("https://api.typesafe.ai", "jev-1.13.0")})

    def test_question_shapes(self):
        self.assertEqual(jev.score("s", "q"), {"type": "score", "id": "s", "question": "q", "min": 1, "max": 5,
                                               "labels": ["1", "2", "3", "4", "5"]})
        self.assertEqual(jev.noul("n", "q"), {"type": "noul", "id": "n", "question": "q"})
        self.assertEqual(jev.choice("c", "q", ("x", "y")),
                         {"type": "choice", "id": "c", "question": "q", "options": ["x", "y"],
                          "descriptions": {}})
        self.assertEqual(jev.choice("c", "q", {"x": "the x one", "y": "the y one"}),
                         {"type": "choice", "id": "c", "question": "q", "options": ["x", "y"],
                          "descriptions": {"x": "the x one", "y": "the y one"}})
        with self.assertRaises(ValueError):
            jev.score("s", "q", 1, 3, ["low", "high"])

    def test_choice_descriptions_become_masked_criteria(self):
        c = self.client(FakeOpener(answer_all(default_value)))
        body = c.body("s", [jev.choice("d", "which?", {"build": "code and doc changes", "ops": "",
                                                      "leak": f"key {CANARY_KEY} here"})])
        criteria = body["questions"]["d"]["criteria"]
        self.assertEqual(list(criteria), ["build", "ops", "leak"])
        self.assertEqual((criteria["build"], criteria["ops"]), ("code and doc changes", None))
        self.assertNotIn(CANARY_KEY, criteria["leak"])
        self.assertEqual(c.body("s", [jev.choice("d", "which?", ["a", "b"])])["questions"]["d"]["criteria"],
                         {"a": None, "b": None})

    def test_choice_probabilities_kept_for_asked_options_only(self):
        q = jev.choice("k", "which?", ["yes", "no", "maybe"])

        def parsed(probs):
            body = json.dumps({"answers": {"k": {"type": "choice", "choice": "yes", "confidence": 0.8,
                                                 "probabilities": probs}}}).encode()
            return jev._parse(body, [q])["k"]
        self.assertEqual(parsed({"yes": 0.9, "no": 0.1, "other": 0.5})["probabilities"], {"yes": 0.9, "no": 0.1})
        self.assertNotIn("probabilities", parsed({"yes": "0.9", "no": True, "maybe": -0.1}))
        self.assertNotIn("probabilities", parsed([0.9, 0.1]))
        self.assertNotIn("probabilities", parsed({}))
        self.assertEqual(parsed({"yes": 1, "no": None})["probabilities"], {"yes": 1.0})
        self.assertEqual(parsed({"yes": 0.9}), {"value": "yes", "confidence": 0.8, "band": "confirm",
                                                "probabilities": {"yes": 0.9}})

    def test_choice_probability_nan_is_dropped(self):
        q = jev.choice("k", "which?", ["yes", "no"])
        body = (b'{"answers": {"k": {"type": "choice", "choice": "yes", "confidence": 0.8,'
                b' "probabilities": {"yes": NaN, "no": 0.1}}}}')
        self.assertEqual(jev._parse(body, [q])["k"]["probabilities"], {"no": 0.1})

    def test_long_choice_description_is_cut_with_the_marker(self):
        c = self.client(FakeOpener(answer_all(default_value)))
        limit = jev.WIRE["max_question_chars"]
        long = " ".join(f"Step {i}: describe the division in plain words." for i in range(limit // 40))
        self.assertGreater(len(long), limit)
        body = c.body("s", [jev.choice("d", "which?", {"a": long, "b": "short"})])
        cut = body["questions"]["d"]["criteria"]["a"]
        self.assertLessEqual(len(cut), limit)
        self.assertTrue(cut.endswith(jev.WIRE["truncated_marker"]))
        self.assertEqual(body["questions"]["d"]["criteria"]["b"], "short")

    def test_wire_body_matches_documented_request(self):
        """docs.typesafe.ai/api "Example request" shapes: questions is a map keyed by id with
        type + instructions (+ criteria: option map for choice, ordered level list for score)."""
        opener = FakeOpener(answer_all(default_value))
        self.client(opener).ask("Help! My payouts have been failing for 3 days.", [
            jev.noul("is_urgent", "Does this convey urgency?"),
            jev.choice("department", "Which team should handle this?", ["billing", "technical", "sales"]),
            jev.score("frustration", "How frustrated is the customer?", 0, 2, ["Calm", "Frustrated", "Very angry"])])
        self.assertEqual(opener.requests[0]["body"], {
            "model": "jev-1.13.0",
            "state": "Help! My payouts have been failing for 3 days.",
            "questions": {
                "is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"},
                "department": {"type": "choice", "instructions": "Which team should handle this?",
                               "criteria": {"billing": None, "technical": None, "sales": None}},
                "frustration": {"type": "score", "instructions": "How frustrated is the customer?",
                                "criteria": ["Calm", "Frustrated", "Very angry"]}}})

    def test_documented_responses_parse(self):
        """Fixture bodies copied from docs.typesafe.ai/api ("Example response" per answer type) and
        from vercel.com/docs/ai-gateway/sdks-and-apis/typesafe (gateway answer + provider_metadata)."""
        direct = (b'{"model": "jev-1.13.0", "answers": {'
                  b'"is_urgent": {"type": "noul", "noul": 0.95},'
                  b'"department": {"type": "choice", "choice": "billing", "probabilities": '
                  b'{"billing": 0.88, "technical": 0.12, "sales": 0.0}, "confidence": 0.81},'
                  b'"frustration": {"type": "score", "score": 1.05, "legend": {"0": "Calm", "1": "Frustrated", '
                  b'"2": "Very angry"}, "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05}, "confidence": 0.92}},'
                  b'"usage": {"input_tokens": 296, "output_tokens": 20}}')
        qs = [jev.noul("is_urgent", "q"), jev.choice("department", "q", ["billing", "technical", "sales"]),
              jev.score("frustration", "q", 0, 2, ["Calm", "Frustrated", "Very angry"])]
        got = self.client(FakeOpener(raw=direct)).ask("s", qs)
        self.assertEqual(got["is_urgent"]["value"], 0.95)
        self.assertEqual(got["department"], {"value": "billing", "confidence": 0.81, "band": "confirm",
                                             "probabilities": {"billing": 0.88, "technical": 0.12, "sales": 0.0}})
        self.assertEqual(got["frustration"], {"value": 1.05, "confidence": 0.92, "band": "act"})
        gateway = (b'{"model": "typesafe-ai/jev", "answers": {"refund": { "type": "noul", "noul": 0.98 }},'
                   b'"usage": { "input_tokens": 275, "output_tokens": 20 }, "provider_metadata": {"gateway": '
                   b'{"routing": {"originalModelId": "typesafe-ai/jev"}, "cost": "0.00001155"}}}')
        c = self.client(FakeOpener(raw=gateway), base_url=jev.GATEWAY_BASE_URL, model="jev")
        self.assertEqual(c.ask("s", [jev.noul("refund", "q")])["refund"]["value"], 0.98)

    def test_state_and_question_truncation(self):
        opener = FakeOpener(answer_all(default_value))
        self.client(opener).ask("ab1 " * 25_000, [jev.noul("a", "cd2 " * 10_000)])
        req = opener.requests[0]
        self.assertLessEqual(len(req["raw"]), jev.WIRE["max_request_chars"])
        self.assertTrue(req["body"]["state"].endswith("[truncated]"))
        self.assertLessEqual(len(req["body"]["questions"]["a"]["instructions"]), jev.WIRE["max_question_chars"])
        self.assertTrue(req["body"]["questions"]["a"]["instructions"].endswith("[truncated]"))
        self.client(opener).ask('"q\\' * 40_000, [jev.noul("a", "q")])  # escapes double in JSON
        self.assertLessEqual(len(opener.requests[1]["raw"]), jev.WIRE["max_request_chars"])
        self.assertTrue(opener.requests[1]["body"]["state"].endswith("[truncated]"))

    def test_no_key_never_calls(self):
        os.environ.pop("JEV_API_KEY")
        opener = FakeOpener(answer_all(default_value))
        self.assertIsNone(self.client(opener).ask("s", [jev.noul("a", "q")]))
        self.assertEqual(opener.requests, [])


    def test_wall_clock_timeout_with_deadline(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(request, timeout):
            release.wait(5)
            return b'{"answers": []}'
        started = time.monotonic()
        got = self.client(slow).ask("s", [jev.noul("a", "q")], deadline=started + 0.5)
        elapsed = time.monotonic() - started
        self.assertIsNone(got)
        self.assertLess(elapsed, 0.5)  # deadline minus RESERVE_S, plus slack
        self.assertTrue(self.log_lines()[-1].endswith("timeout"))

    def test_masking_time_counts_against_deadline(self):
        real = mask.redact

        def slow_redact(text):
            time.sleep(0.15)
            return real(text)
        orig, mask.redact = mask.redact, slow_redact
        self.addCleanup(setattr, mask, "redact", orig)
        opener = FakeOpener(answer_all(default_value))
        self.client(opener).ask("s", [jev.noul("a", "q")], deadline=time.monotonic() + 1.0)
        # two redactions (state + question) = 0.3 s, reserve 0.2 s -> at most ~0.5 s left
        self.assertLessEqual(opener.requests[0]["timeout"], 0.5)
        opener = FakeOpener(answer_all(default_value))
        self.assertIsNone(self.client(opener).ask("s", [jev.noul("a", "q")], deadline=time.monotonic() + 0.4))
        self.assertEqual(opener.requests, [])
        self.assertTrue(self.log_lines()[-1].endswith("timeout"))

    def test_code_diffs_and_blobs_never_sent(self):
        opener = FakeOpener(answer_all(default_value))
        self.client(opener).ask(CODE_TEXT, [jev.noul("a", "q")])
        raw = opener.requests[0]["raw"]
        for canary in CODE_CANARIES:
            self.assertNotIn(canary, raw)
        self.assertIn("[code omitted]", raw)
        self.assertIn("please check this", raw)
        opener = FakeOpener(answer_all(default_value))
        jev.OPENER = opener
        self.configure()
        jev.classify_pointers([{"unit": "u1", "summary": CODE_TEXT}])
        for canary in CODE_CANARIES:
            self.assertNotIn(canary, opener.requests[0]["raw"])

    def test_strip_code_tilde_fence_and_blank_line_in_hunk(self):
        got = jev.strip_code("hi\n~~~python\ndef secret_fn(): pass\n~~~\nbye")
        self.assertNotIn("secret_fn", got)
        self.assertEqual(got, "hi\n[code omitted]\nbye")
        self.assertNotIn("secret_fn", jev.strip_code("hi\n~~~~\ndef secret_fn(): pass"))  # unterminated
        got = jev.strip_code("@@ -1,4 +1,4 @@\n a\n\n-old_secret_code()\n+new_secret_code()\nend")
        self.assertNotIn("old_secret_code", got)
        self.assertNotIn("new_secret_code", got)
        self.assertTrue(got.endswith("\nend"))
        got = jev.strip_code("@@ -1,6 +1,6 @@\n def a():\n     pass\n\n\n-def old_secret():\n"
                             "+def new_secret():\n     pass")
        self.assertNotIn("old_secret", got)
        self.assertNotIn("new_secret", got)
        self.assertNotIn("pass", got)
        got = jev.strip_code("@@ -1 +1 @@\n-x\n\n\nNormal sentence here.")
        self.assertIn("Normal sentence here.", got)
        self.assertNotIn("-x", got)

    def test_base_exception_in_worker_returns_none(self):
        def interrupted(request, timeout):
            raise KeyboardInterrupt()
        self.assertIsNone(self.client(interrupted).ask("s", [jev.noul("a", "q")]))
        self.assertTrue(self.log_lines()[-1].endswith("error:KeyboardInterrupt"))

    def test_secret_straddling_old_cut_point_is_masked(self):
        old_cut = jev.WIRE["max_request_chars"] + 4096
        for secret in (CANARY_KEY, CANARY_SEED):
            opener = FakeOpener(answer_all(default_value))
            filler = "ab1 " * ((old_cut - len(PEM) - 8) // 4)
            state = filler + PEM + "\n" + "x " * 2 + secret + " tail"
            self.assertLess(len(filler) + len(PEM), old_cut)
            self.assertGreater(len(filler) + len(PEM) + 5 + len(secret), old_cut)
            self.client(opener).ask(state, [jev.noul("a", "q")])
            body = opener.requests[0]["body"]["state"]
            self.assertIn("[REDACTED]", body)
            self.assertNotIn("CANARY", body)
            self.assertNotIn("abandon", body)
            self.assertNotIn("MIIEow", body)


class HelpersTest(JevCase):
    def test_disabled_or_unconfigured_returns_none_without_calls(self):
        opener = jev.OPENER = FakeOpener(answer_all(default_value))
        self.assertIsNone(jev.classify_prompt("hi"))  # defaults: disabled
        self.configure(enabled=True, base_url="")
        self.assertIsNone(jev.suggest_tier("brief"))
        self.configure(enabled=True)
        os.environ.pop("JEV_API_KEY")
        self.assertIsNone(jev.card_needs_refresh(["feat: x"]))
        self.assertEqual(opener.requests, [])

    def test_helpers_parse(self):
        self.configure()
        jev.OPENER = FakeOpener(answer_all(default_value))
        got = jev.classify_prompt("ship it", ["build", "rnd"])
        self.assertEqual(got, {"target": "build", "needs_plan": 0.97, "irreversible": 0.97, "urgency": 2,
                               "routine_kind": "status", "confidence": 0.95})
        self.assertEqual(jev.suggest_tier("rename a var"), {"tier": "hard", "confidence": 0.95})
        self.assertEqual(jev.match_scope("add search", [{"name": "search", "scope": ["src/search/**"]}]),
                         {"match": "search", "new_justified": 0.97, "confidence": 0.95})
        self.assertEqual(jev.card_needs_refresh(["feat: new button"]), 0.97)
        self.assertEqual(jev.classify_pointers([{"unit": "u1", "status": "done"}]),
                         {"u1": {"wake_now": 0.97, "user_now": 0.97}})
        jev.OPENER = FakeOpener(exc=TimeoutError())
        self.assertIsNone(jev.classify_pointers([{"unit": "u1"}]))

    def test_suggest_tier_mapping(self):
        self.configure()
        jev.OPENER = FakeOpener(answer_all(lambda q: 0 if q["type"] == "score" else 0.1))  # level 1
        self.assertEqual(jev.suggest_tier("b")["tier"], "mechanical")
        jev.OPENER = FakeOpener(answer_all(lambda q: 2 if q["type"] == "score" else 0.1))  # level 3
        self.assertEqual(jev.suggest_tier("b")["tier"], "standard")
        jev.OPENER = FakeOpener(answer_all(lambda q: 3 if q["type"] == "score" else 0.1))  # level 4
        self.assertEqual(jev.suggest_tier("b")["tier"], "hard")
        body = jev.OPENER.requests[0]["body"]["questions"]["complexity"]
        self.assertEqual(body["criteria"], list(jev.COMPLEXITY_LEVELS))


def ptr(unit, status="done", urgent=False):
    return (Path(f"/x/{unit}.json"), {"unit": unit, "status": status, "urgent": urgent, "from": "w"})


class InboxGateTest(JevCase):
    def scores(self, mapping):
        def reply(qs):
            return {qid: {"type": "noul", "noul": mapping[int(qid.split(".")[1])]} for qid in qs}
        jev.OPENER = FakeOpener(reply)
        return jev.OPENER

    def test_adds_urgency_at_act_and_orders_first(self):
        self.configure()
        self.scores({0: 0.2, 1: 0.95})
        items = [ptr("a"), ptr("b")]
        out = inbox._jev("hi", items)
        self.assertEqual([p["unit"] for _x, p in out], ["b", "a"])
        self.assertTrue(out[0][1]["urgent"])
        self.assertFalse(out[1][1]["urgent"])
        self.assertFalse(items[1][1]["urgent"])  # input pointers are not mutated

    def test_never_removes_urgency(self):
        self.configure()
        self.scores({0: 0.0, 1: 0.0, 2: 0.89})
        items = [ptr("a"), ptr("b", urgent=True), ptr("c", status="failed")]
        out = inbox._jev("hi", items)
        self.assertEqual([p["unit"] for _x, p in out], ["b", "c", "a"])
        self.assertTrue(out[0][1]["urgent"])
        self.assertEqual(out[1][1]["status"], "failed")
        self.assertFalse(out[2][1]["urgent"])  # 0.89 < ACT
        self.assertEqual(len(out), 3)

    def test_unchanged_on_none(self):
        self.configure()
        jev.OPENER = FakeOpener(status=500, raw=b"")
        items = [ptr("a"), ptr("b", urgent=True)]
        self.assertIs(inbox._jev("hi", items), items)

    def test_disabled_untouched(self):
        self.configure(enabled=False)
        opener = self.scores({0: 0.99})
        items = [ptr("a")]
        self.assertIs(inbox._jev("hi", items), items)
        self.assertEqual(opener.requests, [])

    def test_out_of_budget_skips_call(self):
        self.configure()
        opener = self.scores({0: 0.99})
        items = [ptr("a")]
        import time
        self.assertIs(inbox._jev("hi", items, deadline=time.monotonic() - 1), items)
        self.assertEqual(opener.requests, [])


class DrainParityTest(JevCase):
    def test_eval_drain_request_equals_hook_request(self):
        self.configure()
        hook = jev.OPENER = FakeOpener(answer_all(default_value))
        jev.classify_pointers([{**cmd_jev.EVAL_POINTER, "summary": "Checkout done; tests green"}])
        ev = FakeOpener(answer_all(default_value))
        client = jev.JevClient("https://api.jev.test", opener=ev)
        rows = cmd_jev.run([{"id": "r1", "kind": "report", "text": "Checkout done; tests green"}], client)
        self.assertEqual(ev.requests[0]["body"], hook.requests[0]["body"])
        self.assertEqual(rows[0]["answer"], True)


class ExtractTest(JevCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "projects"
        session = self.root / "-srv-orca-acme-shop"
        (session / "sess-1" / "subagents").mkdir(parents=True)
        install_fixture(session / "sess-1.jsonl")
        shutil.copy(TRANSCRIPTS / "metrics_subagent.jsonl", session / "sess-1" / "subagents" / "agent-a1.jsonl")
        self.cli = load_cli()

    def test_extract_masked_dedup_newest_first(self):
        rows = cmd_jev.extract(self.root, days=60, now=NOW)
        self.assertEqual([(r["kind"], r["text"]) for r in rows], [
            ("prompt", "continue with the plan"),
            ("prompt", "mail [REDACTED] about the deploy"),
            ("prompt", "Please build the checkout page with tests"),
            ("prompt", "status?"),
            ("report", "Checkout done; key [REDACTED] rotated"),
        ])
        self.assertEqual(rows[2]["at"], "2026-10-15T01:00:00+00:00")  # newest duplicate kept
        self.assertTrue(all(r["source"] == "sess-1.jsonl" for r in rows))
        self.assertEqual(len({r["id"] for r in rows}), 5)
        self.assertEqual(set(rows[0]), {"id", "kind", "text", "at", "source"})
        self.assertEqual(cmd_jev.extract(self.root, days=60, n_prompts=2, n_reports=0, now=NOW)[1]["text"],
                         "mail [REDACTED] about the deploy")

    def test_extract_strips_code(self):
        line = {"type": "user", "uuid": "c1", "timestamp": "2026-10-15T09:00:00Z",
                "message": {"role": "user", "content": CODE_TEXT}}
        path = self.root / "-srv-orca-acme-shop" / "sess-2.jsonl"
        path.write_text(json.dumps(line) + "\n")
        rows = cmd_jev.extract(self.root, days=60, now=NOW)
        text = json.dumps(rows)
        for canary in CODE_CANARIES:
            self.assertNotIn(canary, text)
        self.assertIn("[code omitted]", rows[0]["text"])

    def test_extract_main_slugs_only(self):
        reg = (support.FIXTURES / "hooks" / "registry.json").read_text().replace("@ROOT@", "/srv/orca")
        (self.home / "registry.json").write_text(reg)
        main = self.root / "-srv-orca-acme"
        wt = self.root / "-srv-orca-acme-build"
        main.mkdir()
        wt.mkdir()
        install_fixture(main / "s.jsonl")
        line = {"type": "user", "uuid": "w1", "timestamp": "2026-10-15T09:00:00Z",
                "message": {"role": "user", "content": "worktree prompt"}}
        (wt / "w.jsonl").write_text(json.dumps(line) + "\n")
        from orche import registry
        slugs = cmd_jev.main_slugs(registry.load())
        self.assertIn("-srv-orca-acme", slugs)
        self.assertNotIn("-srv-orca-acme-build", slugs)
        rows = cmd_jev.extract(self.root, days=60, now=NOW, slugs=slugs)
        self.assertEqual(len(rows), 5)
        self.assertNotIn("worktree prompt", json.dumps(rows))
        self.assertTrue(all(r["source"] == "s.jsonl" for r in rows))  # -srv-orca-acme-shop excluded
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(["jev", "eval", "extract", "--days", "100000", "--root", str(self.root),
                                  "--project", "acme"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue(), "")
        self.assertIn("wrote 6 samples", out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli.main(["jev", "eval", "extract", "--project", "nope"]), 1)

    def test_extract_cli_writes_samples(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(["jev", "eval", "extract", "--days", "100000", "--root", str(self.root)])
        self.assertEqual(code, 0, err.getvalue())
        rows = [json.loads(line) for line in (self.home / "jev-eval" / "samples.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 6)  # + the August prompt inside the wide window
        self.assertNotIn(FIXTURE_KEY[:12], (self.home / "jev-eval" / "samples.jsonl").read_text())
        self.assertNotIn("Implement the checkout form", (self.home / "jev-eval" / "samples.jsonl").read_text())
        self.assertIn("wrote 6 samples (5 prompts, 1 reports)", out.getvalue())
        self.assertIn("warning: registry has no projects", err.getvalue())
        mode = stat.S_IMODE((self.home / "jev-eval" / "samples.jsonl").stat().st_mode)
        self.assertEqual(mode, 0o600)


class CliTest(JevCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_jsonl(self, name, rows):
        path = self.home / "jev-eval" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_run_unconfigured_exits_1_disabled_runs(self):
        self.write_jsonl("samples.jsonl", [{"id": "s1", "kind": "report", "text": "hi"}])
        self.configure(enabled=False, base_url="")
        code, _out, err = self.orche("jev", "eval", "run")
        self.assertEqual(code, 1)
        self.assertIn("jev.base_url", err)
        self.assertIn("JEV_API_KEY", err)
        self.assertFalse((self.home / "jev-eval" / "results.jsonl").exists())
        self.configure(enabled=False)
        jev.OPENER = FakeOpener(answer_all(default_value))
        code, _out, err = self.orche("jev", "eval", "run")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.home / "jev-eval" / "results.jsonl").exists())

    def test_run_writes_results(self):
        self.configure()
        jev.OPENER = FakeOpener(answer_all(default_value))
        self.write_jsonl("samples.jsonl", [{"id": "s1", "kind": "prompt", "text": "hi"},
                                           {"id": "r1", "kind": "report", "text": "done"}])
        code, out, err = self.orche("jev", "eval", "run")
        self.assertEqual(code, 0, err)
        rows = [json.loads(x) for x in (self.home / "jev-eval" / "results.jsonl").read_text().splitlines()]
        self.assertEqual([(r["id"], r["placement"]) for r in rows],
                         [("s1", "drain"), ("s1", "routine"), ("s1", "intake"), ("r1", "drain")])
        self.assertEqual(rows[0]["answer"], True)
        self.assertEqual(rows[1]["answer"], "status")
        self.assertEqual(rows[2]["answer"], {"needs_plan": True, "irreversible": True, "urgency": 2})
        self.assertEqual(set(rows[0]), {"id", "placement", "model", "answer", "confidence", "ms"})
        self.assertEqual(rows[0]["model"], "jev-1.13.0")
        mode = stat.S_IMODE((self.home / "jev-eval" / "results.jsonl").stat().st_mode)
        self.assertEqual(mode, 0o600)
        code, _out, _err = self.orche("jev", "eval", "run", "--placement", "routine")
        rows = (self.home / "jev-eval" / "results.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 1)

    def test_report_accuracy_calibration_thresholds(self):
        results, labels = [], []
        # drain: 10 rows at 0.95 all right, 10 at 0.75 with 8 right, 10 at 0.35 with 3 right.
        for conf, right in ((0.95, 10), (0.75, 8), (0.35, 3)):
            for i in range(10):
                rid = f"d{conf}-{i}"
                results.append({"id": rid, "placement": "drain", "answer": True, "confidence": conf, "ms": 100})
                labels.append({"id": rid, "placement": "drain", "label": i < right})
        results.append({"id": "x", "placement": "routine", "answer": "status", "confidence": 0.9, "ms": 80})
        labels.append({"id": "x", "placement": "routine", "label": "status"})
        results.append({"id": "y", "placement": "intake", "answer": {"needs_plan": True, "urgency": 2},
                        "confidence": 0.6, "ms": 90})
        labels.append({"id": "y", "placement": "intake", "label": {"needs_plan": True}})
        results.append({"id": "z", "placement": "drain", "answer": None, "confidence": None, "ms": 1000})
        labels.append({"id": "z", "placement": "drain", "label": True})
        results.append({"id": "nolabel", "placement": "drain", "answer": True, "confidence": 0.9, "ms": 1})
        for r in results:
            r["model"] = "jev-1.13.0"
        results.append({"id": "x", "placement": "routine", "model": "jev-1.14.0", "answer": "confirm",
                        "confidence": 0.8, "ms": 70})
        self.write_jsonl("results.jsonl", results)
        self.write_jsonl("labels.jsonl", labels)
        code, out, err = self.orche("--json", "jev", "eval", "report")
        self.assertEqual(code, 0, err)
        models = json.loads(out)["models"]
        self.assertEqual(sorted(models), ["jev-1.13.0", "jev-1.14.0"])
        self.assertEqual(models["jev-1.14.0"]["routine"]["accuracy"], 0.0)
        self.assertEqual(list(models["jev-1.14.0"]), ["routine"])
        rep = {"placements": models["jev-1.13.0"]}
        d = rep["placements"]["drain"]
        self.assertEqual((d["n"], d["unanswered"], d["unlabeled"]), (30, 1, 1))
        self.assertEqual(d["accuracy"], 0.7)
        bins = {b["bin"]: (b["n"], b["accuracy"]) for b in d["calibration"]}
        self.assertEqual(bins["0.9-1.0"], (10, 1.0))
        self.assertEqual(bins["0.7-0.8"], (10, 0.8))
        self.assertEqual(bins["0.3-0.4"], (10, 0.3))
        self.assertEqual(bins["0.0-0.1"], (0, None))
        # cumulative: >=0.4 -> 18/20 = 0.9 (act); >=0.0 -> 0.7, so confirm also starts at 0.4
        self.assertEqual(d["thresholds"], {"act": 0.4, "confirm": 0.4})
        self.assertEqual(rep["placements"]["routine"]["accuracy"], 1.0)
        self.assertEqual(rep["placements"]["intake"]["accuracy"], 1.0)
        code, out, _err = self.orche("jev", "eval", "report")
        self.assertIn("model jev-1.13.0", out)
        self.assertIn("model jev-1.14.0", out)
        self.assertIn("drain: n 30 accuracy 0.700", out)
        self.assertIn("drain: suggested act 0.4 confirm 0.4", out)

    def test_threshold_none_when_never_reached(self):
        self.assertIsNone(cmd_jev._threshold([(0.95, False), (0.5, True)], 0.9))
        self.assertEqual(cmd_jev._threshold([(0.95, True), (0.5, False)], 0.9), 0.6)

    def test_label_not_implemented(self):
        code, out, _err = self.orche("jev", "eval", "label")
        self.assertEqual(code, 0)
        self.assertIn("labels.jsonl", out)
        self.assertIn("NOT produced", out)

    def test_enable_disable_and_dry_run(self):
        cfg = self.home / "config.json"
        code, out, _err = self.orche("--dry-run", "jev", "enable")
        self.assertEqual(code, 0)
        self.assertIn("would set jev.enabled=true", out)
        self.assertFalse(cfg.exists())
        self.assertEqual(self.orche("jev", "enable")[0], 0)
        self.assertTrue(json.loads(cfg.read_text())["jev"]["enabled"])
        self.orche("jev", "disable", "--dry-run")
        self.assertTrue(json.loads(cfg.read_text())["jev"]["enabled"])
        self.assertEqual(self.orche("jev", "disable")[0], 0)
        self.assertFalse(json.loads(cfg.read_text())["jev"]["enabled"])

    def test_default_config_disabled_with_key_env(self):
        from orche import config
        self.assertFalse(config.load()["jev"]["enabled"])
        self.assertEqual(config.load()["jev"]["api_key_env"], "JEV_API_KEY")


if __name__ == "__main__":
    import unittest
    unittest.main()
