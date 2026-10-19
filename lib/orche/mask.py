"""The single secret-masking library. Everything leaving the machine passes `redact()`.

PATTERNS is `[(name, rule)]`; every rule has `subn(repl, text)`: a compiled regex replaces the
whole match, a `_Keep` rule keeps its group 1 (a key word such as `DB_PASSWORD=`, or the
`scheme://user:` of a URL) and replaces only the value after it. Order matters: specific token
shapes go first, so the `<secret word> = <value>` rules then see `[REDACTED]` and leave it alone.
Those rules are two: config syntax (`KEY=value`, `"key": value`, `KEY: value`) masks any value that
is not a reference; prose (`the token: …`) masks only a credential-like value (6+ characters with a
digit, a symbol or a lower-then-upper case change) or a quoted one. Known gap: a letters-only
password written as prose (`password: correcthorse`) is not masked. Git shas, ordinary words and
file paths never match (tests/test_mask.py pins both sides).
"""
import re

from .bip39 import WORDSET as _BIP39

REDACTED = "[REDACTED]"
MNEMONIC_MIN_WORDS = 12

_NB = r"(?<![A-Za-z0-9])"  # left boundary for prefix-style tokens
_NUM = r"(?:\d{1,2}[.)]\s*)?"  # optional list numbering such as "1." or "1)"
_WORD = r"[a-z]{3,8}"
# `<key>` + optional closing quote + `:`/`=` (same line) + optional opening quote.
_ASSIGN = r"""["']?[ \t]*[:=][ \t]*["']?"""
_SECRET_WORDS = r"(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|bearer)"
# A key holding a secret word as a whole `_`/`-`/`.` separated part (`tokens: 3`, `max_tokens=8` stay),
# never part of a path or a file name (`src/token.ts: …` stays). Group 1 opens here.
_KEY_START = r"(?<![A-Za-z0-9_./\\-])("
_SECRET_KEY = r"(?:[A-Za-z0-9]+[_.-])*" + _SECRET_WORDS + r"(?:[_-][A-Za-z0-9]+)*"
# Config-style keys that may take `:` (YAML): an UPPER_SNAKE env name, or a dotted config key.
_UPPER_KEY = (r"(?-i:(?:[A-Z0-9]+[_-])*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API[_-]?KEY|AUTHORIZATION|BEARER)"
              r"(?:[_-][A-Z0-9]+)*)")
_DOTTED_KEY = r"(?:[A-Za-z0-9_-]+\.)+" + _SECRET_WORDS + r"(?:[_-][A-Za-z0-9]+)*"
_VALUE = r"[^\s\"'`,;]+"
# Never masked: an already-masked value, a header's Bearer/Basic, a reference (`$X`, `${X}`, `<x>`,
# `%(x)s`, `{{ x }}`, `process.env.X`, `os.environ…`, `env.X`, `secrets.X`), an ENV_VAR_NAME,
# a path (`/…`, `./…`, `~/…`) or a plain number.
_SKIP = (r"(?!\[REDACTED\])(?!(?:bearer|basic)[ \t])"
         r"(?!\$|<|%\(|\{\{|process\.env\b|os\.environ\b|env\.|secrets\.)"
         r"(?!(?-i:[A-Z][A-Z0-9_]*)(?![^\s\"'`,;]))"
         r"(?!(?:~|\.{1,2})?/)"
         r"(?!\d+(?![^\s\"'`,;]))")
# A prose value must look like a credential: 6+ characters with a digit, a symbol or mixed case
# (a lower-case letter followed by an upper-case one), unless it is quoted.
_C = r"[^\s\"'`,;]"
_SHAPED = (r"(?=" + _C + r"{6})(?:(?=" + _C + r"*[0-9!@#$%^&*+=~?])"
           r"|(?=" + _C + r"*(?-i:[a-z][A-Z])))")


class _Keep:
    """A rule that masks only what follows its group 1."""

    def __init__(self, pattern):
        self.rx = re.compile(pattern)

    def subn(self, repl, text):
        return self.rx.subn(lambda m: m.group(1) + repl, text)


PATTERNS = [
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    # scheme://user:password@host -> the password only.
    ("url_credentials", _Keep(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:)[^/\s@]+(?=@)")),
    ("authorization_header", _Keep(r"(?i)(authorization" + _ASSIGN + r"(?:bearer|basic|token)[ \t]+)[^\s\"']+")),
    ("bearer_token", _Keep(r"(?i)" + _NB + r"(bearer[ \t]+)[A-Za-z0-9._~+/=-]{16,}")),
    ("anthropic_key", re.compile(_NB + r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(_NB + r"sk-[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(_NB + r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("aws_access_key", re.compile(_NB + r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("aws_secret_key", _Keep(r"(?i)(aws_secret_access_key" + _ASSIGN + r")[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("github_token", re.compile(_NB + r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(_NB + r"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("google_api_key", re.compile(_NB + r"AIza[0-9A-Za-z_-]{35}")),
    ("jwt", re.compile(_NB + r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("hex_private_key", re.compile(r"0x[0-9a-fA-F]{64}")),
    ("rpc_url", re.compile(r"https://[^/\s]*(?:alchemy|infura|quicknode)[^\s]*")),
    # Keep the key, mask the value (same line, not already masked, not a reference, see _SKIP).
    # Config syntax masks any value: `key=value` (.env, TOML, shell), a quoted `"key": value` (JSON),
    # and `KEY: value` / `db.password: value` (YAML with an env-style or dotted key).
    ("secret_assignment", _Keep(
        r"(?i)" + _KEY_START + r"(?:" + _SECRET_KEY + r"""(?:["']?[ \t]*=|["'][ \t]*:)"""
        + r"|" + _UPPER_KEY + r"[ \t]*:|" + _DOTTED_KEY + r"[ \t]*:)"
        + r"""[ \t]*["']?)""" + _SKIP + _VALUE)),
    # Prose (`token: …`, `the secret: …`) masks only a credential-like value (_SHAPED) or a quoted
    # one, so `secret: none`, `token: GITHUB_TOKEN`, `pwd: /path` stay readable. Accepted miss: a
    # letters-only password written as prose (`password: correcthorse`).
    ("secret_prose", _Keep(
        r"(?i)" + _KEY_START + _SECRET_KEY + r"""[ \t]*:[ \t]*["']?)"""
        + r"""(?:(?<=["'])""" + _SKIP + _VALUE + r"""(?=["'])|(?<!["'])""" + _SKIP + _SHAPED + _VALUE + r")")),
    # Candidate run: >= 12 lowercase 3-8 letter words separated by whitespace/newlines,
    # optionally numbered. Only BIP39 sub-runs of >= 12 words are redacted (see _redact_mnemonics).
    ("mnemonic", re.compile(
        r"(?<![A-Za-z0-9])" + _NUM + _WORD
        + r"(?:\s+" + _NUM + _WORD + r"){%d,}(?![A-Za-z0-9])" % (MNEMONIC_MIN_WORDS - 1))),
    # Bounded local part / labels keep this linear on long @-less runs.
    ("email", re.compile(r"[A-Za-z0-9._%+-]{1,64}@"
                         r"(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}(?![A-Za-z])")),
]

_WORD_RX = re.compile(r"(?<![A-Za-z])" + _WORD + r"(?![A-Za-z])")


def _bip39_spans(run: str):
    """(start, end) spans of runs of >= 12 consecutive BIP39 words inside `run`."""
    spans, streak = [], []
    for m in list(_WORD_RX.finditer(run)) + [None]:
        if m is not None and m.group(0) in _BIP39:
            streak.append(m)
            continue
        if len(streak) >= MNEMONIC_MIN_WORDS:
            spans.append((streak[0].start(), streak[-1].end()))
        streak = []
    return spans


def _redact_mnemonics(rx, text):
    count = 0

    def sub(match):
        nonlocal count
        run = match.group(0)
        for start, end in reversed(_bip39_spans(run)):
            run = run[:start] + REDACTED + run[end:]
            count += 1
        return run

    return rx.sub(sub, text), count


def redact_counts(text: str):
    """(redacted_text, {pattern name: replacements})."""
    counts = {}
    if not text:
        return text, counts
    for name, rule in PATTERNS:
        if name == "mnemonic":
            text, n = _redact_mnemonics(rule, text)
        else:
            text, n = rule.subn(REDACTED, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts


def redact(text: str):
    """Return (redacted_text, number_of_replacements)."""
    text, counts = redact_counts(text)
    return text, sum(counts.values())


def has_secret(text: str) -> bool:
    return bool(text) and redact(text)[1] > 0
