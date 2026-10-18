"""HR: onboarding of units (`onboarding`), creation limits (`limits`), the weekly audit (`audit`).

Shared CLI glue for `cmd_init`, `cmd_feat`, `cmd_stage` and `cmd_hr` lives here.
"""
import json
import sys

from .. import paths, statusline

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3
PLUGIN_ROOT = paths.PLUGIN_ROOT


def out(args, data, lines) -> None:
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for line in [lines] if isinstance(lines, str) else lines:
            print(line)


def fail(code, msg) -> int:
    print(f"orche: {msg}", file=sys.stderr)
    return code


def warn(msg) -> None:
    print(f"orche: warning: {msg}", file=sys.stderr)


def dry(msg) -> None:
    """A non-Orca dry-run action, printed like orca's `[dry-run] <argv>` lines."""
    print(f"[dry-run] {msg}", file=sys.stderr)


def refresh_status_line(reg, key) -> None:
    """Rewrite ORCHE_HOME/status/<proj>.line after a registry change (statusline.refresh)."""
    statusline.refresh(reg, key)
