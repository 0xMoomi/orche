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

Close: a member (a brief whose lineage is L) is terminal when the newest report for its
current rev, from its target to the session its reports go to (roster.recipient), is `done`, `failed`,
`stop` or a runner `info` without `retry` (stopped); `blocked`/`question` keep it open. Under
`ORCHE_HOME/flow/<p>/.lock`, a done/stopped report to the project's Main of a lineage with two or more
members is posted quiet (flow.shape; a division always wakes for its children's reports); the report
that leaves every member terminal writes `<root>/.orche/units/<L>/LINEAGE.md` and posts one close
pointer from `orche` to the project's Main, then records the close (members, revs, statuses) in
`flow/<p>/closed.json`, so a close is posted once. A post that cannot take the lock within LOCK_WAIT_S
writes no close; the heartbeat (repair) posts one a crash or a busy lock missed, never one whose
pointer is already in Main's inbox.
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


def _newest(b, reads):
    """(path, pointer) of the newest report for `b` at its rev, from its target to the session its reports
    go to (roster.recipient: the sender while it is a live Main or division, else the target's division
    or Main); None."""
    target, sender = _ends(b)
    to = (reads.roster.recipient(target, sender) if target else None) or sender
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


def quiets(sc, ptr):
    """`ptr` (a report of `sc`) is a done/stopped report to the project's Main of a lineage with two or
    more members (the close wakes Main). A division recipient always wakes for its children's reports."""
    if sc is None or status_of(ptr) not in ("done", "stopped") or ptr.get("status") == "info" and ptr.get("retry"):
        return False
    from .. import roster
    main = roster.Roster(sc.reg).main(sc.project)
    if main is None or ptr.get("to") != main.session:
        return False
    return len(groups(sc.reg, sc.project).get(sc.lineage, [])) >= 2


def _counts(ms):
    return {s: sum(1 for _b, _f, x in ms if x == s) for s in TERMINAL}


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


def _final(ms):
    return len(ms) >= 2 and all(s in TERMINAL for _b, _f, s in ms)


def close(reg, project, lineage, ms, reads=None):
    """Write LINEAGE.md (atomically), then the close pointer to Main, then the close's record in
    closed.json (a crash in between leaves a pointer that repair finds and only records); the pointer,
    or None (no Main)."""
    main = (reads or Reads(reg)).roster.main(project)
    head = next((b for b, _f, _s in ms if b.unit == lineage), ms[0][0])
    md = head.root / brief.UNITS / lineage / "LINEAGE.md"
    c = _counts(ms)
    rows = ["| unit | to | rev | status | report |", "|---|---|---|---|---|"]
    for b, found, s in ms:
        report = (found[1].get("report_path") if found else "") or "-"
        rows.append(f"| {b.unit} | {_ends(b)[0] or '-'} | {b.rev} | {s} | {report} |")
    util.atomic_write(md, f"# Lineage {lineage}\n\nclosed: {util.now_iso()}   units: {len(ms)}, {c['done']} done, "
                          f"{c['failed']} failed, {c['stopped']} stopped\n\n" + "\n".join(rows) + "\n")
    ptr = None
    if main is not None:
        summary = "\n".join([f"lineage {lineage} closed: {len(ms)} units: {c['done']} done, {c['failed']} failed, "
                             f"{c['stopped']} stopped"] + [f"{b.unit} {s}" for b, _f, s in ms[:4]])
        ptr = inbox.make(lineage, "orche", main.session, "done", report_path=str(md), brief_rev=head.rev,
                         summary=summary, lineage=lineage, lineage_closed=True)
        inbox.write(ptr)
    _record(project, lineage, ms)
    log.log("events", f"lineage closed {lineage} project={project} n={len(ms)}")
    return ptr


def _due(reads, project, lineage):
    """(members, due): the lineage is final and its close is not yet recorded for these members."""
    ms = members(reads.reg, project, lineage, reads)
    if not _final(ms):
        return ms, False
    rec = _recorded(project).get(lineage)
    return ms, not (isinstance(rec, dict) and rec.get("members") == _signature(ms))


def settle(sc):
    """After a report of `sc` was written under the held lock: close the lineage when it is now final.
    The close pointer, or None. Raises what the close raises (inbox.post then wakes the report's
    recipient, so a failed close never leaves a quiet report silent)."""
    if sc is None:
        return None
    reads = Reads(sc.reg)
    ms, due = _due(reads, sc.project, sc.lineage)
    return close(sc.reg, sc.project, sc.lineage, ms, reads) if due else None


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
    once every member is terminal, else open, naming the member it waits on."""
    ms = members(reg, project, lineage, reads)
    if not ms:
        return None
    name = " ".join(str(lineage).split())
    n = f"{len(ms)} unit{'' if len(ms) == 1 else 's'}"
    if all(s in TERMINAL for _b, _f, s in ms):
        c = _counts(ms)
        parts = ", ".join(f"{c[s]} {s}" for s in TERMINAL if c[s])
        return f"[orche] lineage {name}: closed — {n} ({parts})"
    running = [(b, s) for b, _f, s in ms if s not in TERMINAL]
    b, s = next(((b, s) for b, s in running if s in OPEN), running[0])
    return f"[orche] lineage {name}: open — {len(running)} of {n} running; waiting on {b.unit} ({s})"


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
