"""Staff errands: a small task the owner gives his CEO Staff, run as one headless `orche run` under Staff's
name and Main's rules, whose result comes back to Staff (`orche errand`, lib/orche/cmd_errand.py).

The errand unit: a unit folder whose BRIEF.md is `to` and `from` `staff-<p>` (TIER standard, TIMEBOX 10m by
default, at most TIMEBOX_MAX_S), its own lineage. The runner takes the Staff pseudo-member (member()): it works
in the main checkout on Opus 5.5 at medium with the errand persona (PERSONA) and footer (FOOTER), and files
its result to `staff-<p>` (`via: runner`, never quiet). Its state is derived (state()): running, waiting,
answered, needs-main, needs-owner, failed, handed, dropped, lost.

Who: a project's Staff pane (`ORCHE_SESSION=staff-<p>` of a registered project, ORCHE_RUNNER unset); the
project's Main may also stop one. No start, answer or handover in a Staff turn Orche started (the Staff turn
record says `owner: false`). The owner's pause (G8) refuses start, answer and handover; stop always works.
At start the flow guards G2, G3 and G4 apply, at an answer (an AMEND) G1, G2, G4 and G5. At most OPEN_MAX
errands of a project are open (running or waiting); the runner's target lock lets one run at a time.

Handover: work that needs Main goes to `main-<p>` as one `external-request` `via: staff` carrying the errand's
lineage. Main's final message of an Orche turn that took an errand item comes back to Staff (forward_reply,
REPLY.md, `via: main`). Staff items that cannot be delivered are copied to Main (forward), and the heartbeat
renudges, forwards and files lost errands (sweep).

Events: `errand start|answer|handover|stop|refused|forward|forwarded|renudge|lost` (and `errand changed`
from the runner, runs/job.py).
Import-light: no import of deliver, dispatch or reconcile at load (dispatch is loaded inside start for the
STANDING text).
"""
import datetime as _dt
import os
from pathlib import Path

from . import brief, inbox, log, mask, paths, roster, runs, staff, units, util

PERSONA = paths.PLUGIN_ROOT / "templates" / "personas" / "errand.md"
FOOTER = "run/errand.md"
TIMEBOX_DEFAULT, TIMEBOX_MAX_S = "10m", 20 * 60
OPEN_MAX = 2
RENUDGE_AFTER, FORWARD_AFTER, OPEN_TTL = (_dt.timedelta(minutes=10), _dt.timedelta(minutes=30),
                                          _dt.timedelta(minutes=60))
VIA_STAFF, VIA_MAIN = "staff", "main"
KIND = "staff"                       # the pseudo-member's roster kind
REASONS = ("asleep", "undelivered")  # why forward() copies Staff items to Main
NEEDS_MAIN = "NEEDS MAIN:"
HANDED = "handed to Main"
DROPPED = "dropped by the owner"
LOST = "errand lost: no result 60 minutes after it started"
FORWARD_PREFIX = "Errand result for the owner: "
ASK_PREFIX = "The owner asks Main (via Staff): "
STOP_ASK = "The owner asks to stop this request and its units"
NOT_STARTED = "could not start: "
UNDELIVERED_REPLY = ("Your reply about {unit} did not reach the owner (his Staff window could not be opened): give it "
                     "to him here.")
JOINED = "JOINED"   # in an errand's unit folder: the errand lineage Main's briefs for it joined (forward_reply)
FIELDS = {
    "SCOPE": "Read what the gates allow. New files only in this unit's folder, or outside the repository in a "
             "folder the GOAL names (new names only).",
    "ACCEPT": "The GOAL answered in plain words, each fact with its source; every file created listed with its "
              "full path.",
    "VERIFY": "Each fact traced to the file, command or page it came from.",
    "FORBID": "Changing the project's tracked files, branches, pull requests, queue or secrets; overwriting, "
              "moving or deleting an existing file; dispatching or reporting to anyone.",
    "REPORT": "Your final message: the runner files it to Staff.",
}


class Refused(Exception):
    """An errand action Orche refuses (exit 2). str(exc) is the one-line reason, no prefix; `rule` names it
    for the events line."""

    def __init__(self, text, rule="refused"):
        super().__init__(text)
        self.rule = rule


def _refuse(rule, text, session, unit="-"):
    log.log("events", f"errand refused session={session or '-'} unit={unit or '-'} rule={rule}")
    return Refused(text, rule)


# ---- the Staff pseudo-member and the errand unit ---------------------------------------------------------

def member(reg, session):
    """The Staff pseudo-member of `session` when it is the Staff of a registered project: Member(session,
    project, "staff", "staff", None, entry={}, home=the project's path); None otherwise."""
    key = staff.project_of(reg, session)
    if key is None:
        return None
    home = units.project_root(reg, key)
    return roster.Member(session, key, KIND, "staff", None, entry={}, home=home) if home is not None else None


def is_errand(b) -> bool:
    """Brief `b` (readable) is addressed to a Staff session."""
    return b is not None and bool(b.rev) and staff.is_staff(b.to)


def is_errand_lineage(reg, project, name) -> bool:
    """The brief of unit `name` in `project` is an errand (a lineage an errand started). Never raises."""
    try:
        return is_errand(brief.find(reg, str(name or ""), project=project))
    except Exception:  # noqa: BLE001 - a malformed registry entry names no errand
        return False


def _key(path, ptr):
    t = inbox.pointer_time(path, ptr)
    try:
        ns = path.stat().st_mtime_ns
    except OSError:
        ns = 0
    return (t.timestamp() if t is not None else 0.0, ns)


def _sorted(found):
    return [(k, p) for k, p in sorted(((_key(path, p), p) for path, p in found), key=lambda x: x[0])]


def _results(b):
    """[(key, pointer)] oldest first: the errand's results at its current rev in Staff's inbox (pending or
    drained), from Staff (the runner's filings, the end markers, a lost marker)."""
    return _sorted((path, p) for path, p in inbox.about(b.to, b.unit, b.to) if p.get("brief_rev") == b.rev)


def _requests(reg, b):
    """[(key, pointer)] oldest first: the errand's handovers in Main's inbox (pending or drained)."""
    main = roster.Roster(reg).main(b.project)
    if main is None:
        return []
    return _sorted((path, p) for path, p in inbox.about(main.session, b.unit, b.to)
                   if p.get("status") == "external-request" and p.get("via") == VIA_STAFF)


def _unseen_request(reg, b) -> bool:
    """An errand `b`'s handover (a `via staff` external-request) is still pending in Main's inbox."""
    main = roster.Roster(reg).main(b.project)
    return main is not None and any(
        p.get("unit") == b.unit and p.get("from") == b.to and p.get("status") == "external-request"
        and p.get("via") == VIA_STAFF for _path, p in inbox.pending(main.session))


def _replies(reg, b):
    """[(key, pointer)] oldest first: Main's replies on the errand in Staff's inbox (pending or drained)."""
    main = roster.Roster(reg).main(b.project)
    if main is None:
        return []
    return _sorted((path, p) for path, p in inbox.about(b.to, b.unit, main.session) if p.get("via") == VIA_MAIN)


def _rev_age(b, now):
    """Seconds since BRIEF.rev<N>.md (else BRIEF.md) of the current rev was written; None when unreadable."""
    for path in (b.folder / f"BRIEF.rev{b.rev}.md", b.path):
        try:
            return now.timestamp() - path.stat().st_mtime
        except OSError:
            continue
    return None


def _needs_main(ptr) -> bool:
    return any(ln.strip().startswith(NEEDS_MAIN) for ln in str(ptr.get("summary") or "").splitlines())


def _marker(ptr, status) -> bool:
    return ptr.get("via") == VIA_STAFF and ptr.get("status") == status


def state(reg, b, now=None) -> str:
    """running|waiting|answered|needs-main|needs-owner|failed|handed|dropped|lost for errand brief `b`.

    running: a live run lock. handed: a `via staff` external-request for the unit in Main's inbox at or after
    the newest result, or the newest result is the "handed to Main" end marker. With no result at the current
    rev: waiting while BRIEF.rev<N>.md is younger than OPEN_TTL or a start is pending (_pending_start), else
    lost. A runner `info` while a usage hold holds the unit: waiting. Else the newest result: done is
    needs-main when a summary line starts `NEEDS MAIN:`, else answered; blocked or question is needs-owner;
    failed is failed; stop, or a runner `info` (stopped, busy, not started) is dropped, except a runner
    `info` "unverified: ..." (a result without a STATUS line), which is answered; other `info` is answered."""
    now = now or units.now()
    if runs.running(b.project, b.unit):
        return "running"
    results = _results(b)
    newest = results[-1] if results else None
    requests = _requests(reg, b)
    if newest is not None and _marker(newest[1], "done"):
        return "handed"
    if requests and (newest is None or requests[-1][0] >= newest[0]):
        return "handed"
    if newest is None:
        age = _rev_age(b, now)
        young = age is not None and age < OPEN_TTL.total_seconds()
        return "waiting" if young or _pending_start(b) else "lost"
    p = newest[1]
    status = p.get("status")
    if status == "info" and p.get("via") == "runner" and _held(b):
        return "waiting"
    if status == "done":
        return "needs-main" if _needs_main(p) else "answered"
    if status in ("blocked", "question"):
        return "needs-owner"
    if status == "failed":
        return "failed"
    if status == "stop":
        return "dropped"
    if status == "info" and p.get("via") == "runner" and not str(p.get("summary") or "").startswith("unverified:"):
        return "dropped"
    return "answered"


def _held(b) -> bool:
    """A usage hold that is still on holds errand `b` (flow/usage: the heartbeat re-runs it when it ends)."""
    from .flow import usage
    try:
        return any(u.get("unit") == b.unit for v in usage.VENDORS
                   for u in usage.held_units(usage.active(v), b.project))
    except Exception:  # noqa: BLE001 - an unreadable hold holds nothing
        return False


def _queued(b) -> bool:
    """An `orche run <unit>` process of errand `b` is alive without its lock yet (waiting for a run slot or
    its target, runs.acquire): one `ps` of command lines. False when `ps` cannot tell."""
    from . import shell
    res = shell.run(["ps", "-axww", "-o", "command="], timeout=5)
    return res.ok and runs._run_command(b.unit).search(res.out or "") is not None


def _pending_start(b) -> bool:
    """A start of errand `b` that has not run yet: a usage hold holds it, or its run waits for a slot."""
    return _held(b) or _queued(b)


def _errands(reg, key, within=None, now=None):
    """The project's errand briefs, unit order; `within` (a timedelta): only those whose BRIEF.md was written
    that recently, or that have a live run."""
    from .flow import lineage
    now = now or units.now()
    out = []
    for d in lineage.unit_dirs(reg, key):
        if within is not None and runs.running(key, d.name) is None:
            try:
                if now.timestamp() - (d / "BRIEF.md").stat().st_mtime > within.total_seconds():
                    continue
            except OSError:
                continue
        b = brief.find(reg, d.name, project=key)
        if is_errand(b):
            out.append(b)
    return out


def open_errands(reg, key, now=None) -> list:
    """Project `key`'s errand Briefs in state running or waiting."""
    now = now or units.now()
    return [b for b in _errands(reg, key, OPEN_TTL, now) if state(reg, b, now) in ("running", "waiting")]


# ---- start, answer, hand over, stop ----------------------------------------------------------------------

def _staff(reg, session, unit="-"):
    """Staff's Member for an errand action, after the who and runner checks (Refused)."""
    m = member(reg, session)
    if m is None:
        raise _refuse("not-staff", "only a project's CEO Staff runs errands (ORCHE_SESSION=staff-<project>)",
                      session, unit)
    if os.environ.get("ORCHE_RUNNER"):
        raise _refuse("runner", "an errand cannot start errands: the runner files your final message to Staff",
                      session, unit)
    return m


def _not_orche_turn(session, unit="-"):
    """The loop breaker: no start, answer or handover in a Staff turn Orche started. A missing turn record
    counts as the owner's turn."""
    from .flow import lineage
    turn = lineage.read_turn(session)
    if isinstance(turn, dict) and turn.get("owner") is False:
        raise _refuse("orche-turn", "this turn is Orche's (an errand result or a reply from Main): tell the owner "
                                    "and start nothing until he answers", session, unit)


def _not_paused(key, session, unit="-"):
    from .flow import guard
    p = guard.paused(key)
    if p is not None:
        at = units.parse_iso(p.get("at"))
        reason = f" ({p['reason']})" if p.get("reason") else ""
        raise _refuse("G8", f"{key} is paused by the owner since {units.hhmm(at) if at else '?'}{reason}: nothing "
                            f"starts until he resumes it in Main", session, unit)


def _guarded(reg, block, m, unit="-", runner=False):
    """The flow guards for `block` from Staff (Refused with the guard's text, without Main's ask)."""
    from .flow import guard
    hit = guard.safe(guard.check, m.session, unit, reg, block, m, to=m.session, runner=runner)
    if hit is not None:
        guard.logged(hit.rule, m.session, hit.unit, hit.n)
        text = hit.text.split(" Ask the owner before")[0].rstrip()
        raise _refuse(hit.rule, f"{text} One word from the owner to Main lifts it.", m.session, unit)


def _errand(reg, m, unit):
    """The project's errand brief `unit` (ValueError: a bad id, an unknown unit or not this project's errand)."""
    err = brief.unit_id_error(unit)
    if err:
        raise ValueError(err)
    b = brief.find(reg, unit, project=m.project)
    if b is None or not b.rev:
        raise ValueError(f"unknown unit {unit!r} in {m.project} (no .orche/units/{unit}/BRIEF.md)")
    if not is_errand(b) or b.to != staff.session_of(m.project):
        raise ValueError(f"{unit} is not an errand of {m.project}")
    return b


def timebox_seconds(text):
    """Seconds named by a TIMEBOX text (`10m`, `1h`); ValueError when it names none."""
    from .runs import job
    secs = job.timebox_raw(text)
    if secs is None:
        raise ValueError(f"bad TIMEBOX {text!r} (want e.g. 10m)")
    return secs


def _block(goal, context, timebox):
    fields = {"TO": "-", "GOAL": str(goal or "").strip(), "CONTEXT": str(context or "").strip() or "(none)",
              **FIELDS, "TIMEBOX": timebox, "TIER": "standard"}
    try:
        return brief.Block("brief", fields)
    except brief.Refusal as exc:
        raise ValueError(str(exc).removeprefix(brief.REFUSE)) from None


def _standing(reg, key):
    from . import dispatch
    return dispatch.standing_text(reg, key)


def _peek(m, goal) -> str:
    """The unit id brief.write would allocate now for `goal` (nothing is created)."""
    import re
    date = f"{units.now().date():%Y%m%d}"
    slug = util.slugify(" ".join(mask.redact(goal)[0].split()[:4])) or "unit"
    base = Path(m.home) / brief.UNITS
    pat = re.compile(rf"^{re.escape(util.slugify(m.project))}-.+-{date}-(\d+)$")
    try:
        names = [p.name for p in base.iterdir()]
    except OSError:
        names = []
    n = 1 + max([int(x.group(1)) for x in (pat.match(name) for name in names) if x] or [0])
    return f"{util.slugify(m.project)}-{util.slugify(slug)}-{date}-{n}"


def _write(reg, m, block):
    return brief.write(block, m, m.session, _standing(reg, m.project))


def start(reg, session, goal, context="", timebox=None, *, dry_run=False) -> str:
    """Start an errand from Staff `session`: its brief (rev 1, to and from Staff, its own lineage), then
    runs.start. The unit id (`dry_run`: the id it would get; nothing is written). Refused (in this order): not
    a project's Staff; under the runner; Orche's Staff turn; paused; a flow guard (G2, G3, G4); OPEN_MAX open;
    TIMEBOX over TIMEBOX_MAX_S. ValueError: an empty GOAL or a TIMEBOX that names no time."""
    m = _staff(reg, session)
    timebox = str(timebox or TIMEBOX_DEFAULT).strip()
    secs = timebox_seconds(timebox)
    block = _block(goal, context, timebox)
    _not_orche_turn(session)
    _not_paused(m.project, session)
    _guarded(reg, block, m)
    found = open_errands(reg, m.project)
    if len(found) >= OPEN_MAX:
        raise _refuse("open", f"{len(found)} errands are open ({', '.join(b.unit for b in found)}): ask again when "
                              f"one is back", session)
    if secs > TIMEBOX_MAX_S:
        raise _refuse("timebox", f"TIMEBOX {timebox} is over {TIMEBOX_MAX_S // 60}m: an errand is small; hand "
                                 f"bigger work to Main", session)
    if dry_run:
        return _peek(m, block.fields["GOAL"])
    b = _write(reg, m, block)
    log.log("events", f"errand start unit={b.unit} project={m.project} timebox={secs}s")
    _run(m, b)
    return b.unit


def answer(reg, session, unit, text, *, dry_run=False) -> int:
    """The owner's answer to the errand's question: AMEND rev N+1 (CONTEXT gains "Owner's answer HH:MM: ...",
    SUPERSEDES cause `owner`), then runs.start. The new rev. Refused: not Staff; under the runner; Orche's
    Staff turn; paused; a flow guard (G1, G2, G4, G5); nothing to answer (the newest result is not blocked or
    question). ValueError: an unknown unit, not this project's errand, or an empty answer."""
    m = _staff(reg, session, unit)
    b = _errand(reg, m, unit)
    words = str(text or "").strip()
    if not words:
        raise ValueError("--answer is empty")
    _not_orche_turn(session, unit)
    _not_paused(m.project, session, unit)
    context = f"{b.fields.get('CONTEXT', '').rstrip()}\nOwner's answer {units.hhmm(units.now())}: {words}".strip()
    block = brief.Block("amend", {"CONTEXT": context}, unit, b.rev + 1,
                        f"rev {b.rev}: owner: the owner answered the errand's question")
    _guarded(reg, block, m, unit, runner=True)
    if state(reg, b) != "needs-owner":
        raise _refuse("nothing-to-answer", f"{unit} asks nothing: its newest result is no question for the owner",
                      session, unit)
    if dry_run:
        return b.rev + 1
    nb = brief.write(block, m, m.session, _standing(reg, m.project), prior=b)
    log.log("events", f"errand answer unit={unit} project={m.project} rev={nb.rev}")
    _run(m, nb)
    return nb.rev


def _run(m, b):
    """runs.start for errand `b`; a False return or an exception is a failed start: the drained end marker
    `failed` "could not start: <reason>" (the errand is not open), then Refused with that text."""
    try:
        why = None if runs.start(b.unit) else "the run was not spawned"
    except Exception as exc:  # noqa: BLE001 - a spawn that raises leaves no run behind
        why = f"{type(exc).__name__}: {exc}"
    if why is None:
        return
    text = NOT_STARTED + why
    try:
        _end_marker(m, b, "failed", inbox.clip(mask.redact(text)[0]))
    except Exception as exc:  # noqa: BLE001 - the refusal still says it
        log.log("hooks", f"errand: start marker {b.unit}: {type(exc).__name__}: {exc}")
    raise _refuse("not-started", text, m.session, b.unit)


def _post_request(reg, m, b, summary, report):
    """One `external-request` `via: staff` for errand `b` to the project's Main (it wakes Main)."""
    main = roster.Roster(reg).main(m.project)
    if main is None:
        raise ValueError(f"{m.project} has no Main to hand {b.unit} to")
    inbox.post(inbox.make(b.unit, m.session, main.session, "external-request", report_path=str(report),
                          brief_rev=b.rev, summary=inbox.clip(mask.redact(summary)[0]), via=VIA_STAFF,
                          lineage=b.lineage))


def _end_marker(m, b, status, summary):
    """An errand's end that Staff files itself ("handed to Main", "dropped by the owner"): written to Staff's
    inbox and drained at once (no wake), so its state and lineage see the unit ended."""
    path = inbox.write(inbox.make(b.unit, m.session, m.session, status, brief_rev=b.rev, summary=summary,
                                  via=VIA_STAFF, lineage=b.lineage))
    inbox.drain([path])


def hand_over(reg, session, *, unit=None, goal=None, context="", dry_run=False) -> str:
    """Hand work to the project's Main: one `external-request` `via: staff` with the errand's lineage.
    New (`goal`, no `unit`): the errand brief is written (no run), the request names its BRIEF.md and the
    errand's own end ("handed to Main") is filed and drained at once. Existing (`unit`): the request names
    REPORT.md (else BRIEF.md) and carries `context` (the owner's word, or his answer to Main's question).
    The unit. Refused: not Staff; under the runner; Orche's Staff turn; paused; already with Main: an
    earlier handover still pending in Main's inbox (Main has not seen it) and no reply from Main since.
    ValueError: neither or both of unit and goal, an unknown unit or not this project's errand."""
    if bool(unit) == bool(goal):
        raise ValueError("hand over either a new request (--goal) or an errand (UNIT)")
    m = _staff(reg, session, unit or "-")
    b = _errand(reg, m, unit) if unit else None
    block = None if unit else _block(goal, context, TIMEBOX_DEFAULT)
    _not_orche_turn(session, unit or "-")
    _not_paused(m.project, session, unit or "-")
    if b is not None and _unseen_request(reg, b):
        replies = _replies(reg, b)
        if not replies or replies[-1][0] < _requests(reg, b)[-1][0]:
            raise _refuse("with-main", f"{unit} is already with Main and Main has not replied since: its answer "
                                       f"comes back here", session, unit)
    if dry_run:
        return unit or _peek(m, block.fields["GOAL"])
    extra = f"\n{str(context).strip()}" if str(context or "").strip() else ""
    if b is None:
        b = _write(reg, m, block)
        _post_request(reg, m, b, ASK_PREFIX + block.fields["GOAL"] + extra, b.path)
        _end_marker(m, b, "done", HANDED)
    else:
        report = b.folder / "REPORT.md"
        _post_request(reg, m, b, ASK_PREFIX + str(b.fields.get("GOAL") or b.unit) + extra,
                      report if report.is_file() else b.path)
    log.log("events", f"errand handover unit={b.unit} project={m.project}")
    return b.unit


def stop(reg, actor, unit, *, dry_run=False) -> str:
    """Stop or drop errand `unit` for `actor` (the project's Staff, or its Main for a running or waiting one):
    running or waiting: runs.stop ("stopped"), and with no live run the drained "dropped by the owner"
    marker at once (it is not open any more); needing the owner or Main: the drained "dropped by the owner"
    marker ("dropped"); with Main: an external-request to Main to stop it and its units ("asked-main").
    Refused: not that project's Staff or Main; under the runner; nothing to stop. ValueError: an unknown
    unit or not this project's errand."""
    r = roster.Roster(reg)
    m = member(reg, actor)
    main = r.member(actor)
    if m is None and (main is None or main.kind != "main"):
        raise _refuse("not-staff", "only the project's CEO Staff or its Main stops an errand", actor, unit)
    if os.environ.get("ORCHE_RUNNER"):
        raise _refuse("runner", "an errand cannot stop errands", actor, unit)
    key = m.project if m is not None else main.project
    sm = m or member(reg, staff.session_of(key))
    if sm is None:
        raise ValueError(f"{key} has no project path, so {unit} is no errand of it")
    b = _errand(reg, sm, unit)
    s = state(reg, b)
    if s in ("running", "waiting"):
        if not dry_run:
            runs.stop(b.project, b.unit, reg=reg)
            if runs.running(b.project, b.unit) is None:
                _end_marker(sm, b, "stop", DROPPED)
        out = "stopped"
    elif m is None:
        raise _refuse("nothing-to-stop", f"{unit} is not running ({s}): its Staff drops it", actor, unit)
    elif s in ("needs-owner", "needs-main"):
        if not dry_run:
            _end_marker(sm, b, "stop", DROPPED)
        out = "dropped"
    elif s == "handed":
        if not dry_run:
            _post_request(reg, sm, b, f"{STOP_ASK}: {b.fields.get('GOAL') or b.unit}", b.path)
        out = "asked-main"
    else:
        raise _refuse("nothing-to-stop", f"{unit} has nothing to stop ({s})", actor, unit)
    if not dry_run:
        log.log("events", f"errand stop unit={unit} project={b.project} by={actor} state={out}")
    return out


# ---- Main's side and delivery ----------------------------------------------------------------------------

RANK = {"item": 0, "handover": 1, "close": 2}


def turn_errands(reg, project, ptrs) -> dict:
    """{lineage: "close"|"handover"|"item"} for the errand lineages among `ptrs` (pointers, or (path,
    pointer) pairs) Main took in a turn: a lineage close, else a `via staff` external-request, else any other
    item of the lineage; the strongest per lineage. A forwarded Staff item (`forwarded`) is no errand item:
    Main tells the owner itself. Never raises."""
    from .flow import lineage
    out, known = {}, {}
    for it in ptrs or ():
        try:
            ptr = it[1] if isinstance(it, tuple) else it
            if not isinstance(ptr, dict) or ptr.get("forwarded"):
                continue
            lin = lineage.of_pointer(ptr, reg, project)
            if not lin:
                continue
            if lin not in known:
                known[lin] = is_errand_lineage(reg, project, lin)
            if not known[lin]:
                continue
            kind = ("close" if ptr.get("lineage_closed")
                    else "handover" if ptr.get("status") == "external-request" and ptr.get("via") == VIA_STAFF
                    else "item")
            if RANK[kind] > RANK.get(out.get(lin), -1):
                out[lin] = kind
        except Exception as exc:  # noqa: BLE001
            log.log("hooks", f"errand: turn item: {type(exc).__name__}: {exc}")
    return out


def join(reg, project, errands) -> list:
    """Main's turn took more than one errand handover (`errands`: the turn record's): its new briefs all
    joined the first handover lineage (lineage.assign), so each other handover's unit folder gets JOINED
    naming it; its close then reaches them too (forward_reply). The units marked. Never raises (logs)."""
    handed = sorted(str(n) for n, kind in (errands or {}).items() if kind == "handover")
    out = []
    for lin in handed[1:]:
        try:
            b = brief.find(reg, lin, project=project)
            if is_errand(b):
                util.atomic_write(b.folder / JOINED, handed[0] + "\n")
                out.append(b.unit)
        except Exception as exc:  # noqa: BLE001 - one errand never costs another
            log.log("hooks", f"errand: join {lin}: {type(exc).__name__}: {exc}")
    return out


def _joined(reg, project, lin) -> list:
    """The project's errand briefs whose JOINED marker names lineage `lin`."""
    from .flow import lineage
    out = []
    for d in lineage.unit_dirs(reg, project):
        try:
            if (d / JOINED).read_text(encoding="utf-8").strip() == lin:
                b = brief.find(reg, d.name, project=project)
                if is_errand(b):
                    out.append(b)
        except OSError:
            continue
    return out


def _reply(main_session, b, body, status):
    """Main's reply `body` to errand `b`: REPLY.md in its folder and one pointer `via: main` to its Staff."""
    path = b.folder / "REPLY.md"
    util.atomic_write(path, body + "\n")
    inbox.post(inbox.make(b.unit, main_session, b.to, status, report_path=str(path), brief_rev=b.rev,
                          summary=inbox.clip(body) or status, via=VIA_MAIN, lineage=b.lineage))


def forward_reply(reg, main_session, text, turn) -> list:
    """Main's final message `text` of an Orche turn whose record `turn` names errand lineages (`errands`),
    to the project's Staff: per lineage, REPLY.md in the errand's folder (masked) and one pointer `via: main`
    (its status follows the closing tag, not the turn's kind: OWNER is question, DONE is done, anything else,
    RUNNING or none, is info; summary = the reply's first lines, masked and clipped). A close forwarded as done
    also goes, as done, to each errand whose JOINED marker names that lineage (the marker is removed). Two or more handovers in the turn: join() first. Nothing while that Staff is
    asleep. The units forwarded. Never raises (logs)."""
    out = []
    try:
        errands = (turn or {}).get("errands") or {}
        m = roster.Roster(reg).member(main_session)
        if not errands or m is None or m.kind != "main":
            return out
        join(reg, m.project, errands)
        if staff.asleep(reg["projects"].get(m.project)):
            log.log("events", f"errand forward project={m.project} skipped=asleep")
            return out
        from .flow import owner
        hit = owner.parse(str(text or ""))
        body = mask.redact(str(text or ""))[0].strip()
        for lin, kind in sorted(errands.items()):
            try:
                b = brief.find(reg, str(lin), project=m.project)
                if not is_errand(b):
                    continue
                status = {"OWNER": "question", "DONE": "done"}.get(hit[0] if hit else None, "info")
                _reply(main_session, b, body, status)
                log.log("events", f"errand forward unit={b.unit} project={m.project} kind={kind} status={status}")
                out.append(b.unit)
                for jb in (_joined(reg, m.project, b.lineage) if kind == "close" and status == "done" else ()):
                    _reply(main_session, jb, body, "done")
                    (jb.folder / JOINED).unlink(missing_ok=True)
                    log.log("events", f"errand forward unit={jb.unit} project={m.project} kind=joined status=done")
                    out.append(jb.unit)
            except Exception as exc:  # noqa: BLE001 - one errand never costs another
                log.log("hooks", f"errand: forward {lin}: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - never fails Main's Stop hook
        log.log("hooks", f"errand: forward_reply {main_session}: {type(exc).__name__}: {exc}")
    return out


def _claim(path) -> bool:
    """Take a pending Staff item for this side: inbox.drain's rename. False when another side (Staff's
    prompt, another forward) drained it first."""
    try:
        inbox.drain([path])
    except FileNotFoundError:
        return False
    return True


def forward(reg, key, items, reason) -> int:
    """Copy Staff items (`items`: (path, pointer) from `staff-<key>`'s inbox) to the project's Main: `info`
    `via: staff`, `forwarded: <reason>`, summary prefixed FORWARD_PREFIX, report and owner step kept;
    `asleep` is quiet (Main shows it at its next prompt), `undelivered` wakes Main. Each item is claimed
    first (drained from Staff; one another side took is skipped), then copied. Main's own reply (`via:
    main`) goes back to Main as `info` `forwarded: undelivered`, no report_path, the summary UNDELIVERED_REPLY
    and the path of its REPLY.md (quiet when `asleep`): never dropped, and no errand item (turn_errands), so
    that Main turn is a normal one. The number posted. ValueError: an unknown reason."""
    if reason not in REASONS:
        raise ValueError(f"bad forward reason {reason!r}")
    main = roster.Roster(reg).main(key)
    if main is None:
        return 0
    session, n = staff.session_of(key), 0
    for path, ptr in items:
        try:
            if not _claim(path):
                continue
            rev = ptr.get("brief_rev")
            rev = rev if isinstance(rev, int) and not isinstance(rev, bool) else 1
            unit = str(ptr.get("unit") or "-")
            quiet = True if reason == "asleep" else None
            if ptr.get("from") == main.session:
                # no report_path: an inlined REPLY.md would replace this instruction in Main's inbox
                kept = str(ptr.get("report_path") or "")
                inbox.post(inbox.make(
                    unit, session, main.session, "info", report_path="", brief_rev=rev,
                    summary=inbox.clip(UNDELIVERED_REPLY.format(unit=unit)
                                       + (f" Your reply: {kept}" if kept else "")),
                    via=VIA_STAFF, forwarded="undelivered", lineage=ptr.get("lineage") or unit, quiet=quiet))
            else:
                inbox.post(inbox.make(
                    unit, session, main.session, "info", report_path=str(ptr.get("report_path") or ""),
                    brief_rev=rev,
                    summary=inbox.clip(FORWARD_PREFIX + str(ptr.get("summary") or ptr.get("status") or "")),
                    via=VIA_STAFF, forwarded=reason, lineage=ptr.get("lineage"), owner=ptr.get("owner"),
                    quiet=quiet))
            n += 1
            log.log("events", f"errand forwarded unit={unit} project={key} reason={reason}")
        except Exception as exc:  # noqa: BLE001 - the next item still goes
            log.log("hooks", f"errand: forward {ptr.get('unit')}: {type(exc).__name__}: {exc}")
    return n


def _lost(b):
    inbox.post(inbox.make(b.unit, b.to, b.to, "failed", brief_rev=b.rev, summary=LOST, via=VIA_STAFF,
                          lineage=b.lineage))
    log.log("events", f"errand lost unit={b.unit} project={b.project}")


def sweep(reg, now=None) -> list:
    """The heartbeat's Staff pass, per registered project: Staff items pending FORWARD_AFTER are forwarded
    to Main (reason asleep or undelivered); else an item pending RENUDGE_AFTER with no nudge in flight wakes
    Staff again; an errand of the last day with no result OPEN_TTL after its brief revision gets `failed`
    LOST. The event words (`renudge <session>`, `forward <session> <n>`, `lost <unit>`). Never raises."""
    from .flow import lineage
    now = now or units.now()
    words = []
    for key, proj in sorted(((reg or {}).get("projects") or {}).items()):
        try:
            session = staff.session_of(key)
            items = inbox.pending(session)
            ages = [(it, inbox.pointer_time(*it)) for it in items]
            old = [it for it, t in ages if t is not None and now - t >= FORWARD_AFTER]
            if old:
                n = forward(reg, key, old, "asleep" if staff.asleep(proj) else "undelivered")
                words.append(f"forward {session} {n}")
            rest = [t for it, t in ages if it not in old and t is not None and now - t >= RENUDGE_AFTER]
            if rest and not inbox.nudged(session):
                inbox.wake(session)
                log.log("events", f"errand renudge session={session} pending={len(rest)}")
                words.append(f"renudge {session}")
            for b in _errands(reg, key, lineage.RECENT, now):
                if state(reg, b, now) == "lost":
                    _lost(b)
                    words.append(f"lost {b.unit}")
        except Exception as exc:  # noqa: BLE001 - one project never stops the pass
            log.log("hooks", f"errand: sweep {key}: {type(exc).__name__}: {exc}")
    return words
