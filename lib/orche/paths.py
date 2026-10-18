"""Filesystem locations: instance state root and project-local state."""
import os
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]   # the plugin checkout: bin/, hooks/, lib/, templates/
SUBDIRS =("logs", "status", "inbox", "cards", "secrets", "briefs", "jev-eval", "archive", "trash")


def orche_home() -> Path:
    """`$ORCHE_HOME` if set, else `~/.claude/orche`. Resolved on every call."""
    env = os.environ.get("ORCHE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "orche"


def ensure_dirs() -> Path:
    home = orche_home()
    for name in SUBDIRS:
        (home / name).mkdir(parents=True, exist_ok=True)
    return home


def project_local(repo_root) -> Path:
    return Path(repo_root) / ".orche"
