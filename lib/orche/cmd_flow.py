"""`orche flow pause <proj> [--reason TEXT]` and `orche flow resume <proj>` (G8, lib/orche/flow/guard.py).

A paused project relays no brief or AMEND (STOP still works) and the heartbeat repairs nothing in it but
drains. Resume needs the owner's word: it removes the pause only when the owner prompted Main after it.
"""
from . import registry, roster
from .cli import EXIT_BLOCKED, EXIT_NOT_FOUND, EXIT_OK, dry, fail, out
from .flow import guard


def register(subparsers):
    p = subparsers.add_parser("flow", help="pause or resume a project's relays (the owner's brake)")
    sub = p.add_subparsers(dest="flow_cmd", required=True)
    q = sub.add_parser("pause", help="relay no brief or AMEND for the project until the owner resumes it")
    q.add_argument("project")
    q.add_argument("--reason", default="", help="shown in `orche status` and the refusal")
    q.set_defaults(func=cmd_pause)
    q = sub.add_parser("resume", help="lift the pause (only after the owner spoke in Main)")
    q.add_argument("project")
    q.set_defaults(func=cmd_resume)


def _key(name):
    found = registry.project(registry.load(), name)
    return found[0] if found else None


def cmd_pause(args):
    key = _key(args.project)
    if key is None:
        return fail(EXIT_NOT_FOUND, f"unknown project: {args.project}")
    if args.dry_run:
        dry(f"pause {key}")
        return EXIT_OK
    data = guard.pause(key, roster.actor(), args.reason)
    out(args, dict(data, project=key), [f"paused {key}: no brief or AMEND is relayed until the owner's word "
                                        f"(orche flow resume {key}); STOP still works"])
    return EXIT_OK


def cmd_resume(args):
    key = _key(args.project)
    if key is None:
        return fail(EXIT_NOT_FOUND, f"unknown project: {args.project}")
    if args.dry_run:
        dry(f"resume {key}")
        return EXIT_OK
    ok, at = guard.resume(key, roster.actor())
    if not ok:
        return fail(EXIT_BLOCKED, f"resume needs the owner's word after {at.astimezone():%H:%M}")
    out(args, {"project": key, "paused": False}, [f"resumed {key}"])
    return EXIT_OK
