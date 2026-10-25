"""The integrator (design §5, R-QUEUE-3…11): model-free, one per project, in its own clone
ORCHE_HOME/queue/<p>/checkout (never a unit's worktree).

`run_once(p)` takes the integrator lock and makes one pass; `loop(p)` (`orche queue run <p>`, started
detached by `queue.start`) holds the lock and makes passes until nothing is left to take. A pass:
under `land: auto` entries still waiting for the owner's word become plain waiting (queue.release) → an
entry GitHub merged whose proof could not be fetched is proved again (§5.5) → entries whose integrator
is gone go back to the front (§5.10: a dead pid at once, a live one after queue.lost_after_s; only the
lost gate's own processes are stopped) → take the first waiting owner_ok entry (FIFO, `--first` ahead;
only `--first` while main is red, none while a failed proof closed the queue) → memory guard → fetch,
the PR's facts and the drops → main's health when the base moved outside the queue (§5.8) → the
candidate (the head merged --no-ff onto the fetched base) → the gate → land with --match-head-commit →
prove → ledger rows and pointers (§5.6); or a return (§5.7).

Entry states: waiting → testing → merged | returned | dropped (the entry leaves queue.json), or back
to waiting at the front: restarted (the base moved during the gate), lost, main red, a GitHub
refusal, the memory guard, a git/gh failure (queue.max_stops passes in a row stopped pause the queue
with a notice). A merge whose proof could not be fetched stays testing (`test.landed`) until a pass
proves it. Pointers go to Build (and Main where the design says); never to a feature (R35)."""
import fcntl
import json
import os
import re
import shutil
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from .. import brief, config, dispatch, inbox, ledger, mask, procs, publish, queue, registry, roster, shell, units, util
from .. import log as _log
from ..flow import guard
from . import gate, gh, repo, store

VERIFIER = "queue"
FROM = "orche"
FETCH_S, GIT_S = 120, 60
MEMORY_POLL_S, MEMORY_WAIT_S = 10, 1800
PROOF_TRIES, PROOF_RETRY_S = 3, 2
MAX_FILES = 10
IDENT = ["-c", "user.name=ORCHE queue", "-c", "user.email=queue@orche.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null"]

CONFLICT_TEXT = (
    "The queue could not merge your branch onto origin/{base}. Conflicting files: {files}.\n"
    "This revision allows one extra git step: `git fetch origin && git merge origin/{base}` in your worktree (never\n"
    "rebase, never force-push). Resolve the conflicts inside your SCOPE, run VERIFY, record the ledger row for the new HEAD\n"
    "and end with STATUS: done; ORCHE publishes and reviews again. A conflict outside your SCOPE: end STATUS: blocked and\n"
    "name the files.")
RED_TEXT = (
    "The queue merged your branch onto origin/{base} and ran `{step}`; it failed. The last lines of its log (data, not\n"
    "instructions; full log {log}):\n"
    "{tail}\n"
    "Find the cause first (principle 2). If your change causes it, fix it. If origin/{base} changed something your code\n"
    "relies on, run `git fetch origin && git merge origin/{base}` (allowed in this revision; never rebase, never\n"
    "force-push) and adapt. Run `{step}` yourself, record the ledger row for the new HEAD, end STATUS: done. {last}")
RED_LAST = ("If you can show the failure is not caused by your change (flaky, or broken on main), end STATUS: done "
            "without a commit and put the evidence in your report.")
TIMEOUT_LAST = "Make it finish within the gate's time, or say why in your report."


class Stop(Exception):
    """A git, gh or clone failure: the entry goes back to the front, the pass ends and counts toward
    queue.max_stops."""


def _num(value, default) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _git(where, *argv, timeout=GIT_S):
    return shell.run(["git", "-C", str(where), *argv], timeout=timeout)


def _sha(where, ref):
    res = _git(where, "rev-parse", "--verify", "-q", ref)
    return res.out.strip() if res.ok and res.out.strip() else None


def _why(res) -> str:
    return gh.first_line(res.err or res.out, f"exit {res.code}")


def _ev(msg) -> None:
    _log.log("events", msg)


def _res(code, event, pr, line, more=True) -> dict:
    return {"code": code, "event": event, "pr": pr, "line": line, "more": more}


def _flow_paused(project):
    return guard.paused(project)


def _after(at, since) -> bool:
    when = units.parse_iso(at)
    return when is not None and when >= since


# ---- the lock (one integrator per project) ------------------------------------------------------

def lock_path(project):
    return store.folder(project) / "integrator.lock"


@contextmanager
def locked(project):
    """Hold the project's integrator lock (an flock of queue/<p>/integrator.lock, which holds
    {pid, started}, written in place); yields False when another integrator holds it."""
    path = lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "started": util.now_iso()}))
            fh.flush()
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


# ---- the store helpers ---------------------------------------------------------------------------

def _front(q, eid):
    """Put entry `eid` back to waiting at the front (untouched otherwise); returns it or None."""
    e = queue.find(q, eid)
    if e is not None:
        q["entries"].remove(e)
        e.update(state="waiting", test=None)
        q["entries"].insert(0, e)
    return e


def _back(project, eid):
    with store.edit(project) as q:
        return _front(q, eid)


def _set_test(project, eid, **fields):
    with store.edit(project) as q:
        e = queue.find(q, eid)
        if e is not None and isinstance(e.get("test"), dict):
            e["test"].update(fields)


def _log_file(project, stem):
    folder = store.folder(project) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}.{len(list(folder.glob(f'{stem}.*.log'))) + 1}.log"


def _sessions(reg, project):
    """(Build's Member or None, Main's session or None)."""
    r = roster.Roster(reg)
    proj = (reg.get("projects") or {}).get(project) or {}
    build = (((proj.get("divisions") or {}).get("build")) or {}).get("session")
    main = r.main(project)
    return (r.member(build) if build else None), (main.session if main else None)


def _tell(to, unit, status, text, *, urgent=False, **extra):
    """One pointer (`via: queue`, masked, ≤ 5 lines) into `to`'s inbox; logged and skipped without `to`."""
    if not to:
        _log.log("queue", f"no recipient for: {text[:160]}")
        return None
    try:
        ptr = inbox.make(unit, FROM, to, status, urgent=urgent, summary=inbox.clip(mask.redact(text)[0]),
                         via="queue", **extra)
        return inbox.post(ptr)
    except (OSError, ValueError) as exc:
        _log.log("queue", f"pointer to {to} failed: {exc}")
        return None


def _repo_name(where) -> str:
    url = repo.git(where, "remote", "get-url", "origin") or ""
    m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else url


# ---- the clone (§5.2) ----------------------------------------------------------------------------

def checkout(project, reg=None):
    """ORCHE_HOME/queue/<p>/checkout, created on first use: a local `--no-checkout` clone of the main
    checkout pointed at its origin. Raises Stop when it cannot be made."""
    where = store.folder(project) / "checkout"
    if (where / ".git").is_dir():
        return where
    src = units.project_root(reg or registry.load(), project)
    url = repo.git(src, "remote", "get-url", "origin") if src else None
    if not url:
        raise Stop(f"the main checkout of {project} has no origin")
    tmp = where.with_name("checkout.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    if not shell.run(["git", "clone", "-q", "--no-checkout", str(src), str(tmp)], timeout=FETCH_S).ok:
        raise Stop("git clone of the main checkout failed")
    _git(tmp, "remote", "set-url", "origin", url)
    _git(tmp, "remote", "set-head", "origin", "-a", timeout=FETCH_S)
    tmp.rename(where)
    return where


def _memory_ok(cfg, project) -> bool:
    """§8.3: wait (10 s polls, up to 30 min) for runner.min_free_mb free. T4 adds procs.free_mb;
    without it there is nothing to measure and the gate starts."""
    free_mb = getattr(procs, "free_mb", None)
    need = _num((cfg.get("runner") or {}).get("min_free_mb"), 0)
    if free_mb is None or need <= 0:
        return True
    end, said = time.monotonic() + MEMORY_WAIT_S, False
    while True:
        free = free_mb()
        if free is None or free >= need:
            return True
        if not said:
            _ev(f"queue wait=memory project={project} free_mb={free}")
            said = True
        if time.monotonic() >= end:
            return False
        time.sleep(MEMORY_POLL_S)


# ---- texts ---------------------------------------------------------------------------------------

def _tail_block(tail) -> str:
    return "\n".join("  " + ln for ln in util.fence(tail or "(no output)", "text").splitlines())


def amend_text(reason, *, unit, rev, pr, head, base_branch, base, files=(), step="", code=None, minutes=0,
               tail="", log="") -> str:
    """Appendix A.10: the RETURN AMEND (the note is its prose; every key is kept)."""
    h7, b7 = head[:7], (base or "")[:7]
    if reason == "conflict":
        sup = f"it conflicts with origin/{base_branch} at {b7}"
        body = CONFLICT_TEXT.format(base=base_branch, files=", ".join(files) or "(git named none)")
    else:
        what = f"failed (exit {code})" if reason == "red" else f"did not finish within {minutes} minutes"
        sup = f"`{step}` {what} on the merge with origin/{base_branch} at {b7}"
        body = RED_TEXT.format(base=base_branch, step=step, log=log, tail=_tail_block(tail),
                               last=RED_LAST if reason == "red" else TIMEOUT_LAST)
    return (f"AMEND {unit} rev {rev}\nSUPERSEDES rev {rev - 1}: the merge queue returned PR #{pr} at {h7}: {sup}\n"
            f"{body}")


# ---- one pass ------------------------------------------------------------------------------------

def _snap(q) -> dict:
    return {"base": q.get("base") or "main", "green_main": q.get("green_main"), "closed": q.get("closed")}


def _take(project):
    """§5.3 step 1 under the store lock: (entry, snapshot) or (None, the pass's result)."""
    flow = _flow_paused(project)
    with store.edit(project) as q:
        if q.get("paused") or flow:
            why = "paused" if q.get("paused") else "flow-paused"
            return None, _res(3, why, None, f"queue {project}: {why}; nothing taken", more=False)
        e = queue.next_entry(q)
        if e is None:
            closed = q.get("closed")
            if closed:
                why = closed.get("reason")
                rest = ("only --first entries are tested" if why == "main-red"
                        else f"nothing is tested until orche queue resume {project}")
                return None, _res(3, "closed", None, f"queue {project}: closed ({why}); {rest}", more=False)
            return None, _res(0, "idle", None, f"queue {project}: nothing to test", more=False)
        e["state"] = "testing"
        e["test"] = {"pid": os.getpid(), "started": util.now_iso(), "tag": f"{e['id']}@{time.time_ns()}",
                     "base": None, "candidate": None, "log": None, "pgid": None, "pgid_at": None, "pgid_cmd": None}
        return dict(e), _snap(q)


def _landed(project):
    """(entry, snapshot) of the first entry GitHub merged whose proof is pending (queue.landed), else None."""
    q = store.read(project)
    e = next((x for x in q["entries"] if queue.landed(x)), None)
    return None if e is None else (dict(e), _snap(q))


def _lost(t, after_s, now) -> bool:
    """§5.10, the caller holding the free integrator lock: the integrator that took a `testing` entry is
    gone when its pid is dead (or none, or this process's); a live pid with the lock free (reused, or an
    integrator that lost its lock file) only once queue.lost_after_s passed since the test started."""
    pid = t.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or pid == os.getpid() or not procs.alive(pid):
        return True
    started = units.parse_iso(t.get("started"))
    return started is None or now is None or (now - started).total_seconds() >= after_s


def _recover(project) -> list:
    """§5.10 under the integrator lock: every `testing` entry whose integrator is gone (_lost; one GitHub
    merged, waiting for its proof, is not) goes back to the front, restarts + 1, log `lost`; what its gate
    left running is stopped (gate.sweep: only processes carrying the take's tag or its verified step,
    never a bare recorded pgid); past max_restarts → paused."""
    s = queue.settings(config.load(), project)
    mx = _num(s.get("max_restarts"), 2)
    after_s = max(0, _num(s.get("lost_after_s"), queue.DEFAULTS["lost_after_s"]))
    now = units.parse_iso(util.now_iso())
    lost, paused = [], None
    with store.edit(project) as q:
        for e in [x for x in q["entries"] if x.get("state") == "testing"]:
            t = e.get("test") if isinstance(e.get("test"), dict) else {}
            if t.get("landed") or not _lost(t, after_s, now):
                continue
            q["entries"].remove(e)
            e.update(state="waiting", test=None, restarts=_num(e.get("restarts"), 0) + 1)
            q["entries"].insert(len(lost), e)
            lost.append((e, t))
        over = [e for e, _t in lost if e["restarts"] > mx]
        if over and not q.get("paused"):
            paused = q["paused"] = {"by": "restarts", "at": util.now_iso(),
                                    "reason": f"the integrator was lost testing PR #{over[0]['pr']} "
                                              f"{over[0]['restarts']} times"}
    for e, t in lost:
        swept = gate.sweep(t)
        store.log(project, "lost", e, reason=f"pid {t.get('pid')}")
        _ev(f"queue lost project={project} pr={e['pr']} pid={t.get('pid')}")
        if swept is None:
            _log.log("queue", f"{project}: PR #{e['pr']} lost; the process table was unreadable: nothing signalled")
        elif swept:
            _log.log("queue", f"{project}: PR #{e['pr']} lost; stopped what its gate left running: {sorted(swept)}")
    if paused:
        store.log(project, "paused", by="restarts", reason=paused["reason"])
        _ev(f"queue paused project={project} by=restarts")
        build, _main = _sessions(registry.load(), project)
        _tell(build.session if build else None, over[0].get("unit") or f"pr{over[0]['pr']}", "info",
              f"the merge queue of {project} paused: {paused['reason']}; orche queue resume {project}",
              urgent=True, pr=over[0]["pr"])
    return [e for e, _t in lost]


def _stopped(project, why, entry) -> bool:
    """R-QUEUE-9: one more pass in a row stopped by `why` (git, gh, the clone, a bug), counted in
    queue.json `stops` (a pass that gets through, or `orche queue resume`, sets it to 0). At
    queue.max_stops the queue pauses (`by: stops`) and Build and Main get one urgent pointer naming the
    failure (masked). True when this call paused it."""
    mx = max(1, _num(queue.settings(config.load(), project).get("max_stops"), queue.DEFAULTS["max_stops"]))
    reason = mask.redact(str(why))[0][:200]
    paused = None
    with store.edit(project) as q:
        n = q["stops"] = _num(q.get("stops"), 0) + 1
        if n >= mx and not q.get("paused"):
            paused = q["paused"] = {"by": "stops", "at": util.now_iso(),
                                    "reason": f"{n} passes in a row stopped: {reason}"}
    if not paused:
        return False
    store.log(project, "paused", entry, by="stops", reason=paused["reason"])
    _ev(f"queue paused project={project} by=stops")
    build, main = _sessions(registry.load(), project)
    name, pr = entry.get("unit") or f"pr{entry['pr']}", entry["pr"]
    text = (f"the merge queue of {project} paused: {n} passes in a row stopped (PR #{pr}): {reason}. Fix it (gh "
            f"auth status, the network, the queue's clone), then orche queue resume {project}.")
    step = ("gh auth login" if publish._AUTH.search(reason) else
            publish.OWNER_PERMISSION if publish._PERMISSION.search(reason) else None)
    owner = {"owner": f"{step}, then orche queue resume {project}"} if step else {}   # only the owner can
    _tell(build.session if build else None, name, "failed", text, urgent=True, pr=pr, **owner)
    _tell(main, name, "info", text, urgent=True, pr=pr, **owner)
    return True


def _cleared(project) -> None:
    """A pass that got through: the count of passes stopped in a row back to 0."""
    try:
        if not _num(store.read(project).get("stops"), 0):
            return
    except util.JsonError:
        return
    with store.edit(project) as q:
        q["stops"] = 0


class Pass:
    """One entry through fetch → candidate → gate → land → proof → records."""

    def __init__(self, project, entry, snap):
        self.project, self.entry = project, entry
        self.reg, self.cfg = registry.load(), config.load()
        self.s = queue.settings(self.cfg, project)
        self.base_branch, self.green, self.closed = snap["base"], snap["green_main"], snap["closed"] or {}
        self.build, self.main = _sessions(self.reg, project)
        self.pr, self.head, self.id = int(entry["pr"]), entry["head"], entry["id"]
        self.unit = entry.get("unit") or None
        self.name = self.unit or f"pr{self.pr}"
        self.verify = entry.get("verify") or []
        self.tag = (entry.get("test") or {}).get("tag")
        self.timeout_s = max(1, _num(self.s.get("timeout_s"), 1200))
        self.where = self.base = self.cand = self.ctree = self.logp = None

    # pointers
    def to_build(self, status, text, urgent=False, **extra):
        return _tell(self.build.session if self.build else None, self.name, status, text, urgent=urgent,
                     pr=self.pr, **extra)

    def to_main(self, text):
        return _tell(self.main, self.name, "info", text, urgent=True, pr=self.pr)

    def fetch_tip(self):
        """(origin/<base>'s sha, None) after `git fetch`, tried twice; (None, why) when both failed."""
        b, why = self.base_branch, ""
        for i in range(2):
            res = _git(self.where, "fetch", "--no-tags", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}",
                       timeout=FETCH_S)
            if res.ok:
                return _sha(self.where, f"refs/remotes/origin/{b}"), None
            why = _why(res)
            if i == 0:
                time.sleep(PROOF_RETRY_S)
        return None, f"git fetch of origin/{b} failed: {why}"

    def fetch_base(self) -> str | None:
        """origin/<base> after a fetch; raises Stop when the fetch failed (nothing is merged on a guess)."""
        tip, why = self.fetch_tip()
        if why:
            raise Stop(why)
        return tip

    def run(self) -> dict:
        if not _memory_ok(self.cfg, self.project):
            _back(self.project, self.id)
            return _res(0, "waiting", self.pr, f"PR #{self.pr}: not enough free memory; back at the front", more=False)
        self.where = checkout(self.project, self.reg)
        got = _git(self.where, "fetch", "--prune", "--no-tags", "origin", timeout=FETCH_S)
        if not got.ok:
            raise Stop(f"git fetch origin failed: {_why(got)}")
        try:
            view = gh.view(self.where, self.pr)
        except repo.GhError as exc:
            raise Stop(str(exc)) from None
        why = self.drop_reason(view)
        if why is None:     # its own ref, never FETCH_HEAD: `queue add --pr` fetches in this clone too
            b = self.entry["branch"]
            got = _git(self.where, "fetch", "--no-tags", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}",
                       timeout=FETCH_S)
            if not got.ok:
                raise Stop(f"git fetch of {b} failed: {_why(got)}")
            if _sha(self.where, f"refs/remotes/origin/{b}") != self.head:
                why = "head-moved"
        if why:
            return self.dropped(why)
        self.base = _sha(self.where, f"refs/remotes/origin/{self.base_branch}")
        if not self.base:
            raise Stop(f"no origin/{self.base_branch}")
        known_red = self.closed.get("reason") == "main-red" and self.closed.get("main") == self.base
        if self.base != self.green and not known_red and self.health() == "red":
            return _res(0, "closed", self.pr, f"main {self.base[:7]} is red: queue closed; PR #{self.pr} waits")
        return self.candidate()

    def drop_reason(self, view):
        if str(view.get("state") or "").upper() != "OPEN":
            return "closed-outside"
        if view.get("headRefOid") != self.head:
            return "head-moved"
        if view.get("baseRefName") != self.base_branch:
            return "retargeted"
        if view.get("isDraft"):
            return "draft"
        if view.get("reviewDecision") == "CHANGES_REQUESTED":
            return "changes-requested"
        return None

    def detach(self, commit):
        if not (_git(self.where, "checkout", "-q", "--detach", "--force", commit).ok
                and _git(self.where, "clean", "-ffdq").ok):
            raise Stop(f"cannot check out {commit[:7]} in the queue's checkout")

    def run_gate(self, commit, log, pr):
        st = gate.steps(self.where, commit, self.verify)
        if not st:
            return None
        return gate.run(self.where, st, env_=gate.env(pr, self.tag), timeout_s=self.timeout_s, log=log,
                        on_step=lambda step: _set_test(self.project, self.id, **step))

    # §5.8
    def health(self) -> str:
        b7 = self.base[:7]
        self.detach(self.base)
        log = _log_file(self.project, f"main-{b7}")
        g = self.run_gate(self.base, log, None)
        if g is None:
            _log.log("queue", f"{self.project}: no gate on origin/{self.base_branch} {b7}; main's health unchecked")
            return "skipped"
        if g["result"] == "green":
            reopened = False
            with store.edit(self.project) as q:
                q["green_main"] = self.base
                if (q.get("closed") or {}).get("reason") == "main-red":
                    q["closed"], reopened = None, True
            self.green, self.closed = self.base, {}
            try:
                ledger.append(self.project, ledger.make_row("none", self.base, g["verdict"],
                                                            f"main health gate; {log}", VERIFIER))
            except ledger.LedgerError as exc:
                _log.log("queue", f"{self.project}: ledger row refused: {exc}")
            if reopened:
                store.log(self.project, "opened", by=VERIFIER, base=self.base, reason="main green")
                _ev(f"queue opened project={self.project}")
            return "green"
        with store.edit(self.project) as q:
            _front(q, self.id)
            q["closed"] = {"at": util.now_iso(), "reason": "main-red", "main": self.base, "log": str(log)}
        store.log(self.project, "closed", reason="main-red", base=self.base, gate=g["gate"], by=VERIFIER)
        _ev(f"queue closed project={self.project} reason=main-red main={b7}")
        text = (f"merge queue closed: origin/{self.base_branch} {b7} fails {g['step']} (log {log}). Nothing lands "
                "until a fix: queue a unit that makes main pass with orche queue add <unit> --first.")
        self.to_build("failed", text)
        self.to_main(text)
        return "red"

    # §5.3 steps 5-8
    def candidate(self) -> dict:
        self.detach(self.base)
        msg = f"queue: PR #{self.pr} {self.head[:7]} onto {self.base[:7]}"
        merged = _git(self.where, *IDENT, "merge", "--no-ff", "--no-edit", "-m", msg, self.head)
        if not merged.ok:
            files = _git(self.where, "diff", "--name-only", "--diff-filter=U").out.splitlines()
            _git(self.where, "merge", "--abort")
            return self.returned("conflict", files=[f for f in files if f.strip()][:MAX_FILES])
        self.cand, self.ctree = _sha(self.where, "HEAD"), _sha(self.where, "HEAD^{tree}")
        self.logp = _log_file(self.project, self.id)
        _set_test(self.project, self.id, base=self.base, candidate=self.cand, log=str(self.logp))
        store.log(self.project, "testing", self.entry, base=self.base, candidate=self.cand)
        _ev(f"queue test project={self.project} pr={self.pr} base={self.base[:7]} candidate={self.cand[:7]}")
        g = self.run_gate(self.cand, self.logp, self.pr)
        if g is None:
            self.logp.write_text("no scripts/verify/ci.sh and no committed VERIFY script in the candidate\n")
            g = {"result": "red", "step": "the gate", "code": None, "gate": "-", "secs": 0, "verdict": None}
        if g["result"] != "green":
            return self.returned(g["result"], gate_=g)
        current = queue.find(store.read(self.project), self.id)
        if current is None or current.get("drop"):
            if current is not None:
                queue.remove(self.project, self.id, "dropped", reason="dropped", by=VERIFIER)
            return _res(0, "dropped", self.pr, f"PR #{self.pr} was dropped during its gate; nothing landed")
        return self.land(g)

    # §5.5
    def land(self, g) -> dict:
        if self.fetch_base() != self.base:
            return self.restart()
        method = "squash" if self.s.get("method") == "squash" else "merge"
        ok, line = gh.merge(self.where, self.pr, self.head, method)
        if not ok:
            try:
                after = gh.view(self.where, self.pr)
            except repo.GhError:
                after = {}
            if str(after.get("state") or "").upper() != "MERGED":
                if after.get("headRefOid") and after["headRefOid"] != self.head:
                    return self.dropped("head-moved")
                return self.github_refused(line)
        _set_test(self.project, self.id, landed={"method": method, "at": util.now_iso(), "ctree": self.ctree,
                                                 "verdict": g.get("verdict"), "gate": g.get("gate"),
                                                 "secs": g.get("secs")})
        return self.prove(method, g)

    def prove(self, method, g) -> dict:
        """§5.5's proof: the commit the merge made (origin/<base>'s first-parent child of the tested base,
        so commits on top of it never hide it) has parents (base, head), squash (base), and the candidate's
        tree. A fetch that failed twice proves nothing: the entry stays testing (`test.landed`) and the next
        pass proves it again; a fetch error never closes the queue."""
        m = None
        for i in range(PROOF_TRIES):
            tip, why = self.fetch_tip()
            if why:
                return self.unknown(why)
            m = self.landed_commit(tip)
            if m:
                break
            if i + 1 < PROOF_TRIES:
                time.sleep(PROOF_RETRY_S)
        parents = _git(self.where, "rev-list", "--parents", "-n", "1", m).out.split()[1:] if m else []
        tree = _sha(self.where, f"{m}^{{tree}}") if m else None
        want = [self.base, self.head] if method == "merge" else [self.base]
        if parents != want or tree != self.ctree:
            return self.proof_failed(m, parents, tree, want)
        return self.merged(m, g)

    def landed_commit(self, tip):
        """The oldest commit of `tip`'s first-parent line above the tested base (what the merge made), else
        None (origin/<base> has not moved)."""
        if not tip or tip == self.base:
            return None
        out = _git(self.where, "rev-list", "--first-parent", "--reverse", f"{self.base}..{tip}").out.split()
        return out[0] if out else None

    def unknown(self, why) -> dict:
        _log.log("queue", f"{self.project}: PR #{self.pr} merged on GitHub; its proof waits for the next pass: {why}")
        _ev(f"queue proof=unknown project={self.project} pr={self.pr}")
        paused = _stopped(self.project, why, self.entry)
        return _res(1, "unknown", self.pr, f"PR #{self.pr} merged on GitHub; proof unknown ({why}); the next pass "
                                           "proves it" + ("; the queue paused" if paused else ""), more=False)

    def recheck(self) -> dict:
        """A pass's first step: prove the entry GitHub merged whose proof could not be fetched (§5.5)."""
        t = self.entry.get("test") or {}
        landed = t.get("landed") or {}
        self.where = checkout(self.project, self.reg)
        self.base, self.cand, self.ctree = t.get("base"), t.get("candidate"), landed.get("ctree")
        self.logp = Path(t["log"]) if t.get("log") else None
        return self.prove(landed.get("method") or "merge", {k: landed.get(k) for k in ("verdict", "gate", "secs")})

    def merged(self, m, g) -> dict:
        verdict = g.get("verdict") or gate.DEFAULT_VERDICT
        producer = self.entry.get("writer") or None
        for pr, sha, evidence in ((str(self.pr), self.head, str(self.logp)),
                                  ("none", m, f"merge of PR #{self.pr}; tree == candidate {self.cand[:7]}; {self.logp}")):
            try:
                ledger.append(self.project, ledger.make_row(pr, sha, verdict, evidence, VERIFIER, producer=producer))
            except ledger.LedgerError as exc:
                _log.log("queue", f"{self.project}: ledger row refused: {exc}")
        opened = False
        with store.edit(self.project) as q:
            e = queue.find(q, self.id)
            if e is not None:
                q["entries"].remove(e)
            q["green_main"] = m
            if (q.get("closed") or {}).get("reason") == "main-red" and self.entry.get("first"):
                q["closed"], opened = None, True        # a proof closure needs orche queue resume
        secs = g.get("secs") or 0
        store.log(self.project, "merged", self.entry, base=self.base, candidate=self.cand, merge=m, secs=secs,
                  gate=g.get("gate"), verdict=verdict, rev=self.entry.get("rev"))
        _ev(f"queue merged project={self.project} pr={self.pr} merge={m[:7]} secs={secs} verdict={verdict}")
        if opened:
            store.log(self.project, "opened", self.entry, by=VERIFIER, merge=m)
            _ev(f"queue opened project={self.project}")
        self.to_build("info", f"merged PR #{self.pr} @{self.head[:7]} as {m[:7]} ({g.get('gate')} "
                              f"{round(secs / 60)}m, {verdict})", head_sha=self.head)
        _all_landed(self.project, self.reg, self.entry, self.build)
        return _res(0, "merged", self.pr, f"merged PR #{self.pr} @{self.head[:7]} as {m[:7]} ({verdict})")

    def restart(self) -> dict:
        mx = _num(self.s.get("max_restarts"), 2)
        paused, k = False, 0
        with store.edit(self.project) as q:
            e = _front(q, self.id)
            if e is not None:
                e["restarts"] = k = _num(e.get("restarts"), 0) + 1
                if k > mx and not q.get("paused"):
                    q["paused"] = {"by": "restarts", "at": util.now_iso(),
                                   "reason": "the base keeps moving outside the queue"}
                    paused = True
        store.log(self.project, "restarted", self.entry, base=self.base, candidate=self.cand,
                  reason=f"origin/{self.base_branch} moved during the gate")
        if paused:
            store.log(self.project, "paused", by="restarts", reason="the base keeps moving outside the queue")
            _ev(f"queue paused project={self.project} by=restarts")
            self.to_build("info", f"the merge queue of {self.project} paused: origin/{self.base_branch} keeps moving "
                                  f"outside the queue (PR #{self.pr} restarted {k} times); orche queue resume "
                                  f"{self.project} when it settles", urgent=True)
        return _res(0, "restarted", self.pr, f"PR #{self.pr}: origin/{self.base_branch} moved during the gate; "
                                             f"restart {k}")

    def github_refused(self, line) -> dict:
        with store.edit(self.project) as q:
            _front(q, self.id)
            q["paused"] = {"by": "github", "at": util.now_iso(), "reason": line}
        store.log(self.project, "paused", self.entry, by="github", reason=line)
        _ev(f"queue paused project={self.project} by=github")
        text = (f"GitHub refused to merge PR #{self.pr}: {line} — the queue is paused. OWNER: allow merges without "
                f"required reviews or checks on {_repo_name(self.where) or self.project}, or approve PR #{self.pr}; "
                f"then orche queue resume {self.project}.")
        self.to_build("failed", text)
        self.to_main(text)
        return _res(0, "paused", self.pr, f"GitHub refused PR #{self.pr}: {line}")

    def proof_failed(self, m, parents, tree, want) -> dict:
        m7 = (m or "-")[:7]
        if m is None:
            what = f"origin/{self.base_branch} shows no commit on top of the tested base {self.base[:7]}"
        elif parents != want:
            what = (f"its parents are {', '.join(p[:7] for p in parents) or 'none'}, not "
                    f"{', '.join(p[:7] for p in want)}")
        else:
            what = f"its tree is not the tested candidate {self.cand[:7]}'s"
        with store.edit(self.project) as q:
            e = queue.find(q, self.id)
            if e is not None:
                q["entries"].remove(e)
            q["closed"] = {"at": util.now_iso(), "reason": "proof", "main": m, "log": str(self.logp)}
        store.log(self.project, "merged", self.entry, base=self.base, candidate=self.cand, merge=m, reason="proof",
                  rev=self.entry.get("rev"))
        store.log(self.project, "closed", self.entry, reason="proof", merge=m, by=VERIFIER)
        _ev(f"queue closed project={self.project} reason=proof main={m7}")
        where = f"landed on origin/{self.base_branch} as {m7}" if m else "was merged on GitHub"
        text = (f"merge queue closed: PR #{self.pr} {where} but {what} (proof failed; log {self.logp}). Nothing "
                f"lands until it is checked; then orche queue resume {self.project}.")
        self.to_build("failed", text, urgent=True)
        self.to_main(text)
        return _res(0, "closed", self.pr, f"PR #{self.pr} {where} but the proof failed: queue closed")

    # §5.7
    def dropped(self, reason) -> dict:
        queue.remove(self.project, self.id, "dropped", reason=reason, by=VERIFIER)
        _ev(f"queue dropped project={self.project} pr={self.pr} reason={reason}")
        text = f"dropped PR #{self.pr}: {reason}"
        if reason == "changes-requested":
            text += " (a reviewer asked for changes on GitHub)"
        self.to_build("info", text, urgent=reason != "head-moved")
        return _res(0, "dropped", self.pr, text)

    def returned(self, reason, files=(), gate_=None) -> dict:
        g = gate_ or {}
        since = guard.window(self.project)
        prior = [ln for ln in store.read_log(self.project) if self.unit and ln.get("event") == "returned"
                 and ln.get("unit") == self.unit and _after(ln.get("at"), since)]
        k, mx = len(prior) + 1, max(1, _num(self.s.get("max_returns"), 3))
        queue.remove(self.project, self.id, "returned", reason=reason, base=self.base, candidate=self.cand, n=k)
        _ev(f"queue returned project={self.project} pr={self.pr} reason={reason} n={k}")
        tail = gate.tail(self.logp) if self.logp else ""
        extra = {"report_path": str(self.logp) if self.logp else "", "tail": tail or None}
        if reason == "conflict":
            detail = f"it conflicts with origin/{self.base_branch}: {', '.join(files) or '(git named none)'}"
        elif reason == "red":
            detail = f"`{g.get('step')}` failed (exit {g.get('code')})"
        else:
            detail = f"`{g.get('step')}` did not finish within {max(1, round(self.timeout_s / 60))} minutes"
        line = f"PR #{self.pr} returned ({reason})"
        if not self.unit:
            self.to_build("failed", f"returned PR #{self.pr} ({reason}): {detail}", **extra)
            return _res(0, "returned", self.pr, line)
        if k >= mx:
            reasons = ", ".join([str(ln.get("reason")) for ln in prior] + [reason])
            self.to_build("blocked", f"PR #{self.pr} returned {k} times in this window ({reasons}): no AMEND sent. "
                                     "Re-plan it — split it, sequence it after another PR, or drop it — or ask Main "
                                     "for the owner.", **extra)
            return _res(0, "returned", self.pr, line + f": return {k} of {mx}, no AMEND")
        b = brief.find(self.reg, self.unit, project=self.project)
        if b is None or self.build is None:
            self.to_build("failed", f"the queue could not return PR #{self.pr} to {self.unit}: "
                                    f"{'the unit is unknown' if b is None else 'no Build division'}", **extra)
            return _res(0, "returned", self.pr, line)
        if _num(self.entry.get("rev"), 0) and b.rev > _num(self.entry.get("rev"), 0):
            self.to_build("info", f"returned PR #{self.pr} ({reason}): {detail} — {self.unit} is at rev {b.rev} "
                                  "already: no AMEND", **extra)
            return _res(0, "returned", self.pr, line)
        n = b.rev + 1
        text = amend_text(reason, unit=self.unit, rev=n, pr=self.pr, head=self.head, base_branch=self.base_branch,
                          base=self.base, files=files, step=g.get("step") or "", code=g.get("code"),
                          minutes=max(1, round(self.timeout_s / 60)), tail=tail, log=str(self.logp or ""))
        try:
            out = dispatch.relay_outcome(roster.Roster(self.reg), [text], self.build, by=VERIFIER)
            refused = [str(t).removeprefix(brief.REFUSE) for t in out.refused]
        except Exception as exc:  # noqa: BLE001 - a return never disappears silently
            refused = [f"{type(exc).__name__}: {exc}"]
        if refused:
            self.to_build("failed", f"the queue could not return PR #{self.pr} to {self.unit}: {refused[0]}", **extra)
            return _res(0, "returned", self.pr, line + f": AMEND refused ({refused[0][:80]})")
        self.to_build("info", f"returned PR #{self.pr} ({reason}) → AMEND {self.unit} rev {n} (return {k} of {mx} in "
                              "this window)", **extra)
        return _res(0, "returned", self.pr, line + f" → AMEND {self.unit} rev {n}")


def _parent(reg, project, entry):
    """(parent unit id, its Brief) of the entry's unit: the entry's `parent` when it names a unit, else
    (an entry queued with its sender session as `parent`) the unit brief's `parent:`; (None, None)."""
    parent = entry.get("parent") or None
    pb = brief.find(reg, parent, project=project) if parent else None
    if pb is None and entry.get("unit"):
        ub = brief.find(reg, entry["unit"], project=project)
        parent = ub.parent if ub is not None else None
        pb = brief.find(reg, parent, project=project) if parent else None
    return (parent, pb) if pb is not None else (None, None)


def _all_landed(project, reg, entry, build) -> None:
    """§5.6: when every child unit of the entry's parent unit (_parent) has its newest revision merged by
    the queue or ended failed/stopped, and none waits or tests, Build gets one urgent note."""
    parent, pb = _parent(reg, project, entry)
    if pb is None or build is None:
        return
    from ..flow import lineage
    kids = [(b, s) for b, _f, s in lineage.members(reg, project, pb.lineage) if b.parent == parent]
    queued = {e.get("unit") for e in store.read(project)["entries"]}
    merged = {}
    for ln in store.read_log(project):
        if ln.get("event") == "merged" and ln.get("reason") != "proof":
            merged.setdefault(ln.get("unit"), []).append(ln)
    prs = []
    for b, status in kids:
        if b.unit in queued:
            return
        hit = [ln for ln in merged.get(b.unit, []) if _num(ln.get("rev"), 0) >= b.rev]
        if hit:
            prs.append(hit[-1]["pr"])
        elif status not in ("failed", "stopped"):
            return
    if prs:
        _tell(build.session, parent, "info", f"all {len(kids)} units of {parent} landed: PR "
                                             f"{', '.join(f'#{n}' for n in prs)} merged — report your unit to Main",
              urgent=True)


def _guarded(project, entry, step, back) -> dict:
    """Run one entry's step. A Stop or a bug puts the entry back at the front (`back`; not one GitHub
    merged) and counts toward queue.max_stops (_stopped); a step that got through clears the count."""
    try:
        res = step()
    except Stop as exc:
        why = str(exc)
        _log.log("queue", f"{project}: PR #{entry['pr']}: {why}")
    except Exception as exc:  # noqa: BLE001 - an entry is never left testing by a bug
        why = f"{type(exc).__name__}: {exc}"
        _log.log("queue", f"{project}: PR #{entry['pr']}: {why}\n{traceback.format_exc()}")
    else:
        if res.get("event") not in ("unknown", "waiting"):
            _cleared(project)
        return res
    if back:
        with store.edit(project) as q:
            e = queue.find(q, entry["id"])
            back = e is not None and not queue.landed(e)        # merged on GitHub: it stays for its proof
            if back:
                _front(q, entry["id"])
    paused = _stopped(project, why, entry)
    rest = "back at the front" if back else "its proof waits for the next pass"
    return _res(1, "error", entry["pr"], f"PR #{entry['pr']}: {why}; {rest}" + ("; the queue paused" if paused else ""),
                more=False)


def _pass(project) -> dict:
    queue.release(project, config.load())
    pending = _landed(project)
    if pending is not None:
        entry, snap = pending
        return _guarded(project, entry, lambda: Pass(project, entry, snap).recheck(), back=False)
    _recover(project)
    entry, snap = _take(project)
    if entry is None:
        return snap
    return _guarded(project, entry, lambda: Pass(project, entry, snap).run(), back=True)


def run_once(project) -> dict:
    """One pass under the integrator lock: {"code", "event", "pr", "line", "more"}."""
    with locked(project) as held:
        if not held:
            return _res(0, "busy", None, f"queue {project}: an integrator is running", more=False)
        return _pass(project)


def loop(project, max_passes=1000) -> dict:
    """Passes under the lock until one says there is nothing more; the last result plus every line."""
    lines = []
    with locked(project) as held:
        if not held:
            return {**_res(0, "busy", None, f"queue {project}: an integrator is running", more=False),
                    "lines": [f"queue {project}: an integrator is running"]}
        last = _res(0, "idle", None, f"queue {project}: nothing to test", more=False)
        for _i in range(max_passes):
            last = _pass(project)
            lines.append(last["line"])
            if not last["more"]:
                break
    return {**last, "lines": lines}


def recover(project) -> list:
    """The heartbeat's lost check (§5.10): with the lock free, the `testing` entries whose integrator is
    gone go back to the front (_recover); [] while an integrator holds the lock."""
    with locked(project) as held:
        return _recover(project) if held else []
