"""vendors: the model-string grammar, the vendor of a model, the recommendation table (design 2026-10-23 §2, §4.1)."""
import json
import unittest

import support
from orche import config, vendors

UNKNOWN = ("unknown vendor 'openai' — Claude ids are plain (claude-opus-5-5), "
           "Codex is `codex` or `codex:<model id>`")


class GrammarTest(unittest.TestCase):
    def test_model_strings(self):
        for ok in ("claude-opus-5-5", "sonnet", "codex", "codex:gpt-6.1-sol"):
            self.assertEqual(vendors.errors(ok), [], ok)
        self.assertEqual(vendors.errors("openai:x"), [UNKNOWN])
        self.assertEqual(vendors.errors("codex:"),
                         ["codex:<model id> needs a model id after `codex:` (or plain `codex` for Codex's default)"])
        self.assertEqual(vendors.errors("gpt-4"),
                         ["unknown model 'gpt-4' — Claude ids start with claude- (claude-opus-5-5) or are an alias "
                          f"({', '.join(vendors.ALIASES)}), Codex is `codex` or `codex:<model id>`"])
        for bad in ("a b", "", None, 3, "x\u200by"):
            self.assertEqual(vendors.errors(bad), ["must be a model id without spaces or control characters"], bad)

    def test_vendor_and_codex_id(self):
        self.assertEqual([vendors.vendor_of(m) for m in ("claude-opus-5-5", "sonnet", "codex", "codex:gpt-6.1-sol",
                                                         "codexx", None)],
                         ["claude", "claude", "codex", "codex", "claude", "claude"])
        self.assertEqual([vendors.codex_id(m) for m in ("codex", "codex:gpt-6.1-sol", "sonnet")],
                         ["", "gpt-6.1-sol", ""])
        self.assertEqual(vendors.label("codex"), "codex (Codex's default model)")
        self.assertEqual(vendors.label("sonnet"), "sonnet")


class RecommendTest(support.TempHomeCase):
    TABLE = {"rnd": ("claude-opus-5-5", "high"), "research": ("claude-sonnet-5-5", "medium"),
             "build": ("claude-opus-5-5", "medium"), "gtm": ("claude-sonnet-5-5", "medium"),
             "ops": ("claude-sonnet-5-5", "medium"), "design": ("claude-opus-5-5", "high"),
             "content": ("claude-sonnet-5-5", "medium"), "custom": ("claude-opus-5-5", "high")}

    def test_every_row_a_custom_name_and_the_fallback(self):
        for name, (model, effort) in self.TABLE.items():
            rec = vendors.recommend(config.load(), name)
            self.assertEqual((rec.model, rec.effort), (model, effort), name)
            self.assertTrue(rec.why)
        self.assertEqual(vendors.recommend(None, "billing")[:2], ("claude-opus-5-5", "high"))   # custom row
        self.assertEqual(vendors.recommend({}, "build")[:2], ("claude-opus-5-5", "high"))       # no table at all

    def test_config_overrides_one_entry(self):
        (self.home / "config.json").write_text(json.dumps(dict(
            support.NO_WAKE, kinds={"build": {"model": "codex", "effort": "high", "why": "second vendor"}})))
        cfg = config.load()
        self.assertEqual(vendors.recommend(cfg, "build"), vendors.Choice("codex", "high", "second vendor"))
        self.assertEqual(vendors.recommend(cfg, "gtm")[:2], ("claude-sonnet-5-5", "medium"))   # the rest kept
        merged = config._merge(config.DEFAULTS, {"kinds": {"rnd": {"model": "sonnet", "effort": "low"}}})
        self.assertEqual(vendors.recommend(merged, "rnd")[:2], ("sonnet", "low"))
        self.assertEqual(vendors.recommend(merged, "research")[:2], ("claude-sonnet-5-5", "medium"))


if __name__ == "__main__":
    unittest.main()


class AvailableEnvTest(support.TempHomeCase):
    def test_version_probe_sees_no_claude_or_anthropic_names(self):
        import os
        from unittest import mock
        from orche import jevkey, oauth
        jevkey.set_key("stored-canary", account=oauth.ENV)
        found = support.fake_codex(self.home / "fake", {})
        canaries = {oauth.ENV: "inherited-canary", "ANTHROPIC_API_KEY": "canary-a",
                    "ANTHROPIC_AUTH_TOKEN": "canary-b", "ANTHROPIC_BASE_URL": "http://canary"}
        with mock.patch.dict(os.environ, canaries):
            self.assertTrue(vendors.available({"models": {"codex_bin": str(found)}}).ok)
        names = json.loads((self.home / "fake" / "version_env.json").read_text())
        self.assertEqual([n for n in names if "CLAUDE" in n or "ANTHROPIC" in n], [])
        self.assertIn("PATH", names)
