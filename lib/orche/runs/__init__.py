"""The Run module: one unit's run from its spawn to its last file (SPEC §14.13).

Interface: start, stop, restart, execute, preview, count/live, reconcile/reconcile_cli/describe/
pending_sweeps, logs_dir/prune_logs, records(roots), RunError.

Run locks under ORCHE_HOME/runs/<proj>/, one set per live `orche run`.

- `<unit>.lock` holds `{"pid", "unit", "started", "started_ns", "run_id"}`, what runs (`to`, `rev`,
  `tier`, `model`, `effort`, `worktree`, `branch`: filing.LOCKED, job.lock_info()) and, once claude
  is up, `child_pid` and `pgid` (its own process group); it is live while its pid (the `orche run`
  process) is. `run_id` (`<unit>@<started_ns>`) is the worker's ORCHE_RUN_ID (procs.py).
- `<to>.target.lock` (same record): one live run per target session, because a target's runs
  share one worktree. A second unit for the same target waits like a run over the project cap.

Light on purpose: this file imports only headless, log, paths, procs, registry, shell and util,
and loads `filing` and `job` only inside the functions that need them (the status line counts
runs on every render; the prompt hook reconciles on every prompt).

start() is the Stop hook's `orche run <unit>`, detached (procs.spawn_detached), dated by SINCE_ENV;
restart() is AMEND: stop(), then start() — the order is the contract.

execute() is `orche run <unit>` (an Outcome: exit code, RUN record, stderr lines) and preview() its
dry run; both load job.py only when called. RunError is resolve's refusal (exit 1 or 2).

reconcile() settles a lock whose `orche run` is gone (SIGKILL, OOM, power loss, a failed filing) or
whose pid now belongs to another program: a minimal RUN.json (`stop_reason: lost`) and `failed`
"runner died: …" to Main, unless a pointer of the run (the runner's or its worker's) is there
(`runner-stale-lock`); then it kills what the run left running (procs.sweep) and drops its locks.
A filed lock never holds a slot or its target: when the sweep has no time or the process table
cannot be read, the lock becomes `<run_id>.sweep` (a pending sweep, no slot) which later reconciles
retry; after SWEEP_TRIES unreadable tables in a row it is settled by `kill -0` of its claude (a read
the budget cut short, procs.NoTime, is "no time" and never counts).
It runs at `orche run` start (and when a dead lock turns up while waiting), in the prompt hook of
any session of the project (cheap: `kill -0`, `ps` only within the budget), and in
`orche status|depts|doctor|metrics|janitor run`.

STOP/AMEND (stop()): a `<unit>.stop` marker, which a running run polls every headless.POLL_S,
then, as the fast path, SIGTERM to the `orche run` process of a live lock once `ps` (at most
PS_TIMEOUT_S) shows that the pid still is that run: a recycled pid drops the lock as stale; a `ps`
that fails or times out counts the lock as live and sends nothing (`runner-pid-check-failed`: never
a duplicate run, never a signal to a pid nobody verified; the poll still stops the run). The runner
kills claude's process group and files `info` "stopped by Main". A run honours
only a marker written after it started (`stop_requested(..., since)`), so an old marker never stops
a fresh run, and a run still waiting for a slot gives up when a marker appears ("stopped by Main
before start"). A unit whose live lock carries a marker is waited for, not refused, so the fresh
run of an AMEND starts once the stopped one has released its slot.
"""
import datetime as _dt
import fcntl
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from .. import headless, log, paths, procs, registry, shell, util

WAIT_S = 1800.0    # default for config runner.queue_wait_s: slot and target waits, then "runner busy"
POLL_S = 2.0
PS_TIMEOUT_S = 1.0  # the `ps` pid check runs inside the Stop hook (3 s budget)
RECONCILE_BUDGET_S = 10.0   # per project, outside hooks: `ps` checks + sweeps of lost runs
TARGET_SUFFIX = ".target.lock"
SWEEP_SUFFIX = ".sweep"     # a filed lost run whose leftovers are not swept yet (holds no slot)
SWEEP_TRIES = 3     # unreadable process tables in a row before a pending sweep is settled by kill -0
LOST = object()     # _try_acquire: a dead run's lock is left; reconcile() first
SINCE_ENV = "ORCHE_RUN_SINCE"   # start() dates the request; job.run_since() reads it


class Busy(Exception):
    """The project's run slots (or the target's one) stayed taken for the whole wait."""


class Duplicate(Exception):
    """A live run of the same unit holds its lock."""


class Cancelled(Exception):
    """The wait for a slot was given up (`cancel()` returned a reason, e.g. a STOP marker)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class RunError(Exception):
    """`orche run` cannot run the unit: exit `code` (1: a bad unit id; 2: an unknown unit, no brief,
    an unknown or retired target, a missing worktree). `job` is set once the target is known: the
    run still files a `failed` pointer."""

    def __init__(self, code, message, job=None):
        super().__init__(message)
        self.code = code
        self.job = job


def execute(reg, unit, cfg):
    """`orche run <unit>` in this process: job.execute's Outcome(code, record, stderr); files, never
    prints."""
    from . import job
    return job.execute(reg, unit, cfg)


def records(roots) -> list:
    """filing.records: the RUN.json records under `roots` (what `orche metrics` sums)."""
    from . import filing
    return filing.records(roots)


def preview(reg, unit, cfg):
    """`orche --dry-run run <unit>`: job.preview's Preview(job, call, argv); writes nothing. Raises
    RunError, or what call_for / headless.argv raise."""
    from . import job
    return job.preview(reg, unit, cfg)


def run_dir(proj: str) -> Path:
    return paths.orche_home() / "runs" / proj


def target_lock(proj: str, to: str) -> Path:
    return run_dir(proj) / f"{to}{TARGET_SUFFIX}"


def alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _read(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unit_locks(folder: Path) -> list:
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".lock") and not n.endswith(TARGET_SUFFIX))
    except OSError:
        return []
    return [folder / n for n in names]


def live(proj: str) -> list:
    """Lock records of live runs for `proj` (stale locks are skipped, not removed)."""
    out = []
    for path in _unit_locks(run_dir(proj)):
        data = _read(path)
        if data is not None and alive(data.get("pid")):
            out.append(data)
    return out


def _clock(iso) -> str:
    """HH:MM (local) of an ISO timestamp, else '?'."""
    try:
        return _dt.datetime.fromisoformat(str(iso)).astimezone().strftime("%H:%M")
    except ValueError:
        return "?"


def count(proj: str) -> int:
    return len(live(proj))


def _owner_lock(folder: Path, data: dict):
    """The unit lock a target-lock record belongs to (same unit and pid), else None."""
    unit = data.get("unit")
    owner = _read(folder / f"{unit}.lock") if unit else None
    return owner if owner is not None and str(owner.get("pid")) == str(data.get("pid")) else None


def _try_acquire(folder: Path, unit: str, info: dict, limit: int):
    """Under the project mutex: take `<unit>.lock` (and `<to>.target.lock`) if a project slot and
    the target are free. (lock path, None), (None, why it waits), or (None, LOST) when an unfiled
    lock of a dead `orche run` is left: reconcile() files it first (never dropped unfiled). An
    unreadable lock and a dead target lock without its unit lock are dropped here; a dead lock
    that is already filed (`reconciled`) holds nothing: it becomes its pending sweep (_settle)."""
    with open(folder / ".mutex", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        busy = 0
        for path in folder.glob("*.lock"):
            target = path.name.endswith(TARGET_SUFFIX)
            data = _read(path)
            if data is None or (not target and not data.get("unit") and not alive(data.get("pid"))):
                path.unlink(missing_ok=True)       # nothing reconcile could file
                if not target:
                    _log_stale(path.stem, data or {})
                continue
            if not alive(data.get("pid")):
                owner = _owner_lock(folder, data) if target else data
                if owner is None:                  # a dead target lock without its unit lock
                    path.unlink(missing_ok=True)
                    continue
                if owner.get("reconciled"):        # filed: holds no slot and no target
                    if target:
                        path.unlink(missing_ok=True)
                    else:
                        _settle(folder, path, data)
                    continue
                return None, LOST
            if target:
                continue
            if path.name == f"{unit}.lock":
                if stop_requested(folder.name, unit, data.get("started_ns") or 0):
                    return None, f"{unit} is being stopped"   # AMEND: wait for it to release the slot
                raise Duplicate(f"{unit} is already running (pid {data.get('pid')}, "
                                f"started {_clock(data.get('started'))})")
            busy += 1
        if busy >= limit:
            return None, f"{busy} live runs"
        to = info.get("to")
        held = _read(target_lock(folder.name, to)) if to else None
        if held is not None:     # live: a dead one returned LOST above
            return None, f"{to} is running {held.get('unit')}"
        started_ns = time.time_ns()
        record = {"pid": os.getpid(), "unit": unit, **info, "started": util.now_iso(),
                  "started_ns": started_ns, "run_id": procs.run_id(unit, started_ns)}
        mine = folder / f"{unit}.lock"
        util.write_json(mine, record)
        if to:
            util.write_json(target_lock(folder.name, to), record)
        return mine, None


def acquire(proj: str, unit: str, info: dict, limit: int, wait_s=None, poll_s=None, cancel=None,
            reg=None) -> Path:
    """Take a run slot for `unit` (and its target, `info["to"]`); waits while the project has
    `limit` live runs or the target runs another unit. Raises Busy / Duplicate, or Cancelled when
    `cancel()` returns a reason (checked on every poll).

    The project's lost runs are reconciled first (reconcile(): a dead or recycled `orche run`
    files `failed` and frees its slot), and again whenever a lock of a dead run turns up."""
    folder = run_dir(proj)
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (WAIT_S if wait_s is None else wait_s)
    limit = max(1, int(limit))
    reg = registry.load() if reg is None else reg
    reconcile(reg, proj, budget_s=RECONCILE_BUDGET_S)
    tidied = True
    while True:
        mine, why = _try_acquire(folder, unit, info, limit)
        if mine is not None:
            return mine
        if why is LOST:
            if not tidied:
                reconcile(reg, proj, budget_s=RECONCILE_BUDGET_S)
                tidied = True
                continue
            why = "a lost run's lock could not be cleared yet"
        tidied = False
        reason = cancel() if cancel is not None else None
        if reason:
            raise Cancelled(reason)
        if time.monotonic() >= deadline:
            raise Busy(f"runner busy: {why}")
        time.sleep(POLL_S if poll_s is None else poll_s)


# The run's own empty `--mcp-config` (headless.marker_path): written when claude starts, removed
# when the run ends (headless.run), when reconcile drops the run's lock or pending sweep, and by
# prune_logs once old. Its path in claude's command line is what procs._is_claude proves a lost
# run's claude by (headless.marker).
def _drop_marker(run_id) -> None:
    if run_id:
        try:
            headless.marker_path(run_id).unlink(missing_ok=True)
        except OSError:
            pass


def run_id_of(path):
    """The lock's `run_id` (the worker's ORCHE_RUN_ID), else None."""
    return (_read(Path(path)) or {}).get("run_id")


def update(path, **fields) -> None:
    """Merge `fields` (e.g. child_pid, pgid) into a lock this process holds."""
    data = _read(Path(path)) or {}
    data.update(fields)
    util.write_json(Path(path), data)


def release(path, pid=None) -> bool:
    """Remove a unit lock and the target lock it took, each only while it still belongs to `pid`
    (default: this process), under the project mutex: a lock another run has taken since stays.
    True when the unit lock was removed here."""
    path = Path(path)
    pid = os.getpid() if pid is None else pid
    with open(path.parent / ".mutex", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = _read(path)
        if data is None or str(data.get("pid")) != str(pid):
            return False
        path.unlink(missing_ok=True)
        to = data.get("to")
        if to:
            held = target_lock(path.parent.name, to)
            mine = _read(held)
            if mine is not None and mine.get("unit") == data.get("unit") and str(mine.get("pid")) == str(pid):
                held.unlink(missing_ok=True)
        return True


def _log_stale(unit, data) -> None:
    log.log("events", f"run unit={unit} to={data.get('to')} runner-stale-lock pid={data.get('pid')}"
                      + (f" child_pid={data['child_pid']}" if data.get("child_pid") else ""))


# ---- STOP / AMEND of a running unit ------------------------------------------

def _marker(proj: str, unit: str) -> Path:
    return run_dir(proj) / f"{unit}.stop"


def marker_time(proj: str, unit: str):
    """mtime_ns of the unit's STOP marker, else None."""
    try:
        return _marker(proj, unit).stat().st_mtime_ns
    except OSError:
        return None


def stop_requested(proj: str, unit: str, since=None) -> bool:
    """A STOP marker exists (written at or after `since`, a time.time_ns() value, when given)."""
    at = marker_time(proj, unit)
    return at is not None and (since is None or at >= since)


def clear_stop(proj: str, unit: str, at=None) -> None:
    """Remove the marker (only the one written at `at` when given: a newer STOP stays)."""
    if at is None or marker_time(proj, unit) == at:
        _marker(proj, unit).unlink(missing_ok=True)


def running(proj: str, unit: str):
    """The live lock record of `unit`, else None."""
    data = _read(run_dir(proj) / f"{unit}.lock")
    return data if data is not None and alive(data.get("pid")) else None


def _run_command(unit):
    """`ps` command line of `orche … run <unit>`: the unit id as a whole token (`…-1` never matches
    `…-12`), after the `orche` launcher; the global flags `--json` / `--dry-run` and `--` may stand
    between `run` and the unit (`orche run --json <unit>` is a live run too)."""
    return re.compile(r"orche.*\srun(?:\s+(?:--json|--dry-run|--))*\s+" + re.escape(unit) + r"(?:\s|$)")


def is_run_process(pid, unit, timeout=PS_TIMEOUT_S):
    """Whether `pid` is `orche run <unit>` per `ps`: True; False when `ps` shows another program (a
    recycled pid) or no process at all; None when `ps` could not tell (failed, timed out, no time
    left in `timeout`)."""
    pid = int(pid)
    if timeout is None or timeout - procs.MARGIN_S <= 0:
        return None
    # -ww: unlimited width (BSD and procps), so a long command line never hides the unit id.
    # Never a `ps` that cannot finish procs.MARGIN_S before what is left (the hook's budget).
    res = shell.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                    timeout=min(PS_TIMEOUT_S, timeout - procs.MARGIN_S))
    text = (res.out or "").strip()
    if res.ok and text:
        return _run_command(unit).search(text) is not None
    if res.code == 1 and not text and not alive(pid):    # ps ran: no such process (any more)
        return False
    return None


# ---- run logs ----------------------------------------------------------------

def logs_dir() -> Path:
    """ORCHE_HOME/logs/runs: one `<unit>.log` per spawned run (start())."""
    return paths.orche_home() / "logs" / "runs"


def prune_logs(keep_days) -> int:
    """Delete `logs/runs/*.log` not modified for `keep_days` days (config runner.log_keep_days;
    below 1: keep everything); RUN.json records are never touched. One events line when anything
    went; the count. Per-run `runner/mcp/*.json` files that old (a run nobody reconciled) go too,
    uncounted."""
    try:
        days = float(keep_days)
    except (TypeError, ValueError):
        log.log("hooks", f"runner: log_keep_days {keep_days!r} is not a number; run logs not pruned")
        return 0
    if days < 1:
        return 0
    cutoff = time.time() - days * 86400
    gone = 0
    for path in logs_dir().glob("*.log"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                gone += 1
        except OSError:
            continue
    for path in headless.marker_dir().glob("*.json"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
    if gone:
        log.log("events", f"run-logs pruned={gone} keep_days={keep_days}")
    return gone


# ---- reconcile: runs whose `orche run` died without filing -------------------

def _left(deadline) -> float:
    return max(0.0, deadline - time.monotonic())


class _Mutex:
    """The project's `.mutex` (flock), held for the `with` block."""

    def __init__(self, folder: Path):
        self.path = folder / ".mutex"

    def __enter__(self):
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.fh.close()


FILED = ("reconciled", "stale-lock")    # filing.lost() outcomes after which Main has the run's pointer


def _sweep_path(folder: Path, data: dict) -> Path:
    name = data.get("run_id") or f"{data.get('unit')}@{data.get('pid')}"
    return folder / f"{name}{SWEEP_SUFFIX}"


def _drop_target(folder: Path, data: dict) -> None:
    to = data.get("to")
    if to:
        held = target_lock(folder.name, to)
        mine = _read(held)
        if mine is not None and mine.get("unit") == data.get("unit") and str(mine.get("pid")) == str(data.get("pid")):
            held.unlink(missing_ok=True)


def _settle(folder: Path, path: Path, data: dict, failures=0) -> bool:
    """Under the project mutex (the caller holds it): a filed lock of a dead run stops holding a
    slot and its target. With something left to kill (`run_id` / `child_pid`) the lock becomes its
    pending sweep `<run_id>.sweep` (a rename: no new space needed), which every later reconcile
    retries (_pending_sweep); else it is removed. False when the rename failed: the lock stays,
    marked, and _try_acquire skips it (it holds nothing either way)."""
    try:
        if data.get("run_id") or data.get("child_pid"):
            if failures:
                try:
                    util.write_json(path, {**data, "sweep_failures": failures})
                except OSError:
                    pass
            os.replace(path, _sweep_path(folder, data))
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.log("hooks", f"runner: {path.name}: cannot set the filed lock aside: {type(exc).__name__}: {exc}")
        return False
    _drop_target(folder, data)
    return True


def _sweep_lost(data: dict, deadline):
    """procs.sweep() of a lost run: its ORCHE_RUN_ID, plus its claude (the lock's child_pid) when
    that one's environment is hidden and procs._is_claude proves it by the run's own
    `--mcp-config` path. (pids signalled or None, whether the table was tried and failed: False
    when there was no time to try or the deadline cut the read short (procs.NoTime))."""
    if procs._budget(deadline) <= 0:
        return None, False
    child, rid = data.get("child_pid"), data.get("run_id")
    claude = ({"pid": child, "pgid": data.get("pgid"), "started_ns": data.get("started_ns"),
               "marker": headless.marker(rid) if rid else None} if child else None)
    try:
        hit = procs.sweep(rid, claude=claude, deadline=deadline)
    except procs.NoTime:
        return None, False
    except Exception as exc:  # noqa: BLE001 - an unreadable table is a failed sweep, never a raise
        log.log("hooks", f"runner: sweep {data.get('run_id')}: {type(exc).__name__}: {exc}")
        hit = None
    return hit, hit is None


def _act(proj, data, action):
    return {"project": proj, "unit": data.get("unit"), "to": data.get("to"), "pid": data.get("pid"),
            "started": data.get("started"), "action": action, "filed": False, "killed": [],
            "removed": False, "pending": False}


def _lost(reg, proj: str, path: Path, data: dict, deadline):
    """One lost run (its `orche run` is dead or its pid is another program now): file it, kill
    what it left running, free its slot. Returns the action record, or None when another process
    settled the lock first.

    Under the project mutex (so two reconcilers never both file): unless the lock says an earlier
    reconcile filed it, filing.lost() writes, from the lock, the minimal RUN.json and the `failed` pointer
    (or finds a pointer the run or its worker filed: `runner-stale-lock`), and the lock is marked
    `reconciled`. A filing that raises (ENOSPC, EACCES, …) leaves the lock as it was for a later
    reconcile. Then, while `deadline` allows, the sweep (_sweep_lost) and the locks go. A filed
    lock never waits for its sweep: without time for it, or when the process table cannot be
    read, it becomes its pending sweep (_settle) and the slot and the target are free at once."""
    unit, pid = data.get("unit"), data.get("pid")
    act = _act(proj, data, "cleared")
    folder = path.parent
    with _Mutex(folder):
        cur = _read(path)
        if cur is None or str(cur.get("pid")) != str(pid):
            return None
        if not cur.get("reconciled"):
            from . import filing
            try:
                act["action"] = filing.lost(reg, proj, cur)
            except OSError as exc:      # ENOSPC, EACCES, …: keep the lock, a later reconcile files it
                log.log("hooks", f"runner: reconcile {unit}: cannot file the lost run: {type(exc).__name__}: {exc}")
                act.update(action="unfiled", error=f"{type(exc).__name__}: {exc}")
                return act
            except Exception as exc:  # noqa: BLE001 - a bug never pins the lock forever
                log.log("hooks", f"runner: reconcile {unit}: {type(exc).__name__}: {exc}")
                act.update(action="error", error=f"{type(exc).__name__}: {exc}")
            if act["action"] in ("stale-lock", "unknown-unit"):
                _log_stale(unit, cur)
            cur["reconciled"] = act["action"]
            try:
                util.write_json(path, cur)
            except OSError:
                pass
        act["filed"] = cur["reconciled"] in FILED
        data = cur
    hit, failed = _sweep_lost(data, deadline) if data.get("run_id") or data.get("child_pid") else ([], False)
    with _Mutex(folder):
        cur = _read(path)
        if cur is None or str(cur.get("pid")) != str(pid):
            return act
        if hit is not None:
            act["killed"] = hit
            path.unlink(missing_ok=True)
            _drop_target(folder, cur)
            _drop_marker(cur.get("run_id"))
            act["removed"] = True
        else:
            act["removed"] = act["pending"] = _settle(folder, path, cur, failures=1 if failed else 0)
    return act


def _pending_sweep(proj: str, path: Path, deadline):
    """Retry a pending sweep (_settle). Swept -> the record goes (an action only when it killed
    something). The table unreadable -> one more failure; after SWEEP_TRIES in a row (logged) the
    record is settled by `kill -0` alone: dropped once its claude (child_pid) is gone, kept (doctor
    warns) while that pid lives. No time (also a read the deadline cut short) -> left as it is for
    a later reconcile."""
    data = _read(path)
    if data is None:
        path.unlink(missing_ok=True)
        return None
    hit, failed = _sweep_lost(data, deadline)
    if hit is None and not failed:
        return None
    unit, rid = data.get("unit"), data.get("run_id")
    act = {**_act(proj, data, "cleared"), "filed": True}
    if hit is not None:
        path.unlink(missing_ok=True)
        _drop_marker(rid)
        if not hit:
            return None
        log.log("events", f"run unit={unit} runner-swept pids={','.join(map(str, hit))}")
        return {**act, "killed": hit, "removed": True}
    n = int(data.get("sweep_failures") or 0) + 1
    child = data.get("child_pid")
    if n == SWEEP_TRIES:
        log.log("hooks", f"runner: {unit}: the process table could not be read {n} times in a row; "
                         f"the pending sweep of {rid} is settled by kill -0 (child_pid {child})")
    if n >= SWEEP_TRIES and not (child and alive(child)):
        path.unlink(missing_ok=True)
        _drop_marker(rid)
        log.log("events", f"run unit={unit} runner-sweep-abandoned run_id={rid} failures={n}")
        return {**act, "action": "abandoned", "removed": True, "failures": n}
    try:
        util.write_json(path, {**data, "sweep_failures": n})
    except OSError:
        pass
    return None


def pending_sweeps(proj: str) -> list:
    """Records of the project's pending sweeps (doctor)."""
    try:
        paths_ = sorted(run_dir(proj).glob(f"*{SWEEP_SUFFIX}"))
    except OSError:
        return []
    return [data for data in (_read(p) for p in paths_) if data is not None]


def reconcile(reg, proj: str, *, budget_s) -> list:
    """Settle the project's lost runs; the actions taken (see _lost), [] when there is nothing.

    For every unit lock: pid dead (`kill -0`) -> lost; pid alive -> `ps -ww` (PS_TIMEOUT_S, and
    only while `budget_s` lasts) shows `orche … run <unit>` -> live, left alone; another program
    (a recycled pid) -> lost; `ps` cannot tell or no time left -> left alone. Then the pending
    sweeps (_pending_sweep). `budget_s` bounds every `ps` and every SIGTERM->SIGKILL wait; with 0
    nothing but `kill -0` and file writes run (Main's prompt hook keeps its 3 s)."""
    deadline = time.monotonic() + max(0.0, float(budget_s or 0))
    out = []
    folder = run_dir(proj)
    try:        # listed first: a sweep that just failed below is not retried in the same pass
        records = sorted(folder.glob(f"*{SWEEP_SUFFIX}"))
    except OSError:
        records = []
    for path in _unit_locks(folder):
        data = _read(path)
        if data is None or not data.get("unit"):
            continue
        if alive(data.get("pid")):
            if _left(deadline) <= 0:
                continue
            try:
                verified = is_run_process(data["pid"], data["unit"], _left(deadline))
            except (KeyError, TypeError, ValueError, OSError):
                verified = None
            if verified is not False:
                continue
        try:
            act = _lost(reg, proj, path, data, deadline)
        except Exception as exc:  # noqa: BLE001 - one bad lock never stops the others (or the caller)
            log.log("hooks", f"runner: reconcile {path.name}: {type(exc).__name__}: {exc}")
            continue
        if act is not None:
            out.append(act)
    for path in records:
        try:
            act = _pending_sweep(proj, path, deadline)
        except Exception as exc:  # noqa: BLE001
            log.log("hooks", f"runner: reconcile {path.name}: {type(exc).__name__}: {exc}")
            continue
        if act is not None:
            out.append(act)
    return out


def reconcile_cli(reg, args, keys=None) -> list:
    """reconcile() for an `orche` command (`keys`: those projects, default all); one stderr line
    per action. Nothing under --dry-run (it mutates: pointers, RUN.json, kills)."""
    if getattr(args, "dry_run", False):
        return []
    out = []
    for key in sorted(reg.get("projects", {})) if keys is None else keys:
        out += reconcile(reg, key, budget_s=RECONCILE_BUDGET_S)
    for act in out:
        print(f"orche: {act['project']}: {describe(act)}", file=sys.stderr)
    return out


def describe(act) -> str:
    """One line for a reconcile action (doctor rows, CLI notes)."""
    unit, pid, at = act.get("unit"), act.get("pid"), _clock(act.get("started"))
    if act["action"] == "unfiled":
        return (f"{unit}: runner pid {pid} died; filing its `failed` pointer failed ({act.get('error')}); "
                "the lock stays for the next reconcile")
    if act["action"] == "abandoned":
        return (f"{unit}: lost run (pid {pid}): the process table could not be read "
                f"{act.get('failures')} times in a row and its claude is gone; leftovers not swept")
    head = {"reconciled": f"runner died: {unit} (pid {pid}, started {at}); filed `failed`",
            "stale-lock": f"{unit}: runner pid {pid} is gone; its pointer was already filed",
            "cleared": f"{unit}: lost run (pid {pid}) already filed",
            "unroutable": f"runner died: {unit} (pid {pid}, started {at}); NOT filed: "
                          f"{act.get('to')} has no route to a recipient",
            "unknown-unit": f"{unit}: runner pid {pid} is gone; no registered project has the unit, "
                            "nothing filed",
            "error": f"{unit}: runner pid {pid} is gone; filing failed ({act.get('error')})"}[act["action"]]
    tail = f"; killed {len(act['killed'])} leftover process(es)" if act.get("killed") else ""
    lock = ("; slot freed, leftovers swept by a later reconcile" if act.get("pending") else
            "; lock removed" if act.get("removed") else "; lock kept")
    return f"{head}{tail}{lock}; re-run with `orche run {unit}`"


def stop(proj: str, unit: str, timeout=PS_TIMEOUT_S, reg=None) -> bool:
    """Ask `unit` to stop: always the marker (a run waiting for a slot gives up on it; a running
    run polls it every headless.POLL_S, kills claude's process group and files the pointer),
    then, as the fast path, SIGTERM to the `orche run` process of a live lock once `ps` verifies the
    pid. Never waits longer than `timeout` (the `ps` check and, for a lost run, its sweep; the Stop
    hook passes what is left of its budget). False when no run of the unit is live; a lock whose
    pid is no longer `orche run <unit>` is a lost run (reconciled: `failed` "runner died", never
    signalled); when `ps` cannot tell, the lock counts as live (`runner-pid-check-failed`): kept,
    never signalled, the run's own poll picks the STOP up."""
    started = time.monotonic()
    util.atomic_write(_marker(proj, unit), util.now_iso() + "\n")
    data = running(proj, unit)
    if data is None:
        return False
    try:
        pid = int(data["pid"])
    except (KeyError, TypeError, ValueError):
        return False      # running() checked the pid: unreachable
    try:
        verified = is_run_process(pid, unit, timeout)
    except OSError:
        verified = None
    if verified is False:
        _lost(registry.load() if reg is None else reg, proj, run_dir(proj) / f"{unit}.lock", data,
              started + (timeout or 0))
        return False
    if verified is None:     # maybe a recycled pid: no signal; the run's marker poll stops it
        log.log("events", f"run unit={unit} to={data.get('to')} runner-pid-check-failed pid={pid}")
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    return True


# ---- start / restart ---------------------------------------------------------

def _argv(unit) -> list:
    """`orche run <unit>` under this very interpreter (the 3.11+ running the hook): no launcher has to
    find Python again after the spawn, so a run never dies there without a pointer."""
    return [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), "run", unit]


def start(unit) -> None:
    """Start `orche run <unit>` detached (procs.spawn_detached: its own session, output appended to
    logs/runs/<unit>.log, the plugin bin first on PATH, ORCHE_RUN_ID dropped); never waits.
    SINCE_ENV (now, ns) dates the request: a STOP marker written after it stops the run even before
    the new process got going; an older marker never does."""
    procs.spawn_detached(_argv(unit), logs_dir() / f"{unit}.log", **{SINCE_ENV: str(time.time_ns())})


def restart(proj: str, unit: str, timeout=PS_TIMEOUT_S, reg=None) -> bool:
    """AMEND: stop() a live run of `unit`, then start() a fresh one, which waits for the stopped run's
    slot (_try_acquire waits while the unit's live lock carries a marker). The order is the contract:
    the marker is older than the fresh run's request time, so it stops only the old run. True when a
    live run was stopped; the fresh run is started either way."""
    stopped = stop(proj, unit, timeout=timeout, reg=reg)
    start(unit)
    return stopped
