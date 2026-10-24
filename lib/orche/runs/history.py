"""Run history (C10 T6): `ORCHE_HOME/runs/<p>/history.jsonl`, one line per RUN.json written (job,
`unrun`, `lost`), each one O_APPEND write:
`{unit, rev, to, model, vendor, effort, status, stop_reason, cost_usd, num_turns, started, finished}`.
Readers: the spend cap (G4) and the run cap (G5) in flow.guard. A corrupt line is skipped.

A run whose worker started but left no cost figure (killed, timed out, crashed, Codex without a
completed turn, lost) records a reservation instead of nothing (reserve(), design C11 §7.3), so G4
counts it; a run that never started its worker (busy, duplicate, stopped before start, exit 127, a
Codex that printed no event) records no cost."""
import json
import math
import os

from .. import log, paths, units, util

KEYS = ("unit", "rev", "to", "model", "vendor", "effort", "status", "stop_reason", "cost_usd", "num_turns",
        "started", "finished")
UNCOUNTED = ("busy",)      # the only unrun reason filed to history: never started claude, not a run for G5
UNKNOWN_USD_PER_MIN = 0.25  # what a minute of a run without a cost figure counts (§7.3)


def path(proj):
    return paths.orche_home() / "runs" / proj / "history.jsonl"


def entry(rec, rev):
    """The history line of RUN.json record `rec` for brief revision `rev`."""
    return {k: (rev if k == "rev" else rec.get(k)) for k in KEYS}


def reserve(started, finished, cap):
    """The cost (USD) a started run without a cost figure counts: its wall time in whole minutes,
    rounded up and at least one (the timestamps are whole seconds), × UNKNOWN_USD_PER_MIN, at most
    `cap` (flow.max_unit_cost_usd). An unreadable time counts one minute."""
    a, b = units.parse_iso(started), units.parse_iso(finished)
    seconds = (b - a).total_seconds() if a is not None and b is not None else 0
    return round(min(float(cap), max(1, math.ceil(seconds / 60)) * UNKNOWN_USD_PER_MIN), 2)


def append(proj, rec, rev):
    """Append `rec`'s line to the project's history in one O_APPEND write. Logs, never raises."""
    target = path(proj)
    data = (json.dumps(entry(rec, rev), ensure_ascii=False) + "\n").encode("utf-8")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as exc:
        log.log("hooks", f"runner: cannot append history for {rec.get('unit')}: {exc}")


def read(proj, since=None, key="finished"):
    """The project's history lines, oldest first; with `since` (aware datetime), only those whose
    `key` time is at or after it. Corrupt lines (bad bytes or bad JSON) are skipped."""
    try:
        data = path(proj).read_bytes()
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        try:
            row = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if since is not None:
            t = units.parse_iso(row.get(key))
            if t is None or t < since:
                continue
        out.append(row)
    return out


def _cost(row):
    try:
        return float(row.get("cost_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def spend(proj, since, unit=None):
    """Headless spend (USD) of the project, or of one unit, finished at or after `since`."""
    return round(sum(_cost(r) for r in read(proj, since) if unit is None or r.get("unit") == unit), 2)


def runs_of(proj, unit, since):
    """The unit's runs started at or after `since`, busy (never started) not counted."""
    return sum(1 for r in read(proj, since, key="started")
               if r.get("unit") == unit and r.get("stop_reason") not in UNCOUNTED)
