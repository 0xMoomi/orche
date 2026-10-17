"""`orche discard <path|session|unit> --why TEXT` — record a discard decision as a GRAVEYARD row.

Nothing is removed here: the row carries a `path:` / `session:` pointer that the next
`orche janitor run` acts on (trash or archive), recording its own row when it does.
"""
import json
import os
import sys

from pathlib import Path

from . import janitor, registry, units, util
from .janitor import graveyard, rules

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3


def register(subparsers):
    p = subparsers.add_parser("discard", help="mark a path, session or retired unit for the janitor (GRAVEYARD row)")
    p.add_argument("target", help="a path, a session name, or a retired unit's session name")
    p.add_argument("--why", required=True, help="why it is discarded")
    p.add_argument("--by", default=None, help="deciding session (default $ORCHE_SESSION or user)")
    p.add_argument("--project", default=None, help="registry project (default: from the target or cwd)")
    p.set_defaults(func=cmd_discard)


def _fail(code, msg):
    print(f"orche: {msg}", file=sys.stderr)
    return code


def _resolve_project(reg, explicit, where):
    if explicit:
        found = registry.project(reg, explicit)
        return found[0] if found else None
    return units.project_from_cwd(reg, where)


def _main_transcript_project(reg, session):
    """Project key when `<claude_projects>/*/<session>.jsonl` starts in a registered main checkout."""
    from .janitor import discover
    root = janitor.configured_dir(janitor.settings()["claude_projects"])
    if not root or not discover.valid_session_name(session):
        return None
    recs = discover.unit_records(reg)
    for path in sorted(Path(root).glob(f"*/{session}.jsonl")):
        cwd = discover.first_cwd(path)
        if cwd and rules.main_checkout_cwd(reg, recs, cwd):
            return units.project_from_cwd(reg, cwd) or "a registered project"
    return None


def cmd_discard(args):
    reg = registry.load()
    target = args.target
    explicit_session = None
    unit = units.find_unit(reg, target)
    if unit is not None:
        if unit.entry.get("status") != "retired":
            return _fail(EXIT_BLOCKED, f"{target} is {unit.entry.get('status')}; retire the unit first")
        key, ptr = unit.project, f"session:{target}"
    elif os.path.lexists(target):
        path = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(target))),
                            os.path.basename(os.path.abspath(target)))
        key = _resolve_project(reg, args.project, path)
        if key is None:
            return _fail(EXIT_USER, f"cannot tell which project owns {path}; pass --project")
        root = units.project_root(reg, key)
        if rules.is_protected(path, root if root and units.is_within(path, root) else None):
            return _fail(EXIT_BLOCKED, f"{path} is protected (.env*, memory/, registry, cards, ledgers)")
        rel = os.path.relpath(path, root) if root and units.is_within(path, root) else path
        key, ptr = key, f"path:{rel}"
    else:
        if target in units.all_sessions(reg):
            return _fail(EXIT_BLOCKED, f"{target} is a registered project session")
        if any(ch.isspace() for ch in target) or "/" in target:
            return _fail(EXIT_NOT_FOUND, f"no such path or session: {target}")
        owner = _main_transcript_project(reg, target)
        if owner:
            return _fail(EXIT_BLOCKED, f"{target} is a Main/Liaison transcript (its cwd is {owner}'s main "
                                       "checkout); refusing")
        explicit_session = target
        key = _resolve_project(reg, args.project, os.getcwd())
        if key is None:
            return _fail(EXIT_USER, "cannot tell the project; pass --project")
        ptr = f"session:{target}"
    if any(ch.isspace() for ch in ptr):
        return _fail(EXIT_USER, f"pointer must not contain whitespace: {ptr}")
    root = units.project_root(reg, key)
    if root is None or not root.is_dir():
        return _fail(EXIT_NOT_FOUND, f"project {key} has no checkout on disk")
    actor = args.by or units.actor()
    recovery = "pending: next `orche janitor run`"
    line = graveyard.row(units.now().date().isoformat(), ptr, args.why, recovery, actor)
    if not args.dry_run:
        graveyard.record(root, ptr, args.why, recovery, actor, project=key, date=units.now().date())
        if explicit_session:
            state = janitor.load_state()
            state.setdefault("discards", {})[explicit_session] = util.now_iso()
            janitor.save_state(state)
    data = {"project": key, "pointer": ptr, "row": line, "file": str(graveyard.project_file(root)),
            "dry_run": args.dry_run}
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(("would append " if args.dry_run else "appended ") + f"to {data['file']}:\n{line}")
    return EXIT_OK
