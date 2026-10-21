"""`orche decide <phase> <decision> <why> <evidence> <result> [--file PATH]` — append to DECISIONS.tsv.

The file is the calling member's (ORCHE_SESSION, else the unit whose worktree holds cwd), in its
memory dir: `.orche/` (Main), `<worktree>/.orche/<div|feat>/<name>/` (division, feature). With no member, pass --file.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

from . import registry, roster, util

EXIT_OK, EXIT_USER = 0, 1
COLUMNS = ("ts", "phase", "decision", "why", "evidence", "result")
HEADER = "\t".join(COLUMNS)
_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " "})


def register(subparsers):
    p = subparsers.add_parser("decide", help="append a row to the unit's DECISIONS.tsv",
                              epilog="Put `--` before the five values if any starts with '-'.")
    for name in COLUMNS[1:]:
        p.add_argument(name)
    p.add_argument("--file", help="DECISIONS.tsv to append to (default: the calling member's memory dir)")
    p.set_defaults(func=cmd_decide)


def escape(value) -> str:
    """Single-line cell; a leading = + - @ gets a `'` so spreadsheets do not run it as a formula."""
    text = str(value).translate(_BREAKS)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def row(phase, decision, why, evidence, result, ts=None) -> str:
    ts = ts or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\t".join([ts] + [escape(v) for v in (phase, decision, why, evidence, result)])


def resolve_file(r: roster.Roster, cwd, env=None) -> Path | None:
    """The member's DECISIONS.tsv; None when no registered member is identified."""
    who = r.identify(cwd, env)
    return who.member.memory / "DECISIONS.tsv" if who.member else None


def append(path: Path, line: str) -> None:
    """One O_APPEND write per row (header created atomically first)."""
    util.append_row(path, HEADER, line)


def cmd_decide(args):
    if args.file:
        path = Path(args.file).expanduser()
    else:
        path = resolve_file(roster.Roster(registry.load()), os.getcwd())
        if path is None:
            print("orche: cannot tell whose DECISIONS.tsv this is: set ORCHE_SESSION (ORCHE panes set it)"
                  " or pass --file", file=sys.stderr)
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
