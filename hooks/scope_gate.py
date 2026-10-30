#!/usr/bin/env python3
"""PreToolUse gate: inside a division/feature worktree, deny edits outside the worktree or the
feature's scope globs. Always allowed: `.orche/`, `docs/` and `.claude/agent-memory/` under the
worktree, `~/.claude/agent-memory/`, the session scratch root (`/private/tmp/claude-<uid>/`,
`$TMPDIR/claude-*`, or $ORCHE_SCRATCH_ROOT), and the session's own unit folders under the real
project root (`<root>/.orche/units/<u>/…` when unit <u>'s brief is addressed to the session),
except their `BRIEF.md`, `BRIEF.rev<N>.md` and `RUN.json`. The CEO Staff (ORCHE_SESSION `staff`) edits
nothing anywhere (lib/orche/staff.py). Internal error -> allow + log."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("scope_gate"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows

ALWAYS = (".orche/", "docs/", ".claude/agent-memory/")
UNIT_FIXED = re.compile(r"^(BRIEF\.md|BRIEF\.rev\d+\.md|RUN\.json)$", re.IGNORECASE)   # dispatch and the runner only;
# any case: macOS file systems are case-insensitive, so `brief.md` is BRIEF.md


def scratch_allowed(path) -> bool:
    """True for Claude Code's own scratch/memory locations (outside any worktree)."""
    from orche import units
    roots = [os.environ.get("ORCHE_SCRATCH_ROOT") or f"/private/tmp/claude-{os.getuid()}",
             os.path.expanduser("~/.claude/agent-memory")]
    if any(units.is_within(path, r) for r in roots if r):
        return True
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and units.is_within(path, tmpdir):
        rel = os.path.relpath(path, os.path.realpath(tmpdir)).split(os.sep)
        return bool(rel) and rel[0].startswith("claude-")
    return False


def own_unit_file(reg, unit, path) -> bool:
    """True for `<realpath(project root)>/.orche/units/<u>/<rest>` (rest non-empty) when unit <u>'s
    brief (rev > 0) is addressed to `unit.session` and `rest` is not BRIEF.md, BRIEF.rev<N>.md or RUN.json."""
    from orche import brief, units
    if unit.home is None:
        return False
    base = os.path.join(os.path.realpath(unit.home), brief.UNITS)
    if not units.is_within(path, base):
        return False
    parts = os.path.relpath(path, base).split(os.sep)
    if len(parts) < 2 or parts[0] in ("", ".", "..") or UNIT_FIXED.match("/".join(parts[1:])):
        return False
    b = brief.find(reg, parts[0], project=unit.project)
    return b is not None and b.rev > 0 and b.to == unit.session


def decide(payload):
    """Deny reason, or None to stay silent."""
    from orche import registry, roster, staff, units
    if staff.is_staff(os.environ.get("ORCHE_SESSION")):
        return staff.reason(payload.get("tool_name"), payload.get("tool_input"))
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    reg = registry.load()
    unit = roster.Roster(reg).place(cwd).unit
    if unit is None:
        return None
    root = os.path.realpath(unit.worktree)
    path = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
    scope = (unit.entry.get("scope") or []) if unit.kind == "feat" else []
    allowed = [f"{root}/{g}" for g in scope] if scope else [f"{root}/"]
    allowed += [f"{root}/{a}" for a in ALWAYS] if scope else []
    if units.is_within(path, root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if not scope or rel.startswith(ALWAYS) or any(units.scope_match(rel, g) for g in scope):
            return None
    elif own_unit_file(reg, unit, path) or scratch_allowed(path):
        return None
    home = os.path.realpath(unit.home) if unit.home is not None else "<project root>"
    return (f"[orche] scope: {target} is outside {unit.session}'s allowed roots: {', '.join(allowed)} "
            f"and its own unit folders under {home}/.orche/units/. "
            "Ask Main to re-scope or dispatch the change to the owning unit.")


def _log_denial(tool, reason):
    """One events line per denial: the gate, the session, the tool and the reason's first 80 characters
    (the reason names only the matched token or path; never file contents or the command). Best-effort."""
    try:
        from orche import log
        what = " ".join(str(reason).split())[:80]
        log.log("events", f"gate=scope session={os.environ.get('ORCHE_SESSION') or '-'} tool={tool or '-'} "
                f"what={what}")
    except Exception:  # noqa: BLE001 - logging never changes the decision
        pass


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception as exc:  # noqa: BLE001 - gates allow on internal error
        try:
            from orche import log
            log.log("hooks", f"scope_gate: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    if reason:
        _log_denial(payload.get("tool_name"), reason)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
