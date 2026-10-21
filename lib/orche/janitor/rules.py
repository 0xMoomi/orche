"""Janitor decisions: triggers, untouchables, and the plan for one run. No side effects.

Triggers (only these archive or trash): registry `retired`; unprocessed `docs/GRAVEYARD.md` rows
with a `path:`/`session:` pointer; a registry worktree Orca no longer lists (retired only with
`--apply-removed`); `orche discard` (which writes a GRAVEYARD row). Age only for regenerable
artifacts: build caches of idle dormant/retired units, Orca Codex `.tmp`, ownerless scratch.
Untouchables are never listed: active/paused units, `.env*`, `memory/`, registry, cards,
LEDGER/GRAVEYARD/DECISIONS/STATUS/HANDOFF/CHARTER. Dirty or live checkouts are listed as skips.
"""
import datetime as _dt
import hashlib
import os
import re
from pathlib import Path

from .. import log, orca, units
from . import Candidate, configured_dir, discover, graveyard, trash, untouchable_sessions

DAY = 86400
RECENT_WRITE_S = 3600
PROTECTED_STEMS = {"LEDGER", "GRAVEYARD", "DECISIONS", "STATUS", "HANDOFF", "CHARTER"}
PROTECTED_DIRS = {"memory", "cards", "agent-memory"}
UNTOUCHABLE = ("active", "paused", "onboarding")
CACHE_STATUSES = ("dormant", "retired")
# Record files ORCHE itself appends to; their edits are not someone's work in progress.
RECORD_FILES = {"docs/GRAVEYARD.md", "docs/LEDGER.tsv"}
RECORD_NAMES_UNDER_ORCHE = {"DECISIONS.tsv", "STATUS.md", "HANDOFF.md"}
POINTER = re.compile(r"(?:^|\s)(path|session):(\S+)")


# ---- predicates -------------------------------------------------------------------------------

def is_protected_name(name: str) -> bool:
    return (name.startswith(".env") or name in PROTECTED_DIRS or name in ("registry.json", ".git")
            or name.split(".", 1)[0] in PROTECTED_STEMS)


def is_protected(path, root=None) -> bool:
    """True if any component of `path` (relative to `root` when given) is protected."""
    p = Path(path)
    parts = p.parts
    if root:
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            pass
    return any(is_protected_name(x) for x in parts)


def contains_protected(path, root=None) -> bool:
    if is_protected(path, root):
        return True
    if os.path.isdir(path) and not os.path.islink(path):
        for _, dirnames, filenames in os.walk(path):
            if any(is_protected_name(n) for n in dirnames + filenames):
                return True
    return False


def idle_days(last_activity, now):
    ts = units.parse_iso(last_activity)
    return None if ts is None else (now - ts).total_seconds() / DAY


def pointer(what):
    """('path'|'session', value) from a GRAVEYARD `what` cell, or None."""
    m = POINTER.search(what or "")
    return (m.group(1), m.group(2).strip("`")) if m else None


def row_hash(project, line) -> str:
    return hashlib.sha256(f"{project}\0{line.strip()}".encode()).hexdigest()[:20]


def purge_due(name, today, days) -> bool:
    try:
        return (today - _dt.date.fromisoformat(name)).days > days
    except ValueError:
        return False


def _session_live(ctx, name):
    if ctx.terminals is None:
        return "orca-unavailable"
    if any(orca.normalize_title(t.get("title")) == name for t in ctx.terminals):
        return f"live terminal {name}"
    return None


def _live(ctx, path):
    """Reason when a live Orca terminal works in (or above/below) `path`, else None."""
    if ctx.terminals is None:
        return "orca-unavailable"
    for t in ctx.terminals:
        wp = t.get("worktreePath")
        if wp and (units.is_within(path, wp) or units.is_within(wp, path)):
            return f"live terminal {orca.normalize_title(t.get('title')) or t.get('handle', '?')}"
    return None


def _wip(ctx, root, ignore=None):
    """`wip:N` when the checkout has uncommitted changes (outside `ignore`), `git-unknown` on error."""
    if root not in ctx.wip:
        ctx.wip[root] = discover.git_porcelain(root)
    lines = ctx.wip[root]
    if lines is None:
        return "git-unknown"
    lines = [ln for ln in lines if not is_record_file(units.porcelain_path(ln))]
    if ignore:
        ign = ignore.rstrip("/")
        lines = [ln for ln in lines if not (units.porcelain_path(ln) == ign
                                            or units.porcelain_path(ln).startswith(ign + "/"))]
    return f"wip:{len(lines)}" if lines else None


def is_record_file(rel) -> bool:
    """ORCHE ledgers/records (GRAVEYARD, LEDGER, unit DECISIONS/STATUS/HANDOFF under .orche/)."""
    rel = rel.strip("/")
    return rel in RECORD_FILES or (rel.startswith(".orche/") and rel.rsplit("/", 1)[-1] in RECORD_NAMES_UNDER_ORCHE)


def _recent(ctx, files):
    newest = max((os.lstat(f).st_mtime for f in files), default=0)
    return "written within the last hour" if ctx.now.timestamp() - newest < RECENT_WRITE_S else None


def _owned_by(ctx, cwd, worktree, project):
    """True when `cwd` is in `worktree` and no non-retired unit owns it at the same depth or deeper.

    Main sessions are not units, so the project's main checkout is guarded explicitly:
    a cwd equal to the project root never matches, and a cwd inside it matches only when the
    retired worktree lies strictly below the root.
    """
    if not worktree or not units.is_within(cwd, worktree):
        return False
    root = discover.real((ctx.reg.get("projects", {}).get(project) or {}).get("path"))
    if root:
        if discover.real(cwd) == root or worktree == root:
            return False
        if units.is_within(cwd, root) and not units.is_within(worktree, root):
            return False
    for u in ctx.units:
        if u.session not in ctx.retired and u.worktree and units.is_within(cwd, u.worktree) \
                and len(u.worktree) >= len(worktree):
            return False
    return True


def _foreign(ctx, path, name, keep):
    """True if a transcript file belongs to a registered session that is not being retired."""
    try:
        rel = Path(path).relative_to(ctx.claude_root).parts
    except ValueError:
        return True
    stem = Path(rel[1]).name.removesuffix(".jsonl") if len(rel) > 1 else ""
    return stem != name and stem in keep


def main_checkout_cwd(reg, unit_recs, cwd) -> bool:
    """True when `cwd` is inside a registered main checkout and not inside a unit worktree below it."""
    for proj in reg.get("projects", {}).values():
        root = discover.real(proj.get("path", ""))
        if root and units.is_within(cwd, root):
            return not any(u.worktree and u.worktree != root and units.is_within(cwd, u.worktree)
                           for u in unit_recs)
    return False


def _transcript_cwd(ctx, path):
    """First `cwd` of the transcript that `path` belongs to (`<stem>.jsonl`, or `path` itself)."""
    try:
        rel = Path(path).relative_to(ctx.claude_root).parts
    except ValueError:
        return discover.first_cwd(path)
    if len(rel) < 2:
        return None
    stem = rel[1].removesuffix(".jsonl")
    jsonl = Path(ctx.claude_root) / rel[0] / f"{stem}.jsonl"
    return discover.first_cwd(jsonl if jsonl.is_file() else path)


def _main_transcript(ctx, path, name):
    """True for a Main transcript (first cwd in a main checkout) that no retired unit `name` owns."""
    cwd = _transcript_cwd(ctx, path)
    if not cwd or not main_checkout_cwd(ctx.reg, ctx.units, cwd):
        return False
    rec = next((u for u in ctx.units if u.session == name), None)
    return not (rec is not None and name in ctx.retired and rec.worktree and units.is_within(cwd, rec.worktree))


def _registered_roots(ctx) -> list:
    roots = [discover.real(p.get("path", "")) for p in ctx.reg.get("projects", {}).values()]
    roots += [u.worktree for u in ctx.units]
    roots += [discover.real(ctx.home), str(ctx.claude_root) if ctx.claude_root else None]
    return [r for r in roots if r]


# ---- plan -------------------------------------------------------------------------------------

def plan(ctx) -> list:
    out = []
    _purge(ctx, out)
    _removed(ctx, out)
    for u in ctx.units:
        if u.session in ctx.retired:
            trigger = "retired" if u.status == "retired" else "orca-removed"
            _sessions(ctx, out, u.session, u.project, u.worktree, trigger, [])
    _graveyard(ctx, out)
    _caches(ctx, out)
    _codex_tmp(ctx, out)
    _scratch(ctx, out)
    return out


def _purge(ctx, out):
    days = int(ctx.st["trash_days"])
    for day in trash.due_dirs(ctx.now.date(), days):
        size, _ = discover.tree_stats(day)
        out.append(Candidate("age:trash", "purge", f"trash/{day.name}", f"older than {days} days", size,
                             kind="purge", path=str(day)))


def _removed(ctx, out):
    """Units whose worktree Orca no longer lists. Retire only with --apply-removed, never active/paused,
    only when the listing looks complete, and only when the worktree is gone from disk too."""
    if not ctx.worktrees:
        if ctx.worktrees is not None and any(u.raw_worktree for u in ctx.units):
            log.log("janitor", "orca worktree list is empty; orca-removed rule skipped")
        return
    for u in ctx.units:
        if u.status == "retired" or not u.raw_worktree or not u.worktree:
            continue  # no resolvable worktree path: never conclude it is gone
        if u.raw_worktree in ctx.worktrees or (u.worktree and u.worktree in ctx.worktrees):
            continue
        if u.status in UNTOUCHABLE:
            log.log("janitor", f"{u.session} ({u.status}) worktree not listed by Orca; left alone")
            continue
        main = discover.real((ctx.reg["projects"].get(u.project) or {}).get("path"))
        if not main or main not in ctx.worktrees:
            log.log("janitor", f"orca listing omits {u.project}'s main checkout; orca-removed skipped")
            continue
        base = dict(kind="unit", unit=u.session, project=u.project)
        reason = _session_live(ctx, u.session)
        if not reason and u.worktree and os.path.lexists(u.worktree):
            reason = "worktree no longer listed by Orca but still on disk"
        if not reason and not ctx.apply_removed:
            reason = "worktree no longer listed by Orca; rerun with --apply-removed to retire"
        if reason:
            out.append(Candidate("orca-removed", "skip", u.session, reason, **base))
            continue
        out.append(Candidate("orca-removed", "retire", u.session, "worktree gone from Orca and disk", **base))
        ctx.retired.add(u.session)


def _sessions(ctx, out, name, project, worktree, trigger, rows):
    existing = next((c for c in out if c.kind == "sessions" and c.unit == name), None)
    if existing is not None:
        existing.rows += rows
        return
    keep = untouchable_sessions(ctx)
    owns = (lambda cwd: _owned_by(ctx, cwd, worktree, project)) if worktree else None
    files = [f for f in discover.session_files(ctx.claude_root, name, owns)
             if not is_protected(f, ctx.claude_root) and not _foreign(ctx, f, name, keep)
             and not _main_transcript(ctx, f, name)]
    target = f"sessions:{name}"
    if not files:
        if rows:
            out.append(Candidate(trigger, "skip", target, "no transcripts found", kind="sessions", unit=name,
                                 project=project, rows=list(rows), settle=True))
        return
    reason = _session_live(ctx, name)
    if not reason and worktree and os.path.isdir(worktree):
        reason = _live(ctx, worktree) or _wip(ctx, worktree)
    reason = reason or _recent(ctx, files)
    size = sum(os.lstat(f).st_size for f in files)
    why = {"retired": "unit retired", "graveyard": "GRAVEYARD row", "orca-removed": "worktree removed"}[trigger]
    out.append(Candidate(trigger, "skip" if reason else "archive", f"{target} ({len(files)} files)",
                         reason or why, size, kind="sessions", unit=name, project=project,
                         files=[str(f) for f in files], rows=list(rows)))


def _graveyard(ctx, out):
    for key, proj in ctx.reg.get("projects", {}).items():
        root = discover.real(proj.get("path", ""))
        if not root:
            continue
        try:
            text = graveyard.project_file(root).read_text(encoding="utf-8")
        except OSError:
            continue
        for line, cells in graveyard.parse_rows(text):
            ptr = pointer(cells[1]) if len(cells) > 1 else None
            if not ptr:
                continue
            h = row_hash(key, line)
            if h in ctx.state["processed"]:
                continue
            kind, value = ptr
            why = cells[2] if len(cells) > 2 else ""
            if kind == "session":
                _graveyard_session(ctx, out, key, value, h)
            else:
                _graveyard_path(ctx, out, key, root, value, why, h)


def _graveyard_session(ctx, out, key, name, h):
    rec = next((u for u in ctx.units if u.session == name), None)
    if not discover.valid_session_name(name):
        out.append(Candidate("graveyard", "skip", f"sessions:{name}", "not a session name",
                             kind="sessions", unit=name, project=key, rows=[h], settle=True))
    elif rec is not None and name not in ctx.retired:
        out.append(Candidate("graveyard", "skip", f"sessions:{name}", f"unit is {rec.status}; retire it first",
                             kind="sessions", unit=name, project=key))
    elif rec is None and name in ctx.all_sessions:
        out.append(Candidate("graveyard", "skip", f"sessions:{name}", "registered project session",
                             kind="sessions", unit=name, project=key))
    elif rec is None and name not in (ctx.state.get("discards") or {}):
        # Repo files are writable by any worker: a bare `session:<id>` is honoured only when the
        # operator ran `orche discard <id>` (recorded in ORCHE_HOME, not in the repo).
        out.append(Candidate("graveyard", "skip", f"sessions:{name}",
                             "not a registry session name (run `orche discard` for other sessions)",
                             kind="sessions", unit=name, project=key))
    else:
        _sessions(ctx, out, name, key, rec.worktree if rec else None, "graveyard", [h])


def _containing_root(ctx, key, target):
    proj = ctx.reg["projects"][key]
    roots = [discover.real(proj.get("path", ""))] + [u.worktree for u in ctx.units if u.project == key]
    inside = [r for r in roots if r and units.is_within(target, r)]
    return max(inside, key=len) if inside else None


def _graveyard_path(ctx, out, key, root, value, why, h):
    target = discover.real_leaf(value if os.path.isabs(value) else os.path.join(root, value))
    owner = _containing_root(ctx, key, target) if target else None
    base = dict(kind="path", path=target, root=owner or "", project=key, rows=[h])
    if owner is None:
        out.append(Candidate("graveyard", "skip", value, "outside the project's checkouts", **base))
        return
    if not os.path.lexists(target):
        out.append(Candidate("graveyard", "skip", value, "target missing", settle=True, **base))
        return
    if contains_protected(target, owner):
        log.log("janitor", f"refused GRAVEYARD pointer to a protected path ({key})")
        ctx.silent_rows.append(h)
        return
    if any(units.is_within(r, target) for r in _registered_roots(ctx)):
        out.append(Candidate("graveyard", "skip", value, "contains a registered checkout or ORCHE state", **base))
        return
    rec = next((u for u in ctx.units if u.worktree == owner), None)
    if rec is not None and rec.status in UNTOUCHABLE and rec.session not in ctx.retired:
        out.append(Candidate("graveyard", "skip", value, f"inside {rec.status} unit {rec.session}", **base))
        return
    rel = os.path.relpath(target, owner)
    reason = _live(ctx, target) or _wip(ctx, owner, ignore=rel)
    size, _ = discover.tree_stats(target)
    out.append(Candidate("graveyard", "skip" if reason else "trash", value, reason or why or "GRAVEYARD row",
                         size, **base))


def _caches(ctx, out):
    days = float(ctx.st["idle_days"])
    for u in ctx.units:
        if u.session not in ctx.retired and u.status not in CACHE_STATUSES:
            continue
        wt = u.worktree
        if not wt or not os.path.isdir(wt):
            continue
        caches = [c for c in discover.cache_dirs(wt, ctx.st["cache_names"]) if not contains_protected(c, wt)]
        if not caches:
            continue
        gate = _live(ctx, wt) or _wip(ctx, wt)
        if gate:
            out.append(Candidate("age:build-cache", "skip", wt, gate, kind="path", project=u.project,
                                 unit=u.session))
            continue
        idle = idle_days(u.last_activity, ctx.now)
        for c in caches:
            rel = c.relative_to(wt).as_posix()
            size, newest = discover.tree_stats(c)
            age = (ctx.now.timestamp() - newest) / DAY
            tracked = discover.git_tracked(wt, rel)
            if tracked is None:
                reason = "git-unknown"
            elif tracked:
                reason = "tracked in git (not regenerable)"
            elif idle is None:
                reason = "unit last_activity unknown"
            elif idle <= days:
                reason = f"unit active {idle:.0f}d ago"
            elif age <= days:
                reason = f"fresh: touched {age:.1f}d ago"
            else:
                reason = None
            out.append(Candidate("age:build-cache", "skip" if reason else "trash", str(c),
                                 reason or f"unit idle {idle:.0f}d, cache untouched {age:.0f}d", size,
                                 path=str(c), root=wt, project=u.project, unit=u.session))


def _aged_children(ctx, directory, trigger, keep=lambda p: True):
    days = float(ctx.st["idle_days"])
    for child in discover.children(directory):
        if child.is_symlink() or not keep(child) or contains_protected(child, directory):
            continue
        size, newest = discover.tree_stats(child)
        age = (ctx.now.timestamp() - newest) / DAY
        if age > days:
            yield Candidate(trigger, "trash", str(child), f"untouched {age:.0f}d", size,
                            path=str(child), root=str(directory))


def _codex_tmp(ctx, out):
    tmp = configured_dir(ctx.st.get("codex_tmp"))
    if tmp and os.path.isdir(tmp):
        out.extend(_aged_children(ctx, tmp, "age:codex-tmp"))


def _scratch(ctx, out):
    """`<scratch_root>/<project-slug>/<session-id>/` whose slug encodes a registered project path."""
    root = configured_dir(ctx.st.get("scratch_root"))
    if not root or not os.path.isdir(root):
        return
    owners = {}
    for key, proj in ctx.reg.get("projects", {}).items():
        for p in {proj.get("path", ""), discover.real(proj.get("path", ""))} - {"", None}:
            owners[discover.claude_slug(p)] = (key, None)
    for u in ctx.units:
        for p in {units.worktree_path(u.raw_worktree), u.worktree} - {""}:
            owners.setdefault(discover.claude_slug(p), (u.project, u))
    keep = untouchable_sessions(ctx) | ctx.retired
    for projdir in discover.children(root):
        owner = owners.get(projdir.name)
        if owner is None or not projdir.is_dir() or projdir.is_symlink():
            continue  # unknown owner: be conservative
        key, rec = owner
        if rec is not None and rec.status in UNTOUCHABLE and rec.session not in ctx.retired:
            continue
        for cand in _aged_children(ctx, projdir, "age:scratch",
                                   keep=lambda p: p.is_dir() and p.name not in keep):
            cand.project, cand.root = key, root
            out.append(cand)
