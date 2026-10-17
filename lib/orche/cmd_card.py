"""`orche card <proj> [--path]` — print a project's card (PROJECT_CARD.md) or its path."""
import json
import sys
from pathlib import Path

from . import registry

EXIT_OK, EXIT_NOT_FOUND = 0, 2


def register(subparsers):
    p = subparsers.add_parser("card", help="print a project's card")
    p.add_argument("project", help="project key or alias")
    p.add_argument("--path", action="store_true", help="print the card's path instead of its text")
    p.set_defaults(func=cmd_card)


def cmd_card(args):
    found = registry.project(registry.load(), args.project)
    if found is None:
        print(f"orche: unknown project: {args.project}", file=sys.stderr)
        return EXIT_NOT_FOUND
    key, proj = found
    path = Path(proj["card"]).expanduser()
    if args.path:
        print(json.dumps({"project": key, "path": str(path)}) if args.json else path)
        return EXIT_OK
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        print(f"orche: card not found: {path}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.json:
        print(json.dumps({"project": key, "path": str(path), "text": text}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
    return EXIT_OK
