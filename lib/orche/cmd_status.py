"""`orche status [proj]` (one line per project, then its `undelivered:`, `unread:`, `blocked:` and `upgrade:` lines) and
`orche statusline [--cwd PATH]`."""
import datetime as _dt
import json
import os
import sys

from . import __version__, brief, inbox, registry, roster, runs, statusline, units

EXIT_OK, EXIT_NOT_FOUND = 0, 2
WAITING = ("blocked", "question")
WAITING_TTL = _dt.timedelta(hours=24)


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
    line = (f"{key} stage={proj['stage']} awake={c['awake']}/{limit} divisions: {' '.join(cells) or '-'}"
            f" · new:{c['new']} · keys:{c['keys']} · unverified:{c['unverified']} · RUNNING {c['running']}")
    divisions = {name: proj.get("divisions", {}).get(name, {}).get("status", "dormant")
                 for name in registry.divisions_of(proj)}
    undelivered, unread = inbox.overdue(roster.Roster(reg), key, now)
    return {"project": key, "stage": proj["stage"], "awake": c["awake"], "limit": limit,
            "divisions": divisions, "active_features": c["active_features"], "new": c["new"],
            "keys": c["keys"], "unverified": c["unverified"], "running": c["running"], "line": line,
            "undelivered": undelivered, "unread": unread, "blocked": blocked(reg, key, now),
            "upgrade": registry.upgrade(proj, __version__)}


def _mtime(path):
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc)
    except OSError:
        return None


def blocked(reg, key, now=None) -> list:
    """Units waiting on the owner: the newest pointer for the unit in `main-<key>`'s inbox (pending or
    drained, under 24 h old) is blocked or question, and no later pointer or BRIEF.md revision (an
    AMEND or STOP files a pointer too) has answered it. `[{unit, status, since, minutes}]`."""
    now = now or units.now()
    try:
        folder = inbox.box(f"main-{key}")
    except ValueError:
        return []
    latest = {}
    for path in list(folder.glob("*.json")) + list((folder / inbox.DRAINED).glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            ptr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(ptr, dict) or not ptr.get("unit"):
            continue
        t = inbox.pointer_time(path, ptr)
        unit = str(ptr["unit"])
        if t is not None and (unit not in latest or t >= latest[unit][0]):
            latest[unit] = (t, ptr)
    out = []
    for unit in sorted(latest):
        t, ptr = latest[unit]
        if ptr.get("status") not in WAITING or now - t >= WAITING_TTL:
            continue
        b = brief.find(reg, unit, project=key)
        answered = _mtime(b.path) if b is not None else None
        if answered is not None and answered > t:
            continue
        out.append({"unit": unit, "status": ptr["status"], "since": t.astimezone().strftime("%H:%M"),
                    "minutes": int((now - t).total_seconds() // 60)})
    return out


def overdue_lines(row) -> list:
    """The project's late deliveries and unread reports (≥ inbox.LATE), each naming its command, then the
    units waiting on the owner."""
    return ([f"  undelivered: {u['unit']} → {u['to']} (rev {u['rev']}, {u['minutes']} min) — "
             f"run `orche deliver {u['unit']}`" for u in row["undelivered"]]
            + [f"  unread: {u['session']} has {u['count']} report(s) waiting (oldest {u['minutes']} min) — "
               f"run `orche nudge {u['session']}`" for u in row["unread"]]
            + [f"  blocked: {u['unit']} ({u['status']}, since {u['since']}, {u['minutes']} min) — waiting on the "
               f"owner; answer in main-{row['project']}" for u in row["blocked"]])


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
    runs.reconcile_cli(reg, args, keys)      # a lost run is filed (and noted on stderr) before counting
    rows = [status_row(reg, key) for key in keys]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print(row["line"])
            for line in overdue_lines(row):
                print(line)
            if row["upgrade"]:   # the stamp of the last `orche init` is missing or older
                print(f"  upgrade: {registry.upgrade_text(row['project'], row['upgrade'])}")
        if not rows:
            print("no projects")
        line = triage_line()
        if line:
            print(line)
    return EXIT_OK


def triage_line():
    """One line when triage is enabled: the backend, the last eval and, on Jev with no Jev eval on
    file, the warning (`orche triage status` has the details). None when triage is off."""
    from . import cmd_triage, config
    try:
        cfg = config.load()
        if not (cfg.get("triage") or {}).get("enabled"):
            return None
        d = cmd_triage.status_data(cfg)
    except Exception:  # noqa: BLE001 - a status line never fails the command
        return None
    e = d["eval"]
    run = e["last_run"]
    seen = f"last eval {run.get('at', '?')} ({e['last_backend']})" if run else "no eval on file"
    return " · ".join(x for x in (f"triage: enabled, backend {d['resolved_backend']}, {seen}",
                                  cmd_triage.eval_warning(d)) if x)


def cmd_statusline(args):
    return statusline.run(args.cwd or os.getcwd(), as_json=args.json)
