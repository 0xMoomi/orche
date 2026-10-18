#!/usr/bin/env python3
"""PreToolUse gate: deny direct reads of secrets (.env files, env dumps, secret managers) and of the
stored Jev key (keychain value lookups, ORCHE_HOME/secrets/jev.key, orche's key API, keys fed to
orche's stdin flags; the rules and their known gaps are in `lib/orche/jevgate.py`).

Denies only on a positive match; prints nothing otherwise (no auto-allow). Internal error -> allow +
log, except that the Jev rules refuse a command they cannot finish checking ("too complex").
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

# This gate is self-contained stdlib code that also runs on old interpreters, so an old Python
# is logged but the gate keeps denying positive matches (stronger than allowing everything).
pyguard.check_hook("env_read_gate")

from orche import jevgate  # noqa: E402  (stdlib only, old-interpreter safe like this file)

REASON = "[orche] secrets are read by 'orche secret run', not by agents. Use 'orche secret ls' for names."
ENV_FILE = re.compile(r"^\.env(\..+)?$", re.IGNORECASE)
ENV_GLOB = re.compile(r"^\.env([.*].*)?$", re.IGNORECASE)
# Only the exact basename `.env.example` is exempt (not `.env.example.local`).
EXAMPLE = re.compile(r"\.env\.example(?![\w.-])", re.IGNORECASE)
# Upper-case, secret-shaped variable names only (`$API_KEY`, `${GITHUB_TOKEN}`; not `$key`, `$monkey`).
SECRET_VAR = (r"\$\{?(?:[A-Z][A-Z0-9_]*)?(KEY|TOKEN|SECRET|PASSWORD|PRIVATE|MNEMONIC|SEED)[A-Z0-9_]*"
              r"(?![A-Za-z0-9_])\}?")
BASH_DENY = (
    re.compile(r"(?<![\w.-])(cat|less|more|head|tail|grep|rg|sed|awk|source|\.)(\s+|\s*<\s*)[^|;&]*"
               r"\.env(\.[a-z]+)?\b", re.IGNORECASE),
    re.compile(r"\bprintenv\b"),
    re.compile(r"gcloud\s+secrets\s+versions\s+access"),
    re.compile(r"\b(?i:echo|printf)\b[^|;&]*" + SECRET_VAR),
)
JEV_REASON = jevgate.REASON
# `env` at a command position (`vercel env pull` is not one).
ENV_CMD = re.compile(r"(?:^|[|;&(]|&&|\|\|)\s*env(?![\w.-])(?P<rest>[^|;&)\n]*)", re.MULTILINE)


def _env_dump(command) -> bool:
    """True when an `env` invocation prints the environment: no command after its options.

    `env FOO=1 cmd`, `env cmd`, `env -u FOO cmd` run a command and are allowed; bare `env`,
    `env | …`, `env > f`, `env -0`, `env -u FOO` dump it.
    """
    for m in ENV_CMD.finditer(command):
        tokens = m.group("rest").split()
        i, runs = 0, False
        while i < len(tokens):
            tok = tokens[i]
            if tok[0] in "<>" or tok[:2] in ("1>", "2>", "&>"):
                break
            if tok in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                i += 2
                continue
            if tok.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
                i += 1
                continue
            runs = True
            break
        if not runs:
            return True
    return False


def _secret_name(name, pattern):
    name = os.path.basename(str(name or "").rstrip("/"))
    return bool(pattern.match(name)) and name.lower() != ".env.example"


def jev_denied(tool, tool_input):
    """True when the call would read the stored Jev key or hand one to orche (or cannot be checked)."""
    return jevgate.reason(tool, tool_input) is not None


def denied(tool, tool_input):
    if tool in ("Read", "Edit", "Write"):
        return _secret_name(tool_input.get("file_path"), ENV_FILE)
    if tool == "Grep":
        return _secret_name(tool_input.get("path"), ENV_FILE) or _secret_name(tool_input.get("glob"), ENV_GLOB)
    if tool == "Bash":
        command = EXAMPLE.sub("ENV_EXAMPLE", str(tool_input.get("command") or ""))
        return any(p.search(command) for p in BASH_DENY) or _env_dump(command)
    return False


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or os.environ.get("ORCHE_KEYMASTER") == "1":
            return 0
        tool, tool_input = payload.get("tool_name"), payload.get("tool_input") or {}
        reason = jevgate.reason(tool, tool_input) or (REASON if denied(tool, tool_input) else None)
        if reason is None:
            return 0
    except Exception as exc:  # noqa: BLE001 - gates allow on internal error
        try:
            from orche import log
            log.log("hooks", f"env_read_gate: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
