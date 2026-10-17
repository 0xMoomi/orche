"""Agent symlinks in `~/.claude/agents/` and the Fable runner model flip.

Only symlinks that point into `<plugin root>/agents/` are ours: anything else with one of our
names (a regular file, or a symlink elsewhere) is skipped and reported, never overwritten.
"""
import os
import re
from pathlib import Path

from . import read_text, write_file

STEP = "agents"
RUNNER_STEP = "runner"
RUNNER_MODEL = "claude-haiku-5-5"
OLD_RUNNER = re.compile(r"^(model:[ \t]*)(claude-haiku-4-5[\w.-]*)[ \t]*$", re.MULTILINE)


def link_target(link) -> str:
    """Where a symlink points, absolute and normalised (without resolving further links)."""
    target = os.readlink(link)
    return os.path.normpath(os.path.join(os.path.dirname(link), target))


def points_into(link, directory) -> bool:
    try:
        target = link_target(link)
    except OSError:
        return False
    base = os.path.normpath(str(directory))
    return target.startswith(base + os.sep) or os.path.realpath(target).startswith(
        os.path.realpath(base) + os.sep)


def install(ctx) -> None:
    src_dir = ctx.plugin_root / "agents"
    dest_dir = ctx.claude / "agents"
    for src in sorted(src_dir.glob("*.md")):
        dest = dest_dir / src.name
        if dest.is_symlink():
            if os.path.realpath(dest) == os.path.realpath(src):
                ctx.note(STEP, "ok", dest, "already linked")
                continue
            if points_into(dest, src_dir):
                if not ctx.dry_run:
                    dest.unlink()
                    dest.symlink_to(src)
                ctx.note(STEP, "relink", dest, f"-> {src}")
                continue
            ctx.note(STEP, "skip", dest, f"existing symlink to {link_target(dest)}, not ORCHE's; left untouched")
        elif dest.exists():
            ctx.note(STEP, "skip", dest, "existing regular file; left untouched")
        else:
            if not ctx.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(src)
            ctx.note(STEP, "symlink", dest, f"-> {src}")


def uninstall(ctx, state) -> None:
    dest_dir = ctx.claude / "agents"
    roots = {ctx.plugin_root / "agents"}
    if state.get("plugin_root"):
        roots.add(Path(state["plugin_root"]) / "agents")
    found = False
    for dest in sorted(dest_dir.glob("*.md")) if dest_dir.is_dir() else ():
        if dest.is_symlink() and any(points_into(dest, root) for root in roots):
            found = True
            target = link_target(dest)
            if not ctx.dry_run:
                dest.unlink()
            ctx.note(STEP, "remove", dest, f"symlink -> {target}")
    if not found:
        ctx.note(STEP, "ok", dest_dir, "no ORCHE agent symlinks")


def flip_runner(ctx) -> None:
    path = ctx.claude / "fable" / "agents" / "runner.md"
    if not path.is_file():
        ctx.note(RUNNER_STEP, "skip", path, "not found")
        return
    text = read_text(path)
    end = text.find("\n---", 4) if text.startswith("---") else -1
    if end < 0:
        ctx.note(RUNNER_STEP, "skip", path, "no frontmatter")
        return
    front = text[:end]
    match = OLD_RUNNER.search(front)
    if not match:
        current = re.search(r"^model:[ \t]*(\S+)", front, re.MULTILINE)
        ctx.note(RUNNER_STEP, "ok", path, f"model {current.group(1) if current else '(none)'}; unchanged")
        return
    new = OLD_RUNNER.sub(lambda m: m.group(1) + RUNNER_MODEL, front, count=1) + text[end:]
    write_file(ctx, RUNNER_STEP, path, new, f"model {match.group(2)} -> {RUNNER_MODEL}")
