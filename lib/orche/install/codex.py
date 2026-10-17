"""Restore `~/.codex/AGENTS.md` from `AGENTS.md.recovered` (only with `--yes`, never replacing a file)."""
import os

STEP = "codex"


def install(ctx) -> None:
    agents = ctx.home / ".codex" / "AGENTS.md"
    recovered = ctx.home / ".codex" / "AGENTS.md.recovered"
    if agents.exists():
        ctx.note(STEP, "ok", agents, "present")
    elif not recovered.is_file():
        ctx.note(STEP, "skip", agents, "missing and no AGENTS.md.recovered to restore")
    elif ctx.dry_run:
        ctx.note(STEP, "move", recovered, f"-> {agents}" + ("" if ctx.yes else "; needs --yes"))
    elif not ctx.yes:
        ctx.note(STEP, "pending", recovered, f"re-run with --yes to move it to {agents}")
    else:
        # No-clobber move: a hard link fails if AGENTS.md appeared meanwhile (rename would replace it).
        try:
            if agents.exists() or agents.is_symlink():
                raise FileExistsError(str(agents))
            os.link(recovered, agents)
        except FileExistsError:
            ctx.note(STEP, "error", agents, "appeared before the move; refusing to replace it")
            return
        recovered.unlink()
        ctx.note(STEP, "move", recovered, f"-> {agents}")
