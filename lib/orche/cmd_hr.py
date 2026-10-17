"""`orche hr log|freeze|unfreeze|audit|correct`.

audit:   weekly HR report (hr/audit.py); `--apply` sleeps the proposed units, `--quiet` prints
         nothing when there is nothing to propose.
correct: append the next numbered standing order to `<repo>/.orche/STANDING.md` (R-STANDING-1).
"""
import json
import re
import sys
from pathlib import Path

from . import config, mask, registry, shell, templates, units, util

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND = 0, 1, 2
_ORDER = re.compile(r"^(\d+)\.\s")


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


def notify(text: str) -> None:
    """macOS notification (masked); no-op when disabled or not on macOS."""
    if not config.load().get("notify") or sys.platform != "darwin":
        return
    safe, _ = mask.redact(text)
    script = f"display notification {json.dumps(safe)} with title \"ORCHE\""
    shell.run(["osascript", "-e", script], timeout=5)


def cmd_log(args):
    path = units.hr_log_path()
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
    verb = "freeze" if frozen else "unfreeze"
    reg = registry.load()
    changed = reg["frozen"] != frozen
    if changed and not args.dry_run:
        reg["frozen"] = frozen
        registry.append_event(reg, type=verb, project="*", unit="hr", by=units.actor())
        registry.save(reg)
        units.hr_log(f"{verb} by {units.actor()}")
        notify(f"HR {verb}: feature-agent creation {'stopped' if frozen else 'allowed'}")
    state = "frozen" if frozen else "unfrozen"
    if not changed:
        msg = f"already {state}"
    elif args.dry_run:
        msg = f"would {verb} (dry-run)"
    else:
        msg = state
    if args.json:
        print(json.dumps({"frozen": reg["frozen"], "changed": changed and not args.dry_run,
                          "dry_run": args.dry_run}, indent=2))
    else:
        print(msg)
    return EXIT_OK


def cmd_freeze(args):
    return _set_frozen(args, True)


def cmd_unfreeze(args):
    return _set_frozen(args, False)


def cmd_audit(args):
    from .hr import audit
    data = audit.run(registry.load(), do_apply=args.apply, dry_run=args.dry_run)
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


def next_order(text: str) -> int:
    return max((int(m.group(1)) for m in map(_ORDER.match, text.splitlines()) if m), default=0) + 1


def cmd_correct(args):
    reg = registry.load()
    found = registry.project(reg, args.project)
    if found is None:
        print(f"orche: unknown project: {args.project}", file=sys.stderr)
        return EXIT_NOT_FOUND
    key, proj = found
    order = " ".join(mask.redact(args.text)[0].split())  # standing orders reach every brief: mask first
    if not order:
        print("orche: --text is empty", file=sys.stderr)
        return EXIT_USER
    path = Path(proj["path"]) / proj.get("standing", ".orche/STANDING.md")
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        tmpl = templates.TEMPLATES_ROOT / "project" / ".orche" / "STANDING.md"
        text = templates.render(tmpl.read_text(encoding="utf-8"), PROJECT=key)
    number, by = next_order(text), args.by or units.actor()
    if args.dry_run:
        print(f"[dry-run] append to {path}: {number}. {order}", file=sys.stderr)
    else:
        sep = "" if not text or text.endswith("\n") else "\n"
        util.atomic_write(path, f"{text}{sep}{number}. {order}\n")
        units.hr_log(f"correct {key} #{number} by {by}: {order}")
    if args.json:
        print(json.dumps({"project": key, "number": number, "path": str(path), "dry_run": args.dry_run}))
    else:
        print(number)
    return EXIT_OK
