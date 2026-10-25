"""The resume card (0.9.0): what a cleared or restarted Main needs to pick up the project's work without
the owner explaining it again. Everything comes from files (briefs, inboxes, the merge queue), never
from the conversation, so `/clear` loses nothing.

`[orche] resume <project>`, then, newest first and at most MAX_OPEN, each open lineage: its root brief's
GOAL (first sentence), its units' states and PRs, the newest report (STATUS — first summary line,
masked) and what it waits on (`waits: owner` / `waits: <unit>` / `next: <hint>`); then the project's
`owner:`, `unread:` and `blocked:` lines as `orche status` prints them, `queue: w/t/m`, and the last
MAX_CLOSED closed lineages. At most MAX_LINES lines and MAX_CHARS characters. Shown by `orche resume`
and by SessionStart for Main (hooks/session_start.py)."""
import re

from . import mask

MAX_OPEN, MAX_CLOSED, MAX_LINES, MAX_CHARS, WIDTH = 8, 3, 40, 3000, 160
_SENTENCE = re.compile(r"^(.+?[.!?。])(?:\s|$)")
_KEEP = ("  owner:", "  unread:", "  blocked:")


def _one(text, width=WIDTH):
    """The first line of `text`, masked, at most `width` characters."""
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    first = mask.redact(first)[0]
    return first if len(first) <= width else first[:width - 1] + "…"


def goal(ms, lineage):
    """The first sentence of the lineage's root brief's GOAL ("" without one)."""
    head = next((b for b, *_ in ms if b.unit == lineage), ms[0][0] if ms else None)
    text = " ".join(str((head.fields or {}).get("GOAL") or "").split()) if head is not None else ""
    m = _SENTENCE.match(text)
    return _one(m.group(1) if m else text, 100)


def last(ms):
    """{unit, status, summary} of the lineage's newest report; None before any."""
    from .flow import lineage
    best = None
    for b, found, _s, _state in ms:
        if found:
            k = lineage._key(*found)
            if best is None or k > best[0]:
                best = (k, b, found[1])
    if best is None:
        return None
    _k, b, p = best
    return {"unit": b.unit, "status": str(p.get("status") or "?").upper(), "summary": _one(p.get("summary"))}


def waits(ms, project):
    """What the open lineage waits on: the owner (a blocked or question unit), else its first unfinished
    unit, else a next step."""
    from .flow import lineage
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


def _done(r):
    counts = {}
    for _b, _f, s, _state in r["members"]:
        counts[s] = counts.get(s, 0) + 1
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))


def data(reg, project, now=None, row=None):
    """The card's content as a dict (the `--json` shape)."""
    from . import cmd_status
    row = row if row is not None else cmd_status.status_row(reg, project, now)
    rows = cmd_status.lineage_rows(reg, project, now, ttl=None, detail=True)
    opened = []
    for r in [r for r in rows if r["open"]][:MAX_OPEN]:
        ms = r["members"]
        opened.append({"lineage": r["lineage"], "goal": goal(ms, r["lineage"]), "units": r["units"],
                       "states": r["states"], "prs": r["prs"], "state": cmd_status.states_text(r),
                       "last": last(ms), "waits": waits(ms, project)})
    closed = [{"lineage": r["lineage"], "goal": goal(r["members"], r["lineage"]), "units": r["units"],
               "done": f"DONE: {r['units']} unit{'' if r['units'] == 1 else 's'} ({_done(r)})"}
              for r in [r for r in rows if not r["open"]][:MAX_CLOSED]]
    q = row.get("queue") or {}
    return {"project": project, "open": opened, "open_total": sum(1 for r in rows if r["open"]),
            "lines": [ln for ln in cmd_status.overdue_lines(row) if ln.startswith(_KEEP)],
            "queue": f"{q.get('waiting', 0)}/{q.get('testing', 0)}/{q.get('merged_today', 0)}" if q else "-",
            "closed": closed}


def render(d) -> str:
    out = [f"[orche] resume {d['project']}"]
    if not d["open"]:
        out.append("nothing in flight")
    for o in d["open"]:
        out.append(f"L {o['lineage']}" + (f" — {o['goal']}" if o["goal"] else ""))
        out.append(f"  {o['state']}")
        if o["last"]:
            out.append(f"  last: {o['last']['unit']} {o['last']['status']}"
                       + (f" — {o['last']['summary']}" if o["last"]["summary"] else ""))
        out.append(f"  {o['waits']}")
    if d["open_total"] > len(d["open"]):
        out.append(f"… {d['open_total'] - len(d['open'])} more open: orche status --project {d['project']}")
    out += d["lines"]
    out.append(f"queue: {d['queue']}")
    out += [f"closed L {c['lineage']}" + (f" — {c['goal']}" if c["goal"] else "") + f"  {c['done']}"
            for c in d["closed"]]
    out = [ln if len(ln) <= WIDTH + 40 else ln[:WIDTH + 39] + "…" for ln in out]
    more = f"… the rest: orche status --project {d['project']}"
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES - 1] + [more]
    while len("\n".join(out)) > MAX_CHARS:
        out = out[:-2] + [more]
    return "\n".join(out)


def card(reg, project, now=None) -> str:
    """The resume card of `project` (a registry key)."""
    return render(data(reg, project, now))


def has_open(reg, project) -> bool:
    """Whether `project` has an open lineage (a member not yet done, failed or stopped)."""
    from .flow import lineage
    reads = lineage.Reads(reg)
    return any(s not in lineage.TERMINAL for name in reads.groups(project)
               for _b, _f, s in lineage.members(reg, project, name, reads))
