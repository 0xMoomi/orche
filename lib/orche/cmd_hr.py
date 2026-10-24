"""`orche hr log|freeze|unfreeze|audit|correct|add|model|choices`.

add:     add one division to a registered project on a model (hr/create.add_division), then start
         the project intake in the background when its stamp misses the division.
model:   move a division to another model (hr/models.set_model).
choices: the division kinds, models and the owner's questions (design 2026-10-23 §10.3; lib/orche/hr/choices.py).

audit:   weekly HR report (hr/audit.py); `--apply` sleeps the proposed units, `--quiet` prints
         nothing when there is nothing to propose.
correct: append the next numbered standing order to `<repo>/.orche/STANDING.md` (R-STANDING-1).
"""
import json

from . import cmd_intake, config, hr, intake, paths, procs, registry
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
    p = sub.add_parser("add", help="add a division to a registered project, on a model")
    p.add_argument("project")
    p.add_argument("division")
    p.add_argument("--model", help="claude-… id, codex or codex:<id> (default: the recommended one)")
    p.add_argument("--label", help="a custom division's label")
    p.add_argument("--charter", help="a custom division's charter")
    p.add_argument("--wake", action="store_true", help="open its pane now (Claude only)")
    p.add_argument("--no-intake", action="store_true", help="do not start the project intake")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("model", help="move a division to another model")
    p.add_argument("project")
    p.add_argument("division")
    p.add_argument("model")
    p.set_defaults(func=cmd_model)
    p = sub.add_parser("choices", help="division kinds, models and the owner's questions (read-only)")
    p.add_argument("project", nargs="?", help="omit for a project not under ORCHE yet")
    p.add_argument("--lang", choices=("ko", "en"), help="question language (default: config lang, else en)")
    p.set_defaults(func=cmd_choices)


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


def _project(reg, name):
    found = registry.project(reg, name)
    if found is None:
        fail(EXIT_NOT_FOUND, f"unknown project: {name}")
    return found


def _intake(args, key, proj, name):
    """Start the project intake in the background when it is on, not `--no-intake`, and the stamp
    misses division `name`. Returns (line or None, status)."""
    if args.no_intake or not intake.settings(config.load()).get("enabled", True):
        return None, "off"
    if not intake.missing_divisions([name], intake.read_stamp(proj["path"])):
        return None, "covered"
    if args.dry_run:
        return f"would run: orche intake {key}", "dry-run"
    procs.spawn_detached(cmd_intake.background_argv(key), paths.orche_home() / "logs" / "intake" / f"{key}.log")
    return f"intake: started in the background (orche intake status {key})", "started"


def cmd_add(args):
    reg = registry.load()
    found = _project(reg, args.project)
    if found is None:
        return EXIT_NOT_FOUND
    key, proj = found
    try:
        data, lines = hr.add_division(reg, key, args.division, model=args.model, label=args.label,
                                      charter=args.charter, wake=args.wake, dry_run=args.dry_run)
    except hr.Refused as exc:
        return fail(exc.code, exc.msg)
    line, data["intake"] = _intake(args, key, proj, args.division)
    if line:
        lines.append(line)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("\n".join(lines))
    return EXIT_OK


def cmd_model(args):
    reg = registry.load()
    found = _project(reg, args.project)
    if found is None:
        return EXIT_NOT_FOUND
    try:
        data, lines = hr.set_model(reg, found[0], args.division, args.model, dry_run=args.dry_run)
    except hr.Refused as exc:
        return fail(exc.code, exc.msg)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else "\n".join(lines))
    return EXIT_OK


def cmd_choices(args):
    from .hr import choices
    reg = registry.load()
    key = None
    if args.project:
        found = registry.project(reg, args.project)
        if found is None:
            return fail(EXIT_NOT_FOUND, f"unknown project {args.project}; for a project not under ORCHE yet, "
                                        "run it without a project")
        key = found[0]
    cfg = config.load()
    data = choices.build(reg, cfg, key, args.lang or cfg.get("lang") or "en")
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return EXIT_OK
    rec = "recommended" if data["lang"] == "en" else "권장"
    for k in data["kinds"]:
        model = k["model"] or k["recommended"]
        line = f"{'✓' if k['exists'] else ' '} {k['key']}  {k['label']} — {k['desc']}  {model}"
        if k["exists"] and k["model"] != k["recommended"]:
            line += f" ({rec} {k['recommended']})"
        print(line.rstrip())
    parts = [m["model"] + ("" if m["available"] else f" (unavailable: {m['reason']})") for m in data["models"]]
    print("models: " + ", ".join(parts))
    return EXIT_OK
