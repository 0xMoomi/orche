"""Janitor discovery: registry units, Orca worktrees/terminals, git state, filesystem walks.

Read-only. Orca listings run even under --dry-run (they mutate nothing); a failed listing
returns None so the rules can skip whatever depends on it.
"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .. import log, orca, shell, units

PRUNE_DIRS = {".git", "node_modules"}
CACHE_DEPTH = 4


@dataclass
class UnitRec:
    session: str
    project: str
    status: str
    worktree: str        # resolved filesystem path ("" if none)
    raw_worktree: str    # registry value (`<repoId>::/path`)
    last_activity: str
    entry: dict          # registry dict (mutations reach the registry)
    event_unit: str


def real(path):
    """Resolved absolute path, or None for unset/empty/`.`/relative input (never the cwd)."""
    if path is None:
        return None
    text = os.path.expanduser(str(path)).strip()
    if not text or not os.path.isabs(text):
        return None
    return os.path.realpath(text)


def real_leaf(path) -> str:
    """Resolve the parent through symlinks but keep the leaf itself (a symlink is moved, not followed)."""
    path = os.path.normpath(os.path.expanduser(str(path)))
    parent = real(os.path.dirname(path))
    return os.path.join(parent, os.path.basename(path)) if parent else None


def unit_records(reg) -> list:
    out = []
    for key, proj in reg.get("projects", {}).items():
        for dname, div in proj.get("divisions", {}).items():
            wt = div.get("worktree", "")
            out.append(UnitRec(div.get("session", ""), key, div.get("status", ""),
                               real(units.worktree_path(wt)) or "", wt, div.get("last_activity", ""), div, dname))
            for feat in div.get("features", []):
                wt = feat.get("worktree", "")
                out.append(UnitRec(feat.get("session", ""), key, feat.get("status", ""),
                                   real(units.worktree_path(wt)) or "", wt, feat.get("last_activity", ""), feat,
                                   f"{dname}/{feat.get('name', '')}"))
    return out


def orca_worktrees():
    """Set of worktree ids and resolved paths Orca knows, or None if the listing failed."""
    try:
        result = orca.worktree("list")
    except Exception as exc:  # noqa: BLE001
        log.log("janitor", f"orca worktree list failed: {exc}")
        return None
    items = result.get("worktrees") if isinstance(result, dict) else result
    if not isinstance(items, list):
        return None
    keys = set()
    for item in items:
        if isinstance(item, dict):
            if item.get("id"):
                keys.add(str(item["id"]))
            if real(item.get("path")):
                keys.add(real(item["path"]))
    return keys


def orca_terminals():
    try:
        return orca.terminals_of(orca.terminal("list"))
    except Exception as exc:  # noqa: BLE001
        log.log("janitor", f"orca terminal list failed: {exc}")
        return None


def git_porcelain(root):
    """Lines of `git status --porcelain` for `root`, or None if git failed."""
    res = shell.run(["git", "-C", root, "status", "--porcelain"], timeout=60)
    if not res.ok:
        return None
    return [line for line in res.out.splitlines() if line.strip()]


def porcelain_path(line: str) -> str:
    rel = line[3:].split(" -> ")[-1].strip()
    if rel.startswith('"') and rel.endswith('"'):
        rel = rel[1:-1]
    return rel.rstrip("/")


def git_tracked(root, rel):
    """True if git tracks anything under `rel`, False if not, None if git failed."""
    res = shell.run(["git", "-C", root, "ls-files", "--", rel], timeout=60)
    if not res.ok:
        return None
    return bool(res.out.strip())


def tree_stats(path):
    """(total file bytes, newest mtime) of a file or directory tree; symlinks are not followed."""
    st = os.lstat(path)
    size, newest = (0 if os.path.isdir(path) else st.st_size), st.st_mtime
    if os.path.isdir(path) and not os.path.islink(path):
        for dirpath, dirnames, filenames in os.walk(path):
            for name in dirnames + filenames:
                try:
                    s = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                newest = max(newest, s.st_mtime)
                if name in filenames:
                    size += s.st_size
    return size, newest


def cache_dirs(root, names) -> list:
    """Directories named in `names` under `root` (depth <= CACHE_DEPTH, no dot-dirs/node_modules)."""
    names, found, root = set(names), [], Path(root)
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        keep = []
        for d in dirnames:
            full = Path(dirpath) / d
            if full.is_symlink():
                continue
            if d in names:
                found.append(full)
            elif d not in PRUNE_DIRS and not d.startswith(".") and depth + 1 < CACHE_DEPTH:
                keep.append(d)
        dirnames[:] = keep
    return sorted(found)


def claude_slug(path) -> str:
    """Claude Code's project-dir name for a cwd: every non-alphanumeric becomes `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def first_cwd(path, lines=5):
    """`cwd` from the first transcript lines that carry one, else None."""
    try:
        with open(path, "rb") as fh:
            for _ in range(lines):
                raw = fh.readline(1 << 20)
                if not raw:
                    break
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                    return obj["cwd"]
    except OSError:
        return None
    return None


def files_under(directory) -> list:
    d = Path(directory)
    if not d.is_dir() or d.is_symlink():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [n for n in dirnames if not os.path.islink(os.path.join(dirpath, n))]
        out += [Path(dirpath) / n for n in filenames if os.path.isfile(os.path.join(dirpath, n))
                and not os.path.islink(os.path.join(dirpath, n))]
    return sorted(out)


def valid_session_name(name) -> bool:
    """A bare file stem: no separators, not `.`/`..`, not hidden."""
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".")


def session_files(claude_root, session, owns_cwd=None) -> list:
    """Transcripts of one session under `claude_root/<slug>/`.

    `<session>.jsonl` and the `<session>/` subagents dir in every project dir, plus every
    `*.jsonl` (with its `<stem>/` dir) whose first `cwd` satisfies `owns_cwd`.
    """
    if not claude_root:
        return []
    root, out = Path(claude_root), []
    if not root.is_dir() or not valid_session_name(session):
        return out
    for proj in sorted(root.iterdir()):
        if not proj.is_dir() or proj.is_symlink():
            continue
        stems = set()
        for f in sorted(proj.glob("*.jsonl")):
            if not f.is_file() or f.is_symlink():
                continue
            if f.stem == session or (owns_cwd is not None and (cwd := first_cwd(f)) and owns_cwd(cwd)):
                out.append(f)
                stems.add(f.stem)
        stems.add(session)
        for stem in sorted(stems):
            out += files_under(proj / stem)
    return out


def children(directory) -> list:
    d = Path(directory)
    if not d.is_dir():
        return []
    try:
        return sorted(d.iterdir())
    except OSError:
        return []
