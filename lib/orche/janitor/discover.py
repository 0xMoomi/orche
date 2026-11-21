"""Janitor discovery: registry units, Orca worktrees/terminals, git state, filesystem walks.

Read-only. Orca listings run even under --dry-run (they mutate nothing); a failed listing
returns None so the rules can skip whatever depends on it.
"""
import fnmatch
import json
import os
import re
import struct
import sys
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
    kind: str = "div"    # div | feat


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
                                   f"{dname}/{feat.get('name', '')}", "feat"))
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
    res = shell.git(root, "status", "--porcelain", timeout=60)
    if not res.ok:
        return None
    return [line for line in res.out.splitlines() if line.strip()]


def git_ignored(root, rel):
    """True if git ignores `rel` (`git check-ignore`: exit 0), False if not (exit 1), None otherwise."""
    res = shell.git(root, "check-ignore", "-q", "--", rel, timeout=60)
    return True if res.code == 0 else False if res.code == 1 else None


def git_ignored_dirs(root):
    """Directories (relative, no trailing `/`) git reports as ignored in `root`, or None if git failed."""
    res = shell.git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", timeout=60)
    if not res.ok:
        return None
    return [ln.rstrip("/") for ln in res.out.splitlines() if ln.endswith("/")]


def git_tracked(root, rel):
    """True if git tracks anything under `rel`, False if not, None if git failed."""
    res = shell.git(root, "ls-files", "--", rel, timeout=60)
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


_FSOPT_NOFOLLOW, _FSOPT_ATTR_CMN_EXTENDED, _ATTR_CMNEXT_PRIVATESIZE = 0x1, 0x20, 0x8
_getattrlist = None


def _private_size(path):
    """darwin: the bytes only this file holds on its volume (APFS clones share theirs), or None."""
    global _getattrlist
    try:
        import ctypes
        import ctypes.util
        if _getattrlist is None:
            _getattrlist = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True).getattrlist
        # struct attrlist: bitmapcount, reserved, common, vol, dir, file, fork (the fifth: extended common)
        attrs = ctypes.create_string_buffer(struct.pack("=HHIIIII", 5, 0, 0, 0, 0, 0, _ATTR_CMNEXT_PRIVATESIZE))
        buf = ctypes.create_string_buffer(12)       # uint32 length, then int64 private size (packed to 4)
        if _getattrlist(os.fsencode(str(path)), attrs, buf, ctypes.sizeof(buf),
                        _FSOPT_NOFOLLOW | _FSOPT_ATTR_CMN_EXTENDED) != 0:
            return None
        return struct.unpack("=Iq", buf.raw)[1]
    except Exception:  # noqa: BLE001 - the fallback counts the file instead
        return None


def reclaim(path) -> int:
    """Bytes deleting the tree at `path` really frees: each regular file once per inode, symlinks not followed.
    darwin: the file's private size (getattrlist); otherwise, or when that fails, its blocks when it has one
    link, else 0 (a hard-linked file frees nothing). Never raises."""
    total, seen = 0, set()
    try:
        walk = [(str(path), [], [""])] if os.path.isfile(path) and not os.path.islink(path) else os.walk(path)
        for dirpath, _dirs, files in walk:
            for name in files:
                full = os.path.join(dirpath, name) if name else dirpath
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not os.path.stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
                own = _private_size(full) if sys.platform == "darwin" else None
                total += own if own is not None else (st.st_blocks * 512 if st.st_nlink == 1 else 0)
    except Exception:  # noqa: BLE001 - never raises: what was counted stands
        pass
    return total


def cache_dirs(root, patterns) -> list:
    """Directories whose own name matches one of the fnmatch `patterns` under `root` (depth <= CACHE_DEPTH;
    a matched dir, other dot-dirs and node_modules are not descended into)."""
    found, root = [], Path(root)
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        keep = []
        for d in dirnames:
            full = Path(dirpath) / d
            if full.is_symlink():
                continue
            if any(fnmatch.fnmatchcase(d, pat) for pat in patterns):
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
