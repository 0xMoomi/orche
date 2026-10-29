"""The project read model: the facts `orche status` and Main's SessionStart card read about one
project. Everything comes from files (briefs, inboxes, run records, the merge queue), never from a
conversation, so `/clear` loses nothing; lib/orche/cmd_status.py renders them (a row per project and the
detail view); card_text() renders the card `orche status <project>` prints and SessionStart shows Main.

lineages(): one row per lineage, newest first (with `members`: its briefs and newest reports). card(): the
newest MAX_OPEN open lineages, each with its root brief's GOAL (first sentence), its last report (masked)
and what it waits on; the owner's open ask, the unread reports and the units waiting on the owner; the
merge queue; the last MAX_CLOSED closed lineages. blocked() and queue_of() are the status row's too."""
import datetime as _dt
import json
import re

from . import brief, inbox, mask, roster, runs, units
from .flow import lineage, owner, usage

MAX_OPEN, MAX_CLOSED, WIDTH = 8, 3, 160
STATE_ORDER = ("merged", "queued", "running", "held", "returned", "done", "info", "failed", "blocked", "question",
               "stopped")
CARD_LINES, CARD_CHARS = 40, 3000   # the card's budget
DETAIL_TTL = _dt.timedelta(hours=24)    # lineages(): closed lineages this recent
WAITING_TTL = _dt.timedelta(hours=24)   # blocked(): a blocked or question pointer this young
_SENTENCE = re.compile(r"^(.+?[.!?。])(?:\s|$)")


def queue_of(key):
    """The project's merge-queue counts (queue.summary without its entries) and its closed record; None
    when its file is unreadable."""
    from . import queue
    try:
        s = queue.summary(key)
    except Exception:  # noqa: BLE001 - a status line never fails the command
        return None
    return {k: v for k, v in s.items() if k not in ("entries", "project", "last")}


def spent_today(reg, key, now=None):
    """The project's spend today (USD, list price) from its Claude Code transcripts, as `orche metrics`
    counts them: the project root's and every division's and feature's worktree's project dir, from local
    midnight. None (shown `n/a`) when the transcript root or a transcript cannot be read. Headless calls
    without a transcript (sealed and blank: reviews, triage) are not in it."""
    from . import metrics
    from .hr import panes
    from .janitor import discover
    now = now or units.now()
    proj = reg["projects"][key]
    trees = [proj.get("path") or ""] + [units.worktree_path(e.get("worktree") or "")
                                        for d in (proj.get("divisions") or {}).values()
                                        for e in [d or {}, *((d or {}).get("features") or [])]]
    names = {discover.claude_slug(discover.real(t)) for t in trees if discover.real(t)}
    try:
        return metrics.spent_since(panes.claude_projects(), names,
                                   now.replace(hour=0, minute=0, second=0, microsecond=0), now)
    except Exception:  # noqa: BLE001 - a status line never fails the command; unknown is n/a
        return None


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
        if ptr.get("status") not in lineage.WAITING or now - t >= WAITING_TTL:
            continue
        b = brief.find(reg, unit, project=key)
        answered = _mtime(b.path) if b is not None else None
        if answered is not None and answered > t:
            continue
        out.append({"unit": unit, "status": ptr["status"], "since": t.astimezone().strftime("%H:%M"),
                    "minutes": int((now - t).total_seconds() // 60)})
    return out


def lineages(reg, key, now=None, ttl=DETAIL_TTL, members=False) -> list:
    """One row per lineage of the last `ttl` (None: any age) or still open, newest first: {lineage, units,
    states: {state: n}, prs, spend, since, open}. A unit's state: running (a live run), held (a usage hold),
    queued (a merge-queue entry), the queue's last word on it (merged, returned, dropped), else its newest
    report's (lineage.members). `members` adds [(Brief, (path, pointer) | None, status, state)] (not JSON)."""
    from . import queue
    from .runs import history
    now = now or units.now()
    try:
        s = queue.summary(key)
    except Exception:  # noqa: BLE001 - a status line never fails the command
        s = {"entries": [], "last": {}}
    queued, said = {e.get("unit") for e in s["entries"]}, s["last"]
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
            rec = runs.last(b.folder) or {}
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
                     "open": is_open, "newest": max(times).isoformat(), **({"members": mine} if members else {})})
    return sorted(rows, key=lambda r: r["newest"], reverse=True)


def _one(text, width=WIDTH):
    """The first line of `text`, masked, at most `width` characters."""
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    first = mask.redact(first)[0]
    return first if len(first) <= width else first[:width - 1] + "…"


def goal(ms, name):
    """The first sentence of the lineage's root brief's GOAL ("" without one)."""
    head = next((b for b, *_ in ms if b.unit == name), ms[0][0] if ms else None)
    text = " ".join(str((head.fields or {}).get("GOAL") or "").split()) if head is not None else ""
    m = _SENTENCE.match(text)
    return _one(m.group(1) if m else text, 100)


def last(ms):
    """{unit, status, summary} of the lineage's newest report; None before any."""
    found = [(lineage.report_key(*f), b, f[1]) for b, f, _s, _state in ms if f]
    if not found:
        return None
    _k, b, p = max(found, key=lambda x: x[0])
    return {"unit": b.unit, "status": str(p.get("status") or "?").upper(), "summary": _one(p.get("summary"))}


def waits(ms, project):
    """What the open lineage waits on: the owner (a blocked or question unit), else its first unfinished
    unit, else a next step."""
    for b, _f, s, _state in ms:
        if s in lineage.WAITING:
            return f"waits: owner — {b.unit} ({s}); answer in main-{project}"
    for b, _f, s, state in ms:
        if s == "external-request":
            return f"next: {b.unit} asked for an external request — act on it or ask the owner"
        if state == "returned":
            return f"next: {b.unit} was returned by the merge queue — fix it and queue it again"
    for b, _f, s, state in ms:
        if s not in lineage.TERMINAL:
            return f"waits: {b.unit} ({state})"
    return "next: read the reports and close or continue the lineage"


def _statuses(ms) -> dict:
    counts = {}
    for _b, _f, s, _state in ms:
        counts[s] = counts.get(s, 0) + 1
    return dict(sorted(counts.items()))


def _unconfirmed(ms) -> int:
    """Members whose newest report says done without a ledger row (the status line's `unverified`)."""
    return sum(1 for _b, f, _s, _state in ms if f and f[1].get("status") == "done" and f[1].get("ledger_row") is False)


def card(reg, key, now=None) -> dict:
    """The card's facts (also the `--json` of `orche status <project>`): `open`, the newest MAX_OPEN open
    lineages {lineage, goal, units, states, prs, last, waits}, and `open_total`; `owner` (flow.owner.status),
    `unread` and `blocked` as the status row has them; `queue` (queue_of); `closed`, the last MAX_CLOSED
    closed lineages {lineage, goal, units, statuses: {status: n}, prs, spend (their runs' cost), unconfirmed
    (done without a ledger row)}; `spent_today` (spent_today, None when unreadable)."""
    rows = lineages(reg, key, now, ttl=None, members=True)
    opened, closed = [r for r in rows if r["open"]], [r for r in rows if not r["open"]][:MAX_CLOSED]
    return {"project": key,
            "open": [{"lineage": r["lineage"], "goal": goal(r["members"], r["lineage"]), "units": r["units"],
                      "states": r["states"], "prs": r["prs"], "last": last(r["members"]),
                      "waits": waits(r["members"], key)} for r in opened[:MAX_OPEN]],
            "open_total": len(opened), "owner": owner.status(key, now),
            "unread": inbox.overdue(roster.Roster(reg), key, now)[1], "blocked": blocked(reg, key, now),
            "queue": queue_of(key),
            "closed": [{"lineage": r["lineage"], "goal": goal(r["members"], r["lineage"]), "units": r["units"],
                        "statuses": _statuses(r["members"]), "prs": r["prs"], "spend": r["spend"],
                        "unconfirmed": _unconfirmed(r["members"])} for r in closed],
            "spent_today": spent_today(reg, key, now)}


def has_open(reg, key) -> bool:
    """Whether `key` has an open lineage (a member not yet done, failed or stopped)."""
    reads = lineage.Reads(reg)
    return any(s not in lineage.TERMINAL for name in reads.groups(key)
               for _b, _f, s in lineage.members(reg, key, name, reads))


# ---- the card's text (`orche status <project>` prints it; SessionStart shows it to Main)

def owner_lines(o) -> list:
    """The open ask (flow.owner.status): `owner: asked HH:MM (<m> min, notified n×[, next HH:MM | waiting
    for quiet hours to end]) — <ask>`; none without one."""
    if not o:
        return []
    tail = ", waiting for quiet hours to end" if o["quiet"] else f", next {o['next']}" if o["next"] else ""
    return [f"  owner: asked {o['since']} ({o['minutes']} min, notified {o['notified']}×{tail}) — {o['text']}"]


def repair_hints(row) -> tuple:
    """(reconcile's live repairs, whether the heartbeat is fresh), read only while a delivery or a report is
    late."""
    from . import reconcile
    reps = reconcile.repairs() if row.get("undelivered") or row["unread"] else {}
    hb = reconcile.heartbeat_json() if reps else {}
    return reps, bool(hb.get("enabled")) and not hb.get("stale", True)


def unread_lines(row, hints) -> list:
    from . import reconcile
    return [f"  unread: {u['session']} has {u['count']} report(s) waiting (oldest {u['minutes']} min) — "
            + reconcile.overdue_hint("unread", u["session"], *hints) for u in row["unread"]]


def blocked_lines(row) -> list:
    return [f"  blocked: {u['unit']} ({u['status']}, since {u['since']}, {u['minutes']} min) — waiting on the "
            f"owner; answer in main-{row['project']}" for u in row["blocked"]]


def states_text(r) -> str:
    """`12 units: 7 merged · 2 queued · …  PRs #41 #43 #44 …` (states in STATE_ORDER)."""
    order = sorted(r["states"].items(), key=lambda kv: (STATE_ORDER.index(kv[0]) if kv[0] in STATE_ORDER
                                                        else len(STATE_ORDER), kv[0]))
    prs = " ".join(f"#{n}" for n in r["prs"][:3]) + (" …" if len(r["prs"]) > 3 else "")
    return f"{r['units']} units: {' · '.join(f'{n} {s}' for s, n in order)}" + (f"  PRs {prs}" if prs else "")


def _prs(prs) -> str:
    return " ".join(f"#{n}" for n in prs[:3]) + (" …" if len(prs) > 3 else "")


def card_text(d) -> str:
    """The card (project.card), in the owner's order: `[orche] resume <project>`; NEEDS YOU (the `owner:`
    and `blocked:` lines as the status row prints them); FINISHED (the closed lineages: GOAL, statuses,
    PRs, their runs' cost, how many are not confirmed); RUNNING (per open lineage its `L` line with the
    GOAL, its states, its last report and what it waits on), or `nothing in flight`; the `unread:` lines;
    `queue: w/t/m · spent today $X`. An empty heading is left out. At most CARD_LINES lines and
    CARD_CHARS characters."""
    from . import metrics
    p, q = d["project"], d["queue"]
    out = [f"[orche] resume {p}"]
    needs = owner_lines(d["owner"]) + blocked_lines(d)
    if needs:
        out += [f"NEEDS YOU ({(1 if d['owner'] else 0) + len(d['blocked'])})"] + needs
    if d["closed"]:
        out.append(f"FINISHED ({len(d['closed'])})")
    for c in d["closed"]:
        prs, n = _prs(c.get("prs") or []), c.get("unconfirmed") or 0
        out.append(f"  closed L {c['lineage']}" + (f" — {c['goal']}" if c["goal"] else "")
                   + f"  DONE: {c['units']} unit{'' if c['units'] == 1 else 's'} ("
                   + ", ".join(f"{n} {s}" for s, n in c["statuses"].items()) + ")"
                   + (f"  PRs {prs}" if prs else "")
                   + (f"  runs ${c['spend']:.2f}" if c.get("spend") else "")
                   + (f"  {n} not confirmed (no ledger row)" if n else ""))
    out += [f"RUNNING ({d['open_total']})"] if d["open"] else ["nothing in flight"]
    for o in d["open"]:
        last = o["last"]
        out += [f"  L {o['lineage']}" + (f" — {o['goal']}" if o["goal"] else ""), f"    {states_text(o)}"]
        if last:
            out.append(f"    last: {last['unit']} {last['status']}"
                       + (f" — {last['summary']}" if last["summary"] else ""))
        out.append(f"    {o['waits']}")
    if d["open_total"] > len(d["open"]):
        out.append(f"  … {d['open_total'] - len(d['open'])} more open: orche status --project {p}")
    out += unread_lines(d, repair_hints(d))
    out.append((f"queue: {q.get('waiting', 0)}/{q.get('testing', 0)}/{q.get('merged_today', 0)}" if q else "queue: -")
               + f" · spent today {metrics.na(d.get('spent_today'), '${:.2f}')}")
    out = [ln if len(ln) <= WIDTH + 40 else ln[:WIDTH + 39] + "…" for ln in out]
    more = f"… the rest: orche status --project {p}"
    if len(out) > CARD_LINES:
        out = out[:CARD_LINES - 1] + [more]
    while len("\n".join(out)) > CARD_CHARS:
        out = out[:-2] + [more]
    return "\n".join(out)
