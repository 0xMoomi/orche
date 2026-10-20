#!/usr/bin/env python3
"""UserPromptSubmit hook: triage a Main prompt, then drain the session's inbox at drain points.

Triage (lib/orche/triage.py, off unless `triage.enabled`) may route a simple prompt straight to a
division and print `{"decision": "block", ...}` so Main spends no turn; otherwise the prompt reaches
Main and the inbox text (plus any triage note) is printed and injected. Fail-open.
"""
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
        from orche import inbox, registry, roster, triage
        r = roster.Roster(registry.load())
        who = r.identify(payload.get("cwd") or os.getcwd())
        outcome = triage.handle(payload, STARTED, r, who)   # never raises
        if outcome.block is not None:
            print(json.dumps(outcome.block, ensure_ascii=False))
            return 0
        # The hook budget runs from STARTED, less the time a classifier call was in flight.
        text = inbox.handle_prompt(payload, STARTED + outcome.model_s, r, who)
        text = "\n\n".join(t for t in (outcome.note, text) if t)
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
