"""Guarded Orche segment appended to `~/.claude/statusline-command.sh`.

The block (templates/statusline-segment.sh) walks up from `current_dir_path` to the first
`.orche/project` marker written by `orche init` and prints `<ORCHE_HOME>/status/<key>.line`
using shell builtins only, silent on any error.
"""
import os
import re
from pathlib import Path

from .. import log, paths, util
from . import (STATE_FILE, StateUnreadable, block_re, has_block, insert_block, load_state, read_text, remove_block,
               write_file)

STEP = "statusline"
START, END = "# >>> orche >>>", "# <<< orche <<<"


def script_path(ctx):
    return ctx.claude / "statusline-command.sh"


def block(ctx) -> str:
    return _segment(ctx.plugin_root)


def _segment(plugin_root) -> str:
    text = read_text(Path(plugin_root) / "templates" / "statusline-segment.sh")
    return text if text.endswith("\n") else text + "\n"


def _final_exit(text):
    """Offset of the last non-empty, non-comment line when it starts with `exit`, else None."""
    lines = list(re.finditer(r"^.*$", text, re.MULTILINE))
    for m in reversed(lines):
        stripped = m.group(0).strip()
        if not stripped or stripped.startswith("#"):
            continue
        return m.start() if re.match(r"exit\b", stripped) else None
    return None


def after_exit(text) -> bool:
    """True when the Orche block sits after a final `exit …` line (so it never runs)."""
    m = block_re(START, END).search(text)
    return bool(m) and _final_exit(text[:m.start()]) is not None


def place(text, blk) -> str:
    """Insert/replace the block; a script ending in `exit …` gets it just before that line.

    `remove_block` still restores the text exactly (the block carries its own leading newline).
    """
    if has_block(text, START, END) and not after_exit(text):
        return insert_block(text, blk, START, END)
    text = remove_block(text, START, END)
    pos = _final_exit(text)
    if pos is None:
        return insert_block(text, blk, START, END)
    return text[:pos] + "\n" + blk + text[pos:]


def install(ctx) -> None:
    path = script_path(ctx)
    if not path.is_file():
        ctx.note(STEP, "skip", path, "not found; point statusLine at a script, then re-run orche install")
        return
    write_file(ctx, STEP, path, place(read_text(path), block(ctx)), "Orche segment block")


def uninstall(ctx) -> None:
    path = script_path(ctx)
    if not path.is_file() or not has_block(read_text(path), START, END):
        ctx.note(STEP, "ok", path, "no Orche block")
        return
    ctx.note(STEP, "remove", None, "Orche segment block")
    write_file(ctx, STEP, path, remove_block(read_text(path), START, END), "Orche block removed")


def present(ctx) -> bool:
    path = script_path(ctx)
    try:
        return path.is_file() and has_block(read_text(path), START, END)
    except OSError:
        return False


def heartbeat_target():
    """The script the heartbeat keeps the block in, or None: only when Orche is installed under this
    ORCHE_HOME (install-state.json readable) and ORCHE_HOME's parent, the Claude folder of the default
    `<home>/.claude/orche`, holds `statusline-command.sh`. Never `Path.home()`: a temp ORCHE_HOME (tests)
    can never reach the real `~/.claude`."""
    home = paths.orche_home()
    if not (home / STATE_FILE).is_file():
        return None
    try:
        load_state()
    except StateUnreadable:
        return None
    path = home.parent / "statusline-command.sh"
    return path if path.is_file() else None


def heartbeat(dry_run) -> list:
    """The heartbeat's class `statusline` (reconcile): another tool (a usage menu bar app) rewrote the
    script and dropped the Orche block -> the block goes back in, placed as `place` does, written
    atomically without a backup. Only ever adds a missing block. One row as janitor's, or none."""
    try:
        path = heartbeat_target()
        if path is None:
            return []
        text = read_text(path)
    except Exception as exc:  # noqa: BLE001 - an unreadable state or script never stops the pass
        log.log("hooks", f"reconcile: statusline: {type(exc).__name__}: {exc}")
        return []
    if has_block(text, START, END):
        return []
    failed = None
    if not dry_run:
        try:
            util.atomic_write(os.path.realpath(path), place(text, _segment(paths.PLUGIN_ROOT)))
        except Exception as exc:  # noqa: BLE001 - tried again next pass
            failed = type(exc).__name__
            log.log("hooks", f"reconcile: statusline: {failed}: {exc}")
    return [{"action": "statusline-repair", "class": "statusline", "project": None, "target": str(path),
             "rev": None, "attempt": 1, "cap": 1, "failed": failed,
             "line": f"{'would put' if dry_run else 'put'} the Orche block back into {path} "
                     "(another tool rewrote the file)"}]
