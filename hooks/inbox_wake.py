#!/usr/bin/env python3
"""FileChanged hook (asyncRewake): turn a write to this session's wake file into a turn (R-INBOX-4).

The session's SessionStart hook watches `inbox/<session>/.wake` (lib/orche/watch.py). When that file
changes and its WATCH record names this Claude session, and a waking pointer or a dispatched unit is
pending (watch.due), the hook prints Orche's own line (the one `orche nudge` / `orche deliver` type) to
stderr and exits 2: Claude Code starts a turn in an idle session, or delivers it at the next tool boundary
in a busy one, and the prompt hook injects the inbox as for a typed line. Any other file, or nothing
pending: exit 0 at once. Fail-open: an internal error exits 0 and logs one hooks.log line.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("inbox_wake"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("agent_id") or payload.get("event") == "unlink":
            return 0
        from orche import watch
        line = watch.changed(payload.get("file_path"), payload.get("session_id"))
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"inbox_wake: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    if not line:
        return 0
    print(line, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
