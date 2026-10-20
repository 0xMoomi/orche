"""Shared helpers: worktree path strings, is_within/project_root, activity files, time, scope globs."""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

from . import paths


def worktree_path(worktree: str) -> str:
    """`<repoId>::/abs/path` -> `/abs/path` (a bare path is returned unchanged)."""
    return worktree.split("::", 1)[1] if "::" in worktree else worktree


def now() -> _dt.datetime:
    """Current local time (aware). Tests patch this."""
    return _dt.datetime.now(_dt.timezone.utc).astimezone()


def parse_iso(text):
    """Aware datetime from ISO text (naive is taken as UTC), or None."""
    try:
        value = _dt.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)


_SAFE_SESSION = re.compile(r"^[A-Za-z0-9._-]+$")


def activity_file(session):
    """ORCHE_HOME/activity/<session> — its mtime is the session's last observed activity."""
    if not session or not _SAFE_SESSION.match(session) or session.startswith("."):
        return None
    return paths.orche_home() / "activity" / session


def touch_activity(session) -> None:
    """Record activity for `session` (hook-safe: cheap, never raises)."""
    try:
        path = activity_file(session)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, None)
    except OSError:
        pass


def last_active(entry: dict):
    """max(registry last_activity, activity touch-file mtime) as an aware datetime, or None."""
    times = [t for t in (parse_iso(entry.get("last_activity")),) if t is not None]
    path = activity_file(entry.get("session"))
    if path is not None:
        try:
            times.append(_dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc))
        except OSError:
            pass
    return max(times) if times else None


def real(path) -> str:
    """`path` with `~` expanded and symlinks resolved, or "" for an empty path."""
    return os.path.realpath(os.path.expanduser(str(path))) if path else ""


def is_within(path, root) -> bool:
    """True if `path` is `root` or below it (both resolved through symlinks)."""
    path, root = real(path), real(root)
    return bool(root) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))


def project_root(reg: dict, key: str):
    """Filesystem path of a registered project, or None."""
    proj = reg.get("projects", {}).get(key) or {}
    return Path(real(proj["path"])) if proj.get("path") else None


def scope_match(rel_path: str, pattern: str) -> bool:
    """Glob match on a `/`-separated relative path; `**` spans directories, `*` and `?` do not.
    `[` is literal (a Next.js `app/[slug]/**` admits only that directory, never `app/s/…`)."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), rel_path) is not None


SETTINGS_LOCAL = ".claude/settings.local.json"


def porcelain_path(line: str) -> str:
    """The path of one `git status --porcelain` line (the target of a rename; quotes and a trailing / dropped)."""
    rel = line[3:].split(" -> ")[-1].strip()
    if rel.startswith('"') and rel.endswith('"'):
        rel = rel[1:-1]
    return rel.rstrip("/")


def orche_owned(rel, session) -> bool:
    """Paths ORCHE itself writes into a unit worktree (expected to be untracked)."""
    return (rel == ".orche" or rel.startswith(".orche/") or rel == f".claude/agents/{session}.md"
            or rel == SETTINGS_LOCAL or rel.startswith(".claude/agent-memory/"))
