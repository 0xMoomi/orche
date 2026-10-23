"""The heartbeat pass (`orche reconcile`, design c10 §2.1): one bounded, idempotent pass, no daemon.

A pass takes a non-blocking flock on ORCHE_HOME/reconcile/.lock (held -> exit 0), settles lost runs
(runs.reconcile), observes the repair classes, plans the due attempts (pure: plan()) and applies
them through the existing paths only, saving reconcile/state.json after each action and writing
reconcile/heartbeat.json last. Classes (design §2.2):

- unread <session>: a waking pointer pending >= inbox.LATE, the pane present and quiet (no output
  for QUIET_MS), not nudged within inbox.NUDGED_TTL, its `.deliver.lock` free -> `orche nudge`;
- closed <session>: waking pointers pending >= inbox.LATE and no pane, outside flow.quiet_hours: a
  Main's pane is reopened in place (`hr.project_pane(switch=False)`), an active division/feature
  woken (`hr.wake`), then `orche nudge`, REOPEN_CAP a day (`reopens:<session>`, STALL_S apart);
  past the cap one escalation; a dormant or paused unit is never woken: one urgent `info` to Main;
- undelivered <unit>: a `via: dispatch` pointer pending >= inbox.LATE -> `orche deliver --retry`;
- drain <unit>: that pointer's revision ran, runs or was reported on (never a STOP) -> drained at
  once under the target's `.deliver.lock` taken non-blocking (uncapped, nothing else is touched);
- busy <unit>: RUN.json of the current revision says `stop_reason: busy`, no run live ->
  `orche run <unit>` once (cap 1, no escalation);
- silent <unit>: the target's newest unit, a pane unit (never Main) whose dispatch was delivered,
  not run, running or reported on at this rev, with no open child of its own, whose pane has been
  idle SILENT_IDLE_S or whose rev is SILENT_TIMEBOX × TIMEBOX old -> `orche deliver --remind` once
  per revision; SILENT_ESCALATE_S later (or at 2 × TIMEBOX) one urgent `info` to the brief's sender;
- waker: the pre-0.6.0 `com.orche.main-waker` job is loaded -> one owner notification a day;
- owner (flow.owner.heartbeat, before the classes above): an answered ask is marked, a filled secret
  no dialog reported is told to Main, then the 10-minute fallback, a held notice or the open ask's
  re-notification (30, 60, 120, 240 min after it; none in quiet hours, one at their end). A
  project's escalation to the owner is a `stuck` notice (owner.announce: the same gap and rules).

Every state key is an episode: `unread|closed:<session>:<oldest waking pointer>`,
`undelivered|silent|busy:<unit>:<rev>`. Attempts fall due when the condition has held STALL_S, 2×,
4× (10, 20, 40 min); after CAP attempts, at 8× (80 min), one escalation. An entry with attempts or
an escalation outlives a pass that does not see its condition (Orca unreachable, a pane busy for a
while) until STATE_TTL, and is dropped only on positive resolution read from local files
(resolved()). A repair whose apply raises still spends its attempt. Events lines carry ids,
counts and times only.
"""
import contextlib
import datetime as _dt
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, brief, config, inbox, log, notify, orca, paths, procs, registry, roster, units, util
from .flow import guard, lineage, owner

STATE_TTL = _dt.timedelta(hours=48)
STALE_INTERVALS = 2       # `orche status` / `orche doctor`: the heartbeat is stale past 2 intervals
KICK_INTERVALS = 3        # the hook kick: missing or older than 3 intervals
QUIET_MS = 2 * 60 * 1000  # a pane with output in the last 2 min is working: no nudge yet
UNREACHABLE_LOG_S = 3600  # `reconcile orca-unreachable` at most hourly
STALL_S = 600             # the first attempt after 10 min, then doubling
CAP = 3                   # attempts before the one escalation (at STALL_S · 2^CAP)
BUDGET_S = 60             # no new action after this long in one pass (the rest is due next pass)
REOPEN_CAP = 2            # closed: reopens (Main) or wakes (division/feature) of one session a day
SILENT_IDLE_S = 1200      # silent: the pane idle this long ...
SILENT_TIMEBOX = 1.5      # ... or the revision this many TIMEBOXes old
SILENT_ESCALATE_S = 1800  # silent: the escalation this long after the reminder, or at 2 × TIMEBOX
KIND = {"unread": "nudge", "undelivered": "deliver", "busy": "restart", "drain": "drain", "silent": "remind"}
EVENT = {"drain": "drain-stale"}
DAILY = ("reopens:", "waker")   # per-day counters, kept for the day
WAKER = "com.orche.main-waker"


def folder() -> Path:
    return paths.orche_home() / "reconcile"


def heartbeat_path() -> Path:
    return folder() / "heartbeat.json"


def state_path() -> Path:
    return folder() / "state.json"


def settings(cfg=None) -> dict:
    return dict((cfg or config.load()).get("reconcile") or config.DEFAULTS["reconcile"])


@dataclass
class _Args:
    dry_run: bool


@dataclass
class Condition:
    cls: str            # unread | closed | undelivered | drain | busy | silent | waker
    key: str            # the episode: "<class>:<target>:<episode>"
    project: str
    target: str         # session (unread, closed) or unit
    age_s: float        # how long the condition has held (its oldest pointer; silent: its revision)
    ready: bool = True  # a repair may start now (unread: not nudged, deliver lock free)
    rev: object = None
    to: str = ""
    sender: str = ""    # who hears the escalation (undelivered, silent) or the oldest pointer's unit
    count: int = 0
    kind: str = ""      # the session's member kind (unread, closed): main | div | feat
    status: str = ""    # its record's status (closed)
    info: dict = field(default_factory=dict)   # silent: since, reason, esc_at, detail


@dataclass
class Action:
    kind: str           # nudge | deliver | drain | restart | reopen | remind | escalate | notify
    cond: Condition
    attempt: int
    cap: int
    extra: dict = field(default_factory=dict)

    def event(self) -> str:
        rev = self.cond.rev if self.cond.rev is not None else "-"
        return (f"reconcile action={EVENT.get(self.kind, self.kind)} project={self.cond.project or '-'} "
                f"target={self.cond.target} rev={rev} attempt={self.attempt}/{self.cap}")

    def describe(self) -> str:
        c, mins = self.cond, int(self.cond.age_s // 60)
        if self.kind == "nudge":
            return f"nudge {c.target} ({c.count} report(s) unread, {mins} min; try {self.attempt}/{self.cap})"
        if self.kind == "deliver":
            return f"deliver {c.target} → {c.to} (rev {c.rev}, {mins} min; try {self.attempt}/{self.cap})"
        if self.kind == "drain":
            return f"drain {c.target} in {c.to} (rev {c.rev} already ran or reported)"
        if self.kind == "restart":
            return f"restart {c.target} (rev {c.rev}, target was busy {mins} min ago; try 1/1)"
        if self.kind == "reopen":
            verb = "reopen" if c.kind == "main" else "wake"
            return (f"{verb} {c.target} pane ({c.count} report(s) waiting, {mins} min; "
                    f"{verb} {self.attempt}/{self.cap} today)")
        if self.kind == "remind":
            return f"remind {c.target} in {c.to} (rev {c.rev}, {c.info.get('reason')}; try 1/1)"
        if self.kind == "notify":
            return f"notify the owner: {c.target} is loaded (once a day)"
        if self.cap == 0:
            return f"escalate {c.cls} {c.target} ({c.status or 'asleep'}, {mins} min)"
        return f"escalate {c.cls} {c.target} (after {self.cap} tries, {mins} min)"


def panes(now_s=None, mark_ok=True):
    """{normalised title: terminal} from one `terminal list`, or None when Orca is unreachable
    (logged as `reconcile orca-unreachable` at most hourly)."""
    try:
        return {orca.normalize_title(t.get("title")): t
                for t in orca.terminals_of(orca.terminal("list", timeout=10))}
    except orca.OrcaError as exc:
        mark = folder() / ".orca-unreachable"
        now_s = time.time() if now_s is None else now_s
        try:
            fresh = now_s - mark.stat().st_mtime < UNREACHABLE_LOG_S
        except OSError:
            fresh = False
        if not fresh and mark_ok:
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.touch()
            log.log("events", f"reconcile orca-unreachable code={getattr(exc, 'code', '')}")
        return None


def _quiet(term) -> bool:
    out = term.get("lastOutputAt")
    return not isinstance(out, (int, float)) or time.time() * 1000 - out >= QUIET_MS


def _projects(reg, keys):
    """(key, project root) of the registered projects in `keys` (default all) with a readable root."""
    for key in sorted(reg.get("projects", {})):
        if keys is not None and key not in keys:
            continue
        try:
            root = units.project_root(reg, key)
        except Exception:  # noqa: BLE001 - a malformed entry skips its project
            continue
        if root is not None:
            yield key, root


def _busy(reg, now, keys) -> list:
    """busy conditions: a unit whose RUN.json (as new as its BRIEF.md) says `stop_reason: busy`, no
    run live. Age: since the record's `finished`."""
    from . import runs
    out = []
    for key, root in _projects(reg, keys):
        for path in sorted((root / brief.UNITS).glob("*/RUN.json")):
            try:
                rec = util.read_json(path, default=None)
                fresh = path.stat().st_mtime >= (path.parent / "BRIEF.md").stat().st_mtime
            except (OSError, util.JsonError):
                continue
            if not isinstance(rec, dict) or rec.get("stop_reason") != "busy" or not fresh:
                continue
            unit = path.parent.name
            b = brief.find(reg, unit, project=key)
            at = units.parse_iso(rec.get("finished"))
            if b is None or not b.rev or at is None or runs.running(key, unit):
                continue
            out.append(Condition("busy", f"busy:{unit}:{b.rev}", key, unit, (now - at).total_seconds(),
                                 rev=b.rev, to=str(rec.get("to") or b.to)))
    return out


def in_quiet_hours(cfg_all, now) -> bool:
    """flow.quiet_hours "HH:MM-HH:MM" (may cross midnight) holds `now`; False when unset or malformed."""
    raw = str(((cfg_all or {}).get("flow") or {}).get("quiet_hours") or "")
    try:
        a, b = (int(h) * 60 + int(m) for h, m in (x.strip().split(":") for x in raw.split("-")))
    except ValueError:
        return False
    t = now.hour * 60 + now.minute
    return a <= t < b if a <= b else (t >= a or t < b)


def probe(term):
    """Seconds since the pane's last output, or None when Orca does not say (the silent class's seam)."""
    out = term.get("lastOutputAt")
    return None if not isinstance(out, (int, float)) else max(0.0, time.time() - out / 1000)


def timebox_s(b) -> float:
    """The brief's TIMEBOX in seconds, uncapped (the runner caps a run at 4 h; a pane is not capped);
    absent or unreadable: the runner's default."""
    from .runs import job
    raw = job._timebox_raw(b.fields.get("TIMEBOX", ""))
    return float(job.DEFAULT_TIMEBOX_S if raw is None else max(60, raw))


def _hm(seconds) -> str:
    h, m = divmod(int(seconds // 60), 60)
    return f"{min(h, 99)}h{m}m"


def _open_child(reg, r, b, to, since) -> bool:
    """`to`, working on `b`, waits on its own dispatch: a brief it sent since `since` (BRIEF.md
    mtime) that is not settled at its own target (not run, not running, not reported on; a pending
    dispatch pointer of it included)."""
    from . import deliver
    for path in (b.root / brief.UNITS).glob("*/BRIEF.md"):
        try:
            if path.parent.name == b.unit or path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        child = brief.find(reg, path.parent.name, project=b.project)
        if child is None or not child.rev or child.sender != to:
            continue
        ct = r.resolve_to(child.to, child.project)
        if ct is not None and ct.status != "retired" and not deliver._settled(child, ct.session):
            return True
    return False


def _silent(reg, r, now, keys, terms, cfg_all) -> list:
    """silent conditions (design §2.2): see the module docstring. Age: since the revision's BRIEF.md."""
    from . import deliver
    out = []
    for key, root in _projects(reg, keys):
        for path in sorted((root / brief.UNITS).glob("*/BRIEF.md")):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            since = _dt.datetime.fromtimestamp(mtime, now.tzinfo)
            elapsed = (now - since).total_seconds()
            if elapsed >= STATE_TTL.total_seconds():
                continue
            b = brief.find(reg, path.parent.name, project=key)
            if b is None or not b.rev:
                continue
            t = r.resolve_to(b.to, b.project)
            if t is None or t.kind == "main" or t.status == "retired":
                continue
            term = terms.get(t.session)
            if term is None or deliver.runner_wanted(r, t.session, cfg_all or {}):
                continue
            tb, idle = timebox_s(b), probe(term)
            idle_due = idle is not None and idle >= SILENT_IDLE_S
            if not idle_due and elapsed < SILENT_TIMEBOX * tb:
                continue
            if deliver._dispatched(t.session, b.unit) is not None or deliver._settled(b, t.session):
                continue
            newest = brief.newest(reg, key, t.session)
            if newest is None or newest.unit != b.unit or _open_child(reg, r, b, t.session, mtime):
                continue
            reason = f"idle {min(9999, int(idle // 60))} min" if idle_due else f"TIMEBOX {_hm(tb)} passed"
            idle_txt = "?" if idle is None else str(int(idle // 60))
            out.append(Condition("silent", f"silent:{b.unit}:{b.rev}", key, b.unit, elapsed,
                                 ready=deliver.lock_free(t.session), rev=b.rev, to=t.session,
                                 sender=b.sender or f"main-{key}",
                                 info={"since": since.isoformat(timespec="seconds"), "reason": reason,
                                       "esc_at": (since + _dt.timedelta(seconds=2 * tb)).isoformat(timespec="seconds"),
                                       "detail": f"idle {idle_txt} min, TIMEBOX {_hm(tb)}"}))
    return out


def _dispatches(reg, m, session, late, now) -> list:
    """undelivered / drain conditions of `session`'s late dispatch pointers, the newest per unit. A
    revision is drained only when it is settled (deliver._settled: ran, runs or reported at that
    rev); a STOP never is (its deliverer stops the run, drops it or types it)."""
    from . import deliver
    newest = {}
    for t, _path, p in late:
        unit = str(p.get("unit") or "")
        if p.get("via") == "dispatch" and unit and (unit not in newest or t >= newest[unit][0]):
            newest[unit] = (t, p)
    out = []
    for unit, (t, p) in sorted(newest.items()):
        rev, age = deliver._rev(p), (now - t).total_seconds()
        b = brief.find(reg, unit)
        if (p.get("status") != "stop" and b is not None and b.rev and rev <= b.rev
                and deliver._settled(b, session)):
            out.append(Condition("drain", f"drain:{unit}:{b.rev}", m.project, unit, age,
                                 ready=deliver.lock_free(session), rev=b.rev, to=session))
            continue
        out.append(Condition("undelivered", f"undelivered:{unit}:{rev}", m.project, unit, age, rev=rev,
                             to=session, sender=str(p.get("from") or f"main-{m.project}")))
    return out


def observe(reg, now, keys=None, wake=True, terms=None, cfg_all=None, hush=False) -> list:
    """The conditions for the projects `keys` (default all): [Condition], sorted by key. `terms`:
    panes() (None: Orca unreachable, no pane class). `hush` (flow.quiet_hours): no closed repair."""
    from . import deliver
    r = roster.Roster(reg)
    out = []
    for session in r.sessions():
        m = r.member(session)
        if m is None or m.status == "retired" or (keys is not None and m.project not in keys):
            continue
        try:
            items = inbox.pending(session)
        except ValueError:
            continue
        late = [(t, path, p) for t, path, p in ((inbox.pointer_time(path, p), path, p) for path, p in items)
                if t is not None and now - t >= inbox.LATE]
        out += _dispatches(reg, m, session, late, now)
        waking = [(t, path, p) for t, path, p in late if inbox.wakes(p)]
        if not wake or not waking or terms is None:
            continue
        t0, path0, p0 = min(waking, key=lambda x: (x[0], x[1].name))
        age, first = (now - t0).total_seconds(), str(p0.get("unit") or "")
        if session in terms:
            out.append(Condition("unread", f"unread:{session}:{path0.name}", m.project, session, age,
                                 ready=_quiet(terms[session]) and not inbox.nudged(session)
                                 and deliver.lock_free(session),
                                 count=len(waking), sender=first, kind=m.kind))
        else:
            out.append(Condition("closed", f"closed:{session}:{path0.name}", m.project, session, age,
                                 ready=not hush, count=len(waking), sender=first, kind=m.kind,
                                 status=m.status))
    out += _busy(reg, now, keys)
    if terms is not None:
        out += _silent(reg, r, now, keys, terms, cfg_all)
    return sorted(out, key=lambda c: c.key)


# ---- plan (pure) -------------------------------------------------------------------------------

def _entry(state, key, now) -> dict:
    """A copy of the state entry for `key`, or a fresh one (also for one older than STATE_TTL)."""
    e = state.get(key)
    first = units.parse_iso(e.get("first")) if isinstance(e, dict) else None
    if first is None or now - first >= STATE_TTL:
        return {"first": now.isoformat(timespec="seconds"), "attempts": [], "escalated": False}
    return dict(e)


def _carried(e, now) -> bool:
    first = units.parse_iso(e.get("first"))
    return bool(e.get("attempts") or e.get("escalated")) and first is not None and now - first < STATE_TTL


def timing(cls) -> tuple:
    """(first attempt after s, cap, escalates) of a class: attempt n+1 is due at first·2^n; the
    escalation at first·2^cap (silent: SILENT_ESCALATE_S after its reminder, or at 2 × TIMEBOX)."""
    return {"busy": (STALL_S, 1, False), "silent": (0.0, 1, True)}.get(cls, (STALL_S, CAP, True))


def _iso(at) -> str:
    return at.isoformat(timespec="seconds")


def _plan_repair(c, e, now, actions) -> None:
    stall, cap, escalates = timing(c.cls)
    attempts = list(e.get("attempts") or [])
    n = len(attempts)
    if n < cap and c.ready and c.age_s >= stall * 2 ** n:
        actions.append(Action(KIND[c.cls], c, n + 1, cap))
        attempts.append(_iso(now))
        n += 1
    if n < cap:
        e["next"] = _iso(now + _dt.timedelta(seconds=max(0.0, stall * 2 ** n - c.age_s)))
        return
    if not escalates or e.get("escalated"):
        e.pop("next", None)
        return
    if c.cls == "silent":
        last = units.parse_iso(attempts[-1]) if attempts else None
        due = [t for t in (last and last + _dt.timedelta(seconds=SILENT_ESCALATE_S),
                           units.parse_iso(c.info.get("esc_at"))) if t is not None]
        at = min(due) if due else None
    else:
        at = now + _dt.timedelta(seconds=max(0.0, stall * 2 ** cap - c.age_s))
    e["next"] = _iso(at) if at is not None else None
    if at is not None and at <= now and len(e.get("attempts") or []) >= cap:
        actions.append(Action("escalate", c, n, cap, {"reminded": attempts[-1] if attempts else None}))


def _plan_closed(c, e, new, now, actions) -> None:
    """closed: a reopen (Main) or wake (active division/feature) when ready, REOPEN_CAP a day, STALL_S
    after the last; past the cap one escalation. A dormant or paused unit: one escalation, no wake."""
    if c.kind != "main" and c.status != "active":
        if c.ready and not e.get("escalated"):
            actions.append(Action("escalate", c, 0, 0))
        return
    r = new.get(f"reopens:{c.target}")
    r = r if isinstance(r, dict) and r.get("day") == now.date().isoformat() else {}
    n, last = int(r.get("n") or 0), units.parse_iso(r.get("at"))
    nxt = last + _dt.timedelta(seconds=STALL_S) if last is not None else now
    e["next"] = _iso(max(nxt, now))
    if not c.ready or nxt > now:
        return
    if n < REOPEN_CAP:
        actions.append(Action("reopen", c, n + 1, REOPEN_CAP))
        e["next"] = _iso(now + _dt.timedelta(seconds=STALL_S))
    elif not e.get("escalated"):
        actions.append(Action("escalate", c, n, REOPEN_CAP))


def plan(conds, state, now, resolved=frozenset()) -> tuple:
    """Pure: (actions, new_state). new_state keeps one entry per condition seen (its attempts so far),
    carries an unseen entry that has attempts or an escalation until STATE_TTL unless its key is in
    `resolved` (positively gone, resolved()), and keeps today's per-day counters; the actions are
    the attempts due now. A ready drain is due at once and keeps no state."""
    new, actions = {}, []
    day = now.date().isoformat()
    seen = {c.key for c in conds}
    for k, v in state.items():
        if not isinstance(v, dict):
            continue
        if k.startswith(DAILY):
            if v.get("day") == day:
                new[k] = v
        elif k not in seen and k not in resolved and _carried(v, now):
            new[k] = v
    for c in conds:
        if c.cls == "drain":
            if c.ready:
                actions.append(Action("drain", c, 1, 1))
            continue
        if c.cls == "waker":
            if (new.get("waker") or {}).get("day") != day:
                actions.append(Action("notify", c, 1, 1))
            continue
        e = new[c.key] = _entry(state, c.key, now)
        e.update(project=c.project, target=c.target, cls=c.cls, to=c.to, seen=_iso(now),
                 **({"rev": c.rev} if c.rev is not None else {}),
                 **({"detail": c.info["detail"]} if c.info.get("detail") else {}))
        if c.cls == "closed":
            _plan_closed(c, e, new, now, actions)
        else:
            _plan_repair(c, e, now, actions)
    return actions, new


def resolved(reg, state) -> set:
    """Keys of carried entries (attempts or an escalation on file) whose condition is positively gone,
    read from local files only (never Orca: an outage resolves nothing). unread/closed: the episode's
    pointer left the inbox; undelivered: no dispatch pointer of that revision is pending; silent and
    busy: the brief is gone or at another revision, or that revision settled (silent: ran, runs or
    reported; busy: its RUN.json records another stop reason)."""
    from . import deliver
    out = set()
    for key, e in state.items():
        if not isinstance(e, dict) or key.startswith(DAILY) or not (e.get("attempts") or e.get("escalated")):
            continue
        try:
            if _gone(reg, key, e, deliver):
                out.add(key)
        except (OSError, ValueError, util.JsonError):
            continue
    return out


def _gone(reg, key, e, deliver) -> bool:
    cls, target, to = e.get("cls"), str(e.get("target") or ""), str(e.get("to") or "")
    if cls in ("unread", "closed"):
        name = key.split(":", 2)[2] if key.count(":") >= 2 else ""
        return not name or not (inbox.box(target) / name).is_file()
    if cls == "undelivered":
        found = deliver._dispatched(to, target) if to else None
        return found is None or deliver._rev(found[1]) != deliver._num(e.get("rev"))
    if cls not in ("silent", "busy"):
        return False
    b = brief.find(reg, target, project=e.get("project"))
    if b is None or b.rev != e.get("rev"):
        return True
    if cls == "silent":
        return bool(to) and deliver._settled(b, to)
    rec = util.read_json(b.folder / "RUN.json", default=None)
    return not isinstance(rec, dict) or rec.get("stop_reason") != "busy"


# ---- apply ---------------------------------------------------------------------------------------

def _orche(*args) -> list:
    return [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), *args]


def _nudge(session) -> None:
    procs.spawn_detached(_orche("nudge", "--", session), paths.orche_home() / "logs" / "nudge.log")


def _post(unit, to, summary, rev=1) -> None:
    inbox.post(inbox.make(unit, "orche", to, "info", brief_rev=rev or 1, urgent=True, summary=summary))


def _escalate(reg, action) -> None:
    c, cap = action.cond, action.cap
    minutes, main = int(c.age_s // 60), f"main-{c.project}"
    if c.cls == "closed" and c.kind == "main":
        _tell_owner(c.project, f"{c.target} is closed with {c.count} report(s) waiting ({minutes} min) after "
                               f"{cap} reopens today — decide: `orche wake {c.target}` now, or leave it "
                               f"(default: ORCHE reopens it tomorrow)")
    elif c.cls == "closed":
        how = f"after {cap} wakes today" if c.status == "active" else f"while {c.status or 'asleep'}"
        _post(c.sender or c.target, main, f"closed: {c.target} has {c.count} report(s) waiting ({minutes} min) "
                                          f"{how} — run `orche wake {c.target}`, or leave it")
    elif c.cls == "silent":
        since = units.parse_iso(c.info.get("since"))
        reminded = units.parse_iso(action.extra.get("reminded"))
        _post(c.target, c.sender or main, f"silent: {c.target} rev {c.rev} in {c.to} since {_hhmm(since)}, "
                                          f"reminded {_hhmm(reminded)} — STOP, AMEND or let it run", rev=c.rev)
    elif c.cls == "undelivered":
        from . import deliver
        last = deliver.last_delivery(c.to, c.target)
        state = str((last or {}).get("state") or "no delivery on file")
        hint = (deliver._hint(roster.Roster(reg), c.to, c.target, state) if last
                else f"retry with `orche deliver {c.target}`")
        _post(c.target, c.sender, f"not delivered after {cap} tries: {c.target} → {c.to} ({state}, pending "
                                  f"{minutes} min)" + (f" — {hint}" if hint else ""), rev=c.rev)
    elif c.kind != "main":
        _post(c.sender or c.target, main, f"unread after {cap} nudges: {c.target} has {c.count} report(s) "
                                          f"waiting ({minutes} min) — check its pane, then `orche nudge {c.target}`")
    else:
        _tell_owner(c.project, f"Main has {c.count} report(s) unread {minutes} min after {cap} nudges; "
                               f"last nudge: {_last_nudge(c.target)} — check the Main pane")


def _hhmm(at) -> str:
    return at.astimezone().strftime("%H:%M") if at is not None else "?"


def _remind(unit, reason) -> None:
    procs.spawn_detached(_orche("deliver", "--remind", reason, "--", unit), paths.orche_home() / "logs" / "deliver.log")


def _reopen(reg, c) -> None:
    """A Main's pane opened in place (no `terminal switch`), or an active division/feature woken
    (hr.wake reopens its missing pane); then its nudge."""
    from . import hr
    if c.kind == "main":
        hr.project_pane(reg["projects"][c.project], dry_run=False, switch=False)
    else:
        hr.wake(reg, roster.Roster(reg).member(c.target))
    _nudge(c.target)


def _last_nudge(session) -> str:
    from . import deliver
    try:
        last = util.read_json(inbox.box(session) / deliver.LAST_NUDGE, default=None)
    except (OSError, util.JsonError):
        last = None
    if not isinstance(last, dict):
        return "none on file"
    at = units.parse_iso(last.get("at"))
    return f"{last.get('state', '?')} at {at.astimezone().strftime('%H:%M') if at else '?'}"


def _tell_owner(project, text) -> None:
    """An owner notification: a project's escalation is a `stuck` notice (flow.owner.announce: recorded
    first, the per-project gap, a held one sent by the next pass, no sound in quiet hours); the global
    waker notice goes through notify.send. Text and title travel as osascript arguments. One events
    line when shown."""
    if project:
        shown = owner.announce(project, "stuck", text, units.now())
    else:
        cfg_all = config.load()
        shown = notify.enabled(cfg_all) and notify.send("ORCHE", text, sound=not in_quiet_hours(cfg_all, units.now()),
                                                        kind="reconcile")
    if shown:
        log.log("events", f"reconcile owner-notified project={project or '-'}")


def waker_loaded() -> bool:
    """The pre-0.6.0 Main waker LaunchAgent is loaded (the doctor's check)."""
    from .install import doctor
    return bool(doctor.main_waker_rows())


def apply(reg, action) -> None:
    from . import deliver, runs
    c, kind = action.cond, action.kind
    if kind == "nudge":
        _nudge(c.target)
    elif kind == "deliver":
        deliver.spawn(c.target, retry=True)
    elif kind == "drain":
        deliver.drain_now(c.to, c.target, int(c.rev or 1))
    elif kind == "restart":
        runs.start(c.target)
    elif kind == "reopen":
        _reopen(reg, c)
    elif kind == "remind":
        _remind(c.target, str(c.info.get("reason") or ""))
    elif kind == "notify":
        _tell_owner("", f"{WAKER} is still loaded: it predates 0.6.0 and doubles Main's wakes — run "
                        f"launchctl bootout gui/{os.getuid()}/{WAKER}")
    else:
        _escalate(reg, action)


def _record(new, a, now) -> None:
    """The bookkeeping of an applied action, also when its apply raised: a failing repair spends its
    attempt, so it is capped and escalated once like any other."""
    at = _iso(now)
    if a.kind == "notify":
        new["waker"] = {"day": now.date().isoformat(), "at": at}
        return
    e = new.get(a.cond.key)
    if a.kind == "drain" or e is None:
        return
    if a.kind == "escalate":
        e["escalated"], e["escalated_at"] = True, at
        e.pop("next", None)
        return
    if a.kind == "reopen":
        new[f"reopens:{a.cond.target}"] = {"day": now.date().isoformat(), "n": a.attempt, "at": at}
    e["attempts"] = list(e.get("attempts") or []) + [at]


@contextlib.contextmanager
def _pass_lock():
    folder().mkdir(parents=True, exist_ok=True)
    with open(folder() / ".lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_heartbeat():
    try:
        data = util.read_json(heartbeat_path(), default=None)
    except util.JsonError:
        return None
    return data if isinstance(data, dict) else None


def write_heartbeat(now, cfg, took_s, actions, enabled=True) -> None:
    old = read_heartbeat() or {}
    day = now.date().isoformat()
    today = (int(old.get("repairs_today") or 0) if old.get("day") == day else 0) + actions
    util.write_json(heartbeat_path(), {"at": now.isoformat(timespec="seconds"), "every_s": int(cfg.get("every_s", 300)),
                                       "version": __version__, "took_s": round(took_s, 2), "actions": actions,
                                       "enabled": bool(enabled), "day": day, "repairs_today": today})


def run_pass(dry_run=False, out=print, keys=None, as_json=False) -> int:
    """One pass over the projects `keys` (default all). 0; 1 when the registry cannot be read; 2 for an
    unknown project. `out` receives the human lines, or one JSON document with `as_json`."""
    started = time.monotonic()
    cfg_all = config.load()
    cfg = settings(cfg_all)
    now = units.now()
    if dry_run:
        return _pass(cfg_all, cfg, now, started, True, out, keys, as_json)
    with _pass_lock() as ok:
        if not ok:
            out(json.dumps({"ran": False, "reason": "another pass is running"}) if as_json
                else "reconcile: another pass is running")
            return 0
        return _pass(cfg_all, cfg, now, started, False, out, keys, as_json)


def _pass(cfg_all, cfg, now, started, dry_run, out, keys=None, as_json=False) -> int:
    doc = {"ran": True, "dry_run": dry_run, "enabled": bool(cfg.get("enabled", True)), "actions": []}

    def done_json():
        if as_json:
            out(json.dumps(doc, ensure_ascii=False))
        return 0
    if not cfg.get("enabled", True):
        if not dry_run:
            write_heartbeat(now, cfg, time.monotonic() - started, 0, enabled=False)
        if not as_json:
            out("reconcile: off (reconcile.enabled false)" + ("; would write the heartbeat only" if dry_run else ""))
        return done_json()
    try:
        reg = registry.load()
    except registry.RegistryError as exc:
        print(f"orche: reconcile: {str(exc).splitlines()[0] if str(exc) else 'registry unreadable'}", file=sys.stderr)
        return 1
    unknown = sorted(set(keys or ()) - set(reg.get("projects", {})))
    if unknown:
        print(f"orche: reconcile: unknown project {unknown[0]!r}", file=sys.stderr)
        return 2
    from . import runs
    try:
        runs.reconcile_cli(reg, _Args(dry_run), keys)   # nothing under --dry-run
    except Exception as exc:  # noqa: BLE001 - lost runs never stop the pass
        log.log("hooks", f"reconcile: runs: {type(exc).__name__}: {exc}")
    # C10 T8: a lineage close a crash or a busy lock missed (never raises; dry-run takes no lock)
    shut = [{"action": "lineage-close", "class": "lineage", "project": p, "target": lin, "rev": None,
             "attempt": 1, "cap": 1, "line": f"{'would close' if dry_run else 'closed'} lineage {lin} ({p})",
             "failed": None} for p, lin in lineage.repair(reg, dry_run, now=now, keys=keys)]
    # C10 T10: answered asks, missed secret resumes, the fallback, held notices, re-notifications
    notes = owner.heartbeat(reg, now, dry_run, keys)
    try:
        state = util.read_json(state_path(), default={})
    except util.JsonError:
        state = {}
    state = state if isinstance(state, dict) else {}
    others = {k: v for k, v in state.items()
              if keys is not None and isinstance(v, dict) and v.get("project") and v.get("project") not in keys}
    mine = {k: v for k, v in state.items() if k not in others}
    wake = bool((cfg_all.get("inbox") or {}).get("wake", True))
    conds = observe(reg, now, keys, wake=wake, terms=panes(mark_ok=not dry_run), cfg_all=cfg_all,
                    hush=in_quiet_hours(cfg_all, now))
    held = {c.project for c in conds if guard.paused(c.project) is not None}
    conds = [c for c in conds if c.project not in held or c.cls == "drain"]   # G8: the owner paused it
    if not dry_run and waker_loaded():
        conds.append(Condition("waker", "waker", "", WAKER, 0.0))
    actions, new = plan(conds, mine, now, resolved(reg, mine))
    new.update(others)
    if dry_run:
        for row in shut + notes:
            doc["actions"].append(row)
            if not as_json:
                out(row["line"])
        for a in actions:
            doc["actions"].append(_row(a, ""))
            if not as_json:
                out(f"would {a.describe()}")
        if not actions and not shut and not notes and not as_json:
            out("reconcile: nothing to repair")
        return done_json()
    done = 0
    util.write_json(state_path(), new)
    for row in shut:
        log.log("events", f"reconcile action=lineage-close project={row['project']} lineage={row['target']}")
        doc["actions"].append(row)
        if not as_json:
            out(row["line"])
        done += 1
    for row in notes:
        log.log("events", f"reconcile action={row['action']} project={row['project']} target={row['target']} "
                          f"rev=- attempt={row['attempt']}/{row['cap']}")
        doc["actions"].append(row)
        if not as_json:
            out(row["line"])
        done += 1
    for a in actions:
        if time.monotonic() - started >= BUDGET_S:
            break
        failed = ""
        try:
            apply(reg, a)
        except Exception as exc:  # noqa: BLE001 - one failed repair never stops the pass; it still counts
            failed = type(exc).__name__
            log.log("hooks", f"reconcile: {a.kind} {a.cond.target}: {failed}: {exc}")
        _record(new, a, now)
        util.write_json(state_path(), new)
        log.log("events", a.event() + (f" failed={failed}" if failed else ""))
        doc["actions"].append(_row(a, failed))
        if not as_json:
            out(a.describe() + (f" — failed ({failed})" if failed else ""))
        done += 1
    took = time.monotonic() - started
    if done:
        log.log("events", f"reconcile pass took={took:.1f}s actions={done}")
    write_heartbeat(now, cfg, took, done)
    return done_json()


def _row(a, failed) -> dict:
    c = a.cond
    return {"action": EVENT.get(a.kind, a.kind), "class": c.cls, "project": c.project or None, "target": c.target,
            "rev": c.rev, "attempt": a.attempt, "cap": a.cap, "line": a.describe(), "failed": failed or None}


# ---- status ------------------------------------------------------------------------------------

FIX = {"unread": "check the pane, then `orche nudge {t}`", "closed": "`orche wake {t}`",
       "undelivered": "`orche deliver {t}`", "silent": "STOP, AMEND or let it run"}


def stalled(project) -> list:
    """`orche status`'s stalled lines' data: the project's repairs on file in state.json that ORCHE has
    tried (attempts) or escalated: [{cls, target, rev, to, tries, cap, escalated, escalated_at, next,
    last, detail}]."""
    try:
        state = util.read_json(state_path(), default={})
    except util.JsonError:
        return []
    out = []
    for key in sorted(state if isinstance(state, dict) else {}):
        e = state[key]
        if not isinstance(e, dict) or e.get("project") != project:
            continue
        attempts = e.get("attempts") or []
        if attempts or e.get("escalated"):
            cls = e.get("cls")
            cap = (REOPEN_CAP if cls == "closed" else timing(cls)[1]) if cls else None
            out.append({"cls": cls, "target": e.get("target"), "rev": e.get("rev"), "to": e.get("to") or None,
                        "tries": len(attempts), "cap": cap, "escalated": bool(e.get("escalated")),
                        "escalated_at": e.get("escalated_at"), "next": e.get("next"),
                        "last": attempts[-1] if attempts else None, "detail": e.get("detail")})
    return out


def stalled_line(s) -> str:
    """One `stalled:` line (design §6)."""
    if s["cls"] == "lineage":
        return f"  stalled: lineage {s['target']} waiting on {s.get('detail') or '?'}"
    rev = f" rev {s['rev']}" if s.get("rev") is not None else ""
    if s["escalated"]:
        fix = FIX.get(s["cls"], "").format(t=s["target"])
        return (f"  stalled: escalated {s['cls']} {s['target']}{rev} at "
                f"{_hhmm(units.parse_iso(s.get('escalated_at')))}" + (f" — {fix}" if fix else ""))
    if s["cls"] == "silent":
        return (f"  stalled: silent {s['target']}{rev} in {s.get('to') or '?'} ({s.get('detail') or '?'}) — "
                f"reminded {_hhmm(units.parse_iso(s.get('last')))}")
    nxt = units.parse_iso(s.get("next"))
    return (f"  stalled: {s['cls']} {s['target']}{rev} — ORCHE retrying ({s['tries']}/{s['cap']}"
            + (f", next {_hhmm(nxt)})" if nxt is not None else ")"))


def repairs() -> dict:
    """{(class, target): entry} of state.json's unread, closed and undelivered entries, the newest
    seen per pair (`orche status`'s hints)."""
    try:
        state = util.read_json(state_path(), default={})
    except util.JsonError:
        return {}
    out = {}
    for e in (state.values() if isinstance(state, dict) else ()):
        if isinstance(e, dict) and e.get("cls") in ("unread", "closed", "undelivered"):
            k = (e["cls"], str(e.get("target") or ""))
            if k not in out or str(e.get("seen") or "") > str(out[k].get("seen") or ""):
                out[k] = e
    return out


def overdue_hint(kind, target, reps, fresh) -> str:
    """The tail of an `unread:` (target: session) or `undelivered:` (target: unit) line (design §6):
    ORCHE's live repair while the heartbeat is fresh, else the command; a pane the last pass found
    closed names `orche wake` (or when ORCHE reopens it)."""
    e = reps.get((kind, target))
    closed = reps.get(("closed", target)) if kind == "unread" else None
    if closed is not None and (e is None or str(closed.get("seen") or "") >= str(e.get("seen") or "")):
        nxt = units.parse_iso(closed.get("next"))
        if fresh and not closed.get("escalated") and nxt is not None:
            return f"pane closed: ORCHE reopens it at {_hhmm(nxt)}"
        return f"pane closed: run `orche wake {target}`"
    tries = len((e or {}).get("attempts") or [])
    if fresh and e is not None and not e.get("escalated") and tries < CAP:
        nxt = units.parse_iso(e.get("next"))
        return f"ORCHE retrying ({tries}/{CAP}" + (f", next {_hhmm(nxt)})" if nxt is not None else ")")
    return f"run `orche {'nudge' if kind == 'unread' else 'deliver'} {target}`"


def heartbeat_age(now=None):
    """(heartbeat dict or None, age in seconds or None)."""
    hb = read_heartbeat()
    at = units.parse_iso((hb or {}).get("at"))
    if hb is None or at is None:
        return hb, None
    return hb, ((now or units.now()) - at).total_seconds()


def heartbeat_line(now=None, cfg=None) -> str:
    """`orche status`'s global line."""
    cfg = settings(cfg)
    every = int(cfg.get("every_s", 300))
    if not cfg.get("enabled", True):
        return "heartbeat: off (reconcile.enabled false)"
    hb, age = heartbeat_age(now)
    if age is None:
        return "heartbeat: never — install the job with orche install"
    mins = max(0, int(age // 60))
    if age > STALE_INTERVALS * every:
        return f"heartbeat: STALE — last pass {mins} min ago (every {every // 60} min); run orche doctor"
    day = (now or units.now()).date().isoformat()
    today = int(hb.get("repairs_today") or 0) if hb.get("day") == day else 0
    return f"heartbeat: last pass {mins} min ago (every {every // 60} min, {today} repairs today)"


def heartbeat_json(now=None, cfg=None) -> dict:
    cfg = settings(cfg)
    hb, age = heartbeat_age(now)
    every = int(cfg.get("every_s", 300))
    return {"enabled": bool(cfg.get("enabled", True)), "at": (hb or {}).get("at"),
            "age_s": None if age is None else int(age), "every_s": every,
            "stale": age is None or age > STALE_INTERVALS * every}


def doctor_row(now=None, cfg=None):
    """(level, "heartbeat", detail): WARN when stale or never ran, OK otherwise (off included)."""
    cfg = settings(cfg)
    every = int(cfg.get("every_s", 300))
    if not cfg.get("enabled", True):
        return ("OK", "heartbeat", "off (reconcile.enabled false)")
    _hb, age = heartbeat_age(now)
    if age is None:
        return ("WARN", "heartbeat", "no pass on file — install the job with orche install, or run orche reconcile")
    mins = max(0, int(age // 60))
    if age > STALE_INTERVALS * every:
        return ("WARN", "heartbeat", f"stale: last pass {mins} min ago (every {every // 60} min) — "
                                     "check the reconcile job (orche install) or run orche reconcile")
    return ("OK", "heartbeat", f"last pass {mins} min ago (every {every // 60} min)")


def kick_path() -> Path:
    return folder() / ".kicked"


def kick(now=None) -> bool:
    """The hook kick (design c10 §2.4): when reconcile is on, the heartbeat is missing or older than
    KICK_INTERVALS × every_s and `reconcile/.kicked` is older than every_s, touch `.kicked` and start
    `orche reconcile` detached. True when a pass was started. Cheap: one config read, one stat."""
    cfg = settings()
    if not cfg.get("enabled", True):
        return False
    every = int(cfg.get("every_s", 300))
    _hb, age = heartbeat_age(now)
    if age is not None and age <= KICK_INTERVALS * every:
        return False
    mark = kick_path()
    try:
        if time.time() - mark.stat().st_mtime < every:
            return False
    except FileNotFoundError:
        pass
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.touch()
    os.utime(mark)
    procs.spawn_detached(_orche("reconcile"), paths.orche_home() / "logs" / "reconcile.log")
    return True
