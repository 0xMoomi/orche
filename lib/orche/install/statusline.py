"""Guarded ORCHE segment appended to `~/.claude/statusline-command.sh`.

The block (templates/statusline-segment.sh) walks up from `current_dir_path` to the first
`.orche/project` marker written by `orche init` and prints `<ORCHE_HOME>/status/<key>.line`
using shell builtins only, silent on any error.
"""
import re

from . import block_re, has_block, insert_block, read_text, remove_block, write_file

STEP = "statusline"
START, END = "# >>> orche >>>", "# <<< orche <<<"


def script_path(ctx):
    return ctx.claude / "statusline-command.sh"


def block(ctx) -> str:
    text = read_text(ctx.plugin_root / "templates" / "statusline-segment.sh")
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
    """True when the ORCHE block sits after a final `exit …` line (so it never runs)."""
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
    write_file(ctx, STEP, path, place(read_text(path), block(ctx)), "ORCHE segment block")


def uninstall(ctx) -> None:
    path = script_path(ctx)
    if not path.is_file() or not has_block(read_text(path), START, END):
        ctx.note(STEP, "ok", path, "no ORCHE block")
        return
    ctx.note(STEP, "remove", None, "ORCHE segment block")
    write_file(ctx, STEP, path, remove_block(read_text(path), START, END), "ORCHE block removed")


def present(ctx) -> bool:
    path = script_path(ctx)
    try:
        return path.is_file() and has_block(read_text(path), START, END)
    except OSError:
        return False
