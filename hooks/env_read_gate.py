#!/usr/bin/env python3
"""PreToolUse gate: deny direct reads of secrets (`.env` files, keys, `secrets/` dirs, environment
dumps, secret managers; rules and gaps in `lib/orche/envgate.py`) and of the stored Jev key
(keychain value lookups, ORCHE_HOME/secrets/jev.key, orche's key API, keys fed to orche's stdin
flags; `lib/orche/jevgate.py`). Both rule sets look at command words and tool paths, never at text
that merely mentions a secret (a Markdown file about `.env`, a commit message, a search pattern),
except the Jev key file's full ORCHE_HOME path, which is refused anywhere in a command.

Denies only on a positive match; prints nothing otherwise (no auto-allow). Internal error -> allow +
log, except that both rule sets refuse a command they cannot finish checking ("too complex").
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

# This gate is self-contained stdlib code that also runs on old interpreters, so an old Python
# is logged but the gate keeps denying positive matches (stronger than allowing everything).
pyguard.check_hook("env_read_gate")

from orche import envgate, jevgate  # noqa: E402  (stdlib only, old-interpreter safe like this file)

REASON = envgate.REASON
JEV_REASON = jevgate.REASON


def jev_denied(tool, tool_input):
    """True when the call would read the stored Jev key or hand one to orche (or cannot be checked)."""
    return jevgate.reason(tool, tool_input) is not None


def denied(tool, tool_input):
    """True when the call would read a secret file or the environment (or cannot be checked)."""
    return envgate.reason(tool, tool_input) is not None


def _log_denial(tool, reason):
    """One events line per denial: the gate, the session, the tool and the reason's first 80 characters
    (the reason names only the matched token or path; never file contents or the command). Best-effort."""
    try:
        from orche import log
        what = " ".join(str(reason).split())[:80]
        log.log("events", f"gate=env_read session={os.environ.get('ORCHE_SESSION') or '-'} tool={tool or '-'} "
                f"what={what}")
    except Exception:  # noqa: BLE001 - logging never changes the decision
        pass


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or os.environ.get("ORCHE_KEYMASTER") == "1":
            return 0
        tool, tool_input = payload.get("tool_name"), payload.get("tool_input") or {}
        reason = jevgate.reason(tool, tool_input) or envgate.reason(tool, tool_input)
        if reason is None:
            return 0
    except Exception as exc:  # noqa: BLE001 - gates allow on internal error
        try:
            from orche import log
            log.log("hooks", f"env_read_gate: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    _log_denial(tool, reason)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
