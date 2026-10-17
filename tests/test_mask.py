import unittest

import support  # noqa: F401
from orche import mask

# Secret-shaped samples are assembled at runtime so the repository itself never
# contains a literal that secret scanners would flag.
HEX64 = "ab" * 32
WORDS12 = " ".join(["abandon ability able", "about above absent", "absorb abstract absurd", "abuse access accident"])
WORDS24 = WORDS12 + " " + "account accuse achieve acid acoustic acquire across action actor actress actual adapt"

SAMPLES = {
    "openai_key": "sk-" + "proj_" + "A1b2C3d4E5f6G7h8I9j0K",
    "aws_access_key": "AKIA" + "ABCDEFGHIJ234567",
    "github_token": "ghp" + "_" + "a" * 36,
    "slack_token": "xox" + "b-" + "123456789012-abcdefghij",
    "private_key_block": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "google_api_key": "AIza" + "S" * 35,
    "hex_private_key": "0x" + HEX64,
    "rpc_url": "https://eth-mainnet.g." + "alchemy.com/v2/abcdef123456",
    "mnemonic_12": WORDS12,
    "mnemonic_24": WORDS24,
    "email": "someone" + "@" + "example.com",
}


class RedactPerClassTest(unittest.TestCase):
    def assertRedacted(self, secret):
        text = f"before {secret} after"
        out, count = mask.redact(text)
        self.assertGreaterEqual(count, 1, secret)
        self.assertNotIn(secret, out)
        self.assertIn(mask.REDACTED, out)
        self.assertTrue(out.startswith("before ") and out.endswith(" after"), out)
        self.assertTrue(mask.has_secret(text))

    def test_openai_key(self):
        self.assertRedacted(SAMPLES["openai_key"])

    def test_aws_access_key(self):
        self.assertRedacted(SAMPLES["aws_access_key"])

    def test_github_token(self):
        self.assertRedacted(SAMPLES["github_token"])

    def test_slack_token(self):
        self.assertRedacted(SAMPLES["slack_token"])

    def test_private_key_block(self):
        self.assertRedacted(SAMPLES["private_key_block"])

    def test_private_key_body_is_removed(self):
        body = "MIIEow" + "IBAAKCAQEA" + "x" * 40
        block = ("-----BEGIN " + "RSA PRIVATE KEY-----\n" + body + "\n" + body[::-1]
                 + "\n-----END RSA PRIVATE KEY-----")
        out, count = mask.redact(f"key:\n{block}\ntrailing text")
        self.assertEqual((out, count), (f"key:\n{mask.REDACTED}\ntrailing text", 1))

    def test_unterminated_private_key_block_redacts_to_end(self):
        out, _ = mask.redact("x\n-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADAN\nmore")
        self.assertEqual(out, f"x\n{mask.REDACTED}")

    def test_google_api_key(self):
        self.assertRedacted(SAMPLES["google_api_key"])

    def test_hex_private_key(self):
        self.assertRedacted(SAMPLES["hex_private_key"])

    def test_rpc_url(self):
        self.assertRedacted(SAMPLES["rpc_url"])
        self.assertRedacted("https://mainnet." + "infura.io/v3/" + "f" * 32)
        self.assertRedacted("https://abc.quicknode" + ".com/key123")

    def test_mnemonic_12_and_24(self):
        out, count = mask.redact(f"seed: {WORDS12}")
        self.assertEqual((out, count), (f"seed: {mask.REDACTED}", 1))
        out, count = mask.redact(f"seed: {WORDS24}")
        self.assertEqual((out, count), (f"seed: {mask.REDACTED}", 1))

    def test_mnemonic_inside_prose(self):
        out, count = mask.redact(f"my seed phrase is {WORDS12}, keep it safe")
        self.assertEqual((out, count), (f"my seed phrase is {mask.REDACTED}, keep it safe", 1))

    def test_mnemonic_one_word_per_line(self):
        text = "words:\n" + "\n".join(WORDS12.split()) + "\nDone."
        self.assertEqual(mask.redact(text), (f"words:\n{mask.REDACTED}\nDone.", 1))

    def test_mnemonic_numbered(self):
        dotted = "\n".join(f"{i}. {w}" for i, w in enumerate(WORDS24.split(), 1))
        out, count = mask.redact(dotted)
        self.assertEqual((out, count), (f"1. {mask.REDACTED}", 1))
        paren = " ".join(f"{i}) {w}" for i, w in enumerate(WORDS12.split(), 1))
        self.assertEqual(mask.redact(paren), (f"1) {mask.REDACTED}", 1))

    def test_email(self):
        self.assertRedacted(SAMPLES["email"])


class LinearTimeTest(unittest.TestCase):
    def test_long_run_without_at_is_fast(self):
        import time
        started = time.monotonic()
        self.assertEqual(mask.redact("x" * 100_000), ("x" * 100_000, 0))
        self.assertLess(time.monotonic() - started, 0.2)


class LeavesOrdinaryTextTest(unittest.TestCase):
    def assertUntouched(self, text):
        self.assertEqual(mask.redact(text), (text, 0))
        self.assertFalse(mask.has_secret(text))

    def test_eth_address_is_not_redacted(self):
        self.assertUntouched("send to 0x" + "a1" * 20 + " now")

    def test_ordinary_code(self):
        self.assertUntouched(
            "def run(argv, *, timeout=20):\n"
            "    task = os.environ.get('ORCHE_HOME')  # sk- prefix alone is fine\n"
            "    return https_get('https://example.com/api/v1')\n"
        )

    def test_short_prose(self):
        self.assertUntouched("the quick brown fox jumps over the lazy dog")

    def test_long_ordinary_sentence(self):
        sentence = ("the team will review each pull request before we merge into main "
                    "and then deploy")
        self.assertEqual(len(sentence.split()), 15)
        self.assertUntouched(sentence)

    def test_eleven_bip39_words(self):
        self.assertUntouched(" ".join(WORDS12.split()[:11]))

    def test_prefix_tokens_need_a_left_boundary(self):
        for text in ("feat/disk-usage-monitoring-dashboard", "risk-assessment-plan", "task-123",
                     "risk-assessment-and-mitigation-plan", "tusk-" + "a" * 30,
                     "xAKIA" + "ABCDEFGHIJ234567", "bigh" + "p_" + "a" * 36, "maxox" + "b-" + "1" * 24,
                     "xAIza" + "S" * 35):
            self.assertUntouched(text)

    def test_empty(self):
        self.assertEqual(mask.redact(""), ("", 0))
        self.assertFalse(mask.has_secret(""))


if __name__ == "__main__":
    unittest.main()
