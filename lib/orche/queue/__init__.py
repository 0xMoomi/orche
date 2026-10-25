"""The merge queue (design §4): one queue per project, an entry per approved PR pinned to its head.

store holds the files, repo the git/gh probes, facts the A.7 facts block. This module is the
library the CLI (cmd_queue) and the integrator (T3) call; it checks no session (the CLI does)."""
import fcntl
import statistics
import sys

from .. import log as _log, paths, procs, runs, units, util
from . import store

DEFAULTS = {"method": "merge", "land": "auto", "timeout_s": 1200, "max_returns": 3, "max_restarts": 2,
            "lost_after_s": 3600, "max_stops": 3, "docs_paths": ["**/*.md", "docs/**", ".orche/**"],
            "per_project": {}}
LAND_AUTO = "land-auto"     # who `approved` an entry that `land: auto` released (never the owner's word)


def settings(cfg, project) -> dict:
    """`queue` config with `queue.per_project.<p>` laid over it."""
    q = {**DEFAULTS, **((cfg or {}).get("queue") or {})}
    return {**q, **((q.get("per_project") or {}).get(project) or {})}


def land_mode(cfg, project) -> str:
    return "owner" if settings(cfg, project).get("land") == "owner" else "auto"


def entry_id(pr, head) -> str:
    return f"pr{int(pr)}-{str(head)[:7]}"


def approved_in_log(project, pr, main_session=None) -> bool:
    """The log shows `approved` for the PR (not a `land: auto` release) and no later drop by Main (§4.4)."""
    ok = False
    for line in store.read_log(project):
        if line.get("pr") != int(pr):
            continue
        if line.get("event") == "approved" and line.get("by") != LAND_AUTO:
            ok = True
        elif line.get("event") == "dropped" and main_session and line.get("by") == main_session:
            ok = False
    return ok


def find(q, ref):
    """The entry `ref` names: its id, `#N` or `N`; else None."""
    ref = str(ref).strip()
    num = ref.lstrip("#")
    for e in q["entries"]:
        if e["id"] == ref or (num.isdigit() and e["pr"] == int(num)):
            return e
    return None


def position(q, eid) -> int:
    return next((i + 1 for i, e in enumerate(q["entries"]) if e["id"] == eid), 0)


def _events(msg) -> None:
    _log.log("events", msg)


def add(project, entry, *, first=False, by="auto", base="main") -> dict:
    """Queue `entry` (§4.3 steps 9-10). Same PR and head: {"status": "already", "position": k}.
    Another head of the same PR: the old entry is dropped (superseded) first. Returns
    {"status": "queued"|"already", "position", "total", "superseded": old entry or None}."""
    superseded = None
    with store.edit(project, base) as q:
        old = next((e for e in q["entries"] if e["pr"] == entry["pr"]), None)
        if old is not None and old["head"] == entry["head"]:
            return {"status": "already", "position": position(q, old["id"]), "total": len(q["entries"]),
                    "superseded": None, "entry": old}
        if old is not None:
            q["entries"].remove(old)
            superseded = old
            store.log(project, "dropped", old, reason="superseded", by=by)
            _events(f"queue dropped project={project} pr={old['pr']} reason=superseded")
        if first:
            at = sum(1 for e in q["entries"] if e.get("state") == "testing")
            q["entries"].insert(at, entry)
        else:
            q["entries"].append(entry)
        pos, total = position(q, entry["id"]), len(q["entries"])
    store.log(project, "added", entry, by=by, reason=entry.get("accept"))
    _events(f"queue add project={project} pr={entry['pr']} unit={entry.get('unit') or '-'} "
            f"head={entry['head'][:7]} by={by} pos={pos}")
    return {"status": "queued", "position": pos, "total": total, "superseded": superseded, "entry": entry}


def drop(project, ref, *, reason=None, by="auto"):
    """Remove the entry `ref` names; returns it, or None when there is none."""
    with store.edit(project) as q:
        e = find(q, ref)
        if e is None:
            return None
        q["entries"].remove(e)
    why = reason or f"by-{by}"
    store.log(project, "dropped", e, reason=why, by=by)
    _events(f"queue dropped project={project} pr={e['pr']} reason={why}")
    return e


def pause(project, *, by, reason=None) -> dict:
    with store.edit(project) as q:
        q["paused"] = {"by": by, "at": util.now_iso(), "reason": reason or ""}
    store.log(project, "paused", by=by, reason=reason)
    _events(f"queue paused project={project} by={by}")
    return q


def resume(project, *, by) -> dict:
    """Open the queue: no pause, no closure, the count of passes stopped in a row back to 0."""
    with store.edit(project) as q:
        q["paused"] = None
        q["closed"] = None
        q["stops"] = 0
    store.log(project, "resumed", by=by)
    _events(f"queue resumed project={project} by={by}")
    return q


def approve(project, refs=None, *, by) -> list:
    """Set owner_ok on the named entries (all waiting ones when `refs` is None); log `approved` for
    each that flipped. Returns the PR numbers approved."""
    done = []
    with store.edit(project) as q:
        targets = q["entries"] if refs is None else [e for e in (find(q, r) for r in refs) if e]
        for e in targets:
            if not e.get("owner_ok"):
                e["owner_ok"] = True
                done.append(e)
    for e in done:
        store.log(project, "approved", e, by=by)
    return [e["pr"] for e in done]


def release(project, cfg) -> list:
    """`land: auto` (R-QUEUE-12): entries still waiting for the owner's word, queued while the project
    was `land: owner`, become plain waiting, each logged `approved` by `land-auto` (approved_in_log never
    counts that as the owner's word). Their PR numbers; [] under `land: owner`."""
    if land_mode(cfg, project) != "auto":
        return []
    try:
        if all(e.get("owner_ok") for e in store.read(project)["entries"]):
            return []
    except (util.JsonError, KeyError, TypeError, AttributeError):
        return []
    done = []
    with store.edit(project) as q:
        for e in q["entries"]:
            if e.get("state") == "waiting" and not e.get("owner_ok"):
                e["owner_ok"] = True
                done.append(dict(e))
    for e in done:
        store.log(project, "approved", e, by=LAND_AUTO, reason="land: auto")
        _events(f"queue approved project={project} pr={e['pr']} by={LAND_AUTO}")
    return [e["pr"] for e in done]


def next_entry(q):
    """The first waiting entry the integrator may test (owner_ok, not dropped), else None. While main
    is red (closed `main-red`) only an entry added with --first; while a failed proof closed the queue,
    none (that needs `orche queue resume`)."""
    closed = q.get("closed")
    if closed and (closed.get("reason") if isinstance(closed, dict) else None) != "main-red":
        return None
    return next((e for e in q["entries"] if e.get("state") == "waiting" and e.get("owner_ok")
                 and not e.get("drop") and (not closed or e.get("first"))), None)


def landed(e) -> bool:
    """An entry GitHub merged whose proof is still pending (`test.landed`, §5.5): it stays testing."""
    t = e.get("test")
    return e.get("state") == "testing" and isinstance(t, dict) and bool(t.get("landed"))


def mark(project, eid, **fields):
    """Merge `fields` into the entry `eid`; returns it (None when gone)."""
    with store.edit(project) as q:
        e = find(q, eid)
        if e is not None:
            e.update(fields)
    return e


def remove(project, eid, event, **fields):
    """Take the entry `eid` out of queue.json and log `event` (merged, returned, lost...)."""
    with store.edit(project) as q:
        e = find(q, eid)
        if e is not None:
            q["entries"].remove(e)
    if e is not None:
        store.log(project, event, e, **fields)
    return e


def startable(project) -> bool:
    """Work the integrator may take now, neither paused nor flow-paused (G8): a waiting owner_ok entry
    (next_entry; unapproved entries alone never start it) or a merge whose proof is pending (landed)."""
    from ..flow import guard
    try:
        q = store.read(project)
    except util.JsonError:
        return False
    return (not q.get("paused") and guard.paused(project) is None
            and (next_entry(q) is not None or any(landed(e) for e in q["entries"])))


def start(project) -> bool:
    """Spawn `orche queue run <p>` detached when an entry can be taken and no integrator holds the
    lock (R-QUEUE-10); True when one runs or was started. Inside a Codex sandbox nothing is spawned
    (the heartbeat starts it)."""
    if runs.in_sandbox():
        _log.log("queue", f"{project}: start deferred (sandbox)")
        return False
    if running(project):
        return True
    if not startable(project):
        return False
    argv = [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), "queue", "run", project]
    try:
        procs.spawn_detached(argv, store.folder(project) / "integrator.log")
    except OSError as exc:
        _log.log("queue", f"{project}: integrator start failed: {exc}")
        return False
    _log.log("queue", f"{project}: integrator started")
    return True


def running(project) -> bool:
    """Whether an integrator holds the project's lock (a non-blocking flock probe of
    queue/<p>/integrator.lock; the lock dies with its process)."""
    path = store.folder(project) / "integrator.lock"
    try:
        with open(path, "r") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _today(line, day) -> bool:
    return str(line.get("at", "")).startswith(day)


def summary(project) -> dict:
    """Counts for `orche queue <p>`: waiting, testing, await (owner_ok false), merged/returned today,
    state (open|paused|closed) and the gate's p50 seconds over the last 20 merges (None without)."""
    q = store.read(project)
    lines = store.read_log(project)
    day = util.now_iso()[:10]
    secs = [ln["secs"] for ln in lines if ln.get("event") == "merged" and isinstance(ln.get("secs"), (int, float))][-20:]
    waiting = [e for e in q["entries"] if e.get("state") == "waiting"]
    return {"project": project, "waiting": len(waiting),
            "testing": sum(1 for e in q["entries"] if e.get("state") == "testing"),
            "await": sum(1 for e in waiting if not e.get("owner_ok")),
            "merged_today": sum(1 for ln in lines if ln.get("event") == "merged" and _today(ln, day)),
            "returned_today": sum(1 for ln in lines if ln.get("event") == "returned" and _today(ln, day)),
            "state": "closed" if q.get("closed") else "paused" if q.get("paused") else "open",
            "paused": q.get("paused"), "closed": q.get("closed"), "gate_p50": statistics.median(secs) if secs else None,
            "entries": q["entries"]}


def reopen(project, closed) -> str:
    """A closed queue's reason and what reopens it (`orche queue`, `orche status`, doctor): main-red → a
    fix queued with --first; proof → only `orche queue resume` (--first never reopens it)."""
    c = closed if isinstance(closed, dict) else {}
    sha = str(c.get("main") or "?")[:7]
    if c.get("reason") == "proof":
        return f"proof — merge {sha} failed its proof: check it, then orche queue resume {project}"
    return f"main-red — main {sha} fails its gate: queue the fix with --first"


def age(at) -> str:
    """`12m`, `3h`, `2d` since an ISO time; `?` when unreadable."""
    when = units.parse_iso(at)
    if when is None:
        return "?"
    now = units.parse_iso(util.now_iso())
    s = max(0, int((now - when).total_seconds()))
    return f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"
