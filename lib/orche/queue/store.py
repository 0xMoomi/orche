"""The merge queue's files (design §4.1): ORCHE_HOME/queue/<p>/queue.json, read-modify-written under
an exclusive flock of queue/<p>/.lock and written atomically (util.write_json), and log.jsonl, one
O_APPEND line per event. Only waiting/testing entries live in queue.json; one entry per PR."""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .. import paths, util

STATES = ("waiting", "testing")
EVENTS = ("added", "approved", "testing", "merged", "returned", "dropped", "restarted", "lost",
          "closed", "opened", "paused", "resumed")
LOG_KEYS = ("at", "id", "pr", "unit", "event", "reason", "base", "candidate", "merge", "secs",
            "gate", "verdict", "by")


def folder(project) -> Path:
    return paths.orche_home() / "queue" / project


def queue_path(project) -> Path:
    return folder(project) / "queue.json"


def log_path(project) -> Path:
    return folder(project) / "log.jsonl"


def empty(project, base="main") -> dict:
    return {"project": project, "base": base, "paused": None, "closed": None, "green_main": None,
            "entries": []}


def read(project) -> dict:
    """The project's queue (an empty one when the file is missing). Raises util.JsonError if corrupt."""
    data = util.read_json(queue_path(project), None)
    return data if isinstance(data, dict) else empty(project)


@contextmanager
def locked(project):
    """Hold the project's queue lock (exclusive flock of queue/<p>/.lock) for the block."""
    where = folder(project)
    where.mkdir(parents=True, exist_ok=True)
    with open(where / ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def edit(project, base=None):
    """Read-modify-write: yields the queue dict under the lock and writes it back when the block
    ends without raising. `base` seeds a queue that does not exist yet."""
    with locked(project):
        q = util.read_json(queue_path(project), None)
        if not isinstance(q, dict):
            q = empty(project, base or "main")
        yield q
        util.write_json(queue_path(project), q)


def log(project, event, entry=None, **fields) -> dict:
    """Append one event line to log.jsonl (one O_APPEND write). `entry` supplies id, pr and unit."""
    if event not in EVENTS:
        raise ValueError(f"bad queue event {event!r}")
    line = {"at": util.now_iso()}
    if entry:
        line.update({"id": entry.get("id"), "pr": entry.get("pr"), "unit": entry.get("unit")})
    line["event"] = event
    line.update({k: v for k, v in fields.items() if v is not None})
    folder(project).mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path(project), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return line


def read_log(project) -> list:
    """Every parseable line of log.jsonl, oldest first."""
    try:
        text = log_path(project).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for raw in text.splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def projects() -> list:
    """The projects that have a queue folder."""
    root = paths.orche_home() / "queue"
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
