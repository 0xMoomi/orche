"""The heartbeat pass (`orche reconcile`, design c10 §2.1): one bounded, idempotent pass, no daemon.

A pass takes a non-blocking flock on ORCHE_HOME/reconcile/.lock (held -> exit 0), settles lost runs
(runs.reconcile), observes the repair classes, plans the due attempts (pure: plan()) and applies
them through the existing paths only, saving reconcile/state.json after each action and writing
reconcile/heartbeat.json last. Classes (design §2.2):

- unread <session>: a waking pointer pending >= inbox.LATE, the pane present and quiet (no output
  for QUIET_MS), not nudged within inbox.NUDGED_TTL, its `.deliver.lock` free -> `orche nudge`;
- closed <session> (never a headless feature, `pane: false`): waking pointers pending >= inbox.LATE and
  no pane, or a Main pane Orca lists disconnected (with or without reports waiting), outside
  flow.quiet_hours: Main's pane is reopened in place (`hr.project_pane(switch=False)`; a disconnected
  one closed first, deliver.reopen_pane; no nudge when nothing waits),
  an active division/feature woken (`hr.wake`), then `orche nudge`, REOPEN_CAP a day (`reopens:<session>`, STALL_S apart);
  past the cap one escalation; a dormant or paused unit is never woken: one urgent `info` to Main;
- undelivered <unit>: a `via: dispatch` pointer pending >= inbox.LATE -> `orche deliver --retry`; a
  `deferred` one (a runner dispatch from inside a Codex sandbox) at once, its first try in that pass;
- drain <unit>: that pointer's revision ran, runs or was reported on (never a STOP) -> drained at
  once under the target's `.deliver.lock` taken non-blocking (uncapped, nothing else is touched);
- busy <unit>: RUN.json of the current revision says `stop_reason: busy`, no run live ->
  `orche run <unit>` once (cap 1, no escalation); while its project (or all projects) is at the slot
  cap the restart waits and spends nothing, and a restart that came back busy again is a fresh try (R34);
- held <unit> (0.9.0, design §8.5): RUN.json says `held` or `usage_limit`, no run live, no STOP since,
  its vendor's usage hold is over -> `orche run <unit>` once per held record, oldest first, at most
  runner.max_parallel_total a pass;
- queue <project> (§5.10, R19): no integrator running: `land: auto` releases entries awaiting the owner,
  a lost `testing` entry goes back to the front (integrator.recover), then an entry the integrator may
  test or a pending proof (queue.startable), neither the queue nor the project paused -> queue.start (no
  git, no gh in the pass; quiet hours and usage holds do not stop it);
- usage <vendor>: an active hold nobody told yet -> the owner notice (Appendix A.15) once; a hold past
  its `until` -> `usage resume` logged once;
- review <project>: a pending Build review (review/build.py: a run's RUN.json `review.build: pending`)
  not running that claim() lets start -> started detached (`orche review --pr N --wait --unit U`,
  never inline), at most one per project a pass, after the repairs and never past
  BUDGET_S;
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

Watchdog (0.7.2): a pass still running WATCHDOG_S after it took the lock is aborted — SIGALRM raises
Abort in the main thread, and when the signal cannot interrupt the call (a read blocked inside the
kernel, e.g. by a macOS privacy check on ~/Downloads) a thread ends the process WATCHDOG_GRACE_S
later. Either way `reconcile/last-abort.json` records {at, phase, path, hint} (hint `privacy` for a
path under a protected folder, else `slow`), one events line is logged, the lock is released and
the pass exits 3. A pass the LaunchAgent ran also stamps heartbeat.json's `job_at`.
"""
import contextlib
import datetime as _dt
import fcntl
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import __version__, brief, config, inbox, log, notify, orca, paths, procs, registry, roster, units, util
from .flow import guard, lineage, owner, usage

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
KIND = {"unread": "nudge", "undelivered": "deliver", "busy": "restart", "drain": "drain", "silent": "remind",
        "held": "rerun"}
EVENT = {"drain": "drain-stale"}
DAILY = ("reopens:", "waker")   # per-day counters, kept for the day
WAKER = "com.orche.main-waker"
WATCHDOG_S = 2 * BUDGET_S       # a pass still running this long after it took the lock is aborted (exit 3)
WATCHDOG_GRACE_S = 15           # ... by a thread this much later when SIGALRM could not interrupt the call
ABORT_EXIT = 3
ABORT_TTL = _dt.timedelta(hours=24)   # `orche doctor` reads an abort this recent
PHASE = "start"                 # the phase of the running pass (the abort record names it)
_exit = os._exit                # test seam: the watchdog thread's exit


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
        if self.kind == "rerun":
            return f"re-run {c.target} (rev {c.rev}, its {c.info.get('vendor')} usage hold is over; try 1/1)"
        if self.kind == "reopen":
            verb = "reopen" if c.kind == "main" else "wake"
            why = f"{c.count} report(s) waiting, {mins} min" if c.count else "disconnected"
            return f"{verb} {c.target} pane ({why}; {verb} {self.attempt}/{self.cap} today)"
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


def _stamp(at) -> str:
    """An episode id from a time, UTC (`20261024T010000Z`): held keys sort oldest first."""
    return at.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _busy(reg, now, keys, cfg_all=None) -> list:
    """busy and held conditions from the projects' RUN.json files (one scan). busy: a unit whose
    RUN.json (as new as its BRIEF.md) says `stop_reason: busy`, no run live; not ready while its
    project is at the slot cap (runs.at_cap: the attempt is not spent, R34). Age: since the record's
    `finished`. held (§8.5): `held` or `usage_limit`, no run live, no STOP marker since the record,
    its vendor's hold over (flow/usage.py); age since that hold's `until`; one episode per record."""
    from . import runs
    out, full = [], {}
    per_pass = runs.caps(cfg_all)[1]
    for key, root in _projects(reg, keys):
        for path in sorted((root / brief.UNITS).glob("*/RUN.json")):
            try:
                rec = util.read_json(path, default=None)
                st = path.stat()
                fresh = st.st_mtime >= (path.parent / "BRIEF.md").stat().st_mtime
            except (OSError, util.JsonError):
                continue
            reason = rec.get("stop_reason") if isinstance(rec, dict) else None
            if reason not in ("busy",) + usage.HELD or not fresh:
                continue
            unit = path.parent.name
            b = brief.find(reg, unit, project=key)
            at = units.parse_iso(rec.get("finished"))
            if b is None or not b.rev or at is None or runs.running(key, unit):
                continue
            to = str(rec.get("to") or b.to)
            if reason == "busy":
                if key not in full:
                    full[key] = runs.at_cap(key, cfg_all)
                out.append(Condition("busy", f"busy:{unit}:{b.rev}", key, unit, (now - at).total_seconds(),
                                     ready=not full[key], rev=b.rev, to=to, info={"since": rec.get("finished")}))
                continue
            vendor = str(rec.get("held_vendor") or rec.get("vendor") or "claude")     # the stage's vendor (R9)
            hold, stop = usage.read(vendor), runs.marker_time(key, unit)
            if usage.is_active(hold, now) or (stop is not None and stop >= st.st_mtime_ns):
                continue
            until = usage.until_of(hold)
            since = until if until is not None and until > at else at
            out.append(Condition("held", f"held:{_stamp(at)}:{unit}:{b.rev}", key, unit,
                                 max(0.0, (now - since).total_seconds()), rev=b.rev, to=to,
                                 info={"since": _stamp(at), "vendor": vendor, "per_pass": per_pass}))
    return out


def _note(action, cls, project, target, line, failed=None) -> dict:
    """A row of a pass step that acted itself (queue, usage, review), as owner.heartbeat's."""
    return {"action": action, "class": cls, "project": project, "target": target, "rev": None, "attempt": 1,
            "cap": 1, "line": line, "failed": failed}


def _queue_starts(reg, keys, dry_run) -> list:
    """Class `queue` (§5.10, R19), per project whose merge queue has entries and no integrator running:
    under `land: auto` entries still waiting for the owner's word become plain waiting (queue.release);
    a `testing` entry whose integrator is gone (its pid dead, or queue.lost_after_s passed) goes back to
    the front (integrator.recover; only its gate's own processes are stopped); then work the integrator
    may take (queue.startable: an approved waiting entry or a merge whose proof is pending; neither the
    queue nor the project paused by `orche queue pause` or `orche flow pause`) -> queue.start(p). The
    queue's own files and the process table only (no git, no gh)."""
    from . import queue
    from .queue import integrator, store
    cfg = config.load()
    rows = []
    for key in sorted(reg.get("projects", {})):
        if keys is not None and key not in keys:
            continue
        try:
            q = store.read(key)
            if not isinstance(q.get("entries"), list) or not q["entries"] or queue.running(key):
                continue
            stale = [e for e in q["entries"] if e.get("state") == "testing" and not queue.landed(e)]
            if dry_run:
                lost = stale
            else:
                queue.release(key, cfg)
                lost = integrator.recover(key) if stale else []
                q = store.read(key)
            can = queue.startable(key)
        except Exception as exc:  # noqa: BLE001 - one unreadable queue never stops the pass
            log.log("hooks", f"reconcile: queue {key}: {type(exc).__name__}: {exc}")
            continue
        for e in lost:
            rows.append(_note("queue-lost", "queue", key, key,
                              f"{'would check the testing' if dry_run else 'recovered the lost'} PR #{e.get('pr')} "
                              f"of {key}{'' if dry_run else ' (back at the front)'}"))
        if not can:
            continue
        waiting = sum(1 for e in q.get("entries") or [] if e.get("state") == "waiting" and e.get("owner_ok"))
        failed = None
        if not dry_run:
            try:
                queue.start(key)
            except Exception as exc:  # noqa: BLE001 - one queue never stops the pass
                failed = type(exc).__name__
                log.log("hooks", f"reconcile: queue start {key}: {failed}: {exc}")
        rows.append(_note("queue-start", "queue", key, key, f"{'would start' if dry_run else 'start'} the merge "
                                                            f"queue of {key} ({waiting} waiting)", failed))
    return rows


def _usage_notes(now, dry_run) -> list:
    """Class `usage` (§8.5): an active hold nobody told yet -> the owner notice (owner.usage_notice,
    Appendix A.15) once, marked first; a hold past its `until` -> `usage resume vendor=<v>` once."""
    rows = []
    for hold in usage.holds():
        vendor, until, n = str(hold.get("vendor") or "?"), usage.until_of(hold), len(usage.held_units(hold))
        project, at = str(hold.get("project") or ""), usage.hhmm(until, now)
        if usage.is_active(hold, now) and not hold.get("notified") and project:
            failed = None
            if not dry_run:
                try:
                    usage.mark(vendor, notified=_iso(now))      # marked first: at most one notice per hold
                    owner.usage_notice(project, vendor, at, n, now)
                except Exception as exc:  # noqa: BLE001 - a notice never stops the pass
                    failed = type(exc).__name__
                    log.log("hooks", f"reconcile: usage notice {vendor}: {failed}: {exc}")
            rows.append(_note("usage-notice", "usage", project, vendor, f"{'would tell' if dry_run else 'told'} the "
                                                                        f"owner: {vendor} usage limit until {at} ({n} units held)", failed))
        elif until is not None and not usage.is_active(hold, now) and not hold.get("resumed"):
            if not dry_run:
                try:
                    usage.mark(vendor, resumed=_iso(now))
                except OSError as exc:
                    log.log("hooks", f"reconcile: usage resume {vendor}: {exc}")
                    continue
                log.log("events", f"usage resume vendor={vendor}")
            rows.append(_note("usage-resume", "usage", project or "-", vendor,
                              f"{vendor} usage hold over at {at}: its {n} held units re-run"))
    return rows


def _review_module():
    """review/build.py (the Build review) with pending(), claim(), running() and start(); None without them."""
    try:
        from .review import build
    except ImportError:
        return None
    ok = all(callable(getattr(build, f, None)) for f in ("pending", "claim", "running", "start"))
    return build if ok else None


REVIEW_CLAIMS = 3          # claim() checks (one gh call each) per project a pass


def _reviews(reg, keys, dry_run, started) -> list:
    """Class `review`: per project, its oldest pending Build review (review.build.pending: a run's
    RUN.json says `review.build: pending` and no build row exists at that head) that is not running and
    that claim() lets start (the PR open at that head, the reviewer's vendor not held, fewer than
    `review.max_starts` starts) -> review.build.start(project, pr, unit), detached: the pass never runs
    a review inline. At most one start per project a pass and REVIEW_CLAIMS claims, never past
    BUDGET_S, never for a project the owner paused. Nothing without the module."""
    mod = _review_module()
    if mod is None:
        return []
    rows = []
    for key in sorted(reg.get("projects", {})):
        if (keys is not None and key not in keys) or guard.paused(key) is not None:
            continue
        if time.monotonic() - started >= BUDGET_S:
            break
        try:
            items = list(mod.pending(key, reg) or [])
            todo = [x for x in items if not mod.running(key, x["pr"], x["sha"])]
        except Exception as exc:  # noqa: BLE001 - one project's reviews never stop the pass
            log.log("hooks", f"reconcile: review pending {key}: {type(exc).__name__}: {exc}")
            continue
        for item in todo[:REVIEW_CLAIMS]:
            pr, failed = item["pr"], None
            if not dry_run:
                try:
                    if not mod.claim(key, item, reg):
                        continue
                    mod.start(key, pr, item.get("unit"))
                except Exception as exc:  # noqa: BLE001 - it is pending again next pass
                    failed = type(exc).__name__
                    log.log("hooks", f"reconcile: build review {key} #{pr}: {failed}: {exc}")
            rows.append(_note("build-review", "review", key, f"pr-{pr}", f"{'would start' if dry_run else 'start'} the "
                                                                        f"Build review of {key} PR #{pr} ({len(items)} pending)", failed))
            break
    return rows


def in_quiet_hours(cfg_all, now) -> bool:
    """flow.quiet_hours "HH:MM-HH:MM" (may cross midnight) holds `now`'s own wall time (the pass's clock,
    not the host's zone); False when unset or malformed."""
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
                             to=session, sender=str(p.get("from") or f"main-{m.project}"),
                             info={"deferred": True} if p.get("deferred") else {}))
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
                if t is not None and (now - t >= inbox.LATE or p.get("deferred"))]
        out += _dispatches(reg, m, session, late, now)
        waking = [(t, path, p) for t, path, p in late if inbox.wakes(p)]
        if not wake or terms is None or (m.entry or {}).get("pane") is False:
            continue   # a headless feature (`pane: false`) has no pane to reopen: its runs read the inbox
        dead = m.kind == "main" and session in terms and orca.disconnected(terms[session])
        if not waking and not dead:
            continue
        if waking:
            t0, path0, p0 = min(waking, key=lambda x: (x[0], x[1].name))
            age, first, episode = (now - t0).total_seconds(), str(p0.get("unit") or ""), path0.name
        else:
            age, first, episode = 0.0, "", "pane"    # a dead Main pane, nothing waiting: its own episode
        if session in terms and not dead:
            out.append(Condition("unread", f"unread:{session}:{episode}", m.project, session, age,
                                 ready=_quiet(terms[session]) and not inbox.nudged(session)
                                 and deliver.lock_free(session),
                                 count=len(waking), sender=first, kind=m.kind))
        else:
            out.append(Condition("closed", f"closed:{session}:{episode}", m.project, session, age,
                                 ready=not hush, count=len(waking), sender=first, kind=m.kind,
                                 status=m.status, info={"handle": terms[session].get("handle")} if dead else {}))
    out += _busy(reg, now, keys, cfg_all)
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
    return {"busy": (STALL_S, 1, False), "silent": (0.0, 1, True), "held": (0.0, 1, False)}.get(
        cls, (STALL_S, CAP, True))


def _came_back(e, c) -> bool:
    """A busy episode whose last restart is older than the busy record on file: that restart came back
    busy (R34), so it is a fresh try."""
    tries = e.get("attempts") or []
    last, since = (units.parse_iso(tries[-1]) if tries else None), units.parse_iso(c.info.get("since"))
    return last is not None and since is not None and since > last


def _iso(at) -> str:
    return at.isoformat(timespec="seconds")


def _plan_repair(c, e, now, actions) -> None:
    stall, cap, escalates = timing(c.cls)
    attempts = list(e.get("attempts") or [])
    n = len(attempts)
    if n < cap and c.ready and (c.age_s >= stall * 2 ** n or (n == 0 and c.info.get("deferred"))):
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
    new, actions, reruns = {}, [], 0
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
                 **({"detail": c.info["detail"]} if c.info.get("detail") else {}),
                 **({"since": c.info["since"]} if c.cls == "held" else {}))
        if c.cls == "closed":
            _plan_closed(c, e, new, now, actions)
            continue
        if c.cls == "busy" and _came_back(e, c):
            e["attempts"] = []
        if c.cls == "held" and reruns >= int(c.info.get("per_pass") or 1):
            c = replace(c, ready=False)       # at most runner.max_parallel_total re-runs a pass, oldest first
        n = len(actions)
        _plan_repair(c, e, now, actions)
        reruns += c.cls == "held" and len(actions) > n
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
    if cls == "held":
        b = brief.find(reg, target, project=e.get("project"))
        if b is None or b.rev != e.get("rev"):
            return True
        rec = util.read_json(b.folder / "RUN.json", default=None)
        at = units.parse_iso(rec.get("finished")) if isinstance(rec, dict) else None
        return at is None or rec.get("stop_reason") not in usage.HELD or _stamp(at) != e.get("since")
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


def _escalate(reg, action, now) -> None:
    c, cap = action.cond, action.cap
    minutes, main = int(c.age_s // 60), f"main-{c.project}"
    if c.cls == "closed" and c.kind == "main" and not c.count:
        _tell_owner(c.project, f"{c.target}'s pane is disconnected after {cap} reopens today — decide: "
                               f"`orche wake {c.target}` now, or leave it (default: ORCHE reopens it tomorrow)", now)
    elif c.cls == "closed" and c.kind == "main":
        _tell_owner(c.project, f"{c.target} is closed with {c.count} report(s) waiting ({minutes} min) after "
                               f"{cap} reopens today — decide: `orche wake {c.target}` now, or leave it "
                               f"(default: ORCHE reopens it tomorrow)", now)
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
                               f"last nudge: {_last_nudge(c.target)} — check the Main pane", now)


def _hhmm(at) -> str:
    return at.astimezone().strftime("%H:%M") if at is not None else "?"


def _remind(unit, reason) -> None:
    procs.spawn_detached(_orche("deliver", "--remind", reason, "--", unit), paths.orche_home() / "logs" / "deliver.log")


def _reopen(reg, c) -> None:
    """A Main's pane opened in place (no `terminal switch`; a disconnected one closed and reopened,
    deliver.reopen_pane) and stamped MAIN_REOPENED, or an active division/feature woken (hr.wake
    reopens its missing pane); then its nudge when reports wait (a bare reopen types nothing)."""
    from . import deliver, hr
    if c.kind == "main":
        if c.info.get("handle"):
            if deliver.reopen_pane(reg, c.target, c.info["handle"]) is None:
                raise orca.OrcaError(f"{c.target} not listed connected after the reopen", code="not_reopened")
        else:
            hr.project_pane(reg["projects"][c.project], dry_run=False, switch=False)
        util.write_json(inbox.box(c.target) / MAIN_REOPENED, {"at": units.now().isoformat()})
    else:
        hr.wake(reg, roster.Roster(reg).member(c.target))
    if c.count:
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


def _tell_owner(project, text, now) -> None:
    """An owner notification: a project's escalation is a `stuck` notice (flow.owner.announce: recorded
    first, the per-project gap, a held one sent by the next pass, no sound in quiet hours); the global
    waker notice goes through notify.send. Text and title travel as osascript arguments. One events
    line when shown."""
    if project:
        shown = owner.announce(project, "stuck", text, now)
    else:
        cfg_all = config.load()
        shown = notify.enabled(cfg_all) and notify.send("ORCHE", text, sound=not in_quiet_hours(cfg_all, now),
                                                        kind="reconcile")
    if shown:
        log.log("events", f"reconcile owner-notified project={project or '-'}")


def waker_loaded() -> bool:
    """The pre-0.6.0 Main waker LaunchAgent is loaded (the doctor's check)."""
    from .install import doctor
    return bool(doctor.main_waker_rows())


def apply(reg, action, now) -> None:
    from . import deliver, runs
    c, kind = action.cond, action.kind
    if kind == "nudge":
        _nudge(c.target)
    elif kind == "deliver":
        deliver.spawn(c.target, retry=True)
    elif kind == "drain":
        deliver.drain_now(c.to, c.target, int(c.rev or 1))
    elif kind in ("restart", "rerun"):
        runs.start(c.target)
    elif kind == "reopen":
        _reopen(reg, c)
    elif kind == "remind":
        _remind(c.target, str(c.info.get("reason") or ""))
    elif kind == "notify":
        _tell_owner("", f"{WAKER} is still loaded: it predates 0.6.0 and doubles Main's wakes — run "
                        f"launchctl bootout gui/{os.getuid()}/{WAKER}", now)
    else:
        _escalate(reg, action, now)


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
    """The lock file's handle while this process holds the pass lock, else None (another pass)."""
    folder().mkdir(parents=True, exist_ok=True)
    with open(folder() / ".lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield fh
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---- watchdog (0.7.2) ----------------------------------------------------------------------------

class Abort(BaseException):
    """SIGALRM after WATCHDOG_S, raised in the main thread wherever the pass is: a BaseException, so
    no `except Exception` around a repair swallows it. `frame` is where it rang."""

    def __init__(self, frame):
        super().__init__("reconcile pass ran too long")
        self.frame = frame


def _phase(name) -> None:
    global PHASE
    PHASE = name


def abort_path() -> Path:
    return folder() / "last-abort.json"


def read_abort():
    try:
        data = util.read_json(abort_path(), default=None)
    except (OSError, util.JsonError):
        return None
    return data if isinstance(data, dict) else None


def _abs_path(value) -> str:
    if isinstance(value, os.PathLike):
        try:
            value = os.fspath(value)
        except TypeError:
            return ""
    return value if isinstance(value, str) and value.startswith("/") and "\n" not in value else ""


ROOTS = ()   # the registered projects' paths, set by each pass for stuck_path


def stuck_path(frame) -> str:
    """The innermost project path on the stack from `frame` outwards, stopping at run_pass: the first
    absolute path a local holds (a str or os.PathLike, in the frame's own order: arguments first —
    `os.scandir`'s caller holds the directory it lists) that is under a registered project's root
    (ROOTS), a protected folder or a volume under /Volumes (privacy.protected), else the next frame
    out; "" when none. Values, not the formatted traceback (its file names are ORCHE's own); ORCHE's
    own paths (bin/orche.py's __file__) and other strings never count."""
    from . import privacy
    stop = run_pass.__code__
    while frame is not None and frame.f_code is not stop:
        try:
            values = list(frame.f_locals.values())
        except Exception:  # noqa: BLE001 - a frame that cannot be read is skipped
            values = []
        found = next((p for p in map(_abs_path, values) if p and (privacy.within(p, ROOTS) or privacy.protected(p))),
                     "")
        if found:
            return found
        frame = frame.f_back
    return ""


def record_abort(frame) -> dict:
    """reconcile/last-abort.json {at, phase, path, hint[, interpreter]} and one events line (phase and
    hint); never raises. hint: `privacy` when the path is under a macOS-protected folder
    (privacy.protected), else `slow`; the job's own aborts name its interpreter (privacy.python_app)."""
    from . import privacy
    path = stuck_path(frame)
    rec = {"at": _iso(units.now()), "phase": PHASE, "path": path,
           "hint": "privacy" if privacy.protected(path) else "slow"}
    if is_job():
        rec["interpreter"] = os.path.realpath(sys.executable)
    with contextlib.suppress(Exception):
        util.write_json(abort_path(), rec)
    with contextlib.suppress(Exception):
        log.log("events", f"reconcile abort phase={rec['phase']} hint={rec['hint']}")
    return rec


def abort_line(rec, seconds) -> str:
    where = f" at {rec['path']}" if rec.get("path") else ""
    tail = " (macOS privacy: run orche install --grant)" if rec.get("hint") == "privacy" else ""
    return f"orche: reconcile: aborted after {seconds:g} s in phase {rec['phase']}{where}{tail}"


def _last_words(line) -> None:
    with contextlib.suppress(Exception):
        print(line, file=sys.stderr)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


@contextlib.contextmanager
def watchdog(seconds, fh):
    """The pass watchdog (main thread only; `seconds` 0 or None: off). SIGALRM at `seconds` raises
    Abort in the main thread; WATCHDOG_GRACE_S later a daemon thread ends a pass the signal could not
    interrupt (a call blocked inside the kernel): it records the abort from the main thread's stack,
    releases the lock on `fh` and exits ABORT_EXIT. Leaving the block disarms both."""
    if not seconds or threading.current_thread() is not threading.main_thread():
        yield
        return
    from . import privacy  # noqa: F401  (imported before arming: the last-resort thread never imports)
    from .install import launchd  # noqa: F401  (is_job's, for the same reason)
    done, gate, main = threading.Event(), threading.Lock(), threading.main_thread().ident

    def ring(_signum, frame):
        raise Abort(frame)

    def last_resort():
        if done.wait(seconds + WATCHDOG_GRACE_S):
            return
        with gate:
            if done.is_set():
                return
            rec = record_abort(sys._current_frames().get(main))
            with contextlib.suppress(OSError, ValueError):
                fcntl.flock(fh, fcntl.LOCK_UN)
            # the line for reconcile.log (the job's stdout/stderr) and every buffered line flushed, as
            # _exit skips that; at most 1 s: a main thread stuck inside a write holds the stream's lock
            say = threading.Thread(target=_last_words, args=(abort_line(rec, seconds),), daemon=True)
            say.start()
            say.join(1.0)
            _exit(ABORT_EXIT)

    old = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    threading.Thread(target=last_resort, name="reconcile-watchdog", daemon=True).start()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        with gate:
            done.set()


JOB_ENV = "ORCHE_RECONCILE_JOB"   # the job's plist sets it to 1


def is_job() -> bool:
    """This process is the LaunchAgent's pass: its plist sets ORCHE_RECONCILE_JOB=1, and launchd sets
    XPC_SERVICE_NAME to the job's label; either is enough."""
    from .install import launchd
    return os.environ.get(JOB_ENV) == "1" or os.environ.get("XPC_SERVICE_NAME") == launchd.RECONCILE


def _denied(dot):
    """The first folder of `dot` (a project's `.orche`) or of its subfolders (`units`, …) this process
    cannot list, else None. Missing folders are not denied."""
    try:
        names = os.listdir(dot)
    except FileNotFoundError:
        return None
    except OSError:
        return str(dot)
    for name in sorted(names):
        sub = dot / name
        try:
            if not sub.is_dir():
                continue
            os.listdir(sub)
        except FileNotFoundError:
            continue
        except OSError:
            return str(sub)
    return None


def unreadable(reg, keys=None) -> dict:
    """{key: path} of the registered projects (`keys`: only those) whose `.orche`, or a folder in it
    (`units`, …), this process cannot list (0.7.2, 0.7.3): a macOS privacy denial or a chmod is an
    OSError here, while a glob there returns [] silently (3.13+) or raises mid-pass (3.11, 3.12).
    A missing `.orche` is not unreadable."""
    out = {}
    for key in sorted(reg.get("projects") or {}):
        proj = reg["projects"][key]
        path = proj.get("path") if isinstance(proj, dict) else None
        if not path or (keys is not None and key not in keys):
            continue
        where = _denied(Path(path) / ".orche")
        if where is not None:
            out[key] = where
    return out


def _owner_of(reg, filename):
    """The registered project whose root holds `filename` (the longest match), else None."""
    if not filename:
        return None
    name, best = os.path.abspath(os.fsdecode(filename)), None
    for key, proj in (reg.get("projects") or {}).items():
        path = proj.get("path") if isinstance(proj, dict) else None
        if not path:
            continue
        root = os.path.abspath(str(path))
        if (name == root or name.startswith(root.rstrip(os.sep) + os.sep)) and (
                best is None or len(root) > len(best[1])):
            best = (key, root)
    return best[0] if best else None


def _denial(reg, exc, blocked):
    """The project `exc` (an OSError other than FileNotFoundError, under a registered root not yet in
    `blocked`) makes unreadable, else None."""
    if not isinstance(exc, OSError) or isinstance(exc, FileNotFoundError):
        return None
    key = _owner_of(reg, exc.filename)
    return key if key is not None and key not in blocked else None


def _block(blocked, key, path, dry_run=False) -> None:
    blocked[key] = str(path)
    if not dry_run:
        log.log("events", f"reconcile unreadable project={key} path={path}")


def read_heartbeat():
    try:
        data = util.read_json(heartbeat_path(), default=None)
    except util.JsonError:
        return None
    return data if isinstance(data, dict) else None


def write_heartbeat(now, cfg, took_s, actions, enabled=True, unreadable=None, readable=()) -> None:
    """heartbeat.json. Only a pass of the LaunchAgent itself that read the projects (enabled) stamps
    `job_at` and sets `unreadable` (the projects it could not list; absent when none); every other
    pass carries both over (it runs with another process's access), adds the projects it could not
    list itself, and drops those in `readable` (read by it, not under a protected folder)."""
    old = read_heartbeat() or {}
    day = now.date().isoformat()
    today = (int(old.get("repairs_today") or 0) if old.get("day") == day else 0) + actions
    doc = {"at": now.isoformat(timespec="seconds"), "every_s": int(cfg.get("every_s", 300)),
           "version": __version__, "took_s": round(took_s, 2), "actions": actions,
           "enabled": bool(enabled), "day": day, "repairs_today": today}
    job = is_job()
    own = job and enabled
    job_at = now.isoformat(timespec="seconds") if own else old.get("job_at")
    if job_at:   # the LaunchAgent's last completed pass, carried over by the hook-kicked ones
        doc["job_at"] = job_at
    exe = os.path.realpath(sys.executable) if job else old.get("interpreter")
    if exe:      # the job's interpreter: what Full Disk Access must list (privacy.python_app)
        doc["interpreter"] = exe
    if own:
        blocked = sorted(unreadable or [])
    elif enabled:
        carried = [k for k in (old.get("unreadable") or []) if k not in set(readable)]
        blocked = sorted(set(carried) | set(unreadable or []))
    else:
        blocked = old.get("unreadable")
    if blocked:
        doc["unreadable"] = blocked
    util.write_json(heartbeat_path(), doc)


def run_pass(dry_run=False, out=print, keys=None, as_json=False, watchdog_s=None) -> int:
    """One pass over the projects `keys` (default all). 0; 1 when the registry cannot be read; 2 for an
    unknown project; 3 when the watchdog aborted it (`watchdog_s`, default WATCHDOG_S; 0: off).
    `out` receives the human lines, or one JSON document with `as_json`."""
    started = time.monotonic()
    _phase("start")
    cfg_all = config.load()
    cfg = settings(cfg_all)
    now = units.now()
    if dry_run:
        return _pass(cfg_all, cfg, now, started, True, out, keys, as_json)
    seconds = WATCHDOG_S if watchdog_s is None else watchdog_s
    with _pass_lock() as fh:
        if fh is None:
            out(json.dumps({"ran": False, "reason": "another pass is running"}) if as_json
                else "reconcile: another pass is running")
            return 0
        try:
            with watchdog(seconds, fh):
                return _pass(cfg_all, cfg, now, started, False, out, keys, as_json)
        except Abort as exc:
            rec = record_abort(exc.frame)
            if as_json:
                out(json.dumps({"ran": False, "aborted": rec}, ensure_ascii=False))
            else:
                print(abort_line(rec, seconds), file=sys.stderr)
            return ABORT_EXIT


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
    global ROOTS
    ROOTS = tuple(str(p["path"]) for p in reg.get("projects", {}).values() if isinstance(p, dict) and p.get("path"))
    # 0.7.2/0.7.3: a project whose `.orche` (or a folder in it) this process cannot list is
    # `unreadable` (macOS privacy, a chmod): skipped for the rest of the pass, never an error
    _phase("privacy")
    every = sorted(reg.get("projects", {})) if keys is None else list(keys)
    blocked = {}
    for key, where in unreadable(reg, keys).items():
        _block(blocked, key, where, dry_run)

    def scope():
        return keys if not blocked else [k for k in every if k not in blocked]

    def denied(exc) -> bool:
        key = _denial(reg, exc, blocked)
        if key is not None:
            _block(blocked, key, exc.filename, dry_run)
        return key is not None
    from . import runs
    _phase("runs")
    for key in every:
        if key in blocked:
            continue
        try:
            runs.reconcile_cli(reg, _Args(dry_run), [key])   # nothing under --dry-run
        except Exception as exc:  # noqa: BLE001 - lost runs never stop the pass
            if not denied(exc):
                log.log("hooks", f"reconcile: runs: {type(exc).__name__}: {exc}")
    # C10 T8: a lineage close a crash or a busy lock missed (never raises; dry-run takes no lock)
    _phase("lineage")
    shut = [{"action": "lineage-close", "class": "lineage", "project": p, "target": lin, "rev": None,
             "attempt": 1, "cap": 1, "line": f"{'would close' if dry_run else 'closed'} lineage {lin} ({p})",
             "failed": None} for p, lin in lineage.repair(reg, dry_run, now=now, keys=scope())]
    # C10 T10: answered asks, missed secret resumes, the fallback, held notices, re-notifications
    _phase("owner")
    notes = list(owner.heartbeat(reg, now, dry_run, scope()))
    _phase("queue")        # 0.9.0: the merge queue's integrator, started when an entry waits
    notes += _queue_starts(reg, scope(), dry_run)
    _phase("usage")        # 0.9.0: a usage hold's owner notice, once; its end, once
    notes += _usage_notes(now, dry_run)
    _phase("observe")
    try:
        state = util.read_json(state_path(), default={})
    except util.JsonError:
        state = {}
    state = state if isinstance(state, dict) else {}
    others = {k: v for k, v in state.items()
              if keys is not None and isinstance(v, dict) and v.get("project") and v.get("project") not in keys}
    mine = {k: v for k, v in state.items() if k not in others}
    wake = bool((cfg_all.get("inbox") or {}).get("wake", True))
    terms = panes(mark_ok=not dry_run)
    while True:   # a denial mid-scan: that project is unreadable, the others are observed again
        try:
            conds = observe(reg, now, scope(), wake=wake, terms=terms, cfg_all=cfg_all,
                            hush=in_quiet_hours(cfg_all, now))
            break
        except OSError as exc:
            if not denied(exc):
                raise
    held = {c.project for c in conds if guard.paused(c.project) is not None}
    conds = [c for c in conds if c.project not in held or c.cls == "drain"]   # G8: the owner paused it
    if not dry_run and waker_loaded():
        conds.append(Condition("waker", "waker", "", WAKER, 0.0))
    actions, new = plan(conds, mine, now, resolved(reg, mine))
    new.update(others)
    if dry_run:
        later = _reviews(reg, scope(), True, started)
        for row in shut + notes:
            doc["actions"].append(row)
            if not as_json:
                out(row["line"])
        for a in actions:
            doc["actions"].append(_row(a, ""))
            if not as_json:
                out(f"would {a.describe()}")
        for row in later:
            doc["actions"].append(row)
            if not as_json:
                out(row["line"])
        if not actions and not shut and not notes and not later and not as_json:
            out("reconcile: nothing to repair")
        return done_json()
    done = 0
    _phase("apply")
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
        if a.cond.project in blocked:     # denied by an earlier repair of this pass
            continue
        failed = ""
        _record(new, a, now)              # spent before it runs: an abort mid-apply still counts (2/day cap)
        util.write_json(state_path(), new)
        try:
            apply(reg, a, now)
        except Exception as exc:  # noqa: BLE001 - one failed repair never stops the pass; it still counts
            failed = type(exc).__name__
            if not denied(exc):
                log.log("hooks", f"reconcile: {a.kind} {a.cond.target}: {failed}: {exc}")
        log.log("events", a.event() + (f" failed={failed}" if failed else ""))
        doc["actions"].append(_row(a, failed))
        if not as_json:
            out(a.describe() + (f" — failed ({failed})" if failed else ""))
        done += 1
    _phase("review")       # 0.9.0: a pending Build review, at most one a pass, after the repairs
    for row in _reviews(reg, scope(), False, started):
        log.log("events", f"reconcile action={row['action']} project={row['project']} target={row['target']} "
                          f"rev=- attempt=1/1" + (f" failed={row['failed']}" if row["failed"] else ""))
        doc["actions"].append(row)
        if not as_json:
            out(row["line"] + (f" — failed ({row['failed']})" if row["failed"] else ""))
        done += 1
    took = time.monotonic() - started
    if done:
        log.log("events", f"reconcile pass took={took:.1f}s actions={done}")
    _phase("heartbeat")
    from . import privacy
    shielded = {k for k, _ in privacy.protected_projects(reg)}
    write_heartbeat(now, cfg, took, done, unreadable=sorted(blocked),
                    readable=[k for k in every if k not in blocked and k not in shielded])
    return done_json()


MAIN_REOPENED = ".main-reopened"   # in inbox/<main-p>/: {at} of the heartbeat's last reopen of Main's pane


def main_reopened(reg, key):
    """When the heartbeat last reopened the project's Main pane (an aware datetime), or None."""
    main = (reg["projects"].get(key) or {}).get("main_session")
    try:
        rec = util.read_json(inbox.box(main) / MAIN_REOPENED, default=None) if main else None
    except (OSError, ValueError, util.JsonError):
        return None
    return units.parse_iso(rec.get("at")) if isinstance(rec, dict) else None


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
    from . import deliver
    out = []
    for key in sorted(state if isinstance(state, dict) else {}):
        e = state[key]
        if not isinstance(e, dict) or e.get("project") != project or e.get("cls") == "held":
            continue          # a held unit's re-run in flight is not a stall
        attempts = e.get("attempts") or []
        if attempts or e.get("escalated"):
            cls = e.get("cls")
            cap = (REOPEN_CAP if cls == "closed" else timing(cls)[1]) if cls else None
            last = deliver.last_delivery(e.get("to"), e.get("target")) if cls == "undelivered" and e.get("to") else None
            out.append({"cls": cls, "target": e.get("target"), "rev": e.get("rev"), "to": e.get("to") or None,
                        "reopened": (last or {}).get("reopened"),
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
    how = "pane reopened" if s.get("reopened") else "retrying"    # the deliverer found the pane dead
    return (f"  stalled: {s['cls']} {s['target']}{rev} — ORCHE {how} ({s['tries']}/{s['cap']}"
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
        from . import deliver
        last = deliver.last_delivery(e.get("to"), target) if kind == "undelivered" and e.get("to") else None
        how = "pane reopened" if (last or {}).get("reopened") else "retrying"
        return f"ORCHE {how} ({tries}/{CAP}" + (f", next {_hhmm(nxt)})" if nxt is not None else ")")
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
    if job_blocked(now or units.now(), hb, None, every):   # without `job`: the privacy findings only
        return "heartbeat: blocked by macOS privacy — run orche install --grant"
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


def privacy_abort(now, job_at):
    """last-abort.json when it says `privacy`, is at most ABORT_TTL old and no job pass (`job_at`, an
    aware datetime or None) came after it; else None. One rule for the doctor and the install."""
    ab = read_abort() or {}
    ab_at = units.parse_iso(ab.get("at"))
    if (ab.get("hint") == "privacy" and ab_at is not None and now - ab_at <= ABORT_TTL
            and (job_at is None or job_at < ab_at)):
        return ab
    return None


def job_blocked(now, hb, job, every) -> str:
    """The heartbeat row's finding about the LaunchAgent, "" when none (0.7.2): a `privacy` abort at
    most ABORT_TTL old with no job pass after it; or, with `job` (doctor.job_state), a loaded job with
    no pass of its own within STALE_INTERVALS × every_s of its load (the fix names `--grant` only
    when a registered project is under a protected folder)."""
    job, job_at = job or {}, units.parse_iso((hb or {}).get("job_at"))
    keys = (hb or {}).get("unreadable")
    if keys and job_at is not None and now - job_at <= ABORT_TTL:
        return f"heartbeat job cannot read {', '.join(map(str, keys))} (macOS privacy) — run: orche install --grant"
    ab = privacy_abort(now, job_at)
    if ab is not None:
        return (f"heartbeat job cannot read {ab.get('path') or 'a project folder'} (macOS privacy) — "
                "run: orche install --grant")
    load_at = job.get("load_at")
    if (job.get("loaded") and load_at is not None and (now - load_at).total_seconds() > STALE_INTERVALS * every
            and (job_at is None or job_at < load_at)):
        fix = ("run: orche install --grant" if job.get("protected")
               else f"run orche reconcile and read {paths.orche_home() / 'logs' / 'reconcile.log'}")
        return f"heartbeat job loaded, no pass yet — {fix}"
    return ""


def doctor_row(now=None, cfg=None, job=None):
    """(level, "heartbeat", detail): WARN when the job is blocked (job_blocked), stale or never ran,
    OK otherwise (off included)."""
    cfg = settings(cfg)
    every = int(cfg.get("every_s", 300))
    if not cfg.get("enabled", True):
        return ("OK", "heartbeat", "off (reconcile.enabled false)")
    hb, age = heartbeat_age(now)
    blocked = job_blocked(now or units.now(), hb, job, every)
    if blocked:
        return ("WARN", "heartbeat", blocked)
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
