#!/usr/bin/env python3
"""Stop hook: gate completion claims, run the overnight predicate, re-arm the inbox wake.
It relays nothing: briefs go out through `orche dispatch` (a fenced block in the reply is plain text).

Exit 2 + stderr makes the model continue with the message; exit 0 lets it stop. Fail-open.
StopFailure (a turn ended on an API error) only re-arms the inbox wake; its output is ignored.
"""
import json
import os
import sys
import time

STARTED = time.monotonic()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("dispatch_relay"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows


def _kick():
    """Start a reconcile pass when the heartbeat is stale (reconcile.kick); never fails the hook."""
    try:
        from orche import reconcile
        reconcile.kick()
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"reconcile kick: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("agent_id"):
            return 0
        from orche import dispatch
        if payload.get("hook_event_name") == "StopFailure":
            dispatch.handle_stop_failure(payload)
            return 0
        code, message = dispatch.handle_stop(payload, STARTED)
        _kick()
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"dispatch_relay: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    if code:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
