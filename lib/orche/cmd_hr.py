"""`orche hr log|freeze|unfreeze|audit|correct`.

audit:   weekly HR report (hr/audit.py); `--apply` sleeps the proposed units, `--quiet` prints
         nothing when there is nothing to propose.
correct: append the next numbered standing order to `<repo>/.orche/STANDING.md` (R-STANDING-1).
"""
import json

from . import hr, registry
from .cli import EXIT_NOT_FOUND, EXIT_OK, fail


def register(subparsers):
    hr = subparsers.add_parser("hr", help="HR log and creation freeze switch")
    sub = hr.add_subparsers(dest="hr_command", metavar="<hr-command>", required=True)
    p = sub.add_parser("log", help="print the last lines of hr-log.md")
    p.add_argument("--tail", type=int, default=20, metavar="N", help="number of lines (default 20)")
    p.set_defaults(func=cmd_log)
    sub.add_parser("freeze", help="stop feature-agent creation").set_defaults(func=cmd_freeze)
    sub.add_parser("unfreeze", help="allow feature-agent creation again").set_defaults(func=cmd_unfreeze)
    p = sub.add_parser("audit", help="weekly HR report: idle units, awake limit, overlaps, corrections")
    p.add_argument("--apply", action="store_true", help="sleep the proposed units")
    p.add_argument("--quiet", action="store_true", help="print nothing when there is nothing to propose")
    p.set_defaults(func=cmd_audit)
    p = sub.add_parser("correct", help="append a numbered standing order to the project's STANDING.md")
    p.add_argument("project")
    p.add_argument("--text", required=True)
    p.add_argument("--by", help="session giving the order (default: $ORCHE_SESSION or user)")
    p.set_defaults(func=cmd_correct)


def cmd_log(args):
    path = hr.log_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# HR log\n\n", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-args.tail:] if args.tail > 0 else []
    if args.json:
        print(json.dumps({"path": str(path), "lines": tail}, indent=2, ensure_ascii=False))
    else:
        for line in tail:
            print(line)
    return EXIT_OK


def _set_frozen(args, frozen: bool):
    data, line = hr.set_frozen(registry.load(), frozen, dry_run=args.dry_run)
    print(json.dumps(data, indent=2) if args.json else line)
    return EXIT_OK


def cmd_freeze(args):
    return _set_frozen(args, True)


def cmd_unfreeze(args):
    return _set_frozen(args, False)


def cmd_audit(args):
    data = hr.run_audit(registry.load(), do_apply=args.apply, dry_run=args.dry_run)
    if args.quiet and not data["proposals"] and not data["corrections"]:
        return EXIT_OK
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"would write {data['report']}" if args.dry_run else f"report: {data['report']}")
    print(f"awake: {data['awake']}/{data['limit']}")
    print(data["metrics"])
    for p in data["proposals"]:
        print(f"propose: sleep {p['unit']} ({p['why']})")
    for o in data["overlaps"]:
        print(f"overlap: {o['project']} {o['a']} ({o['glob_a']}) ~ {o['b']} ({o['glob_b']})")
    for c in data["corrections"]:
        times = f" x{c['count']}" if c["count"] > 1 else ""
        print(f"candidate for structural enforcement: {c['project']} {c['source']}: {c['text']}{times}")
    for a in data["applied"]:
        print(f"applied: {a.get('result') or 'refused ' + a['unit'] + ': ' + a['error']}")
    if not data["proposals"] and not data["corrections"]:
        print("nothing to propose")
    return EXIT_OK


def cmd_correct(args):
    reg = registry.load()
    found = registry.project(reg, args.project)
    if found is None:
        return fail(EXIT_NOT_FOUND, f"unknown project: {args.project}")
    try:
        data, number = hr.correct(reg, found[0], args.text, by=args.by, dry_run=args.dry_run)
    except hr.Refused as exc:
        return fail(exc.code, exc.msg)
    print(json.dumps(data) if args.json else number)
    return EXIT_OK
