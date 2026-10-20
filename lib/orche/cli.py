"""Shared CLI glue: exit codes and the stdout/stderr line helpers; stdlib only."""
import json
import sys

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3


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
