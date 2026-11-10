"""The daily brief: once per local day and project, Main opens the day by telling the owner what happened
since the last brief and what waits for him. No model call builds it.

The brief day starts when flow.quiet_hours end (08:00 by default; local midnight when they are unset or
malformed): day() is the date of that start. The janitor's pass builds it (build, from `orche janitor run`;
the heartbeat starts that pass at the first full pass of a brief day when the janitor's last pass is older
than the day's start, janitor.heartbeat): yesterday's spend by role needs a scan of every transcript, too
slow for a heartbeat pass. Per project, once per brief day (`flow/<p>/daily.json` {day, at, since, text,
summary, sent_at, last_sent}): the facts since the last brief sent (`last_sent`, else the last 24 hours):

- landed: each PR the merge queue merged, with its unit's one-line result (at most LIST_MAX, then "and N
  more"), and units done without a PR;
- ended without landing: failed and unverified units since then, and units still blocked;
- waiting for the owner: the open ask (flow.owner) and each unit whose newest report asks him (a question
  or an owner step), with how long it waited, oldest first;
- held or stalled: units an active usage hold stopped, the heartbeat's repairs on file (reconcile.stalled);
- yesterday's spend, total and by role (metrics.compute with a per-project classifier, plus the review
  and Codex worker calls of the project's run history), when the transcripts can be read.

A project with none of the first four gets `text: null` (no brief, no wake). Otherwise the facts are
written to `flow/<p>/daily/REPORT.md`. The heartbeat (heartbeat, after the janitor step) delivers a
brief built for the current day, outside quiet hours and not while the project is paused: one urgent
`info` pointer from `orche` to Main (unit DAILY_UNIT, summary `daily brief: …`, report_path the
REPORT.md, which the inbox inlines), stamped `sent_at` before it is posted, so a day never gets two.
A brief held past the day is never sent: the next day's covers its window. config `flow.daily_brief`
(default true) turns it off (`orche flow brief off|on`)."""
import datetime as _dt
from collections import defaultdict

from .. import config, inbox, log, mask, roster, units, util
from . import guard, lineage, owner, usage

STATE, REPORT = "daily.json", "REPORT.md"
DAILY_UNIT = inbox.DAILY_UNIT
FIRST = _dt.timedelta(hours=24)      # no brief sent yet: the window is the last 24 hours
LIST_MAX = 8                         # lines per list, then "and N more"
LINE_MAX = 140                       # characters of one result or ask
ENDED = ("failed", "unverified")


def enabled(cfg_all=None) -> bool:
    """config `flow.daily_brief` (default true)."""
    flow = (cfg_all if cfg_all is not None else config.load()).get("flow") or {}
    return bool(flow.get("daily_brief", True))


def _end_of_quiet(cfg_all):
    """(hour, minute) where flow.quiet_hours end; (0, 0) when unset or malformed."""
    raw = str(((cfg_all or {}).get("flow") or {}).get("quiet_hours") or "")
    try:
        (_ah, _am), (bh, bm) = (tuple(int(x) for x in part.strip().split(":")) for part in raw.split("-"))
    except ValueError:
        return 0, 0
    return (bh, bm) if 0 <= bh < 24 and 0 <= bm < 60 else (0, 0)


def day_start(cfg_all, now):
    """The start of the brief day holding `now`: the latest end of quiet hours at or before it."""
    h, m = _end_of_quiet(cfg_all)
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return start if start <= now else start - _dt.timedelta(days=1)


def day(cfg_all, now) -> str:
    return day_start(cfg_all, now).date().isoformat()


def _path(project, name=STATE):
    return guard.folder(project) / name


def report_path(project):
    return guard.folder(project) / "daily" / REPORT


def read(project) -> dict:
    return util.read_dict(_path(project)) or {}


def wanted(reg, now, cfg_all, last_pass_at) -> bool:
    """The janitor's pass should run for the brief: it is on, outside quiet hours, the janitor's last pass
    (`last_pass_at`, epoch seconds or None) is older than the brief day's start and some project has no
    brief built for the day."""
    if not enabled(cfg_all) or owner.in_quiet_hours(cfg_all, now):
        return False
    if last_pass_at is not None and last_pass_at >= day_start(cfg_all, now).timestamp():
        return False
    d = day(cfg_all, now)
    return any(read(k).get("day") != d for k in (reg or {}).get("projects", {}))


# ---- the facts ------------------------------------------------------------------------------------

def age(seconds) -> str:
    """`45 min`, `3 h 20 min`, `2 d 3 h` (a part that is zero is left out; at least `1 min`)."""
    m = max(1, int(seconds) // 60)
    if m < 60:
        return f"{m} min"
    d, h, m = m // 1440, m % 1440 // 60, m % 60
    if d:
        return f"{d} d" + (f" {h} h" if h else "")
    return f"{h} h" + (f" {m} min" if m else "")


def _one(text, limit=LINE_MAX) -> str:
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    first = " ".join(mask.redact(first)[0].split())
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


def _result(b, found) -> str:
    """A unit's one-line result: its newest report's summary, else its GOAL."""
    p = found[1] if found else None
    return _one((p or {}).get("summary")) or _one((b.fields or {}).get("GOAL")) or "-"


def _listed(lines) -> list:
    return lines[:LIST_MAX] + ([f"  - and {len(lines) - LIST_MAX} more"] if len(lines) > LIST_MAX else [])


def facts(reg, key, now, since) -> dict:
    """The brief's facts for `key` since `since`: {landed, ended, waiting, held} as lists of lines
    (`waiting` oldest first)."""
    from .. import project, queue, reconcile
    members = {}
    for row in project.lineages(reg, key, now, ttl=None, members=True):
        for b, found, status, state in row["members"]:
            members[b.unit] = (b, found, status, state)

    def when(found):
        return inbox.pointer_time(*found) if found else None

    landed, merged = [], set()
    for ln in queue.merged_since(key, since):
        unit = ln.get("unit")
        merged.add(unit)
        m = members.get(unit)
        landed.append(f"  - PR #{ln.get('pr')}: {_result(m[0], m[1]) if m else unit or '-'}")
    ended, waits = [], []
    for unit, (b, found, status, state) in sorted(members.items()):
        t, p = when(found), (found[1] if found else {})
        label = lineage.label(found, status)
        if status in lineage.WAITING and (status == "question" or p.get("owner")):
            waits.append((t or now, unit, f"{unit} asks: {_one(p.get('owner') or p.get('summary'))}"))
        elif label == "blocked":
            ended.append(f"  - {unit} blocked: {_result(b, found)}")
        elif t is not None and t >= since and label in ENDED:
            ended.append(f"  - {unit} {label}: {_result(b, found)}")
        elif (t is not None and t >= since and label == "done" and unit not in merged
              and state not in ("queued", "merged")):
            landed.append(f"  - {unit} (done, no PR): {_result(b, found)}")
    a = owner.open_ask(key)
    asked = units.parse_iso((a or {}).get("at"))
    if asked is not None:
        waits = [w for w in waits if w[1] != a.get("unit")]
        waits.append((asked, a.get("unit") or "", f"OWNER: {_one(a.get('text'), owner.ASK_MAX)}"))
    waiting = [f"  - waiting {age((now - t).total_seconds())}: {text}" for t, _u, text in sorted(waits, key=lambda w: w[0])]
    held = []
    for hold in usage.holds():
        if not usage.is_active(hold, now):
            continue
        label = owner.VENDOR_LABEL.get(hold.get("vendor"), hold.get("vendor"))
        held += [f"  - {u.get('unit')} held by the {label} usage limit until {usage.hhmm(usage.until_of(hold), now)}"
                 for u in hold.get("units") or [] if isinstance(u, dict) and u.get("project") == key]
    held += [f"  - {reconcile.stalled_line(s).strip()}" for s in reconcile.stalled(key)]
    return {"landed": landed, "ended": ended, "waiting": waiting, "held": held}


def something(f) -> bool:
    return bool(f and (f["landed"] or f["ended"] or f["waiting"] or f["held"]))


def spend(reg, keys, now, root=None) -> dict:
    """{key: (total, [(role, USD)])} of yesterday (local calendar day) for each of `keys`: the transcripts
    by role (metrics.compute with a classifier that names the project too) plus the review and Codex worker
    calls in the project's run history (metrics.headless_spend; its intake and ask costs are not per
    project and are left out). {} when the transcripts cannot be read."""
    from .. import metrics
    from ..hr import panes
    if not keys:
        return {}
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    projects = reg.get("projects") or {}
    per = {k: metrics.classifier({"projects": {k: projects[k]}}) for k in keys if k in projects}

    def classify(folder, name):
        for k, c in per.items():
            role = c(folder, name)
            if role != metrics.OTHER:
                return k, role
        return "", metrics.OTHER
    m = metrics.compute(root or panes.claude_projects(), days=1, now=end, classify=classify)
    if m.get("roles") is None:
        return {}
    out = {}
    for k in per:
        roles = defaultdict(float)
        for (p, role), usd in m["roles"].items():
            if p == k:
                roles[role] += usd
        head = metrics.headless_spend([k], days=1, now=end)
        for role in ("review", metrics.WORKERS):
            roles[role] += head.get(role, 0.0)
        rows = metrics.role_rows({"roles": dict(roles), "headless": {}}) or []
        out[k] = (round(sum(r[1] for r in rows), 2), [(r[0], r[1]) for r in rows])
    return out


def render(key, f, now, since, first, spent=None) -> str:
    """The facts text Main receives (English, data for Main's reply)."""
    window = "the last 24 hours" if first else "the last brief"
    out = [f"Daily brief for {key}, {now.strftime('%A %Y-%m-%d %H:%M')}. "
           f"Since {since.astimezone().strftime('%Y-%m-%d %H:%M')} ({window})."]
    for title, lines in (("Landed", f["landed"]), ("Ended without landing", f["ended"]),
                         ("Waiting for the owner, oldest first", f["waiting"]),
                         ("Held or stalled", f["held"])):
        if lines:
            out += [f"{title} ({len(lines)}):", *_listed(lines)]
    yesterday = (now.date() - _dt.timedelta(days=1)).isoformat()
    if spent is not None:
        total, rows = spent
        out.append(f"Spend yesterday ({yesterday}, list price): ${total:.2f}"
                   + (" (" + ", ".join(f"{role} ${usd:.2f}" for role, usd in rows) + ")" if rows else ""))
    else:
        out.append(f"Spend yesterday ({yesterday}): not available")
    return "\n".join(out) + "\n"


def summary(f) -> str:
    return (f"daily brief: {len(f['landed'])} landed, {len(f['ended'])} ended without landing, "
            f"{len(f['waiting'])} wait for the owner, {len(f['held'])} held or stalled")


# ---- build (the janitor's pass) and delivery (the heartbeat) --------------------------------------

def build(reg, now=None, cfg_all=None, root=None) -> list:
    """Build each project's brief for the current brief day, once (the janitor's pass); its lines. Nothing
    while it is off or inside quiet hours. Never raises for one project."""
    now = now or units.now()
    cfg_all = cfg_all if cfg_all is not None else config.load()
    if not enabled(cfg_all) or owner.in_quiet_hours(cfg_all, now):
        return []
    d, made = day(cfg_all, now), {}
    for key in sorted(reg.get("projects", {})):
        rec = read(key)
        if rec.get("day") == d:
            continue
        last = units.parse_iso(rec.get("last_sent"))
        since = last or now - FIRST
        try:
            f = facts(reg, key, now, since)
        except Exception as exc:  # noqa: BLE001 - one project never stops the pass
            log.log("hooks", f"daily: facts {key}: {type(exc).__name__}: {exc}")
            f = None
        made[key] = (rec, since, last is None, f)
    try:
        spent = spend(reg, [k for k, v in made.items() if something(v[3])], now, root)
    except Exception as exc:  # noqa: BLE001 - the brief goes without its spend line
        log.log("hooks", f"daily: spend: {type(exc).__name__}: {exc}")
        spent = {}
    lines = []
    for key, (rec, since, first, f) in made.items():
        new = {"day": d, "at": units.iso(now), "since": units.iso(since), "text": None, "summary": None,
               "sent_at": None, "last_sent": rec.get("last_sent")}
        if something(f):
            new["text"], new["summary"] = render(key, f, now, since, first, spent.get(key)), summary(f)
            util.atomic_write(report_path(key), new["text"])
        util.write_json(_path(key), new)
        lines.append(f"{key}: daily brief " + ("built: " + new["summary"][len("daily brief: "):] if new["text"]
                                                 else "not needed (nothing to say)"))
    if lines:
        log.log("events", f"daily built={sum(1 for v in made.values() if something(v[3]))} quiet={len(lines)}")
    return [f"daily brief ({d}):", *(f"  {ln}" for ln in lines)] if lines else []


def heartbeat(reg, now, dry_run=False, keys=None, cfg_all=None) -> list:
    """The heartbeat's delivery: each brief built for the current day and not yet sent, outside quiet hours,
    the project not paused, becomes one waking pointer to Main. Rows in the pass's shape. Never raises."""
    cfg_all = cfg_all if cfg_all is not None else config.load()
    if not enabled(cfg_all) or owner.in_quiet_hours(cfg_all, now):
        return []
    d, rows, r = day(cfg_all, now), [], roster.Roster(reg)
    for key in sorted(reg.get("projects", {})):
        if keys is not None and key not in keys:
            continue
        try:
            rec = read(key)
            main = r.main(key)
            if (rec.get("day") != d or not rec.get("text") or rec.get("sent_at") or main is None
                    or guard.paused(key) is not None):
                continue
            line = f"post the daily brief to {main.session} ({rec.get('summary')})"
            if not dry_run:
                sent = {**rec, "sent_at": units.iso(now), "last_sent": rec.get("at")}
                util.write_json(_path(key), sent)            # stamped first: never twice for one day
                try:
                    inbox.post(inbox.make(DAILY_UNIT, "orche", main.session, "info", brief_rev=0, urgent=True,
                                          summary=rec["summary"], report_path=str(report_path(key))))
                except Exception:
                    util.write_json(_path(key), rec)        # the next pass tries again
                    raise
            rows.append({"action": "daily-brief", "class": "daily", "project": key, "target": main.session,
                         "rev": None, "attempt": 1, "cap": 1, "failed": None,
                         "line": ("would " if dry_run else "") + line})
        except Exception as exc:  # noqa: BLE001 - one project never stops the pass
            log.log("hooks", f"daily: deliver {key}: {type(exc).__name__}: {exc}")
    return rows
