#!/usr/bin/env python3
"""SessionEnd hook: move an ended ORCHE session's scratchpad into the janitor trash (never deletes).

Only sessions launched by ORCHE ($ORCHE_SESSION set) are touched; every other session's scratch is
left to the janitor's age rule. Scratch root: $ORCHE_SCRATCH_ROOT, else /private/tmp/claude-<uid>;
candidates are <root>/*/<session_id>. Each move is recorded in ORCHE_HOME/graveyard.md and is
undoable with `orche janitor undo <id>` (30-day trash). Fail-open.
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("session_end"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("agent_id"):
            return 0
        me = os.environ.get("ORCHE_SESSION")
        if not me:
            return 0
        session_id = str(payload.get("session_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,}", session_id):
            return 0
        from orche import log, units
        from orche.janitor import graveyard, trash
        root = os.environ.get("ORCHE_SCRATCH_ROOT") or f"/private/tmp/claude-{os.getuid()}"
        for path in glob.glob(os.path.join(glob.escape(root), "*", session_id)):
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            now = units.now()

            def record(info, path=path, now=now):
                graveyard.record(None, f"scratch {path}", f"session-end: {me} ended",
                                 f"orche janitor undo {info['id']}", "session_end", date=now.date())
            item = trash.move(path, root, trigger="session-end", reason=f"{me} ended", now=now, record=record)
            log.log("hooks", f"session_end: trashed scratch {path} -> {item['id']}")
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"session_end: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
