"""Session archives: `ORCHE_HOME/archive/<unit>/sessions-<ts>.tar.gz` (.tar.zst with zstd) + manifest.

Order: tar -> verify (member count and sizes equal the manifest) -> manifest -> GRAVEYARD row
(via `record`) -> delete originals. `restore` writes files back and never overwrites.
"""
import os
import re
import tarfile
from pathlib import Path

from .. import paths, shell, util


class ArchiveError(RuntimeError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def zstd_bin():
    return shell.which("zstd")


def safe_name(name) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name)).strip(".")
    if not safe:
        raise ArchiveError(f"bad unit name: {name!r}")
    return safe


def unit_dir(name) -> Path:
    return paths.orche_home() / "archive" / safe_name(name)


def _arcname(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return "abs/" + path.as_posix().lstrip("/")


def _verify(tar_path, entries, mode) -> bool:
    try:
        with tarfile.open(tar_path, mode) as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
    except (tarfile.TarError, OSError):
        return False
    sizes = {m.name: m.size for m in members}
    return len(members) == len(entries) and all(sizes.get(e["arcname"]) == e["size"] for e in entries)


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _prune_empty_dirs(files, base: Path):
    """Remove emptied subdirectories (e.g. `<slug>/<session>/subagents`), never `<slug>/` itself."""
    dirs = sorted({p for f in files for p in Path(f).parents
                   if base in p.parents and len(p.relative_to(base).parts) >= 2}, key=lambda p: -len(p.parts))
    for d in dirs:
        try:
            d.rmdir()
        except OSError:
            pass


def archive_sessions(name, files, base, *, trigger, reason, project="", now, record) -> dict:
    files, base = [Path(f) for f in files], Path(base)
    if not files:
        raise ArchiveError("nothing to archive")
    entries = [{"path": str(f), "arcname": _arcname(f, base), "size": f.stat().st_size} for f in files]
    dest_dir = unit_dir(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    zstd = zstd_bin()
    stem, n = f"sessions-{stamp}", 1
    while any((dest_dir / f"{stem}{ext}").exists() for ext in (".tar.gz", ".tar.zst", ".manifest.json")):
        n += 1
        stem = f"sessions-{stamp}-{n}"
    tar_path = dest_dir / (f"{stem}.tar" if zstd else f"{stem}.tar.gz")
    with tarfile.open(tar_path, "w" if zstd else "w:gz") as tf:
        for f, e in zip(files, entries):
            tf.add(str(f), arcname=e["arcname"], recursive=False)
    os.chmod(tar_path, 0o600)
    if not _verify(tar_path, entries, "r:" if zstd else "r:gz"):
        _remove(tar_path)
        raise ArchiveError(f"archive verification failed for {name}; originals kept")
    final = tar_path
    if zstd:
        final = dest_dir / f"{stem}.tar.zst"
        made = shell.run([zstd, "-q", "-19", str(tar_path), "-o", str(final)], timeout=600)
        tested = shell.run([zstd, "-q", "-t", str(final)], timeout=600) if made.ok else made
        if not tested.ok:
            _remove(final)
            _remove(tar_path)
            raise ArchiveError(f"zstd failed for {name}: {tested.err.strip()}; originals kept")
        _remove(tar_path)
        os.chmod(final, 0o600)
    manifest = {"unit": name, "archive": str(final), "created": now.isoformat(timespec="seconds"),
                "trigger": trigger, "reason": reason, "project": project, "base": str(base),
                "count": len(entries), "files": entries, "status": "archived"}
    util.write_json(dest_dir / f"{stem}.manifest.json", manifest)
    record(manifest)
    for f in files:
        _remove(f)
    _prune_empty_dirs(files, base)
    return manifest


def manifests(name) -> list:
    out = []
    for path in sorted(unit_dir(name).glob("sessions-*.manifest.json")):
        data = util.read_json(path, default={}) or {}
        out.append((path, data))
    return out


def _open(archive_path: Path, scratch: Path):
    if archive_path.name.endswith(".tar.zst"):
        zstd = zstd_bin()
        if not zstd:
            raise ArchiveError(f"zstd is needed to read {archive_path}")
        res = shell.run([zstd, "-q", "-d", "-f", str(archive_path), "-o", str(scratch)], timeout=600)
        if not res.ok:
            raise ArchiveError(f"zstd -d failed: {res.err.strip()}")
        return tarfile.open(scratch, "r:")
    return tarfile.open(archive_path, "r:gz")


def restore(name, *, dry_run=False) -> list:
    """Write every not-yet-restored archive of `name` back to its original paths."""
    pending = [(p, m) for p, m in manifests(name) if m.get("status") == "archived"]
    if not pending:
        raise ArchiveError(f"no archived sessions for {name}", 2)
    clash = [e["path"] for _, m in pending for e in m.get("files", []) if os.path.lexists(e["path"])]
    if clash:
        raise ArchiveError("refusing to overwrite existing files: " + ", ".join(clash[:5]), 3)
    restored = []
    for mpath, m in pending:
        if dry_run:
            restored += [e["path"] for e in m["files"]]
            continue
        scratch = mpath.with_name(mpath.name + ".restore.tar")
        try:
            with _open(Path(m["archive"]), scratch) as tf:
                for e in m["files"]:
                    member = tf.getmember(e["arcname"])
                    src = tf.extractfile(member)
                    target = Path(e["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, (member.mode & 0o777) or 0o600)
                    with os.fdopen(fd, "wb") as out:
                        while chunk := src.read(1 << 20):
                            out.write(chunk)
                    os.utime(target, (member.mtime, member.mtime))
                    restored.append(str(target))
        finally:
            _remove(scratch)
        m.update(status="restored", restored=util.now_iso())
        util.write_json(mpath, m)
    return restored
