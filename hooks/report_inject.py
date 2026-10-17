#!/usr/bin/env python3
"""UserPromptSubmit hook: drain the session's inbox at drain points (stdout is injected). Fail-open."""
import json
import os
import sys
import time

STARTED = time.monotonic()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("report_inject"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("agent_id"):
            return 0
        from orche import inbox
        text = inbox.handle_prompt(payload, STARTED)
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"report_inject: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
