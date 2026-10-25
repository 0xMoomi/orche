"""`orche status [proj]` (one line per project, then its `owner:`, `undelivered:`, `unread:`, `blocked:`,
`looping:`, `paused:`, `queue:` and `upgrade:` lines, at most CAP_LINES per kind;
then the global `triage:` and `heartbeat:` lines), `orche status --project <p>` (the detail view: every
sub-line, then one `L` line per lineage of the last 24 h or still open) and
`orche statusline [--cwd PATH]`.

0.9.0 (design §9.1): the project line carries `queue w/t/m` (waiting / testing / merged today, when
the merge queue has any) and `runs n/cap` (live runs / runner.max_parallel); a usage hold of the
project's units is a `paused:` line."""
import datetime as _dt
import json
import os
import sys

from . import __version__, brief, config, inbox, registry, roster, runs, statusline, units, util, vendors
from .flow import guard, owner, shape, usage

CAP_LINES = 3      # sub-lines per kind; the rest: `… <n> more: orche status --project <p>`
DETAIL_TTL = _dt.timedelta(hours=24)   # --project: closed lineages this recent
STATE_ORDER = ("merged", "queued", "running", "held", "returned", "done", "info", "failed", "blocked", "question",
               "stopped")

EXIT_OK, EXIT_NOT_FOUND = 0, 2
WAITING = ("blocked", "question")
WAITING_TTL = _dt.timedelta(hours=24)


def register(subparsers):
    p = subparsers.add_parser("status", help="one line per project: stage, awake, divisions, counters")
    p.add_argument("project", nargs="?", help="project key or alias (default: all)")
    p.add_argument("--project", dest="detail", metavar="PROJECT", default=None,
                   help="the detail view: every sub-line, then one line per lineage (last 24 h or still open)")
    p.set_defaults(func=cmd_status)
    p = subparsers.add_parser("statusline", help="print and cache the status-line segment for a cwd")
    p.add_argument("--cwd", default=None, help="directory to resolve the project from (default: cwd)")
    p.set_defaults(func=cmd_statusline)


def queue_of(key):
    """The project's merge-queue counts (queue.summary without its entries) and its closed record; None
    when its file is unreadable."""
    from . import queue
    from .queue import store
    try:
        s, closed = queue.summary(key), store.read(key).get("closed")
    except Exception:  # noqa: BLE001 - a status line never fails the command
        return None
    return {**{k: v for k, v in s.items() if k not in ("entries", "project")}, "closed": closed}


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
    q, cap = queue_of(key), runs.caps(cfg)[0]
    qcell = (f" · queue {q['waiting']}/{q['testing']}/{q['merged_today']}"
             if q and (q["waiting"] or q["testing"] or q["merged_today"]) else "")
    limit = reg["limits"]["max_awake_sessions"]
    models = {name: proj.get("divisions", {}).get(name, {}).get("model")
              for name in registry.divisions_of(proj)}
    # A non-Claude division is marked with its vendor (`rnd○[codex]`); the status bar is unchanged.
    cells = [cell + (f"[{vendors.vendor_of(m)}]" if m and vendors.vendor_of(m) != "claude" else "")
             for cell, m in zip(statusline.division_cells(proj, c["active_features"]), models.values())]
    line = (f"{key} stage={proj['stage']} awake={c['awake']}/{limit} divisions: {' '.join(cells) or '-'}{qcell}"
            f" · new:{c['new']} · keys:{c['keys']} · unverified:{c['unverified']} · runs {c['running']}/{cap}")
    divisions = {name: proj.get("divisions", {}).get(name, {}).get("status", "dormant")
                 for name in registry.divisions_of(proj)}
    undelivered, unread = inbox.overdue(roster.Roster(reg), key, now)
    return {"project": key, "stage": proj["stage"], "awake": c["awake"], "limit": limit,
            "divisions": divisions, "models": {n: m for n, m in models.items() if m},
            "active_features": c["active_features"], "new": c["new"],
            "keys": c["keys"], "unverified": c["unverified"], "running": c["running"], "line": line,
            "undelivered": undelivered, "unread": unread, "blocked": blocked(reg, key, now),
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
    """The project's late deliveries and unread reports (≥ inbox.LATE), each with ORCHE's live repair
    while the heartbeat is fresh, else its command (a closed pane: `orche wake`), then the units
    waiting on the owner."""
    from . import reconcile
    reps = reconcile.repairs() if row["undelivered"] or row["unread"] else {}
    hb = reconcile.heartbeat_json() if reps else {}
    fresh = bool(hb.get("enabled")) and not hb.get("stale", True)
    return (owner_lines(row.get("owner"))
            + [f"  undelivered: {u['unit']} → {u['to']} (rev {u['rev']}, {u['minutes']} min) — "
             + reconcile.overdue_hint("undelivered", u["unit"], reps, fresh) for u in row["undelivered"]]
            + [f"  unread: {u['session']} has {u['count']} report(s) waiting (oldest {u['minutes']} min) — "
               + reconcile.overdue_hint("unread", u["session"], reps, fresh) for u in row["unread"]]
            + [f"  blocked: {u['unit']} ({u['status']}, since {u['since']}, {u['minutes']} min) — waiting on the "
               f"owner; answer in main-{row['project']}" for u in row["blocked"]]
            + [f"  looping: {u['unit']} — revised {u['n']}× without the owner since {u['since']}: the next AMEND "
               f"needs the owner" if u["kind"] == "revisions" else
               f"  looping: {u['unit']} — same report {u['n']}× ({u['status']}, rev {u['rev']}): repeats did not "
               f"wake Main" for u in row.get("looping") or []]
            + _paused_lines(row) + _queue_lines(row))


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
        return [f"  queue: closed ({queue.reopen(row.get('project') or '<project>', q.get('closed'))})"
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


def _read(path) -> dict:
    try:
        data = util.read_json(path, default=None)
    except (OSError, util.JsonError):
        return {}
    return data if isinstance(data, dict) else {}


def lineage_rows(reg, key, now=None, ttl=DETAIL_TTL, detail=False) -> list:
    """`orche status --project <p>`: one row per lineage of the last `ttl` (None: any age) or still open,
    newest first: {lineage, units, states: {state: n}, prs, spend, since, open}. A unit's state: running (a
    live run), held (a usage hold), queued (a merge-queue entry), the queue's last word on it (merged,
    returned, dropped), else its newest report's (lineage.members). `detail` adds `members`:
    [(Brief, (path, pointer) | None, status, state)] (orche resume; not JSON)."""
    from .flow import lineage
    from .queue import store
    from .runs import history
    now = now or units.now()
    try:
        q, qlog = store.read(key), store.read_log(key)
    except Exception:  # noqa: BLE001 - a status line never fails the command
        q, qlog = {"entries": []}, []
    queued = {e.get("unit") for e in q.get("entries") or [] if isinstance(e, dict)}
    said = {ln["unit"]: ln.get("event") for ln in qlog
            if ln.get("unit") and ln.get("event") in ("merged", "returned", "dropped")}
    spent = {}
    for r in history.read(key):
        try:
            spent[r.get("unit")] = spent.get(r.get("unit"), 0.0) + float(r.get("cost_usd") or 0)
        except (TypeError, ValueError):
            continue
    reads, rows = lineage.Reads(reg), []
    for name in reads.groups(key):
        states, prs, times, cost, mine = {}, [], [], 0.0, []
        ms = lineage.members(reg, key, name, reads)
        for b, found, status in ms:
            rec = _read(b.folder / "RUN.json")
            state = ("running" if runs.running(key, b.unit) else "held" if rec.get("stop_reason") in usage.HELD
                     else "queued" if b.unit in queued else said.get(b.unit) or status)
            states[state] = states.get(state, 0) + 1
            mine.append((b, found, status, state))
            pr = rec.get("pr")
            pr = pr.get("number") if isinstance(pr, dict) else pr
            if isinstance(pr, int):
                prs.append(pr)
            cost += spent.get(b.unit, 0.0)
            try:
                times.append(_dt.datetime.fromtimestamp(b.path.stat().st_mtime, _dt.timezone.utc))
            except OSError:
                continue
        is_open = any(s not in lineage.TERMINAL for _b, _f, s in ms)
        if not times or (not is_open and ttl is not None and now - max(times) > ttl):
            continue
        rows.append({"lineage": name, "units": len(ms), "states": states, "prs": sorted(set(prs)),
                     "spend": round(cost, 2), "since": min(times).astimezone().strftime("%H:%M"),
                     "open": is_open, "newest": max(times).isoformat(), **({"members": mine} if detail else {})})
    return sorted(rows, key=lambda r: r["newest"], reverse=True)


def states_text(r) -> str:
    """`12 units: 7 merged · 2 queued · …  PRs #41 #43 #44 …` (states in STATE_ORDER)."""
    order = sorted(r["states"].items(), key=lambda kv: (STATE_ORDER.index(kv[0]) if kv[0] in STATE_ORDER
                                                        else len(STATE_ORDER), kv[0]))
    prs = " ".join(f"#{n}" for n in r["prs"][:3]) + (" …" if len(r["prs"]) > 3 else "")
    return f"{r['units']} units: {' · '.join(f'{n} {s}' for s, n in order)}" + (f"  PRs {prs}" if prs else "")


def lineage_line(r) -> str:
    """`L <lineage>  12 units: 7 merged · 2 queued · …  PRs #41 #43 #44 …  $14.20  since 10:12`."""
    return f"L {r['lineage']}  {states_text(r)}  ${r['spend']:.2f}  since {r['since']}"


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
    cfg = config.load()
    rows = [status_row(reg, key, cfg=cfg) for key in keys]
    for row in rows:
        row["stalled"] = stalled(reg, row["project"])
        if detail:
            row["lineages"] = lineage_rows(reg, row["project"])
    beat = heartbeat()
    if args.json:
        for row in rows:
            row["heartbeat"] = beat[1]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print(row["line"])
            # repairs ORCHE is retrying or escalated (lib/orche/reconcile.py); upgrade: the stamp of the
            # last `orche init` is missing or older
            sub = (overdue_lines(row) + [stalled_line(s) for s in row["stalled"]]
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


def stalled_line(s) -> str:
    from . import reconcile
    return reconcile.stalled_line(s)


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
