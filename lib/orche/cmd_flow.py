"""`orche flow pause <proj> [--reason TEXT]`, `orche flow resume <proj>` (G8, lib/orche/flow/guard.py) and
`orche flow brief on|off` (config `flow.daily_brief`, the daily brief: lib/orche/flow/daily.py).

A paused project relays no brief or AMEND (STOP still works) and the heartbeat repairs nothing in it but
drains. Resume needs the owner's word: it removes the pause only when the owner prompted Main after it.
"""
from . import config, registry, roster, statusline, util
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
    q = sub.add_parser("brief", help="the daily brief Main gives after quiet hours: on (the default) or off")
    q.add_argument("state", choices=("on", "off"))
    q.set_defaults(func=cmd_brief)


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
    statusline.touch(key)       # PAUSED in the status line
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
    statusline.touch(key)
    out(args, {"project": key, "paused": False}, [f"resumed {key}"])
    return EXIT_OK


def cmd_brief(args):
    """Set `flow.daily_brief` in ORCHE_HOME/config.json (only the file's own keys are saved)."""
    try:
        raw = util.read_json(config.path(), default={}) or {}
    except util.JsonError as exc:
        return fail(EXIT_BLOCKED, f"cannot read config: {exc}")
    value = args.state == "on"
    if not isinstance(raw.get("flow"), dict):
        raw["flow"] = {}
    raw["flow"]["daily_brief"] = value
    if args.dry_run:
        dry(f"set flow.daily_brief={'true' if value else 'false'}")
        return EXIT_OK
    config.save(raw)
    out(args, {"daily_brief": value}, [f"daily brief {'on: Main opens each day after quiet hours' if value else 'off'}"])
    return EXIT_OK
