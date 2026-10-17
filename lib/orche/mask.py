"""The single secret-masking library. Everything leaving the machine passes `redact()`."""
import re

from .bip39 import WORDSET as _BIP39

REDACTED = "[REDACTED]"
MNEMONIC_MIN_WORDS = 12

_NB = r"(?<![A-Za-z0-9])"  # left boundary for prefix-style tokens
_NUM = r"(?:\d{1,2}[.)]\s*)?"  # optional list numbering such as "1." or "1)"
_WORD = r"[a-z]{3,8}"

PATTERNS = [
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("openai_key", re.compile(_NB + r"sk-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(_NB + r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(_NB + r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack_token", re.compile(_NB + r"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("google_api_key", re.compile(_NB + r"AIza[0-9A-Za-z_-]{35}")),
    ("hex_private_key", re.compile(r"0x[0-9a-fA-F]{64}")),
    ("rpc_url", re.compile(r"https://[^/\s]*(?:alchemy|infura|quicknode)[^\s]*")),
    # Candidate run: >= 12 lowercase 3-8 letter words separated by whitespace/newlines,
    # optionally numbered. Only BIP39 sub-runs of >= 12 words are redacted (see _mnemonic_sub).
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


def redact(text: str):
    """Return (redacted_text, number_of_replacements)."""
    if not text:
        return text, 0
    total = 0
    for name, rx in PATTERNS:
        if name == "mnemonic":
            text, n = _redact_mnemonics(rx, text)
        else:
            text, n = rx.subn(REDACTED, text)
        total += n
    return text, total


def has_secret(text: str) -> bool:
    return bool(text) and redact(text)[1] > 0
