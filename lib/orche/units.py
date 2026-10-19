"""Shared CLI helpers: unit lookup by session name, project lookup from a cwd, HR log, time."""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import paths, util

STALE_HANDOFF_SECONDS = 30 * 60


@dataclass
class Unit:
    """A division or feature agent resolved from its session name."""
    session: str
    project: str
    kind: str          # "div" | "feat"
    name: str          # division name or feature name
    entry: dict        # the registry dict (mutating it mutates the registry)
    event_unit: str    # value for events[].unit: "<division>" or "build/<feature>"

    @property
    def worktree_path(self) -> Path:
        return Path(worktree_path(self.entry.get("worktree", "")))

    @property
    def handoff(self) -> Path:
        return self.worktree_path / ".orche" / self.kind / self.name / "HANDOFF.md"


def worktree_path(worktree: str) -> str:
    """`<repoId>::/abs/path` -> `/abs/path` (a bare path is returned unchanged)."""
    return worktree.split("::", 1)[1] if "::" in worktree else worktree


def find_unit(reg: dict, session: str):
    """Resolve `div-<proj>-<division>` / `feat-<proj>-<feature>` by exact session match, else None.

    A feature name can be re-created after retirement, so a non-retired match wins over a retired one.
    """
    matches = []
    for key, proj in reg.get("projects", {}).items():
        for div_name, div in proj.get("divisions", {}).items():
            if div.get("session") == session:
                matches.append(Unit(session, key, "div", div_name, div, div_name))
            for feat in div.get("features", []):
                if feat.get("session") == session:
                    matches.append(Unit(session, key, "feat", feat["name"], feat, f"{div_name}/{feat['name']}"))
    return next((u for u in matches if u.entry.get("status") != "retired"), matches[0] if matches else None)


def project_from_cwd(reg: dict, cwd) -> str | None:
    """Walk up from `cwd` to a registered project path, a unit worktree path, or a `.orche/project` marker."""
    owners = {}
    for key, proj in reg.get("projects", {}).items():
        roots = [proj.get("path", "")]
        for div in proj.get("divisions", {}).values():
            roots.append(worktree_path(div.get("worktree", "")))
            roots += [worktree_path(f.get("worktree", "")) for f in div.get("features", [])]
        for root in roots:
            if root:  # resolved through symlinks, like is_within
                owners.setdefault(_real(root), key)
    here = Path(_real(cwd))
    for directory in (here, *here.parents):
        key = owners.get(str(directory))
        if key:
            return key
        marker = directory / ".orche" / "project"
        if marker.is_file():
            try:
                key = marker.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            return key if key in reg.get("projects", {}) else None
    return None


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


def actor() -> str:
    """Who is acting, for events[].by: $ORCHE_SESSION, else `user`."""
    return os.environ.get("ORCHE_SESSION") or "user"


def hr_log_path() -> Path:
    return paths.orche_home() / "hr-log.md"


def hr_log(text: str) -> None:
    """Append one `- <ts> <text>` line to ORCHE_HOME/hr-log.md (created with a header if missing)."""
    path = hr_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# HR log\n\n")
        fh.write(f"- {util.now_iso()} {text}\n")


def _real(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path))) if path else ""


def is_within(path, root) -> bool:
    """True if `path` is `root` or below it (both resolved through symlinks)."""
    path, root = _real(path), _real(root)
    return bool(root) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))


def unit_from_cwd(reg: dict, cwd):
    """The division/feature whose worktree contains `cwd` (deepest match), else None."""
    best, best_len = None, -1
    for key, proj in reg.get("projects", {}).items():
        for div_name, div in proj.get("divisions", {}).items():
            candidates = [(Unit(div.get("session", ""), key, "div", div_name, div, div_name), div)]
            for feat in div.get("features", []):
                candidates.append((Unit(feat.get("session", ""), key, "feat", feat.get("name", ""), feat,
                                        f"{div_name}/{feat.get('name', '')}"), feat))
            for unit, entry in candidates:
                root = worktree_path(entry.get("worktree", ""))
                if root and is_within(cwd, root) and len(_real(root)) > best_len:
                    best, best_len = unit, len(_real(root))
    return best


def all_sessions(reg: dict) -> list:
    """Every session name the registry knows (main, liaison, divisions, features), sorted."""
    out = set()
    for proj in reg.get("projects", {}).values():
        for key in ("main_session", "liaison_session"):
            if proj.get(key):
                out.add(proj[key])
        for div in proj.get("divisions", {}).values():
            if div.get("session"):
                out.add(div["session"])
            for feat in div.get("features", []):
                if feat.get("session"):
                    out.add(feat["session"])
    return sorted(out)


def session_from_cwd(reg: dict, cwd):
    """Own session name from cwd: a unit worktree's session, else the project's main session."""
    unit = unit_from_cwd(reg, cwd)
    if unit is not None and unit.session:
        return unit.session
    for proj in reg.get("projects", {}).values():
        if proj.get("path") and is_within(cwd, proj["path"]):
            return proj.get("main_session") or None
    return None


def project_root(reg: dict, key: str):
    """Filesystem path of a registered project, or None."""
    proj = reg.get("projects", {}).get(key) or {}
    return Path(_real(proj["path"])) if proj.get("path") else None


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
