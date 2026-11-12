#!/usr/bin/env python3
"""PreToolUse gate: inside a division/feature worktree, deny edits outside the worktree or the
feature's scope globs. Always allowed: `.orche/`, `docs/` and `.claude/agent-memory/` under the
worktree, `~/.claude/agent-memory/`, the session scratch root (`/private/tmp/claude-<uid>/`,
`$TMPDIR/claude-*`, or $ORCHE_SCRATCH_ROOT), and the session's own unit folders under the real
project root (`<root>/.orche/units/<u>/…` when unit <u>'s brief is addressed to the session),
except their `BRIEF.md`, `BRIEF.rev<N>.md` and `RUN.json`. A CEO Staff pane (ORCHE_SESSION `staff-<project>`)
edits nothing anywhere (lib/orche/staff.py). A Staff errand (the same session under ORCHE_RUNNER,
lib/orche/errand.py) may write only in the unit folder of the unit it runs (its ORCHE_RUN_ID, else the live
run of its target lock; none: refused), except the files Orche owns there (ERRAND_FIXED), and the scratch
root, and may create a new file (Write, nothing there yet) inside a folder its brief's GOAL or CONTEXT names
by path when that folder is a safe place in the owner's home (safe_folder); everything else is refused.
Internal error -> allow + log."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("scope_gate"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows

ALWAYS = (".orche/", "docs/", ".claude/agent-memory/")
NAMED_PATH = re.compile(r"(?:~|/)[^\s\"'`<>|;,()]*")   # a path written in an errand's GOAL or CONTEXT
UNIT_FIXED = re.compile(r"^(BRIEF\.md|BRIEF\.rev\d+\.md|RUN\.json)$", re.IGNORECASE)   # dispatch and the runner only;
# any case: macOS file systems are case-insensitive, so `brief.md` is BRIEF.md
ERRAND_FIXED = re.compile(r"^(BRIEF\.md|BRIEF\.rev\d+\.md|RUN\.json|PROGRESS\.json|REPORT\.md|REPLY\.md|JOINED)$",
                          re.IGNORECASE)   # an errand's unit folder: what the runner and Orche write


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


def safe_folder(reg, folder) -> bool:
    """`folder` (resolved) is a place an errand may create files in: inside the owner's home folder but not
    the home itself, below no dot folder (`~/.claude`, `~/.ssh`, ...) and not in `~/Library`, neither inside
    nor holding a registered project, the Orche plugin or ORCHE_HOME."""
    from orche import paths, units
    home = os.path.realpath(os.path.expanduser("~"))
    folder = os.path.realpath(folder)
    if not os.path.isabs(home) or home == os.sep or folder == home or not units.is_within(folder, home):
        return False
    parts = os.path.relpath(folder, home).split(os.sep)
    if parts[0] == "Library" or any(p.startswith(".") for p in parts):
        return False
    roots = [paths.PLUGIN_ROOT, paths.orche_home()]
    roots += [p.get("path") for p in ((reg or {}).get("projects") or {}).values() if isinstance(p, dict)]
    return not any(r and (units.is_within(folder, r) or units.is_within(r, folder)) for r in roots)


def named_new_file(reg, b, tool, path) -> bool:
    """True when the errand of brief `b` may create `path` with `tool`: a Write of a file that does not
    exist yet, inside a folder its GOAL or CONTEXT names by path (`~` expanded) that is a safe_folder, the
    path itself below no dot folder of it."""
    if tool != "Write" or os.path.lexists(path):
        return False
    text = f"{b.fields.get('GOAL') or ''}\n{b.fields.get('CONTEXT') or ''}"
    for raw in NAMED_PATH.findall(text):
        folder = os.path.realpath(os.path.expanduser(raw.rstrip(".:!?")))
        if len(folder) > 1 and os.path.isabs(folder) and folder != path and _below(path, folder) \
                and safe_folder(reg, folder):
            return True
    return False


def _below(path, folder) -> bool:
    """`path` is inside `folder` and no part of it below `folder` is a dot name."""
    from orche import units
    if not units.is_within(path, folder):
        return False
    return not any(p.startswith(".") for p in os.path.relpath(path, folder).split(os.sep))


def errand_unit(reg, m):
    """The errand brief of the unit the errand run of Staff member `m` is running: its ORCHE_RUN_ID
    (`<unit>@<ns>`), else the unit of the live run holding `m`'s target lock; None when neither names an
    errand of `m`."""
    from orche import brief, errand, runs, util
    unit = (os.environ.get("ORCHE_RUN_ID") or "").rpartition("@")[0]
    if not unit:
        try:
            lock = util.read_dict(runs.target_lock(m.project, m.session)) or {}
        except OSError:     # unreadable: no unit, so refused
            lock = {}
        unit = str(lock.get("unit") or "") if runs.alive(lock.get("pid")) else ""
    if not unit or brief.unit_id_error(unit):
        return None
    b = brief.find(reg, unit, project=m.project)
    return b if errand.is_errand(b) and b.to == m.session else None


def own_errand_file(b, path) -> bool:
    """`path` is inside errand `b`'s own unit folder and is not a file Orche writes there (ERRAND_FIXED)."""
    from orche import units
    folder = os.path.realpath(b.folder)
    if not units.is_within(path, folder) or path == folder:
        return False
    return not ERRAND_FIXED.match(os.path.relpath(path, folder).replace(os.sep, "/"))


def _errand_allows(payload) -> bool:
    """A Staff errand's file tool on a path it may write: its own unit folder, scratch, or a named new file."""
    from orche import errand, registry, staff
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target or payload.get("tool_name") not in staff.FILE_TOOLS:
        return False
    reg = registry.load()
    m = errand.member(reg, os.environ.get("ORCHE_SESSION"))
    b = errand_unit(reg, m) if m is not None else None
    if b is None:
        return False
    cwd = payload.get("cwd") or os.getcwd()
    path = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
    return (own_errand_file(b, path) or scratch_allowed(path)
            or named_new_file(reg, b, payload.get("tool_name"), path))


def decide(payload):
    """Deny reason, or None to stay silent."""
    from orche import registry, roster, staff, units
    if staff.is_staff(os.environ.get("ORCHE_SESSION")):
        if os.environ.get("ORCHE_RUNNER") and _errand_allows(payload):
            return None
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
