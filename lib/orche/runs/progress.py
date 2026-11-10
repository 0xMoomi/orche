"""A running unit's progress record: `<unit folder>/PROGRESS.json`, next to RUN.json.

The runner (runs/job.py) feeds every line of the worker's `stream-json` output to a Recorder, which keeps
facts only, never conversation text: when the run started and its TIMEBOX, the time of the last event,
the turns so far, the last tool with a short masked target (a file's base name or a shell command's
program, never an environment assignment, through mask.py), whether a tool call is open (since when), and
tokens and dollars so far when the events carry usage (dollars from metrics.PRICES; null when the model
has no price). It is written atomically at most every WRITE_EVERY_S and once at the end, marked `ended`.
A line that is not JSON, or an event of an unknown type, never breaks a run: a non-JSON line is skipped, an
unknown event only counts as an event.

Readers (`orche status`, the status line, the heartbeat's silent class) use view(): a record counts only
while the run's lock is live and names the same run id, so a stale one is never shown as live. A run whose
last event is STALLED_S old with no tool call open is `stalled`; one silent NOTE_S (no event at all) gets
the heartbeat's urgent note to the brief's sender (reconcile.py, class silent). Neither kills a run: the
TIMEBOX bounds it. A Codex run writes no record. While it runs, the Recorder also re-renders the project's
cached status line at most every LINE_EVERY_S (statusline.touch), so its elapsed time stays fresh.
"""
import json
import os
import re
import shlex
import threading
import time
from pathlib import Path

from .. import util

FILE = "PROGRESS.json"
WRITE_EVERY_S = 5.0
LINE_EVERY_S = 60.0     # a run re-renders its project's cached status line at most this often
STALLED_S = 600     # no event and no tool call open this long: `stalled` (status, status line)
NOTE_S = 1200       # no event at all this long: the heartbeat's urgent note to the brief's sender
FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")
TARGET_CHARS = 40


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


WRAPPERS = ("env", "sudo", "time", "nohup", "command", "exec", "nice")   # skipped, with their options
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _program(command) -> str:
    """The base name of a shell command's program: its first word that is not an environment assignment
    (`NAME=value`), a wrapper (WRAPPERS) or a wrapper's option; '' when there is none or it holds `=`."""
    try:
        words = shlex.split(command)
    except ValueError:            # unbalanced quotes: plain words
        words = command.split()
    wrapped = False
    for word in words:
        if _ASSIGN.match(word) or wrapped and word.startswith("-"):
            continue
        base = os.path.basename(word)
        if base in WRAPPERS:
            wrapped = True
            continue
        return "" if "=" in base else base
    return ""


def target_of(name, inp) -> str:
    """A tool call's short masked target: a file tool's base name, a shell command's program (_program),
    else '' (never a URL, a pattern or a query)."""
    from .. import mask
    if not isinstance(inp, dict):
        return ""
    if name in FILE_TOOLS:
        raw = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or "").rstrip("/"))
    elif name == "Bash":
        raw = _program(str(inp.get("command") or ""))
    else:
        return ""
    return mask.redact(raw)[0][:TARGET_CHARS]


class Recorder:
    """The progress of one run (every attempt of it): feed() each stdout line, attempt() before a new
    worker call, close() at the end. Thread-safe; never raises from feed()."""

    def __init__(self, path, run_id, started, timebox_s, model, clock=time.time, project=None):
        self.path, self.clock, self.project, self._line_at = Path(path), clock, project, None
        self.rec = {"run_id": run_id, "started": started, "timebox_s": int(timebox_s), "last_event": None,
                    "turns": 0, "tool": None, "target": None, "tool_open_since": None, "tokens": None,
                    "usd": None, "ended": None}
        self._model, self._lock, self._timer = model, threading.RLock(), None
        self._written, self._dirty = 0.0, False
        self._base_turns, self._base_tokens, self._base_usd, self._base_known = 0, 0, 0.0, False
        self._prior, self._usage, self._open = None, {}, {}   # {message id: (model, usage)}; {tool id: name}
        self._turns, self._tokens, self._usd, self._att_usd = 0, 0, None, None   # this attempt's
        self.flush()

    def attempt(self, model, prior=None) -> None:
        """A new worker call of the same run: its counts add to the run's. `prior`: a resumed session's
        total so far (its result reports the whole conversation's)."""
        with self._lock:
            self._base_turns += self._turns
            self._base_tokens += self._tokens
            self._base_usd += self._att_usd or 0.0
            self._base_known = self._base_known or self._att_usd is not None
            self._model, self._prior, self._usage, self._open = model, prior, {}, {}
            self._turns, self._tokens, self._usd, self._att_usd = 0, 0, None, None

    def feed(self, line) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        with self._lock:
            try:
                self._event(event)
                self._sync()
            except (TypeError, ValueError, AttributeError):    # an event of an unexpected shape: skipped
                pass
            self._dirty = True
            self._maybe_write()

    def _event(self, event) -> None:
        self.rec["last_event"] = util.now_iso()
        kind, msg = event.get("type"), event.get("message")
        if kind == "assistant" and isinstance(msg, dict):
            mid = str(msg.get("id") or f"n{len(self._usage)}")
            if mid not in self._usage:
                self._turns += 1
            if isinstance(msg.get("usage"), dict):
                self._usage[mid] = (str(msg.get("model") or self._model), msg["usage"])
            else:
                self._usage.setdefault(mid, (self._model, {}))
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")[:TARGET_CHARS]
                    self.rec["tool"], self.rec["target"] = name, target_of(name, block.get("input")) or None
                    self._open[str(block.get("id"))] = name
            if self._open and self.rec["tool_open_since"] is None:
                self.rec["tool_open_since"] = self.rec["last_event"]
        elif kind == "user" and isinstance(msg, dict):
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._open.pop(str(block.get("tool_use_id")), None)
            if not self._open:
                self.rec["tool_open_since"] = None
        elif kind == "result":
            self._open.clear()
            self.rec["tool_open_since"] = None
            turns, usd = _num(event.get("num_turns")), _num(event.get("total_cost_usd"))
            if turns is not None and self._prior is None:
                self._turns = int(turns)
            if usd is not None:
                self._usd = max(0.0, usd - (self._prior or 0.0))

    def _sync(self) -> None:
        """tokens and usd of the run so far: the earlier attempts' plus this one's (its result's dollars once
        read, else its usage priced; None when a model has no price)."""
        from .. import metrics
        tokens, est, priced = 0, 0.0, True
        for model, usage in self._usage.values():
            tokens += sum(metrics._count(usage, k) for k in metrics.USAGE_KEYS)
            row = metrics.known_row(model, metrics.PRICES)
            if row is None:
                priced = False
            else:
                est += metrics.cost(usage, row)
        self._tokens = tokens
        self._att_usd = self._usd if self._usd is not None else est if priced and tokens else None
        known = self._base_known or self._att_usd is not None
        self.rec["turns"] = self._base_turns + self._turns
        self.rec["tokens"] = (self._base_tokens + tokens) or None
        self.rec["usd"] = round(self._base_usd + (self._att_usd or 0.0), 4) if known else None

    def _maybe_write(self) -> None:
        wait = self._written + WRITE_EVERY_S - self.clock()
        if wait <= 0:
            self.flush()
        elif self._timer is None:          # the last event before a quiet spell is written too
            self._timer = threading.Timer(wait, self._timed)
            self._timer.daemon = True
            self._timer.start()

    def _timed(self) -> None:
        with self._lock:
            self._timer = None
            if self._dirty and self.rec["ended"] is None:
                self.flush()

    def flush(self) -> None:
        with self._lock:
            try:
                util.write_json(self.path, self.rec)
            except OSError:
                pass
            self._written, self._dirty = self.clock(), False
            if self.project and self.rec["ended"] is None and (
                    self._line_at is None or self._written - self._line_at >= LINE_EVERY_S):
                self._line_at = self._written
                from .. import statusline          # the elapsed time on the status line, at most a minute old
                statusline.touch(self.project)

    def close(self) -> None:
        """The last write, marked `ended` (readers then never show it as live)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self.rec["ended"] = util.now_iso()
            self.flush()


def read(path):
    """The record at `path`, else None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _age(iso, now) -> float | None:
    from .. import units
    at = units.parse_iso(iso) if iso else None
    return None if at is None else max(0.0, (now - at).total_seconds())


def view(lock, now=None) -> dict:
    """A live run's facts for the readers, from its lock record (runs.live) and its PROGRESS.json (the lock's
    `progress`): {unit, to, run_id, alive, elapsed_s, timebox_s, turns, tool, target, tool_open_s, quiet_s,
    usd, tokens, stalled, live_record}; alive: the worker process (the lock's child_pid) is alive. Without a
    record that names the lock's run id and has not ended, only the lock's facts (live_record False, no
    stall)."""
    from .. import runs, units
    now = now or units.now()
    started = _age(lock.get("started"), now)
    rec = read(lock["progress"]) if lock.get("progress") else None
    if rec is None or rec.get("run_id") != lock.get("run_id") or rec.get("ended"):
        rec = None
    timeout = _num(lock.get("timeout_s"))
    child = lock.get("child_pid")
    out = {"unit": lock.get("unit"), "to": lock.get("to"), "run_id": lock.get("run_id"),
           "alive": runs.alive(child) if child else False, "elapsed_s": started,
           "timebox_s": _num((rec or {}).get("timebox_s")) or (timeout - 60 if timeout and timeout > 60 else None),
           "turns": None, "tool": None, "target": None, "tool_open_s": None, "quiet_s": None, "usd": None,
           "tokens": None, "stalled": False, "live_record": rec is not None}
    if rec is None:
        return out
    quiet = _age(rec.get("last_event"), now)
    out.update(turns=rec.get("turns"), tool=rec.get("tool"), target=rec.get("target"), usd=_num(rec.get("usd")),
               tokens=_num(rec.get("tokens")), tool_open_s=_age(rec.get("tool_open_since"), now),
               quiet_s=quiet if quiet is not None else started)
    out["stalled"] = (out["quiet_s"] is not None and out["quiet_s"] >= STALLED_S and out["tool_open_s"] is None)
    return out


def views(proj, now=None) -> list:
    """view() of every live run of `proj`, oldest first."""
    from .. import runs
    out = [view(lock, now) for lock in runs.live(proj)]
    return sorted(out, key=lambda v: -(v["elapsed_s"] or 0))


def minutes(seconds) -> str:
    """`22m`, `1h05m`; `?` for None."""
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def line(v) -> str:
    """`feat-x  12m of 30m · turn 14 · Edit ladder.py · $1.20` (`in Bash 14m` while a tool call is open,
    `stalled 12m` past STALLED_S without an event)."""
    parts = [f"{minutes(v['elapsed_s'])} of {minutes(v['timebox_s'])}"]
    if v["turns"] is not None:
        parts.append(f"turn {v['turns']}")
    tool = " ".join(x for x in (v["tool"], v["target"]) if x)
    if v["tool_open_s"] is not None and v["tool"]:
        parts.append(f"in {tool} {minutes(v['tool_open_s'])}")
    elif tool:
        parts.append(tool)
    if v["usd"] is not None:
        parts.append(f"${v['usd']:.2f}")
    if v["stalled"]:
        parts.append(f"stalled: no event for {minutes(v['quiet_s'])}")
    return f"{v['unit']}  " + " · ".join(parts)
