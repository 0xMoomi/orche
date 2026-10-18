"""`orche depts [proj]` — divisions and feature agents with status, session, branch, activity, idle
days and RUNNING (live `orche run` workers for the session)."""
import json
import sys

from . import registry, runs, units

EXIT_OK, EXIT_NOT_FOUND = 0, 2
COLUMNS = ("project", "unit", "status", "session", "branch", "last_activity", "idle", "running")


def register(subparsers):
    p = subparsers.add_parser("depts", help="table of divisions and feature agents")
    p.add_argument("project", nargs="?", help="project key or alias (default: all)")
    p.set_defaults(func=cmd_depts)


def _idle_days(at, now):
    return None if at is None else max(0, (now - at).days)


def _row(key, unit, entry, now, live):
    """Activity = max(registry last_activity, ORCHE_HOME/activity/<session> mtime)."""
    at = units.last_active(entry)
    shown = at.astimezone().isoformat(timespec="seconds") if at is not None else entry.get("last_activity", "")
    session = entry.get("session", "")
    return {"project": key, "unit": unit, "status": entry.get("status", "dormant"),
            "session": session, "branch": entry.get("branch", ""),
            "last_activity": shown, "idle_days": _idle_days(at, now),
            "running": sum(1 for lock in live if session and lock.get("to") == session)}


def rows(reg, keys, now=None) -> list:
    now = now or units.now()
    out = []
    for key in keys:
        divisions = reg["projects"][key].get("divisions", {})
        live = runs.live(key)
        for name in registry.divisions_of(reg["projects"][key]):
            if name not in divisions:
                continue
            out.append(_row(key, name, divisions[name], now, live))
            for feat in divisions[name].get("features", []):
                out.append(_row(key, f"{name}/{feat['name']}", feat, now, live))
    return out


def render(table) -> list:
    """Left-aligned columns separated by two spaces; idle shown as `<n>d` or `-`."""
    cells = [[c.upper() for c in COLUMNS]]
    for r in table:
        idle = "-" if r["idle_days"] is None else f"{r['idle_days']}d"
        cells.append([r["project"], r["unit"], r["status"], r["session"], r["branch"],
                      r["last_activity"], idle, str(r["running"])])
    widths = [max(len(row[i]) for row in cells) for i in range(len(COLUMNS))]
    return ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in cells]


def cmd_depts(args):
    reg = registry.load()
    if args.project:
        found = registry.project(reg, args.project)
        if found is None:
            print(f"orche: unknown project: {args.project}", file=sys.stderr)
            return EXIT_NOT_FOUND
        keys = [found[0]]
    else:
        keys = sorted(reg["projects"])
    runs.reconcile_cli(reg, args, keys)
    table = rows(reg, keys)
    if args.json:
        print(json.dumps(table, indent=2, ensure_ascii=False))
    elif table:
        print("\n".join(render(table)))
    else:
        print("no divisions")
    return EXIT_OK
