"""Lineage and parent: which chain of work every brief and report belongs to.

Each BRIEF.md header carries `lineage: <id>   parent: <unit|->`. A lineage id is the unit that started
it; a brief without the line (written before lineages) is its own lineage with no parent. Pointers copy both fields
from the unit's brief.

Assignment for a new brief (AMEND keeps both):
  division  parent = the division's current unit (brief.newest), lineage inherited from it
  triage    starts a lineage
  Main      joins lineage L when the turn began with an ORCHE line ([orche] …) and the pointers
            injected into it carry exactly one lineage L; otherwise the turn's first new brief starts
            a lineage that its later new briefs share (parent: -)

The turn record `ORCHE_HOME/sessions/<session>.turn.json` {owner, lineages, at, started} is written
by the prompt hook for Main and read here.

Batch and close: a member (a brief whose lineage is L) ends when the newest report for its current rev,
from its target to the session its reports go to (route: roster.recipient), is `done`, `failed`, `stop`,
a runner `info` without `retry` (stopped), `blocked` or `question` (STOPS); or when no run of it is live
and its target is unknown or retired (gone), or a `via: dispatch` STOP at its rev or later from its sender
or Main sits in the target's inbox (stop): both count as stopped. It settles once it ended, every child
(a member whose `parent:` is its unit) settled and, when it has children and is not gone, its newest
report is not a runner filing and is newer than every child's (a head files its unit after its workers).
A session waits on the members routed to it (Main: the lineage's roots) and everything under them.
Under `ORCHE_HOME/flow/<p>/.lock`, a report that ends its unit, would wake, is filed on its unit's route
and at its rev, while its recipient waits on two or more units, is posted `quiet: "batch"` (flow.shape;
an older rev's report is posted `quiet: true`); blocked, question, an owner step and urgent pointers
wake at once. The report that leaves a session's batch settled posts one close from `orche`: to Main it
writes `<root>/.orche/units/<L>/LINEAGE.md` and records the close (members, revs, statuses) in
`flow/<p>/closed.json`, so it is posted once per signature; to any other session it is one `done`
(`lineage_closed: true`) while a held pointer of L is pending and no waking pointer is. A post that
cannot take the lock within LOCK_WAIT_S writes no close; the heartbeat posts Main's missed close
(repair), a session's missed close, and wakes a hold whose units are all past their TIMEBOX deadline,
or whose oldest held pointer is RECENT old, once (holds).
"""
import datetime as _dt
import fcntl
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .. import brief, inbox, log, paths, util

def turn_path(session):
    return paths.orche_home() / "sessions" / f"{util.slugify(session)}.turn.json"


def read_turn(session):
    return util.read_dict(turn_path(session))


def write_turn(session, owner, lineages, at=None):
    """A new turn began for `session`: `owner` (the owner typed it) and the injected pointers' lineages.
    `at` is the time the turn read its inbox (an aware datetime); without one, now."""
    path = turn_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = at.isoformat(timespec="seconds") if at is not None else util.now_iso()
    util.write_json(path, {"owner": bool(owner), "lineages": sorted(set(lineages)), "at": stamp,
                           "started": None})


def begin_turn(session, owner):
    """The prompt hook's first act for Main, before anything that can raise: the record becomes
    {owner, lineages: [], started: None}, so a hook that fails later never leaves the previous turn's
    lineages in force (no lineage starts a new one: the safe error). A failed write removes the old
    record. Never raises."""
    try:
        write_turn(session, owner, [])
    except Exception as exc:  # noqa: BLE001 - the prompt goes on; the old record must not survive
        log.log("hooks", f"lineage: turn {session}: {type(exc).__name__}: {exc}")
        try:
            turn_path(session).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def of_pointer(ptr, reg=None, project=None):
    """The lineage a pointer names: its `lineage` field; without one (ORCHE's own `relayed …`, deliverer
    and heartbeat notes, a report written before lineages) the lineage of its unit's brief in `project`, and "" when no
    brief holds that unit — such a note never names a lineage after its unit. Never raises."""
    if ptr.get("lineage"):
        return str(ptr["lineage"])
    if reg is None:
        return ""
    try:
        b = brief.find(reg, str(ptr.get("unit") or ""), project=project)
    except Exception:  # noqa: BLE001 - a malformed registry entry names no lineage
        return ""
    return b.lineage if b is not None and b.rev else ""


def fields(b):
    """The pointer fields for brief `b` (None: no brief → no fields)."""
    if b is None or not b.rev:
        return {}
    return {"lineage": b.lineage, "parent": b.parent or None}


AUTOMATED = ("triage", "janitor")   # relayers whose briefs start their own lineage, outside Main's turn


def assign(reg, frm, by=None):
    """(lineage, parent) for a new brief from Member `frm`: lineage None = start one with the new unit."""
    if by in AUTOMATED:
        return None, None
    if frm.kind == "div":
        p = brief.newest(reg, frm.project, frm.session)
        return (p.lineage, p.unit) if p is not None else (None, None)
    turn = read_turn(frm.session)
    if turn is None:
        return None, None
    lineages = turn.get("lineages") or []
    if not turn.get("owner") and len(lineages) == 1:
        return lineages[0], None
    return turn.get("started"), None


def started(frm, lineage):
    """Main's turn started `lineage`: the turn's later new briefs share it."""
    turn = read_turn(frm.session)
    if turn is not None and frm.kind != "div" and not turn.get("started"):
        turn["started"] = lineage
        util.write_json(turn_path(frm.session), turn)


def depth(reg, b):
    """How many briefs deep `b` is along its `parent:` chain (a brief with no parent is 1)."""
    seen, n = {b.unit}, 1
    while b.parent and b.parent not in seen:
        nxt = brief.find(reg, b.parent, project=b.project)
        if nxt is None or not nxt.rev:
            break
        seen.add(nxt.unit)
        b, n = nxt, n + 1
    return n


def children(reg, b, dirs, since=None):
    """How many briefs in `dirs` (unit folders of b's project) name `b` as their parent; with `since` (a
    timestamp) only those first sent then or later (BRIEF.rev1.md's mtime, else BRIEF.md's)."""
    n = 0
    for d in dirs:
        c = brief.find(reg, d.name, project=b.project)
        if c is not None and c.rev and c.parent == b.unit and (since is None or _sent(d) >= since):
            n += 1
    return n


def _sent(folder_):
    for name in ("BRIEF.rev1.md", "BRIEF.md"):
        try:
            return (folder_ / name).stat().st_mtime
        except OSError:
            continue
    return 0.0


# ---- close --------------------------------------------------------

TERMINAL = ("done", "failed", "stopped")
OPEN = ("blocked", "question", "external-request")
WAITING = ("blocked", "question")
STOPS = TERMINAL + WAITING          # a unit's report that ends it
LOCK_WAIT_S, LOCK_POLL_S = 5.0, 0.02
RECENT = _dt.timedelta(hours=24)    # repair and status look only at lineages this young
_ENDS = re.compile(r"^to: (\S+)\s+from: (\S+)\s*$", re.M)


@dataclass(frozen=True)
class Scope:
    reg: dict
    project: str
    lineage: str


def folder(project):
    return paths.orche_home() / "flow" / util.slugify(project)


def scope(ptr):
    """The Scope of a unit session's report that carries a lineage; None for anything else. Never raises."""
    if (ptr.get("lineage_closed") or not ptr.get("lineage") or ptr.get("from") in (None, "orche")
            or ptr.get("via") == "dispatch"):
        return None
    try:
        from .. import registry, roster
        reg = registry.load()
        r = roster.Roster(reg)
        m = r.member(ptr.get("from")) or r.member(ptr.get("to"))
        return Scope(reg, m.project, str(ptr["lineage"])) if m is not None else None
    except Exception as exc:  # noqa: BLE001 - a lineage never costs a pointer
        log.log("hooks", f"lineage: scope {ptr.get('unit')}: {type(exc).__name__}: {exc}")
        return None


@contextmanager
def lock(sc):
    """Hold `flow/<project>/.lock` for `sc`; yields whether it is held: False for None, and when another
    holder kept it LOCK_WAIT_S (the caller then writes no close: the heartbeat posts the missed one)."""
    if sc is None:
        yield False
        return
    try:
        path = folder(sc.project) / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a")
    except OSError as exc:
        log.log("hooks", f"lineage: lock {sc.project}: {exc}")
        yield False
        return
    with fh:
        end, held = time.monotonic() + LOCK_WAIT_S, False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    log.log("hooks", f"lineage: lock {sc.project} busy; the close is left to the heartbeat")
                    break
                time.sleep(LOCK_POLL_S)
        try:
            yield held
        finally:
            if held:
                fcntl.flock(fh, fcntl.LOCK_UN)


def unit_dirs(reg, project):
    from .. import units
    root = units.project_root(reg, project)
    d = root / brief.UNITS if root is not None else None
    try:
        return sorted(p for p in d.iterdir() if p.is_dir()) if d is not None else []
    except OSError:
        return []


def groups(reg, project):
    """{lineage: [Brief]} for every brief in `project` (unit order)."""
    out = {}
    for d in unit_dirs(reg, project):
        b = brief.find(reg, d.name, project=project)
        if b is not None and b.rev:
            out.setdefault(b.lineage, []).append(b)
    return out


class Reads:
    """One call's reads (settle, repair, lines, stalled): the roster, each project's briefs (groups) and
    each inbox (pending and drained) read once."""

    def __init__(self, reg, known=None):
        from .. import roster
        self.reg = reg
        self.roster = roster.Roster(reg)
        self._groups = dict(known or {})
        self._boxes = {}

    def groups(self, project):
        if project not in self._groups:
            self._groups[project] = groups(self.reg, project)
        return self._groups[project]

    def about(self, session, unit, frm):
        """[(path, pointer)] in `session`'s inbox for `unit` from `frm` (ValueError: an unsafe name)."""
        if session not in self._boxes:
            by = {}
            for path, p in inbox.about_all(session):
                by.setdefault((p.get("unit"), p.get("from")), []).append((path, p))
            self._boxes[session] = by
        return self._boxes[session].get((unit, frm), [])


def _ends(b):
    m = _ENDS.search(b.text)
    return (m.group(1), m.group(2)) if m else ("", "")


def report_key(path, p):
    """A report pointer's newest-last order: its `at`, then its file's mtime."""
    t = inbox.pointer_time(path, p)
    try:
        ns = path.stat().st_mtime_ns
    except OSError:
        ns = 0
    return (t.timestamp() if t is not None else 0.0, ns)


def route(b, reads):
    """(target, the session `b`'s reports go to): roster.recipient of the header's ends, else its sender."""
    target, sender = _ends(b)
    return target, ((reads.roster.recipient(target, sender) if target else None) or sender)


def _newest(b, reads):
    """(path, pointer) of the newest report for `b` at its rev, from its target to the session its reports
    go to (roster.recipient: the sender while it is a live Main or division, else the target's division
    or Main); None."""
    target, to = route(b, reads)
    try:
        found = reads.about(to, b.unit, target) if target and to else []
    except ValueError:
        return None
    best = None
    for path, p in found:
        if p.get("via") == "dispatch" or str(p.get("brief_rev")) != str(b.rev):
            continue
        k = report_key(path, p)
        if best is None or k > best[0]:
            best = (k, path, p)
    return (best[1], best[2]) if best else None


def status_of(p):
    """A member's status from its newest report (None: no report yet → running)."""
    if p is None:
        return "running"
    s = p.get("status")
    if s == "stop" or (s == "info" and p.get("via") == "runner" and not p.get("retry")):
        return "stopped"
    return s if s in TERMINAL + OPEN else "running"


def members(reg, project, lineage, reads=None):
    """[(Brief, (path, pointer) | None, status)] for `lineage` in `project`."""
    reads = reads or Reads(reg)
    out = []
    for b in reads.groups(project).get(lineage, []):
        found = _newest(b, reads)
        out.append((b, found, status_of(found[1] if found else None)))
    return out


def _rev(p):
    """A pointer's `brief_rev` as an int (missing: 1)."""
    return int(p.get("brief_rev") or 1)


def _ended_by(b, reads):
    """How `b` ended without a report of its own: "gone" (no live run, its target unknown or retired),
    "stop" (no live run, a `via: dispatch` STOP at its rev or later from its sender or Main sits in the
    target's inbox, pending or drained); else None. The STOP marker never counts: an AMEND writes one."""
    from .. import runs
    if runs.running(b.project, b.unit) is not None:
        return None
    target, _to = route(b, reads)
    m = reads.roster.member(target) if target else None
    if m is None or m.status == "retired":
        return "gone"
    main = reads.roster.main(b.project)
    for frm in {_ends(b)[1], main.session if main is not None else ""} - {""}:
        try:
            found = reads.about(target, b.unit, frm)
        except ValueError:
            continue
        for _path, p in found:
            try:
                if p.get("via") == "dispatch" and p.get("status") == "stop" and _rev(p) >= b.rev:
                    return "stop"
            except (TypeError, ValueError):
                continue
    return None


def _effective(reads, ms):
    """`ms` with `stopped` for a member that has not ended by its report but is gone or was stopped."""
    return [(b, f, "stopped" if s not in STOPS and _ended_by(b, reads) is not None else s) for b, f, s in ms]


def top(reads, bs, session):
    """The briefs among `bs` (one lineage) that `session` waits on directly: the roots (parent not a
    member) for Main or None, else those whose reports go to `session`."""
    main = reads.roster.main(bs[0].project) if bs else None
    if session is None or (main is not None and session == main.session):
        units_ = {b.unit for b in bs}
        return [b for b in bs if b.parent not in units_]
    return [b for b in bs if route(b, reads)[1] == session]


def _under(bs, tops):
    """`tops` and every descendant of them among `bs` by `parent`, in member order."""
    keep, grew = {b.unit for b in tops}, True
    while grew:
        grew = False
        for b in bs:
            if b.unit not in keep and b.parent in keep:
                keep.add(b.unit)
                grew = True
    return [b for b in bs if b.unit in keep]


def descendants(reg, b, reads=None):
    """[(Brief, status)] for every unit under `b` by `parent` in its lineage, in member order, with
    the effective status (_effective: a gone or STOPped member reads `stopped`)."""
    reads = reads or Reads(reg)
    ms = _effective(reads, members(reg, b.project, b.lineage, reads))
    bs = [m for m, _f, _s in ms]
    mine = next((m for m in bs if m.unit == b.unit), None)
    under = {m.unit for m in _under(bs, [mine])} - {b.unit} if mine is not None else set()
    return [(m, s) for m, _f, s in ms if m.unit in under]


def _whys(reads, ms):
    """{unit: why it is not settled | None (settled)} over effective members `ms`."""
    by = {b.unit: (b, f, s) for b, f, s in ms}
    kids = {}
    for b, _f, _s in ms:
        if b.parent in by and b.parent != b.unit:
            kids.setdefault(b.parent, []).append(b.unit)
    memo = {}

    def why(u):
        if u in memo:
            return memo[u]
        memo[u] = None                       # re-entered: settled (parents are older briefs)
        b, f, s = by[u]
        ks = kids.get(u, [])
        if s not in STOPS:
            memo[u] = s
        elif any(why(k) is not None for k in ks):
            memo[u] = "units running"
        elif ks and _ended_by(b, reads) != "gone":
            newest = max((report_key(*by[k][1]) for k in ks if by[k][1]), default=None)
            mine = f[1] if f else None
            if mine is None or mine.get("via") == "runner" or (newest is not None and report_key(*f) <= newest):
                memo[u] = "report due"
        return memo[u]
    return {u: why(u) for u in by}


def waiting(reads, project, lineage, session=None, ms=None):
    """[(Brief, why)] of the units `session` (None: Main) waits on directly in `lineage` that have not
    settled; [] = its batch is complete. `ms`: the lineage's effective members, when already read."""
    if ms is None:
        ms = _effective(reads, members(reads.reg, project, lineage, reads))
    whys = _whys(reads, ms)
    return [(b, whys[b.unit]) for b in top(reads, [b for b, _f, _s in ms], session) if whys[b.unit] is not None]


def quiets(sc, ptr):
    """How `ptr` (a report of `sc`) is posted quiet: "batch" when it would wake, ends its unit (TERMINAL),
    is filed on its unit's route at the unit's rev, carries no owner step and is not urgent, while its
    recipient waits on two or more units of the lineage (descendants counted); True for such a report
    at an older rev; else False."""
    if (sc is None or not inbox.wakes(ptr) or ptr.get("urgent") or ptr.get("owner")
            or status_of(ptr) not in TERMINAL):
        return False
    reads = Reads(sc.reg)
    bs = reads.groups(sc.project).get(sc.lineage, [])
    b = next((x for x in bs if x.unit == ptr.get("unit")), None)
    if b is None or route(b, reads) != (ptr.get("from"), ptr.get("to")):
        return False
    if _rev(ptr) < b.rev:
        return True
    return "batch" if len(_under(bs, top(reads, bs, ptr["to"]))) >= 2 else False


def _counts(ms):
    return {s: sum(1 for _b, _f, x in ms if x == s) for s in STOPS}


def _tally(ms):
    """`d done, f failed, s stopped`, then `b blocked`, `q question` when not zero."""
    c = _counts(ms)
    return ", ".join([f"{c[s]} {s}" for s in TERMINAL] + [f"{c[s]} {s}" for s in WAITING if c[s]])


def _closed_path(project):
    return folder(project) / "closed.json"


def _signature(ms):
    return [f"{b.unit}@{b.rev}:{s}" for b, _f, s in ms]


def _recorded(project):
    """closed.json {lineage: {members, at}}; {} when it is missing, corrupt or unreadable."""
    try:
        data = util.read_json(_closed_path(project), default={})
    except (util.JsonError, OSError):
        data = {}
    return data if isinstance(data, dict) else {}


def _record(project, lineage, ms):
    rec = _recorded(project)
    rec[lineage] = {"members": _signature(ms), "at": util.now_iso()}
    path = _closed_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    util.write_json(path, rec)


def close(reg, project, lineage, ms, reads=None):
    """Write LINEAGE.md (atomically), then the close pointer to Main, then the close's record in
    closed.json (a crash in between leaves a pointer that repair finds and only records); the pointer,
    or None (no Main)."""
    main = (reads or Reads(reg)).roster.main(project)
    head = next((b for b, _f, _s in ms if b.unit == lineage), ms[0][0])
    md = head.root / brief.UNITS / lineage / "LINEAGE.md"
    rows = ["| unit | to | rev | status | report |", "|---|---|---|---|---|"]
    for b, found, s in ms:
        report = (found[1].get("report_path") if found else "") or "-"
        rows.append(f"| {b.unit} | {_ends(b)[0] or '-'} | {b.rev} | {s} | {report} |")
    util.atomic_write(md, f"# Lineage {lineage}\n\nclosed: {util.now_iso()}   units: {len(ms)}, {_tally(ms)}\n\n"
                          + "\n".join(rows) + "\n")
    ptr = None
    if main is not None:
        summary = "\n".join([f"lineage {lineage} closed: {len(ms)} units: {_tally(ms)}"]
                            + [f"{b.unit} {s}" for b, _f, s in ms[:4]])
        ptr = inbox.make(lineage, "orche", main.session, "done", report_path=str(md), brief_rev=head.rev,
                         summary=summary, lineage=lineage, lineage_closed=True)
        inbox.write(ptr)
    _record(project, lineage, ms)
    log.log("events", f"lineage closed {lineage} project={project} n={len(ms)}")
    _finished(project, lineage, ms)
    return ptr


def _finished(project, lineage, ms):
    """After Main's close: the owner's finish notice (notify.finished: the root GOAL, the counts, the PRs,
    the members' headless cost, the reports done without a ledger row), unless a member waits on the
    owner (that ask notifies already); then the status line. Never raises."""
    try:
        from .. import notify, runs, statusline
        from .. import project as facts
        from ..runs import history
        if not any(s in WAITING for _b, _f, s in ms):
            prs = set()
            for b, _f, _s in ms:
                pr = (runs.last(b.folder) or {}).get("pr")
                pr = pr.get("number") if isinstance(pr, dict) else pr
                if isinstance(pr, int):
                    prs.add(pr)
            notify.finished(project, facts.goal(ms, lineage), {s: n for s, n in _counts(ms).items() if s in TERMINAL},
                            sorted(prs), history.units_spend(project, {b.unit for b, _f, _s in ms}),
                            sum(1 for _b, f, s in ms if f and s == "done" and f[1].get("ledger_row") is False))
        statusline.touch(project)
    except Exception as exc:  # noqa: BLE001 - a notice never costs the close
        log.log("hooks", f"lineage: finish notice {lineage}: {type(exc).__name__}: {exc}")


def _head_close(reads, project, lineage, session, ms):
    """Write the close of `session`'s batch in `lineage` (no LINEAGE.md, no record); the pointer."""
    bs = [b for b, _f, _s in ms]
    mine = {b.unit for b in _under(bs, top(reads, bs, session))}
    sub = [(b, f, s) for b, f, s in ms if b.unit in mine]
    head = next((b for b in bs if b.unit == lineage), bs[0])
    summary = "\n".join([f"lineage {lineage}: every unit you wait on ended: {len(sub)} units, {_tally(sub)}"]
                        + [f"{b.unit} {s}" for b, _f, s in sub[:4]])
    ptr = inbox.make(lineage, "orche", session, "done", brief_rev=head.rev, summary=summary, lineage=lineage,
                     lineage_closed=True)
    inbox.write(ptr)
    log.log("events", f"lineage batch closed {lineage} project={project} session={session} n={len(sub)}")
    return ptr


def _due(reads, project, lineage):
    """(effective members, due): two or more members, Main's batch complete, and its close not yet
    recorded for these members."""
    ms = _effective(reads, members(reads.reg, project, lineage, reads))
    if len(ms) < 2 or waiting(reads, project, lineage, None, ms):
        return ms, False
    rec = _recorded(project).get(lineage)
    return ms, not (isinstance(rec, dict) and rec.get("members") == _signature(ms))


def _holding(session, lineage=None):
    """{lineage: oldest pending `quiet: "batch"` pointer time} in `session`'s inbox (only `lineage`'s
    when given); {} when a waking pointer is pending (that wake reads them) or the name is unsafe."""
    try:
        items = inbox.pending(session)
    except ValueError:
        return {}
    if any(inbox.wakes(p) for _x, p in items):
        return {}
    out = {}
    for path, p in items:
        lin = str(p.get("lineage") or "")
        if p.get("quiet") != "batch" or not lin or (lineage is not None and lin != lineage):
            continue
        t = inbox.pointer_time(path, p)
        if t is not None and (lin not in out or t < out[lin]):
            out[lin] = t
    return out


def settle(sc, ptr):
    """After `ptr` (a report of `sc`) was written under the held lock: post the close of its recipient's
    batch when that is complete and holds a pointer of the lineage (not Main), then Main's close when
    due. The pointers written. Raises what a close raises (inbox.post then wakes the report's recipient,
    so a failed close never leaves a held report silent)."""
    if sc is None:
        return []
    reads, out = Reads(sc.reg), []
    main = reads.roster.main(sc.project)
    to = ptr.get("to")
    if to and (main is None or to != main.session) and _holding(to, sc.lineage):
        ms = _effective(reads, members(sc.reg, sc.project, sc.lineage, reads))
        if ms and not waiting(reads, sc.project, sc.lineage, to, ms):
            out.append(_head_close(reads, sc.project, sc.lineage, to, ms))
    ms, due = _due(reads, sc.project, sc.lineage)
    if due:
        c = close(sc.reg, sc.project, sc.lineage, ms, reads)
        if c is not None:
            out.append(c)
    return out


def _covered(reads, main, lineage, ms):
    """A close pointer for `lineage` in `main`'s inbox (pending or drained) is newer than every member's
    newest report: that close went out, only its record is missing."""
    newest = max((report_key(f[0], f[1]) for _b, f, _s in ms if f), default=None)
    return newest is not None and any(p.get("lineage_closed") and report_key(path, p) > newest
                                      for path, p in reads.about(main, lineage, "orche"))


def _missed(reads, project, lineage, now):
    """(members, covered) when `lineage`'s close is due and its newest report is within RECENT (covered:
    a close pointer newer than those reports is already in Main's inbox); else None."""
    ms, due = _due(reads, project, lineage)
    if not due:
        return None
    times = [inbox.pointer_time(f[0], f[1]) for _b, f, _s in ms if f]
    if not times or any(t is None for t in times) or now - max(times) > RECENT:
        return None
    main = reads.roster.main(project)
    return ms, main is not None and _covered(reads, main.session, lineage, ms)


def _young(bs, cutoff):
    """Some member's BRIEF.md was written at or after `cutoff` (a timestamp)."""
    for b in bs:
        try:
            if b.path.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def repair(reg, dry_run=False, out=None, now=None, keys=None):
    """Heartbeat: post the close a crash or a busy lock missed — final, unrecorded, newest report within
    RECENT, and no close pointer newer than its reports in Main's inbox (one already there is only
    recorded, so a lost or corrupt closed.json re-posts nothing). Per project (`keys`: only those) its
    briefs are read once and lineages whose BRIEF.md files are all older than RECENT are skipped before
    any report is read; then, under the project's lock (busy: left to the next pass), each inbox is read
    once. `dry_run` takes no lock and writes nothing. Returns [(project, lineage)] closed (dry_run: due);
    `out` gets one line each. Never raises."""
    from .. import units
    now = now or units.now()
    cutoff = (now - RECENT).timestamp()
    done = []
    for project in sorted(reg.get("projects") or {}):
        if keys is not None and project not in keys:
            continue
        try:
            gs = groups(reg, project)
            young = [lin for lin, bs in gs.items() if len(bs) >= 2 and _young(bs, cutoff)]
            if young:
                done += [(project, lin) for lin in _repair(reg, project, gs, young, now, dry_run)]
        except Exception as exc:  # noqa: BLE001 - one project never stops the pass
            log.log("hooks", f"lineage: repair {project}: {type(exc).__name__}: {exc}")
    if out is not None:
        for project, lin in done:
            out(f"{'would close' if dry_run else 'closed'} lineage {lin} ({project})")
    return done


def _repair(reg, project, gs, lins, now, dry_run):
    """The lineages among `lins` whose missed close this pass posts (dry_run: would post)."""
    reads = Reads(reg, {project: gs})
    if dry_run:
        return [lin for lin in lins if (m := _missed(reads, project, lin, now)) is not None and not m[1]]
    posted = []
    with lock(Scope(reg, project, "")) as held:
        if not held:
            return []
        for lin in lins:
            try:
                m = _missed(reads, project, lin, now)
                if m is not None and m[1]:
                    _record(project, lin, m[0])          # the close went out; only its record was lost
                elif m is not None:
                    posted.append((lin, close(reg, project, lin, m[0], reads)))
            except Exception as exc:  # noqa: BLE001 - one lineage never stops the others
                log.log("hooks", f"lineage: repair {lin}: {type(exc).__name__}: {exc}")
    for _lin, ptr in posted:
        if ptr is not None:
            inbox.wake(ptr["to"])
    return [lin for lin, _p in posted]


def deadline(b):
    """When `b` is overdue (aware datetime): a live run's lock `started` + `timeout_s` (the runner's kill
    time); else BRIEF.md's mtime + its TIMEBOX (reconcile.timebox_s) + job.GRACE_S; None: unreadable."""
    from .. import reconcile, runs, units
    from ..runs import job
    lk = runs.running(b.project, b.unit)
    if lk is not None:
        t = units.parse_iso(lk.get("started"))
        if t is not None and isinstance(lk.get("timeout_s"), (int, float)):
            return t + _dt.timedelta(seconds=lk["timeout_s"])
    try:
        sent = _dt.datetime.fromtimestamp(b.path.stat().st_mtime, _dt.timezone.utc)
    except OSError:
        return None
    return sent + _dt.timedelta(seconds=reconcile.timebox_s(b) + job.GRACE_S)


def holds(reg, dry_run=False, now=None, keys=None, closed=()):
    """Heartbeat: end the holds a post left. A live session holding a `quiet: "batch"` pointer and no
    waking one gets its batch's close when the batch is complete (Main: only when due and not among
    `closed`, the (project, lineage) pairs repair closed), else one urgent `info` (the bound) when every
    unit it still waits on is past its deadline or its oldest held pointer of the lineage is RECENT old.
    Briefs are read only for a project with a hold; writes happen under the project's lock (busy: left
    to the next pass), each written session is woken after it. `dry_run` takes no lock and writes
    nothing. Returns [(project, lineage, session, "close" | "bound")]. Never raises."""
    from .. import roster, units
    now, done = now or units.now(), []
    try:
        r = roster.Roster(reg)
    except Exception as exc:  # noqa: BLE001 - a hold never fails the pass
        log.log("hooks", f"lineage: holds: {type(exc).__name__}: {exc}")
        return done
    for project in sorted(reg.get("projects") or {}):
        if keys is not None and project not in keys:
            continue
        try:
            sessions = [s for s in r.sessions() if (m := r.member(s)) is not None and m.project == project
                        and m.status != "retired"]
            held = {s: h for s in sessions if (h := _holding(s))}
            if held:
                done += _holds(reg, project, held, now, dry_run, set(closed))
        except Exception as exc:  # noqa: BLE001 - one project never stops the pass
            log.log("hooks", f"lineage: holds {project}: {type(exc).__name__}: {exc}")
    return done


def _holds(reg, project, held, now, dry_run, closed):
    reads = Reads(reg, {project: groups(reg, project)})
    if dry_run:
        return [(project, lin, s, kind) for s, h in held.items() for lin, t in h.items()
                if (kind := _hold(reads, project, s, lin, t, now, closed, False)) is not None]
    out = []
    with lock(Scope(reg, project, "")) as ok:
        if not ok:
            return []
        for s in held:
            for lin, t in _holding(s).items():           # again, under the lock
                try:
                    kind = _hold(reads, project, s, lin, t, now, closed, True)
                    if kind is not None:
                        out.append((project, lin, s, kind))
                except Exception as exc:  # noqa: BLE001 - one hold never stops the others
                    log.log("hooks", f"lineage: hold {lin} {s}: {type(exc).__name__}: {exc}")
    for s in dict.fromkeys(s for _p, _l, s, _k in out):
        inbox.wake(s)
    return out


def _hold(reads, project, session, lineage, oldest, now, closed, write):
    """What ends `session`'s hold of `lineage` now ("close", "bound" or None); written when `write`."""
    ms = _effective(reads, members(reads.reg, project, lineage, reads))
    if not ms:
        return None
    main = reads.roster.main(project)
    wait = waiting(reads, project, lineage, session, ms)
    if not wait:
        if main is not None and session == main.session:
            if (project, lineage) in closed:
                return None
            ms, due = _due(reads, project, lineage)
            if not due:
                return None
            if write:
                close(reads.reg, project, lineage, ms, reads)
            return "close"
        if write:
            _head_close(reads, project, lineage, session, ms)
        return "close"
    bs = [b for b, _f, _s in ms]
    tops = top(reads, bs, session)
    whys = _whys(reads, ms)
    late = [deadline(b) for b in _under(bs, tops) if whys[b.unit] is not None]
    if not all(d is None or d <= now for d in late) and now - oldest < RECENT:
        return None
    if write:
        b, why = wait[0]
        inbox.write(inbox.make(lineage, "orche", session, "info", urgent=True, lineage=lineage,
                               summary=f"lineage {lineage}: {len(wait)} of {len(tops)} units still open past "
                                       f"their TIMEBOX; waiting on {b.unit} ({why}); the rest comes with the close"))
    return "bound"


def stalled(reg, project, now=None):
    """`orche status`: open lineages (two or more members, a BRIEF.md younger than RECENT) held by a
    blocked or question member whose report is inbox.LATE old or more: [{lineage, unit, status, minutes}]
    (one per lineage). Never raises."""
    from .. import units
    now = now or units.now()
    try:
        gs = groups(reg, project)
        reads, out = Reads(reg, {project: gs}), []
        for lin in sorted(gs):
            if len(gs[lin]) < 2 or not _young(gs[lin], (now - RECENT).timestamp()):
                continue
            ms = members(reg, project, lin, reads)
            for b, f, s in ms:
                t = inbox.pointer_time(f[0], f[1]) if f and s in WAITING else None
                if t is not None and inbox.LATE <= now - t < RECENT:
                    out.append({"lineage": lin, "unit": b.unit, "status": s,
                                "minutes": int((now - t).total_seconds() // 60)})
                    break
        return out
    except Exception as exc:  # noqa: BLE001 - a status line never fails the command
        log.log("hooks", f"lineage: stalled {project}: {type(exc).__name__}: {exc}")
        return []


def line(reg, project, lineage, reads=None):
    """The prompt hook's line for `lineage`, one member or many (None: no member in `project`): closed
    once Main's batch is complete, else open, naming the first unit it waits on and why."""
    reads = reads or Reads(reg)
    ms = _effective(reads, members(reg, project, lineage, reads))
    if not ms:
        return None
    name = " ".join(str(lineage).split())
    n = f"{len(ms)} unit{'' if len(ms) == 1 else 's'}"
    wait = waiting(reads, project, lineage, None, ms)
    if not wait:
        c = _counts(ms)
        parts = ", ".join(f"{c[s]} {s}" for s in STOPS if c[s])
        return f"[orche] lineage {name}: closed — {n} ({parts})"
    k = sum(1 for _b, _f, s in ms if s not in STOPS)
    b, why = wait[0]
    return f"[orche] lineage {name}: open — {k} of {n} running; waiting on {b.unit} ({why})"


def lines(reg, project, ptrs):
    """One line per lineage the pointers `ptrs` name (of_pointer), in first-seen order; every inbox is
    read once. Never raises."""
    out, seen, reads = [], set(), None
    for p in ptrs:
        lin = of_pointer(p, reg, project)
        if not lin or lin in seen:
            continue
        seen.add(lin)
        try:
            reads = reads or Reads(reg)
            text = line(reg, project, lin, reads)
        except Exception as exc:  # noqa: BLE001 - a lineage line never costs the prompt
            log.log("hooks", f"lineage: line {lin}: {type(exc).__name__}: {exc}")
            continue
        if text:
            out.append(text)
    return out
