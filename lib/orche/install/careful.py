"""The gstack `careful` PreToolUse hook: registered for Bash only when its script exists."""
from pathlib import Path

MARKER = "careful/bin/check-careful.sh"


def script(home) -> Path:
    return Path(home) / ".claude" / "skills" / "gstack" / "careful" / "bin" / "check-careful.sh"


def command(home) -> str:
    return f'bash "{script(home)}"'


def is_careful(cmd) -> bool:
    """Any registration of the careful script (ours or gstack's own `bash -c "exec bash ..."` form)."""
    return MARKER in str(cmd)
