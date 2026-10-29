"""What a run leaves behind (SPEC §14.13): RUN.json (one constructor, run_record), REPORT.md, the
inbox pointer from the target to its recipient (`via: runner`), the events line and the activity
touch — for a finished run (job), a run that never started (unrun) and a lost run (lost, filed
from its lock: what ran, never re-resolved from today's BRIEF.md or registry).

Every pointer filed here carries `via: runner`; any other pointer for the unit from the target is
the worker's own (`orche report`), and then nothing is filed for that run. This module never imports
runs.job, dispatch or cmd_report: reconcile loads it inside the prompt hook.
"""
import os

from .. import brief, inbox, ledger, log, mask, roster, runs, units, util
from . import history

VIA = "runner"       # pointer field `via` on every pointer the runner files
TAIL_CHARS = 2000
IDENTITY = ("unit", "project", "to", "worktree", "branch", "tier", "model", "vendor", "effort")   # what ran
RESULT = ("started", "finished", "timeout_s", "exit_code", "is_error", "stop_reason", "cost_usd",
          "num_turns", "usage", "result_tail", "error", "head_before", "head_sha", "status",
          "pointer_filed", "pr")
LOCKED = ("to", "rev", "tier", "model", "vendor", "effort", "worktree", "branch",
          "timeout_s")   # what runs: job.lock_info's keys
LOST_TIMEOUT_S = 31 * 60   # a lock without timeout_s: job.DEFAULT_TIMEBOX_S + job.GRACE_S


class FilingError(Exception):
    """REPORT.md, RUN.json or the pointer of a finished run could not be written (ENOSPC, EACCES, …).
    job.run keeps the lock: once this process is gone, reconcile files the run as lost."""


def run_record(who, *, started, finished, status, stop_reason, is_error, error, pointer_filed,
               timeout_s=None, exit_code=None, cost_usd=None, num_turns=None, usage=None,
               result_tail="", head_before=None, head_sha=None, pr=None, review=None, stage=None,
               held_vendor=None, holds=0) -> dict:
    """The RUN.json record: `who`'s IDENTITY keys, then the RESULT keys, always in this order (25), then
    those of `review`, `stage`, `held_vendor` and `holds` that are set. `pr`: what a gated done published
    ({number, url, head}, publish.Published.record), else None; `review`: the pointer's review field;
    `stage`, `held_vendor`: where a usage limit held the run (stop reason usage_limit); `holds`: holds_of's
    count. Values are written as given; callers mask claude's strings (D5)."""
    result = {"started": started, "finished": finished, "timeout_s": timeout_s, "exit_code": exit_code,
              "is_error": is_error, "stop_reason": stop_reason, "cost_usd": cost_usd,
              "num_turns": num_turns, "usage": usage, "result_tail": result_tail, "error": error,
              "head_before": head_before, "head_sha": head_sha, "status": status,
              "pointer_filed": pointer_filed, "pr": pr}
    held = {"review": review, "stage": stage, "held_vendor": held_vendor, "holds": holds}
    return {**{k: who[k] for k in IDENTITY}, **{k: result[k] for k in RESULT}, **{k: v for k, v in held.items() if v}}


def masked(value):
    """`value` masked (mask.redact) when it is a string, else as given."""
    return mask.redact(value)[0] if isinstance(value, str) else value


def recipient(reg, who):
    """Where who["to"]'s pointers for who["unit"] go: the sender of the unit's brief when it is a live
    Main or division of the project other than the target; else a feature: its division; else the
    project's Main; else None."""
    b = brief.find(reg, who["unit"], project=who["project"])
    return roster.Roster(reg).recipient(who["to"], b.sender if b is not None else None)


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


def file_pointer(reg, who, rev, folder, status, summary, head=None, link_report=True, extra=None) -> bool:
    """The pointer from who["to"] to its recipient, `via: runner`, `brief_rev` `rev`, REPORT.md
    linked when asked and present, the summary masked then inbox.clip'ped. False (logged to
    hooks.log) when the target routes nowhere."""
    to = recipient(reg, who)
    if not to:
        log.log("hooks", f"runner: no recipient for {who['to']}; pointer for {who['unit']} not filed")
        return False
    report = folder / "REPORT.md"
    linked = link_report and report.is_file()
    from ..flow import lineage     # flow imports inbox; filing stays importable without it at load
    carried = lineage.fields(brief.find(reg, who["unit"], project=who["project"]))
    inbox.post(inbox.make(who["unit"], who["to"], to, status, report_path=str(report) if linked else "",
                          head_sha=head or "",
                          ledger_row=bool(head) and ledger.row_for_sha(who["project"], head) is not None,
                          brief_rev=rev,
                          summary=inbox.clip(mask.redact(summary)[0]) or status, via=VIA, **{**carried, **(extra or {})}))
    return True


def events(who, cost, turns, status, stop=None, rung=None, attempts=1, escalated=0) -> None:
    """The run's events line; `rung` (tier:model:effort of the last attempt), the attempt count and
    whether one climbed the ladder are added for a worker run (ladder.py)."""
    ladder = f" rung={rung} attempts={attempts} escalated={int(escalated)}" if rung else ""
    log.log("events", f"run unit={who['unit']} to={who['to']} model={who['model']} vendor={who['vendor']} "
                      f"cost={cost} turns={turns} status={status} stop={stop or '-'}{ladder}")


def holds_of(folder) -> int:
    """RUN.json `holds`: the unit's runs a usage limit stopped since its last done pointer (§8.5's repeat
    guard, flow/usage.repeats); 0 without one."""
    n = (runs.last(folder) or {}).get("holds")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


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
        routed = bool(recipient(reg, who))
    except Exception:  # noqa: BLE001 - file_pointer below logs the same failure
        routed = False
    rec = run_record(who, started=now, finished=now, status=status, stop_reason=stop_reason,
                     is_error=status == "failed", error=masked(error), pointer_filed=routed,
                     holds=holds_of(folder))      # a run that never started neither adds a hold nor ends the count
    extra = ({"retry": True, "quiet": True} if stop_reason in ("busy", "duplicate")
             else {"quiet": True} if stop_reason in ("capped", "held") else None)
    steps = [lambda: file_pointer(reg, who, rev, folder, status, summary, None, link_report=False, extra=extra),
             lambda: units.touch_activity(who["to"]),
             lambda: events(who, None, None, status, stop_reason)]
    if record:
        steps[:0] = [lambda: util.write_json(folder / "RUN.json", rec),
                     lambda: history.append(who["project"], rec, rev)]
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - the pointer matters more than any single step
            log.log("hooks", f"runner: {who['unit']}: cannot record the failed run: {exc}")
    return rec


def _lost_finished(started, timeout_s) -> str:
    """Now, or `started` + `timeout_s` when that is earlier: the run was killed by then."""
    import datetime as _dt
    now, start = util.now_iso(), units.parse_iso(started)
    if start is None:
        return now
    end = start + _dt.timedelta(seconds=timeout_s)
    return end.astimezone().isoformat(timespec="seconds") if end < units.parse_iso(now) else now


def _drop_codex_out(data):
    """Remove a lost Codex run's `-o` file; a lock without `started_ns` names none. Never raises."""
    from .. import codex, procs, vendors
    if vendors.vendor_of(data.get("model")) != vendors.CODEX or not data.get("unit") \
            or data.get("started_ns") is None:
        return
    try:
        codex.out_path(procs.run_id(data["unit"], data["started_ns"])).unlink(missing_ok=True)
    except OSError as exc:
        log.log("hooks", f"runner: {data['unit']}: cannot remove the codex -o file: {exc}")


def lost(reg, proj, data) -> str:
    """A lost run (runs.reconcile: its `orche run` died without filing) from its lock record `data`.
    "unknown-unit": `proj` has no folder for the unit, nothing to file; "stale-lock": a pointer of
    this run (the runner's or the worker's own) is already filed; "unroutable": the target routes to
    no recipient: the minimal RUN.json (`pointer_filed: false`) and no pointer (logged);
    "reconciled": a minimal RUN.json (`stop_reason: lost`, `finished` now or, earlier, the lock's
    `started` + `timeout_s` (LOST_TIMEOUT_S without it: the worker was killed by then), `cost_usd` the
    reservation of `started` to `finished`, history.reserve, at most flow.max_unit_cost_usd; kept
    when the run had written its own) and `failed` "runner died: …" from the target to Main (feature: Build), events
    `runner-reconciled`, activity.
    The identity and the revision are the lock's (LOCKED): what ran. A lock written before them
    gives `null` identity and the brief's current revision (and target, without `to`). No donegate
    call: a lost run has no claim. A Codex run's `-o` file (codex.out_path of the lock's run id) is
    removed: the dead runner could not. Raises OSError when a write fails (the caller keeps the lock and
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
    rcpt = recipient(reg, who)
    if filed_since(rcpt, unit, to, started):
        return "stale-lock"
    error = f"runner process {pid} died without filing"
    _drop_codex_out(data)
    if not runs.ran_since(folder, units.parse_iso(started)):
        from ..flow import guard     # lazy, as lineage below: reconcile loads this module inside the prompt hook
        timeout_s = data.get("timeout_s")
        timeout_s = timeout_s if isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool) \
            and timeout_s > 0 else LOST_TIMEOUT_S
        finished = _lost_finished(started, timeout_s)
        rec = run_record(who, started=started, finished=finished, status="failed", stop_reason="lost",
                         is_error=True, error=error, pointer_filed=bool(rcpt), timeout_s=timeout_s,
                         cost_usd=history.reserve(started, finished, guard.unit_cap()))
        util.write_json(folder / "RUN.json", rec)
        history.append(proj, rec, rev)
    if not file_pointer(reg, who, rev, folder, "failed",
                        f"runner died: {unit} (pid {pid}, started {runs.clock(started)}); "
                        f"re-run with `orche run {unit}`", None, link_report=False):
        log.log("events", f"run unit={unit} to={to} runner-unroutable pid={pid} filed=false")
        return "unroutable"
    units.touch_activity(to)
    log.log("events", f"run unit={unit} to={to} runner-reconciled pid={pid}")
    return "reconciled"
