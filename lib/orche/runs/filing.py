"""What a run leaves behind (SPEC §14.13): RUN.json (one constructor, run_record), REPORT.md, the
inbox pointer from the target to its recipient (`via: runner`), the events line and the activity
touch — for a finished run (job), a run that never started (unrun) and a lost run (lost, filed
from its lock: what ran, never re-resolved from today's BRIEF.md or registry).

Every pointer filed here carries `via: runner`; any other pointer for the unit from the target is
the worker's own (`orche report`), and then nothing is filed for that run. This module never imports
runs.job, dispatch or cmd_report: reconcile loads it inside the prompt hook.
"""
import os
from pathlib import Path

from .. import brief, inbox, ledger, log, mask, roster, runs, units, util

VIA = "runner"       # pointer field `via` on every pointer the runner files
TAIL_CHARS = 2000
IDENTITY = ("unit", "project", "to", "worktree", "branch", "tier", "model", "effort")      # what ran
RESULT = ("started", "finished", "timeout_s", "exit_code", "is_error", "stop_reason", "cost_usd",
          "num_turns", "usage", "result_tail", "error", "head_before", "head_sha", "status",
          "pointer_filed")
LOCKED = ("to", "rev", "tier", "model", "effort", "worktree", "branch")   # what runs: job.lock_info's keys


class FilingError(Exception):
    """REPORT.md, RUN.json or the pointer of a finished run could not be written (ENOSPC, EACCES, …).
    job.run keeps the lock: once this process is gone, reconcile files the run as lost."""


def run_record(who, *, started, finished, status, stop_reason, is_error, error, pointer_filed,
               timeout_s=None, exit_code=None, cost_usd=None, num_turns=None, usage=None,
               result_tail="", head_before=None, head_sha=None) -> dict:
    """The RUN.json record: `who`'s IDENTITY keys, then the RESULT keys, always in this order (23).
    Values are written as given; callers mask claude's strings (D5)."""
    result = {"started": started, "finished": finished, "timeout_s": timeout_s, "exit_code": exit_code,
              "is_error": is_error, "stop_reason": stop_reason, "cost_usd": cost_usd,
              "num_turns": num_turns, "usage": usage, "result_tail": result_tail, "error": error,
              "head_before": head_before, "head_sha": head_sha, "status": status,
              "pointer_filed": pointer_filed}
    return {**{k: who[k] for k in IDENTITY}, **{k: result[k] for k in RESULT}}


def masked(value):
    """`value` masked (mask.redact) when it is a string, else as given."""
    return mask.redact(value)[0] if isinstance(value, str) else value


def recipient(reg, to):
    """Where `to`'s pointers go (a feature: its division; else the project's Main), else None."""
    return roster.Roster(reg).recipient(to)


def set_aside_report(folder) -> None:
    """An earlier run's REPORT.md becomes REPORT.prev.md, so this run never links a stale one."""
    path = folder / "REPORT.md"
    if path.is_file():
        os.replace(path, folder / "REPORT.prev.md")


def write_report(folder, result) -> None:
    """REPORT.md = the masked result, unless the worker wrote one during this run."""
    path = folder / "REPORT.md"
    if not path.exists() and str(result or "").strip():
        util.atomic_write(path, mask.redact(str(result))[0].rstrip("\n") + "\n")


def record_since(folder, started) -> bool:
    """RUN.json already holds this run (its `started` is at or after the lock's)."""
    try:
        rec = util.read_json(folder / "RUN.json", default=None)
    except util.JsonError:
        return False
    since, at = units.parse_iso(started), units.parse_iso((rec or {}).get("started"))
    return isinstance(rec, dict) and since is not None and at is not None and at >= since


def records(roots) -> list:
    """Every RUN.json record (a dict) under `<root>/.orche/units/*/` of `roots`, in path order; an
    unreadable or malformed file is skipped."""
    out = []
    for root in roots:
        for path in sorted(Path(root).glob(f"{brief.UNITS}/*/RUN.json")):
            try:
                rec = util.read_json(path, default=None)
            except (OSError, util.JsonError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def worker_pointers(rcpt, unit, to) -> set:
    """Names of the pointers for `unit` from `to` in `rcpt`'s inbox, pending or drained, that were not
    filed here (no `via: runner`). Taken before and after claude runs: a new name means the worker
    filed its own report, and the run files none."""
    if not rcpt:
        return set()
    return {path.name for path, ptr in inbox.about(rcpt, unit, to) if ptr.get("via") != VIA}


def filed_since(rcpt, unit, to, started) -> bool:
    """A pointer for `unit` from `to` — filed here or by the worker — at or after `started` is in
    `rcpt`'s inbox, pending or drained: the recipient already heard from this run."""
    if not rcpt:
        return False
    since = units.parse_iso(started)
    for _path, ptr in inbox.about(rcpt, unit, to):
        at = units.parse_iso(ptr.get("at"))
        if since is None or (at is not None and at >= since):
            return True
    return False


def file_pointer(reg, who, rev, folder, status, summary, head=None, link_report=True) -> bool:
    """The pointer from who["to"] to its recipient, `via: runner`, `brief_rev` `rev`, REPORT.md
    linked when asked and present, the summary masked then inbox.clip'ped. False (logged to
    hooks.log) when the target routes nowhere."""
    to = recipient(reg, who["to"])
    if not to:
        log.log("hooks", f"runner: no recipient for {who['to']}; pointer for {who['unit']} not filed")
        return False
    report = folder / "REPORT.md"
    linked = link_report and report.is_file()
    inbox.write(inbox.make(who["unit"], who["to"], to, status, report_path=str(report) if linked else "",
                           head_sha=head or "",
                           ledger_row=bool(head) and ledger.row_for_sha(who["project"], head) is not None,
                           brief_rev=rev,
                           summary=inbox.clip(mask.redact(summary)[0]) or status, via=VIA))
    return True


def events(who, cost, turns, status) -> None:
    log.log("events", f"run unit={who['unit']} to={who['to']} model={who['model']} cost={cost} "
                      f"turns={turns} status={status}")


def unrun(reg, who, rev, folder, status, summary, error, stop_reason, record=True) -> dict:
    """A minimal RUN.json and the pointer when claude never ran (busy, duplicate, stopped before
    start) or the runner itself failed. Best effort: every step is tried and logged. `pointer_filed`
    is False when the target routes nowhere. Returns the record.

    `record` is False when another run of the unit is live (a duplicate `orche run`, or a stop
    before start while an AMEND's old revision still runs): that run writes its own RUN.json, and
    this one would overwrite it. The pointer never links REPORT.md: claude did not run here, so the
    file is an earlier run's or the live run's, still being written."""
    now = util.now_iso()
    try:
        routed = bool(recipient(reg, who["to"]))
    except Exception:  # noqa: BLE001 - file_pointer below logs the same failure
        routed = False
    rec = run_record(who, started=now, finished=now, status=status, stop_reason=stop_reason,
                     is_error=status == "failed", error=masked(error), pointer_filed=routed)
    steps = [lambda: file_pointer(reg, who, rev, folder, status, summary, None, link_report=False),
             lambda: units.touch_activity(who["to"]),
             lambda: events(who, None, None, status)]
    if record:
        steps.insert(0, lambda: util.write_json(folder / "RUN.json", rec))
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - the pointer matters more than any single step
            log.log("hooks", f"runner: {who['unit']}: cannot record the failed run: {exc}")
    return rec


def lost(reg, proj, data) -> str:
    """A lost run (runs.reconcile: its `orche run` died without filing) from its lock record `data`.
    "unknown-unit": `proj` has no folder for the unit, nothing to file; "stale-lock": a pointer of
    this run (the runner's or the worker's own) is already filed; "unroutable": the target routes to
    no recipient: the minimal RUN.json (`pointer_filed: false`) and no pointer (logged);
    "reconciled": a minimal RUN.json (`stop_reason: lost`; kept when the run had written its own) and
    `failed` "runner died: …" from the target to Main (feature: Build), events `runner-reconciled`,
    activity.
    The identity and the revision are the lock's (LOCKED): what ran. A lock written before them
    gives `null` identity and the brief's current revision (and target, without `to`). No donegate
    call: a lost run has no claim. Raises OSError when a write fails (the caller keeps the lock and
    retries later)."""
    unit, started, pid = data.get("unit"), data.get("started"), data.get("pid")
    b = brief.find(reg, unit, project=proj)
    if b is None:
        log.log("hooks", f"runner: lost run of unknown unit {unit!r}: nothing to file")
        return "unknown-unit"
    who = {"unit": unit, "project": b.project,
           **{k: data.get(k) for k in IDENTITY if k not in ("unit", "project")}}
    who["to"] = who["to"] or b.to       # every lock written carries `to`: only a hand-made one falls back
    rev = data.get("rev") or b.rev or 1
    to, folder = who["to"], b.folder
    rcpt = recipient(reg, to)
    if filed_since(rcpt, unit, to, started):
        return "stale-lock"
    error = f"runner process {pid} died without filing"
    if not record_since(folder, started):
        util.write_json(folder / "RUN.json", run_record(
            who, started=started, finished=util.now_iso(), status="failed", stop_reason="lost",
            is_error=True, error=error, pointer_filed=bool(rcpt)))
    if not file_pointer(reg, who, rev, folder, "failed",
                        f"runner died: {unit} (pid {pid}, started {runs._clock(started)}); "
                        f"re-run with `orche run {unit}`", None, link_report=False):
        log.log("events", f"run unit={unit} to={to} runner-unroutable pid={pid} filed=false")
        return "unroutable"
    units.touch_activity(to)
    log.log("events", f"run unit={unit} to={to} runner-reconciled pid={pid}")
    return "reconciled"
