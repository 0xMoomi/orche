"""Trash: `ORCHE_HOME/trash/<yyyy-mm-dd>/<id>/<relative path>` + `manifest.json`; purge after 30 days.

Order for every move: manifest (pending) -> GRAVEYARD row (via `record`) -> move -> manifest (trashed).
"""
import datetime as _dt
import secrets
import shutil
from pathlib import Path

from .. import paths, util

MANIFEST = "manifest.json"


class TrashError(RuntimeError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def root() -> Path:
    return paths.orche_home() / "trash"


def new_id(now) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _relative(path: Path, base) -> Path:
    if base:
        try:
            rel = path.relative_to(base)
            if rel.parts:
                return rel
        except ValueError:
            pass
    return Path(*path.parts[1:])


def move(path, base, *, trigger, reason, project="", size=0, now, record) -> dict:
    """Move `path` into the trash. `record(manifest)` runs after the manifest, before the move."""
    src = Path(path)
    if not (src.exists() or src.is_symlink()):
        raise TrashError(f"missing: {src}", 2)
    rel = _relative(src, base)
    if rel.parts[0] == MANIFEST:
        raise TrashError(f"cannot trash a top-level {MANIFEST}: {src}")
    tid = new_id(now)
    item_dir = root() / now.strftime("%Y-%m-%d") / tid
    dest = item_dir / rel
    manifest = {"id": tid, "path": str(src), "stored": str(dest), "size": size, "reason": reason,
                "trigger": trigger, "project": project, "created": now.isoformat(timespec="seconds"),
                "status": "pending"}
    util.write_json(item_dir / MANIFEST, manifest)
    record(manifest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except Exception as exc:
        manifest.update(status="failed", error=str(exc))
        util.write_json(item_dir / MANIFEST, manifest)
        raise
    manifest["status"] = "trashed"
    util.write_json(item_dir / MANIFEST, manifest)
    return manifest


def find(tid):
    """(item dir, manifest) for a trash id, or None."""
    if not tid or "/" in tid or tid.startswith("."):
        return None
    for day in sorted(root().glob("*")):
        candidate = day / tid / MANIFEST
        if candidate.is_file():
            return candidate.parent, util.read_json(candidate, default={})
    return None


def undo(tid, *, dry_run=False) -> dict:
    found = find(tid)
    if found is None:
        raise TrashError(f"unknown trash id: {tid}", 2)
    item_dir, manifest = found
    if manifest.get("status") != "trashed":
        raise TrashError(f"{tid} is {manifest.get('status')}, not trashed", 3)
    original, stored = Path(manifest["path"]), Path(manifest["stored"])
    if original.exists() or original.is_symlink():
        raise TrashError(f"refusing to overwrite existing {original}", 3)
    if not (stored.exists() or stored.is_symlink()):
        raise TrashError(f"trashed copy is missing: {stored}", 1)
    if dry_run:
        return manifest
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stored), str(original))
    manifest.update(status="restored", restored=util.now_iso())
    util.write_json(item_dir / MANIFEST, manifest)
    return manifest


def purge_due(name, today, days) -> bool:
    """Whether a trash day directory named `name` (YYYY-MM-DD) is older than `days` days."""
    try:
        return (today - _dt.date.fromisoformat(name)).days > days
    except ValueError:
        return False


def due_dirs(today, days) -> list:
    """Trash day directories older than `days` days."""
    return [day for day in sorted(root().glob("*"))
            if purge_due(day.name, today, days) and day.is_dir() and not day.is_symlink()]


def purge(day_dir, *, record) -> int:
    """Remove one trash day directory for good. `record(count)` runs first."""
    day = Path(day_dir)
    if day.parent != root() or day.is_symlink():
        raise TrashError(f"not a trash day directory: {day}")
    count = sum(1 for d in day.iterdir() if d.is_dir())
    record(count)
    shutil.rmtree(day)
    return count
