"""`orche status [proj]` (one line per project) and `orche statusline [--cwd PATH]`."""
import json
import os
import sys

from . import registry, statusline

EXIT_OK, EXIT_NOT_FOUND = 0, 2


def register(subparsers):
    p = subparsers.add_parser("status", help="one line per project: stage, awake, divisions, counters")
    p.add_argument("project", nargs="?", help="project key or alias (default: all)")
    p.set_defaults(func=cmd_status)
    p = subparsers.add_parser("statusline", help="print and cache the status-line segment for a cwd")
    p.add_argument("--cwd", default=None, help="directory to resolve the project from (default: cwd)")
    p.set_defaults(func=cmd_statusline)


def status_row(reg, key, now=None) -> dict:
    proj = reg["projects"][key]
    c = statusline.counts(reg, key, now)
    limit = reg["limits"]["max_awake_sessions"]
    cells = statusline.division_cells(proj, c["active_features"])
    line = (f"{key} stage={proj['stage']} awake={c['awake']}/{limit} divisions: {' '.join(cells)}"
            f" · new:{c['new']} · keys:{c['keys']} · unverified:{c['unverified']}")
    divisions = {name: proj.get("divisions", {}).get(name, {}).get("status", "dormant")
                 for name in registry.DIVISIONS}
    return {"project": key, "stage": proj["stage"], "awake": c["awake"], "limit": limit,
            "divisions": divisions, "active_features": c["active_features"], "new": c["new"],
            "keys": c["keys"], "unverified": c["unverified"], "line": line}


def cmd_status(args):
    reg = registry.load()
    if args.project:
        found = registry.project(reg, args.project)
        if found is None:
            print(f"orche: unknown project: {args.project}", file=sys.stderr)
            return EXIT_NOT_FOUND
        keys = [found[0]]
    else:
        keys = sorted(reg["projects"])
    rows = [status_row(reg, key) for key in keys]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print(row["line"])
        if not rows:
            print("no projects")
    return EXIT_OK


def cmd_statusline(args):
    return statusline.run(args.cwd or os.getcwd(), as_json=args.json)
