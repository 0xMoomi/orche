"""The merge queue: one queue per project, an entry per approved PR pinned to its head.

store holds the files (private to this package), repo the git probes, facts the A.7 facts block. This
module is the library the CLI (cmd_queue), the head review and the integrator (T3) call: `admit` is the
one way a PR becomes an entry, the transitions (add, drop, approve, release, take, set_test, testing,
front, lost, remove, pause, close, reopen, stop, cleared, resume) are the only writers of queue.json and
log.jsonl, and `summary` is the read model (`orche queue`, `orche status`, doctor, the heartbeat). It
checks no session (the CLI does)."""
import fcntl
import os
import re
import statistics
import time

from .. import cli, config, inbox, ledger, log as _log, paths, procs, publish, registry, roster, runs, units, util
from . import facts, gate, repo, store

DOCS_PATHS = ("**/*.md", "docs/**", ".orche/**")   # paths a [skip ci] commit may touch
LAND_AUTO = "land-auto"     # who `approved` an entry that `land: auto` released (never the owner's word)


def settings(cfg, project) -> dict:
    """The merged config's `queue` (config.load: every key there) with `queue.per_project.<p>` laid over it."""
    return config.for_project(cfg, "queue", project)


def land_mode(cfg, project, reg=None) -> str:
    """`owner` for a team project (registry.team: merge waits for the owner's word whatever the config says)
    and for `queue.land` owner; else `auto`. `reg` None: registry.load()."""
    if registry.team(registry.load() if reg is None else reg, project):
        return "owner"
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


class Refused(Exception):
    """A refusal (`orche queue`, admit): its exit code (cli.EXIT_*) and its text."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def verify_cmds(text) -> list:
    """The VERIFY field's backticked commands."""
    return [c.strip() for c in re.findall(r"`([^`]+)`", str(text or "")) if c.strip()]


def _review(cfg, project, pr, head, accept):
    """§4.3 step 6: a requested pre-review's row at `head`, if one ran (R-REVIEW-1). None (admitted) without a
    row or on a pass; a RETURN under review.rounds refuses; a capped row admits only with `accept`, its reason."""
    rounds = int(cfg["review"]["rounds"])
    h7 = head[:7]
    row = ledger.review_for(project, pr, head, "pre")
    if row is None:
        return None
    verdict = row["verdict"]
    if verdict == "pass":
        return None
    try:
        rnd = int(row.get("round") or 1)
    except ValueError:
        rnd = 1
    capped = verdict in ("error", "held", "skipped") or (verdict == "return" and rnd >= rounds)
    if capped and accept:
        return accept
    if capped:
        raise Refused(cli.EXIT_BLOCKED, f"pre-review of {h7} is {verdict} (round {rnd} of {rounds}): "
                                        f're-review, or queue with --accept "<why>"')
    raise Refused(cli.EXIT_BLOCKED, f"pre-review of {h7} returned it (round {rnd} of {rounds}): fix and review again")


def checks(cfg, project, where, *, pr, head, base, verify, accept=None, tier="standard", safe_because="") -> list:
    """§4.3 steps 5-8 on `head` in the checkout `where` (ledger row, a requested pre-review's row, gate, [skip ci]), then a deep
    head's blast-radius fact (R-REVIEW-14: named, and exercised by a test in the PR): the accepted reasons, else
    Refused."""
    from ..review import deep, proves
    h7, accepted = head[:7], []
    row = ledger.row_for_sha(project, head)
    if not row or row.get("verdict") not in ledger.ORDER:
        raise Refused(cli.EXIT_BLOCKED, f"no ledger row for {h7}")
    changed = repo.changed(where, publish.base_ref(where, base), head)
    why_deep = deep(cfg, changed, tier)
    why = _review(cfg, project, pr, head, accept)
    if why:
        accepted.append(why)
    if not gate.steps(where, head, verify):
        raise Refused(cli.EXIT_BLOCKED, "no gate: add scripts/verify/ci.sh (Appendix A.13) or a committed VERIFY script")
    skip = facts.skip_ci_commits(where, publish.base_ref(where, base), head, DOCS_PATHS)
    if skip and not accept:
        what = f"head {h7}" if h7 in skip else f"commit {skip[0]}"
        raise Refused(cli.EXIT_BLOCKED, f"{what} carries [skip ci] on a code change (STANDING 4): add a commit without it")
    if skip:
        accepted.append(accept)
    if why_deep and not safe_because:
        raise Refused(cli.EXIT_BLOCKED, f"{why_deep}: name the one fact it is safe because of "
                                        '(--safe-because "<fact>"); a test in the PR must exercise it')
    if safe_because and not proves(changed):
        raise Refused(cli.EXIT_BLOCKED, "no test in the PR exercises the fact it is safe because of; RETURN it: "
                                        f"add a test that proves: {safe_because}")
    return accepted


def admit(project, pr, head, *, where, base, branch, unit=None, b=None, writer="", accept=None, first=False,
          by="auto", approved_by=None, surface="internal", safe_because="", dry_run=False, reg=None,
          cfg=None) -> dict:
    """§4.3 steps 5-10, the one way a PR becomes an entry (`orche queue add`, the head review): every
    check on `head` in the checkout `where` (Refused when one fails), then the entry pinned to `head` is
    queued (add); a new one waiting for the owner's word (land: owner) asks Main once, and the
    integrator is started. `b`: the unit's Brief (its parent, rev and VERIFY), None for a PR alone.
    `surface`: the head review's (R-REVIEW-12; `user` lands only on the check script's live-ui-verified);
    `safe_because`: the head's blast-radius fact (R-REVIEW-14). add's result plus `started`; dry_run:
    {"entry"}, nothing written."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    verify = verify_cmds(b.fields.get("VERIFY")) if b is not None else []
    accepted = checks(cfg, project, where, pr=pr, head=head, base=base, verify=verify, accept=accept,
                      tier=b.tier if b is not None else "standard", safe_because=safe_because)
    main = roster.Roster(reg).main(project)
    owner_ok = land_mode(cfg, project, reg) != "owner" or approved_in_log(project, pr, main.session if main else None)
    entry = {"id": entry_id(pr, head), "pr": pr, "head": head, "branch": branch, "unit": unit, "writer": writer,
             "parent": b.parent if b else "", "rev": b.rev if b else 0, "verify": verify,
             "approved_by": approved_by or f"{by} (review)", "accept": "; ".join(accepted) or None,
             "surface": surface, "first": bool(first), "owner_ok": owner_ok, "at": None, "state": "waiting",
             "restarts": 0, "drop": False, "test": None}
    if dry_run:
        return {"entry": entry}
    entry["at"] = util.now_iso()
    res = add(project, entry, first=first, by=by, base=base)
    if res["status"] == "already":
        return res
    if not owner_ok and main is not None:
        inbox.post(inbox.make(unit or f"pr{pr}", by, main.session, "question", via="queue",
                              summary=f"PR #{pr} ({unit or branch}) passed both reviews and waits for the owner's "
                                      f"word to land (land: owner): orche queue approve {project} #{pr}"))
    return {**res, "started": start(project) or running(project)}


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


def add(project, entry, *, first=False, by="auto", base=None) -> dict:
    """Queue `entry` (§4.3 steps 9-10); `base` (publish.base) is recorded when the queue has none yet.
    Same PR and head: {"status": "already", "position": k}. Another head of the same PR: the old entry
    is dropped (superseded) first. Returns {"status": "queued"|"already", "position", "total",
    "superseded": old entry or None}."""
    superseded = None
    with store.edit(project) as q:
        q["base"] = q.get("base") or base
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


def pause(project, *, by, reason=None, entry=None, front=None, unless_paused=False):
    """Pause the queue (`orche queue pause`; the integrator: by restarts, stops or github), `front` back to
    waiting at the front first; logged `paused`. The queue, or None when `unless_paused` found it paused."""
    with store.edit(project) as q:
        if front:
            _front(q, front)
        if unless_paused and q.get("paused"):
            return None
        q["paused"] = {"by": by, "at": util.now_iso(), "reason": reason or ""}
    store.log(project, "paused", entry, by=by, reason=reason)
    _events(f"queue paused project={project} by={by}")
    return q


def close(project, reason, *, main, log, entry=None, front=None, **fields) -> None:
    """Close the queue (main-red: only --first entries are tested; proof: nothing until `orche queue
    resume`), `front` back to waiting at the front first; logged `closed` with `fields`."""
    with store.edit(project) as q:
        if front:
            _front(q, front)
        q["closed"] = {"at": util.now_iso(), "reason": reason, "main": main, "log": str(log)}
    store.log(project, "closed", entry, reason=reason, **fields)
    _events(f"queue closed project={project} reason={reason} main={str(main or '-')[:7]}")


def reopen(project, green, *, clear=True, entry=None, **fields) -> bool:
    """Main is green at `green` (green_main); with `clear` (any green main gate; a merge only for a --first
    entry) a main-red closure ends, logged `opened` with `fields`. True when it reopened."""
    with store.edit(project) as q:
        q["green_main"] = green
        opened = bool(clear) and (q.get("closed") or {}).get("reason") == "main-red"
        if opened:
            q["closed"] = None
    if opened:
        store.log(project, "opened", entry, **fields)
        _events(f"queue opened project={project}")
    return opened


def stop(project) -> int:
    """R-QUEUE-9: one more pass in a row stopped (git, gh, the clone, a bug): the count (queue.json `stops`)."""
    with store.edit(project) as q:
        n = q["stops"] = units.num(q.get("stops")) + 1
    return n


def cleared(project) -> None:
    """A pass that got through: the count of passes stopped in a row back to 0."""
    try:
        if not units.num(store.read(project).get("stops")):
            return
    except util.JsonError:
        return
    with store.edit(project) as q:
        q["stops"] = 0


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


def take(project, blocked=False):
    """§5.3 step 1: the entry next_entry picks becomes `testing` with this process's test record, unless the
    queue is paused or `blocked` (flow-paused): (a copy of it or None, the queue)."""
    with store.edit(project) as q:
        e = None if q.get("paused") or blocked else next_entry(q)
        if e is not None:
            e["state"] = "testing"
            e["test"] = {"pid": os.getpid(), "started": util.now_iso(), "tag": f"{e['id']}@{time.time_ns()}",
                         "base": None, "candidate": None, "log": None, "pgid": None, "pgid_at": None, "pgid_cmd": None}
    return (dict(e) if e is not None else None), q


def set_test(project, eid, **fields) -> None:
    """Merge `fields` into the testing entry's test record (the gate's step, the candidate, a merge)."""
    with store.edit(project) as q:
        e = find(q, eid)
        if e is not None and isinstance(e.get("test"), dict):
            e["test"].update(fields)


def testing(project, entry, **test) -> None:
    """The entry's candidate is built: `test` (base, candidate, log) into its test record; logged `testing`."""
    set_test(project, entry["id"], **test)
    store.log(project, "testing", entry, base=test.get("base"), candidate=test.get("candidate"))


def _front(q, eid, restarts=False):
    e = find(q, eid)
    if e is not None:
        q["entries"].remove(e)
        e.update(state="waiting", test=None)
        if restarts:
            e["restarts"] = units.num(e.get("restarts")) + 1
        q["entries"].insert(0, e)
    return e


def front(project, eid, *, restarts=False, unless_landed=False, entry=None, event=None, **fields):
    """Entry `eid` back to waiting at the front (restarts + 1 when asked; one GitHub merged stays for its
    proof when `unless_landed`); `event` logged with `fields` (with `entry` when it is gone). It, or None."""
    with store.edit(project) as q:
        e = find(q, eid)
        e = None if e is None or (unless_landed and landed(e)) else _front(q, eid, restarts)
    if event:
        store.log(project, event, e or entry, **fields)
    return e


def lost(project, is_lost) -> list:
    """§5.10: each `testing` entry whose integrator is gone (`is_lost(test)`; one GitHub merged waits for its
    proof) goes back to the front, restarts + 1, logged `lost`: [(entry, its test record)]."""
    out = []
    with store.edit(project) as q:
        for e in [x for x in q["entries"] if x.get("state") == "testing"]:
            t = e.get("test") if isinstance(e.get("test"), dict) else {}
            if t.get("landed") or not is_lost(t):
                continue
            q["entries"].remove(e)
            e.update(state="waiting", test=None, restarts=units.num(e.get("restarts")) + 1)
            q["entries"].insert(len(out), e)
            out.append((e, t))
    for e, t in out:
        store.log(project, "lost", e, reason=f"pid {t.get('pid')}")
        _events(f"queue lost project={project} pr={e['pr']} pid={t.get('pid')}")
    return out


def mark(project, eid, **fields):
    """Merge `fields` into the entry `eid`; returns it (None when gone)."""
    with store.edit(project) as q:
        e = find(q, eid)
        if e is not None:
            e.update(fields)
    return e


def remove(project, eid, event, entry=None, **fields):
    """Take the entry `eid` out of queue.json and log `event` (merged, returned, dropped; with `entry` when
    it is gone already)."""
    with store.edit(project) as q:
        e = find(q, eid)
        if e is not None:
            q["entries"].remove(e)
    if e is not None or entry is not None:
        store.log(project, event, e if e is not None else entry, **fields)
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
    try:
        procs.spawn_orche(["queue", "run", project], store.folder(project) / "integrator.log")
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
    """The read model (`orche queue`, `orche status`, doctor, the heartbeat): waiting, testing, await
    (owner_ok false), merged/returned today, state (open|paused|closed), the gate's p50 seconds over the
    last 20 merges (None without), the entries and `last`, each unit's last word (merged, returned,
    dropped). Raises util.JsonError when queue.json is corrupt."""
    q = store.read(project)
    lines = store.read_log(project)
    entries = [e for e in (q.get("entries") if isinstance(q.get("entries"), list) else []) if isinstance(e, dict)]
    day = util.now_iso()[:10]
    secs = [ln["secs"] for ln in lines if ln.get("event") == "merged" and isinstance(ln.get("secs"), (int, float))][-20:]
    waiting = [e for e in entries if e.get("state") == "waiting"]
    return {"project": project, "waiting": len(waiting),
            "testing": sum(1 for e in entries if e.get("state") == "testing"),
            "await": sum(1 for e in waiting if not e.get("owner_ok")),
            "merged_today": sum(1 for ln in lines if ln.get("event") == "merged" and _today(ln, day)),
            "returned_today": sum(1 for ln in lines if ln.get("event") == "returned" and _today(ln, day)),
            "state": "closed" if q.get("closed") else "paused" if q.get("paused") else "open",
            "paused": q.get("paused"), "closed": q.get("closed"), "gate_p50": statistics.median(secs) if secs else None,
            "entries": entries,
            "last": {ln["unit"]: ln.get("event") for ln in lines
                     if ln.get("unit") and ln.get("event") in ("merged", "returned", "dropped")}}


def projects() -> list:
    """The projects that have a queue folder."""
    root = paths.orche_home() / "queue"
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def landed_heads(project) -> set:
    """The short heads the queue logged `merged` (an entry id is pr<N>-<head7>; a squash leaves no ancestor)."""
    return {str(ln.get("id") or "").rsplit("-", 1)[-1] for ln in store.read_log(project) if ln.get("event") == "merged"}


def closed_text(project, closed) -> str:
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
