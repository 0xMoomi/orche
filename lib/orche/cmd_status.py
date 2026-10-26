"""`orche status` (one line per project, then its `owner:`, `undelivered:`, `unread:`, `blocked:`,
`looping:`, `paused:`, `queue:` and `upgrade:` lines, at most CAP_LINES per kind; then the global
`triage:` and `heartbeat:` lines), `orche status <project>` (its card: what a cleared or restarted Main
picks the work up from, also shown by SessionStart), `orche status --project <p>` (the detail view: every
sub-line, then one `L` line per lineage of the last 24 h or still open) and `orche statusline [--cwd PATH]`.
The facts come from lib/orche/project.py.

0.9.0 (design §9.1): the project line carries `queue w/t/m` (waiting / testing / merged today, when
the merge queue has any) and `runs n/cap` (live runs / runner.max_parallel); a usage hold of the
project's units is a `paused:` line."""
import json
import os
import sys

from . import __version__, config, inbox, project, registry, roster, runs, statusline, units, vendors
from .flow import guard, owner, shape, usage

CAP_LINES = 3      # sub-lines per kind; the rest: `… <n> more: orche status --project <p>`
STATE_ORDER = ("merged", "queued", "running", "verifying", "held", "returned", "done", "info", "failed", "blocked", "question",
               "stopped")

CARD_LINES, CARD_CHARS = 40, 3000   # the card's budget
EXIT_OK, EXIT_NOT_FOUND = 0, 2


def register(subparsers):
    p = subparsers.add_parser("status", help="one line per project: stage, awake, divisions, counters; "
                                             "<project>: its card (open lineages, what waits on whom, the queue)")
    p.add_argument("project", nargs="?", help="project key or alias: its card (default: one line per project)")
    p.add_argument("--project", dest="detail", metavar="PROJECT", default=None,
                   help="the detail view: every sub-line, then one line per lineage (last 24 h or still open)")
    p.set_defaults(func=cmd_status)
    p = subparsers.add_parser("statusline", help="print and cache the status-line segment for a cwd")
    p.add_argument("--cwd", default=None, help="directory to resolve the project from (default: cwd)")
    p.set_defaults(func=cmd_statusline)


def usage_holds(key, now=None) -> list:
    """The project's units in an active usage hold (flow/usage.py): [{vendor, until, hhmm, units}]."""
    now = now or units.now()
    out = []
    for hold in usage.holds():
        mine = usage.held_units(hold, key)
        if mine and usage.is_active(hold, now):
            out.append({"vendor": hold.get("vendor"), "until": hold.get("until"),
                        "hhmm": usage.hhmm(usage.until_of(hold), now), "units": len(mine)})
    return out


def status_row(reg, key, now=None, cfg=None) -> dict:
    proj = reg["projects"][key]
    c = statusline.counts(reg, key, now)
    cfg = config.load() if cfg is None else cfg
    q, cap = project.queue_of(key), runs.caps(cfg)[0]
    qcell = (f" · queue {q['waiting']}/{q['testing']}/{q['merged_today']}"
             if q and (q["waiting"] or q["testing"] or q["merged_today"]) else "")
    limit = reg["limits"]["max_awake_sessions"]
    models = {name: proj.get("divisions", {}).get(name, {}).get("model")
              for name in registry.divisions_of(proj)}
    # A non-Claude division is marked with its vendor (`rnd○[codex]`); the status bar is unchanged.
    cells = [cell + (f"[{vendors.vendor_of(m)}]" if m and vendors.vendor_of(m) != "claude" else "")
             for cell, m in zip(statusline.division_cells(proj, c["workers"]), models.values())]
    line = (f"{key} stage={proj['stage']} awake={c['awake']}/{limit} divisions: {' '.join(cells) or '-'}{qcell}"
            f" · new:{c['new']} · keys:{c['keys']} · unverified:{c['unverified']} · runs {c['running']}/{cap}")
    from . import reconcile
    revived = reconcile.main_reopened(reg, key)
    if revived is not None and ((now or units.now()) - revived).total_seconds() < 86400:
        line += f" · main pane reopened {revived.astimezone().strftime('%H:%M')}"   # the heartbeat revived Main
    divisions = {name: proj.get("divisions", {}).get(name, {}).get("status", "dormant")
                 for name in registry.divisions_of(proj)}
    undelivered, unread = inbox.overdue(roster.Roster(reg), key, now)
    return {"project": key, "stage": proj["stage"], "awake": c["awake"], "limit": limit,
            "divisions": divisions, "models": {n: m for n, m in models.items() if m},
            "active_features": c["active_features"], "new": c["new"],
            "keys": c["keys"], "unverified": c["unverified"], "running": c["running"], "line": line,
            "undelivered": undelivered, "unread": unread, "blocked": project.blocked(reg, key, now),
            "upgrade": registry.upgrade(proj, __version__),
            "looping": looping(reg, key, now), "paused": paused(key, now), "owner": owner.status(key, now),
            "queue": q, "runs": {"live": c["running"], "cap": cap}, "holds": usage_holds(key, now)}


def owner_lines(o) -> list:
    """The open ask (flow.owner.status): `owner: asked HH:MM (<m> min, notified n×[, next HH:MM | waiting
    for quiet hours to end]) — <ask>`; none without one."""
    if not o:
        return []
    tail = ", waiting for quiet hours to end" if o["quiet"] else f", next {o['next']}" if o["next"] else ""
    return [f"  owner: asked {o['since']} ({o['minutes']} min, notified {o['notified']}×{tail}) — {o['text']}"]


def looping(reg, key, now):
    """G1 holds and quiet repeats in one shape: `[{unit, kind: revisions|repeats, n, since, status, rev}]`."""
    blank = {"since": None, "status": None, "rev": None}
    return ([dict(blank, kind="revisions", **u) for u in guard.looping(reg, key, now)]
            + [dict(blank, kind="repeats", **u) for u in shape.repeats(f"main-{key}", guard.window(key, now))])


def paused(key, now):
    """Spend cap (G4) and owner pause in one shape: `[{by: spend|owner, since, spent, cap, reason}]`."""
    blank = {"spent": None, "cap": None, "reason": None}
    s, p = guard.spend_status(key, now), guard.pause_status(key)
    return (([dict(blank, by="spend", **s)] if s else [])
            + ([dict(blank, by="owner", since=p.get("since"), reason=p.get("reason"))] if p else []))


def overdue_lines(row) -> list:
    """The project's late deliveries and unread reports (≥ inbox.LATE), each with ORCHE's live repair
    while the heartbeat is fresh, else its command (a closed pane: `orche wake`), then the units
    waiting on the owner."""
    from . import reconcile
    hints = _hints(row)
    return (owner_lines(row.get("owner"))
            + [f"  undelivered: {u['unit']} → {u['to']} (rev {u['rev']}, {u['minutes']} min) — "
             + reconcile.overdue_hint("undelivered", u["unit"], *hints) for u in row["undelivered"]]
            + _unread_lines(row, hints) + _blocked_lines(row)
            + [f"  looping: {u['unit']} — revised {u['n']}× without the owner since {u['since']}: the next AMEND "
               f"needs the owner" if u["kind"] == "revisions" else
               f"  looping: {u['unit']} — same report {u['n']}× ({u['status']}, rev {u['rev']}): repeats did not "
               f"wake Main" for u in row.get("looping") or []]
            + _paused_lines(row) + _queue_lines(row))


def _hints(row) -> tuple:
    """(reconcile's live repairs, whether the heartbeat is fresh), read only while a delivery or a report is
    late."""
    from . import reconcile
    reps = reconcile.repairs() if row.get("undelivered") or row["unread"] else {}
    hb = reconcile.heartbeat_json() if reps else {}
    return reps, bool(hb.get("enabled")) and not hb.get("stale", True)


def _unread_lines(row, hints) -> list:
    from . import reconcile
    return [f"  unread: {u['session']} has {u['count']} report(s) waiting (oldest {u['minutes']} min) — "
            + reconcile.overdue_hint("unread", u["session"], *hints) for u in row["unread"]]


def _blocked_lines(row) -> list:
    return [f"  blocked: {u['unit']} ({u['status']}, since {u['since']}, {u['minutes']} min) — waiting on the "
            f"owner; answer in main-{row['project']}" for u in row["blocked"]]


def _paused_lines(row):
    return ([f"  paused: autonomous spend ${p['spent']:.2f} since {p['since']} (cap ${p['cap']:g}) — until the "
             f"owner answers" if p["by"] == "spend" else
             f"  paused: by the owner since {p['since']}" + (f" ({p['reason']})" if p["reason"] else "")
             + f" — resume on their word: orche flow resume {row['project']}" for p in row.get("paused") or []]
            + [f"  paused: {h['vendor']} usage limit until {h['hhmm']} ({h['units']} units held)"
               for h in row.get("holds") or []])


def _clock(iso) -> str:
    at = units.parse_iso(iso) if iso else None
    return at.astimezone().strftime("%H:%M") if at is not None else ""


def _queue_lines(row):
    """`queue:` while the project's merge queue is closed or paused."""
    from . import queue
    q = row.get("queue") or {}
    if q.get("state") == "closed":
        at = _clock((q.get("closed") if isinstance(q.get("closed"), dict) else {}).get("at"))
        return [f"  queue: closed ({queue.closed_text(row.get('project') or '<project>', q.get('closed'))})"
                + (f" since {at}" if at else "")]
    if q.get("state") == "paused":
        p = q.get("paused") if isinstance(q.get("paused"), dict) else {}
        at = _clock(p.get("at"))
        return [f"  queue: paused by {p.get('by') or '?'}" + (f" since {at}" if at else "")
                + f" — orche queue resume {row['project']}"]
    return []


def capped(lines, project) -> list:
    """At most CAP_LINES sub-lines per kind (the word before the first colon), then `  <kind>: … <n>
    more: orche status --project <p>`."""
    groups = {}
    for line in lines:
        groups.setdefault(line.strip().split(":", 1)[0], []).append(line)
    out = []
    for kind, some in groups.items():
        out += some[:CAP_LINES]
        if len(some) > CAP_LINES:
            out.append(f"  {kind}: … {len(some) - CAP_LINES} more: orche status --project {project}")
    return out


def states_text(r) -> str:
    """`12 units: 7 merged · 2 queued · …  PRs #41 #43 #44 …` (states in STATE_ORDER)."""
    order = sorted(r["states"].items(), key=lambda kv: (STATE_ORDER.index(kv[0]) if kv[0] in STATE_ORDER
                                                        else len(STATE_ORDER), kv[0]))
    prs = " ".join(f"#{n}" for n in r["prs"][:3]) + (" …" if len(r["prs"]) > 3 else "")
    return f"{r['units']} units: {' · '.join(f'{n} {s}' for s, n in order)}" + (f"  PRs {prs}" if prs else "")


def lineage_line(r) -> str:
    """`L <lineage>  12 units: 7 merged · 2 queued · …  PRs #41 #43 #44 …  $14.20  since 10:12`."""
    return f"L {r['lineage']}  {states_text(r)}  ${r['spend']:.2f}  since {r['since']}"


def card_text(d) -> str:
    """The card (project.card): `[orche] resume <project>`, then per open lineage its `L` line with the
    GOAL, its states, its last report and what it waits on; the `owner:`, `unread:` and `blocked:` lines as
    the status row prints them; `queue: w/t/m`; the closed lineages. At most CARD_LINES lines and CARD_CHARS
    characters."""
    p, q = d["project"], d["queue"]
    out = [f"[orche] resume {p}"] + ([] if d["open"] else ["nothing in flight"])
    for o in d["open"]:
        last = o["last"]
        out += [f"L {o['lineage']}" + (f" — {o['goal']}" if o["goal"] else ""), f"  {states_text(o)}"]
        if last:
            out.append(f"  last: {last['unit']} {last['status']}"
                       + (f" — {last['summary']}" if last["summary"] else ""))
        out.append(f"  {o['waits']}")
    if d["open_total"] > len(d["open"]):
        out.append(f"… {d['open_total'] - len(d['open'])} more open: orche status --project {p}")
    out += owner_lines(d["owner"]) + _unread_lines(d, _hints(d)) + _blocked_lines(d)
    out.append(f"queue: {q.get('waiting', 0)}/{q.get('testing', 0)}/{q.get('merged_today', 0)}" if q else "queue: -")
    out += [f"closed L {c['lineage']}" + (f" — {c['goal']}" if c["goal"] else "")
            + f"  DONE: {c['units']} unit{'' if c['units'] == 1 else 's'} ("
            + ", ".join(f"{n} {s}" for s, n in c["statuses"].items()) + ")" for c in d["closed"]]
    out = [ln if len(ln) <= project.WIDTH + 40 else ln[:project.WIDTH + 39] + "…" for ln in out]
    more = f"… the rest: orche status --project {p}"
    if len(out) > CARD_LINES:
        out = out[:CARD_LINES - 1] + [more]
    while len("\n".join(out)) > CARD_CHARS:
        out = out[:-2] + [more]
    return "\n".join(out)


def cmd_status(args):
    reg = registry.load()
    detail = getattr(args, "detail", None)
    want = detail or args.project
    if want:
        found = registry.project(reg, want)
        if found is None:
            print(f"orche: unknown project: {want}", file=sys.stderr)
            return EXIT_NOT_FOUND
        keys = [found[0]]
    else:
        keys = sorted(reg["projects"])
    runs.reconcile_cli(reg, args, keys)      # a lost run is filed (and noted on stderr) before counting
    if want and not detail:
        d = project.card(reg, keys[0])
        text = card_text(d)
        print(json.dumps({**d, "text": text}, indent=2, ensure_ascii=False) if args.json else text)
        return EXIT_OK
    cfg = config.load()
    rows = [status_row(reg, key, cfg=cfg) for key in keys]
    for row in rows:
        row["stalled"] = stalled(reg, row["project"])
        if detail:
            row["lineages"] = project.lineages(reg, row["project"])
    beat = heartbeat()
    from . import reconcile
    if args.json:
        for row in rows:
            row["heartbeat"] = beat[1]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print(row["line"])
            # repairs ORCHE is retrying or escalated (lib/orche/reconcile.py); upgrade: the stamp of the
            # last `orche init` is missing or older
            sub = (overdue_lines(row) + [reconcile.stalled_line(s) for s in row["stalled"]]
                   + ([f"  upgrade: {registry.upgrade_text(row['project'], row['upgrade'])}"] if row["upgrade"] else []))
            for line in (sub if detail else capped(sub, row["project"])):
                print(line)
            for lin in row.get("lineages") or []:
                print(lineage_line(lin))
        if not rows:
            print("no projects")
        line = triage_line()
        if line:
            print(line)
        if beat[0]:
            print(beat[0])
    return EXIT_OK


def stalled(reg, project) -> list:
    """The repairs ORCHE is retrying or escalated (reconcile.stalled), then the lineages a blocked or
    question member holds open (flow.lineage.stalled), in one shape."""
    from . import reconcile
    from .flow import lineage
    try:
        out = reconcile.stalled(project)
    except Exception:  # noqa: BLE001 - a status line never fails the command
        out = []
    blank = {"rev": None, "to": None, "tries": 0, "cap": None, "escalated": False, "escalated_at": None,
             "next": None, "last": None}
    return out + [dict(blank, cls="lineage", target=s["lineage"],
                       detail=f"{s['unit']} ({s['status']} {s['minutes']} min)")
                  for s in lineage.stalled(reg, project)]


def heartbeat():
    """(the `heartbeat:` line, its JSON) for the reconcile pass (lib/orche/reconcile.py)."""
    from . import reconcile
    try:
        return reconcile.heartbeat_line(), reconcile.heartbeat_json()
    except Exception:  # noqa: BLE001 - a status line never fails the command
        return None, None


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
