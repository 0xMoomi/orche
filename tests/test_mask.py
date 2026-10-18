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


JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpM"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
# The intake review's masking table (docs/internal/sdd/…/intake-review.md B(3)) plus the shapes the
# intake fix round added: input -> exact output. Rows marked "passes" in the review now mask.
REVIEW_TABLE = [
    ("sk-" + "ant-api03-" + "A" * 30, "[REDACTED]"),
    ("sk-" + "proj-" + "B" * 30, "[REDACTED]"),
    ("AKIA" + "IOSFODNN7EXAMPLE", "[REDACTED]"),
    ("aws_secret_access_key=" + AWS_SECRET, "aws_secret_access_key=[REDACTED]"),
    ("AWS_SECRET_ACCESS_KEY: " + AWS_SECRET, "AWS_SECRET_ACCESS_KEY: [REDACTED]"),
    ("password=hunter2", "password=[REDACTED]"),
    ("DB_PASSWORD=hunter2", "DB_PASSWORD=[REDACTED]"),
    ('{"passwd": "hunter2"}', '{"passwd": "[REDACTED]"}'),
    ("spring.datasource.password=abc", "spring.datasource.password=[REDACTED]"),
    (JWT, "[REDACTED]"),
    ("SUPABASE_ANON_KEY=" + JWT, "SUPABASE_ANON_KEY=[REDACTED]"),
    ("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "[REDACTED]"),
    ("ghp" + "_" + "a" * 36, "[REDACTED]"),
    ("github_pat_" + "A1" * 15, "[REDACTED]"),
    ("xox" + "b-" + "1" * 24, "[REDACTED]"),
    ("postgres://admin:" + "s3cret@db.example.com:5432/app", "postgres://admin:[REDACTED]@db.example.com:5432/app"),
    ("sk_" + "live_" + "a" * 24, "[REDACTED]"),
    ("sk_" + "test_" + "a" * 24, "[REDACTED]"),
    ("rk_" + "live_" + "b" * 24, "[REDACTED]"),
    ("AIza" + "S" * 35, "[REDACTED]"),
    ("api_key: " + "0123456789abcdef" * 2, "api_key: [REDACTED]"),
    ("x-api-key: abc123", "x-api-key: [REDACTED]"),
    ("GITHUB_TOKEN=ghp" + "_" + "b" * 36, "GITHUB_TOKEN=[REDACTED]"),
    ("secret: x\nnext line", "secret: [REDACTED]\nnext line"),
    ("Authorization: Bearer abc.def-123", "Authorization: Bearer [REDACTED]"),
    ('curl -H "Authorization: Bearer xyz"', 'curl -H "Authorization: Bearer [REDACTED]"'),
    ("Bearer " + "Ab1." * 6, "Bearer [REDACTED]"),
    ("someone" + "@" + "example.com", "[REDACTED]"),
]
# Text that must come back unchanged.
NOT_SECRETS = [
    "RESEND_API_KEY",                                   # an env variable NAME (intake lists names only)
    "see `.env.example` for API_KEY and DB_PASSWORD",
    "pk_" + "live_" + "c" * 24,                         # Stripe publishable key: public by design
    "commit 0aed3d6 and 894fc62d5b0e9a3c1f2e7d4b6a8c0e1f3a5b7d9c",
    "src/auth/token.ts", "src/auth/token.ts: handles login", "lib/secret_store.py: y", "/bin/pwd: z",
    "lib/orche/secrets/store.py", "tests/test_password_reset.py",
    "tokens: 1234", "max_tokens=100", "input_tokens: 5", "cwd=/tmp",
    "the password reset page", "rotate the API key yearly", "a token is passed in the header",
    "Bearer authentication is used", "Authorization header required",
    "http://localhost:8080/a@b", "https://github.com/acme/shop/pull/12",
    "npm run build && npm test", "`make test`; `make run`",
]


class ReviewTableTest(unittest.TestCase):
    def test_review_table(self):
        for text, want in REVIEW_TABLE:
            with self.subTest(text=text):
                self.assertEqual(mask.redact(text)[0], want)
                self.assertTrue(mask.has_secret(text))

    def test_false_positives(self):
        for text in NOT_SECRETS:
            with self.subTest(text=text):
                self.assertEqual(mask.redact(text), (text, 0))

    def test_key_word_kept_value_masked_once(self):
        out, counts = mask.redact_counts("export GITHUB_TOKEN=ghp" + "_" + "b" * 36 + " DB_PASSWORD=x")
        self.assertEqual(out, "export GITHUB_TOKEN=[REDACTED] DB_PASSWORD=[REDACTED]")
        self.assertEqual(counts, {"github_token": 1, "secret_assignment": 1})

    def test_every_rule_takes_a_replacement(self):
        """secrets/share.py walks PATTERNS itself: every rule answers subn(repl, text)."""
        for name, rule in mask.PATTERNS:
            if name != "mnemonic":
                self.assertEqual(rule.subn("<x>", "plain text"), ("plain text", 0), name)
        from orche.secrets import share
        self.assertEqual(share.redact_counts("DB_PASSWORD=hunter2"),
                         ("DB_PASSWORD=[REDACTED]", {"secret_assignment": 1}))


if __name__ == "__main__":
    unittest.main()
