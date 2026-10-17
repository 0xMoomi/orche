"""Building blocks for `orche install` / `uninstall`: every write into the user's home is
backup-first (`<file>.bak-<ts>` next to the original), guarded (marker-delimited blocks),
idempotent (a second run changes nothing) and fully previewed under `--dry-run`.

Each step records actions on a `Ctx`; the command prints them one line per fact.
"""
import datetime as _dt
import os
import re
import shutil
from pathlib import Path

from .. import util
from ..hr import PLUGIN_ROOT

MIGRATION_LINE = ("Restart any main-/liaison-/div-/feat- panes opened before this version: ORCHE now "
                  "identifies sessions by the ORCHE_SESSION variable its pane commands set.")
MUTATING = {"patch", "create", "backup", "symlink", "relink", "move", "remove", "set", "add", "replace", "write", "restore"}


class Ctx:
    """One install/uninstall run: target home, flags, timestamp, and the recorded actions."""

    def __init__(self, home, *, dry_run=False, yes=False, plugin_root=PLUGIN_ROOT, ts=None):
        self.home = Path(home)
        self.claude = self.home / ".claude"
        self.dry_run = dry_run
        self.yes = yes
        self.plugin_root = Path(plugin_root)
        self.ts = ts or _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        self.actions = []
        self.errors = 0

    def note(self, step, action, path=None, detail=""):
        if action == "error":
            self.errors += 1
        self.actions.append({"step": step, "action": action, "path": str(path) if path else None,
                             "detail": detail, "dry_run": self.dry_run and action in MUTATING})

    def lines(self) -> list:
        out = []
        for act in self.actions:
            verb = f"would {act['action']}" if act["dry_run"] else act["action"]
            text = f"{act['step']}: {verb}"
            if act["path"]:
                text += f" {act['path']}"
            if act["detail"]:
                text += f" ({act['detail']})"
            out.append(text)
        return out


def read_text(path) -> str:
    """File text with line endings preserved."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def backup(ctx, step, path) -> Path:
    """Copy `path` to `<path>.bak-<ts>` (suffix `-N` on a clash); preview only under dry-run."""
    path = Path(path)
    dest = path.with_name(f"{path.name}.bak-{ctx.ts}")
    n = 1
    while dest.exists() or dest.is_symlink():
        dest = path.with_name(f"{path.name}.bak-{ctx.ts}-{n}")
        n += 1
    if not ctx.dry_run:
        shutil.copy2(os.path.realpath(path), dest)
    ctx.note(step, "backup", path, f"-> {dest.name}")
    return dest


def write_file(ctx, step, path, new_text, detail="") -> bool:
    """Write `new_text` to `path` (through a symlink to its target) after a backup.

    Returns True when the file changed (or would change under dry-run).
    """
    path = Path(path)
    exists = path.exists()
    if exists and read_text(path) == new_text:
        ctx.note(step, "ok", path, "unchanged")
        return False
    if exists:
        backup(ctx, step, path)
    if not ctx.dry_run:
        util.atomic_write(os.path.realpath(path) if exists else path, new_text)
    ctx.note(step, "patch" if exists else "create", path, detail)
    return True


def _block_re(start, end):
    return re.compile(r"(?:\n|\A)" + re.escape(start) + r"\n.*?" + re.escape(end) + r"[^\n]*(?:\n|\Z)",
                      re.DOTALL)


def has_block(text, start, end) -> bool:
    return bool(_block_re(start, end).search(text))


def insert_block(text, block, start, end) -> str:
    """Append `block` (markers included, ending in a newline) preceded by one newline, or replace
    the existing block in place. Extra copies are dropped. `remove_block` restores the text exactly.
    """
    pattern = _block_re(start, end)
    found = list(pattern.finditer(text))
    if not found:
        return f"{text}\n{block}"
    first = found[0]
    lead = "\n" if first.group(0).startswith("\n") else ""
    out, pos = [], 0
    for i, m in enumerate(found):
        out.append(text[pos:m.start()])
        if i == 0:
            out.append(lead + block)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def remove_block(text, start, end) -> str:
    return _block_re(start, end).sub("", text)
