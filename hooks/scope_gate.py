#!/usr/bin/env python3
"""PreToolUse gate: inside a division/feature worktree, deny edits outside the worktree or the
feature's scope globs. Always allowed: `.orche/`, `docs/` and `.claude/agent-memory/` under the
worktree, `~/.claude/agent-memory/`, and the session scratch root (`/private/tmp/claude-<uid>/`,
`$TMPDIR/claude-*`, or $ORCHE_SCRATCH_ROOT). Internal error -> allow + log."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("scope_gate"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows

ALWAYS = (".orche/", "docs/", ".claude/agent-memory/")


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


def decide(payload):
    """Deny reason, or None to stay silent."""
    from orche import registry, units
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    unit = units.unit_from_cwd(registry.load(), cwd)
    if unit is None:
        return None
    root = os.path.realpath(unit.worktree_path)
    path = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
    scope = (unit.entry.get("scope") or []) if unit.kind == "feat" else []
    allowed = [f"{root}/{g}" for g in scope] if scope else [f"{root}/"]
    allowed += [f"{root}/{a}" for a in ALWAYS] if scope else []
    if units.is_within(path, root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if not scope or rel.startswith(ALWAYS) or any(units.scope_match(rel, g) for g in scope):
            return None
    elif scratch_allowed(path):
        return None
    return (f"[orche] scope: {target} is outside {unit.session}'s allowed roots: {', '.join(allowed)}. "
            "Ask Main to re-scope or dispatch the change to the owning unit.")


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
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
