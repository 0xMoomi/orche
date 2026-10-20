"""Skill symlinks in `~/.claude/skills/` for a settings-mode install.

A settings-mode install loads no plugin, so each `<plugin root>/skills/<name>/` that holds a
`SKILL.md` is linked as `~/.claude/skills/<name>` (a shared folder without one is not a skill); a plugin-mode install (the plugin ships the skills) and uninstall remove
those links. Only symlinks that point into `<plugin root>/skills/` are ours: anything else with one
of our names (a directory, a file, or a symlink elsewhere) is skipped and reported, never touched.
"""
import os
from pathlib import Path

from .agents import link_target, points_into

STEP = "skills"


def install(ctx, mode) -> None:
    """Link every plugin skill dir with a SKILL.md (settings mode) or remove our links (plugin mode)."""
    if mode != "settings":
        _remove(ctx, {ctx.plugin_root / "skills"}, "plugin mode: the plugin ships the skills", quiet=True)
        return
    src_dir = ctx.plugin_root / "skills"
    dest_dir = ctx.claude / "skills"
    skills = (p for p in src_dir.iterdir() if (p / "SKILL.md").is_file()) if src_dir.is_dir() else ()
    for src in sorted(skills):
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
            ctx.note(STEP, "skip", dest, "existing entry, not ORCHE's; left untouched")
        else:
            if not ctx.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(src)
            ctx.note(STEP, "symlink", dest, f"-> {src}")


def uninstall(ctx, state) -> None:
    roots = {ctx.plugin_root / "skills"}
    if state.get("plugin_root"):
        roots.add(Path(state["plugin_root"]) / "skills")
    _remove(ctx, roots, None, quiet=False)


def _remove(ctx, roots, why, *, quiet) -> None:
    """Remove every `~/.claude/skills/` symlink that points into one of `roots`."""
    dest_dir = ctx.claude / "skills"
    found = False
    for dest in sorted(dest_dir.iterdir()) if dest_dir.is_dir() else ():
        if dest.is_symlink() and any(points_into(dest, root) for root in roots):
            found = True
            target = link_target(dest)
            if not ctx.dry_run:
                dest.unlink()
            ctx.note(STEP, "remove", dest, f"symlink -> {target}" + (f"; {why}" if why else ""))
    if not found and not quiet:
        ctx.note(STEP, "ok", dest_dir, "no ORCHE skill symlinks")
