"""`orche decide <phase> <decision> <why> <evidence> <result> [--file PATH]` — append to DECISIONS.tsv.

The file is the current unit's: a division worktree writes `.orche/div/<name>/DECISIONS.tsv`, a
feature worktree `.orche/feat/<name>/DECISIONS.tsv`, the project's main checkout
`.orche/DECISIONS.tsv`. `bin/log.sh` is the bash equivalent and must produce identical rows.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

from . import registry, units, util

EXIT_OK, EXIT_USER = 0, 1
COLUMNS = ("ts", "phase", "decision", "why", "evidence", "result")
HEADER = "\t".join(COLUMNS)
_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " "})


def register(subparsers):
    p = subparsers.add_parser("decide", help="append a row to the unit's DECISIONS.tsv",
                              epilog="Put `--` before the five values if any starts with '-'.")
    for name in COLUMNS[1:]:
        p.add_argument(name)
    p.add_argument("--file", help="DECISIONS.tsv to append to (default: resolved from cwd)")
    p.set_defaults(func=cmd_decide)


def escape(value) -> str:
    """Single-line cell; a leading = + - @ gets a `'` so spreadsheets do not run it as a formula."""
    text = str(value).translate(_BREAKS)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def row(phase, decision, why, evidence, result, ts=None) -> str:
    ts = ts or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\t".join([ts] + [escape(v) for v in (phase, decision, why, evidence, result)])


def resolve_file(reg, cwd) -> Path | None:
    unit = units.unit_from_cwd(reg, cwd)
    if unit is not None:
        return unit.worktree_path / ".orche" / unit.kind / unit.name / "DECISIONS.tsv"
    for key in reg.get("projects", {}):
        root = units.project_root(reg, key)
        if root and units.is_within(cwd, root):
            return root / ".orche" / "DECISIONS.tsv"
    return None


def append(path: Path, line: str) -> None:
    """One O_APPEND write per row (header created atomically first)."""
    util.append_row(path, HEADER, line)


def cmd_decide(args):
    if args.file:
        path = Path(args.file).expanduser()
    else:
        path = resolve_file(registry.load(), os.getcwd())
        if path is None:
            print("orche: cwd is not inside a registered unit or project; pass --file", file=sys.stderr)
            return EXIT_USER
    line = row(args.phase, args.decision, args.why, args.evidence, args.result)
    if not args.dry_run:
        append(path, line)
    if args.json:
        print(json.dumps({"path": str(path), "dry_run": bool(args.dry_run),
                          "row": dict(zip(COLUMNS, line.split("\t")))}, indent=2, ensure_ascii=False))
    else:
        print(line)
    return EXIT_OK
