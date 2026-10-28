"""The integrator (design §5, R-QUEUE-3…11): model-free, one per project, in its own clone
ORCHE_HOME/queue/<p>/checkout (never a unit's worktree).

`run_once(p)` takes the integrator lock and makes one pass; `loop(p)` (`orche queue run <p>`, started
detached by `queue.start`) holds the lock and makes passes until nothing is left to take. A pass:
under `land: auto` entries still waiting for the owner's word become plain waiting (queue.release) → an
entry GitHub merged whose proof could not be fetched is proved again (§5.5) → entries whose integrator
is gone go back to the front (§5.10: a dead pid at once, a live one after queue.lost_after_s; only the
lost gate's own processes are stopped) → take the first waiting owner_ok entry (FIFO, `--first` ahead;
only `--first` while main is red, none while a failed proof closed the queue) → memory guard → the
clone's stale git locks removed → fetch, the PR's facts and the drops → main's health when the base moved
outside the queue (§5.8) → the candidate (the head merged --no-ff onto the fetched base) → the gate →
land with --match-head-commit → prove → ledger rows and pointers (§5.6); or a return (§5.7). An entry the
head review found user-visible (`surface: user`) lands only when the gate's check script said live-ui-verified;
less returns it (R-REVIEW-12). A team project (registry.team) skips that live rule: the team's finish step shows
the change on the issue before Main says ready (R-TEAM-2).

Entry states: waiting → testing → merged | returned | dropped (the entry leaves queue.json), or back
to waiting at the front: restarted (the base moved during the gate), lost, main red, a GitHub
refusal, the memory guard, a git/gh failure (MAX_STOPS passes in a row stopped pause the queue
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

from .. import brief, config, dispatch, github, inbox, ledger, mask, procs, publish, queue, registry, roster, shell, templates, units, util
from .. import log as _log
from ..flow import guard
from . import gate, repo, store

VERIFIER = "queue"
FROM = "orche"
FETCH_S = 120
MEMORY_POLL_S, MEMORY_WAIT_S = 10, 1800
PROOF_TRIES, PROOF_RETRY_S = 3, 2
MAX_FILES = 10
METHOD = "squash"        # gh pr merge --squash (github.merge), the `landed` record's method
MAX_RETURNS = 3         # a unit's returns inside W before its head gets `blocked`
MAX_STOPS = 3           # passes in a row stopped by git/gh before the queue pauses
LIVE = "live-ui-verified"
LIVE_REASON = "user-visible change: live verification required (the check script's verdict was {level})"
IDENT = ["-c", "user.name=ORCHE queue", "-c", "user.email=queue@orche.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null"]


class Stop(Exception):
    """A git, gh or clone failure: the entry goes back to the front, the pass ends and counts toward
    MAX_STOPS."""


def _sha(where, ref):
    res = shell.git(where, "rev-parse", "--verify", "-q", ref)
    return res.out.strip() if res.ok and res.out.strip() else None


def _why(res) -> str:
    """git's first line; after a fetch's `From <url>` (the transfer worked, a ref update failed) its error line."""
    text = str(res.err or res.out or "")
    if text.lstrip().startswith("From "):
        hit = next((ln.strip() for ln in text.splitlines() if ln.lstrip().lower().startswith(("error:", "fatal:"))), None)
        if hit:
            return hit[:160]
    return github.first_line(res)


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


# ---- files and pointers ------------------------------------------------------------------------

def _log_file(project, stem):
    folder = store.folder(project) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}.{len(list(folder.glob(f'{stem}.*.log'))) + 1}.log"


def _sessions(reg, project, writer=None):
    """(the head of `writer` (an entry's writer session; roster.Roster.head: a worker's division, else
    Build) or None, Main's session or None). Without `writer`: Build, who keeps the queue-wide notes
    (a pause is traffic control, not one worker's business)."""
    r = roster.Roster(reg)
    main = r.main(project)
    return r.head(r.member(writer) if writer else None, project), (main.session if main else None)


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
    shell.git(tmp, "remote", "set-url", "origin", url)
    shell.git(tmp, "remote", "set-head", "origin", "-a", timeout=FETCH_S)
    tmp.rename(where)
    return where


STALE_LOCK_S = 60


def _unlock(where) -> None:
    """The locks a git killed mid-write (a lost integrator, a timeout) left in the clone: `.git/*.lock` and
    `.git/refs/**/*.lock`; each fails every later git that needs it, pass after pass, until the queue
    pauses. Called under the integrator lock before a pass's first git, as a CI runner clears them before
    each job (GitLab Runner's writeGitCleanup). A lock younger than STALE_LOCK_S stays: `orche queue add
    --pr` fetches into this clone without the integrator lock, and its git may be writing right now."""
    git_dir = Path(where) / ".git"
    now = time.time()
    stale = []
    for p in [*git_dir.glob("*.lock"), *(git_dir / "refs").rglob("*.lock")]:
        try:
            if now - p.stat().st_mtime < STALE_LOCK_S:
                continue
        except FileNotFoundError:
            continue
        p.unlink(missing_ok=True)
        stale.append(p)
    if stale:
        _log.log("queue", f"removed stale git locks in {where}: "
                          f"{', '.join(str(p.relative_to(git_dir)) for p in stale)}")


def _memory_ok(cfg, project) -> bool:
    """§8.3: wait (10 s polls, up to 30 min) for runner.min_free_mb free. T4 adds procs.free_mb;
    without it there is nothing to measure and the gate starts."""
    free_mb = getattr(procs, "free_mb", None)
    need = units.num((cfg.get("runner") or {}).get("min_free_mb"), 0)
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
               tail="", log="", level="") -> str:
    """Appendix A.10: the RETURN AMEND (the note is its prose; every key is kept)."""
    h7, b7 = head[:7], (base or "")[:7]
    if reason == "conflict":
        sup = f"it conflicts with origin/{base_branch} at {b7}"
        body = templates.load("queue/conflict.md", BASE=base_branch, FILES=", ".join(files) or "(git named none)")
    elif reason == "live":
        sup = LIVE_REASON.format(level=level)
        body = templates.load("queue/live.md", BASE=base_branch, LEVEL=level, LOG=log)
    else:
        what = f"failed (exit {code})" if reason == "red" else f"did not finish within {minutes} minutes"
        sup = f"`{step}` {what} on the merge with origin/{base_branch} at {b7}"
        last = templates.load("queue/red-last.md" if reason == "red" else "queue/timeout-last.md")
        body = templates.load("queue/red.md", BASE=base_branch, STEP=step, LOG=log, TAIL=_tail_block(tail), LAST=last)
    return (f"AMEND {unit} rev {rev}\nSUPERSEDES rev {rev - 1}: the merge queue returned PR #{pr} at {h7}: {sup}\n"
            f"{body}")


# ---- one pass ------------------------------------------------------------------------------------

def _snap(q) -> dict:
    return {"base": q.get("base"), "green_main": q.get("green_main"), "closed": q.get("closed")}


def _take(project):
    """§5.3 step 1 (queue.take): (entry, snapshot) or (None, the pass's result)."""
    flow = _flow_paused(project)
    e, q = queue.take(project, blocked=bool(flow))
    if e is not None:
        return e, _snap(q)
    if q.get("paused") or flow:
        why = "paused" if q.get("paused") else "flow-paused"
        return None, _res(3, why, None, f"queue {project}: {why}; nothing taken", more=False)
    closed = q.get("closed")
    if closed:
        why = closed.get("reason")
        rest = ("only --first entries are tested" if why == "main-red"
                else f"nothing is tested until orche queue resume {project}")
        return None, _res(3, "closed", None, f"queue {project}: closed ({why}); {rest}", more=False)
    return None, _res(0, "idle", None, f"queue {project}: nothing to test", more=False)


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
    merged, waiting for its proof, is not) goes back to the front (queue.lost); what its gate left running
    is stopped (gate.sweep: only processes carrying the take's tag or its verified step, never a bare
    recorded pgid); past max_restarts → paused."""
    s = queue.settings(config.load(), project)
    mx = units.num(s.get("max_restarts"), config.DEFAULTS["queue"]["max_restarts"])
    after_s = max(0, units.num(s["lost_after_s"], config.DEFAULTS["queue"]["lost_after_s"]))
    now = units.parse_iso(util.now_iso())
    lost = queue.lost(project, lambda t: _lost(t, after_s, now))
    for e, t in lost:
        swept = gate.sweep(t)
        if swept is None:
            _log.log("queue", f"{project}: PR #{e['pr']} lost; the process table was unreadable: nothing signalled")
        elif swept:
            _log.log("queue", f"{project}: PR #{e['pr']} lost; stopped what its gate left running: {sorted(swept)}")
    over = [e for e, _t in lost if e["restarts"] > mx]
    reason = f"the integrator was lost testing PR #{over[0]['pr']} {over[0]['restarts']} times" if over else ""
    if over and queue.pause(project, by="restarts", reason=reason, unless_paused=True):
        build, _main = _sessions(registry.load(), project)
        _tell(build.session if build else None, over[0].get("unit") or f"pr{over[0]['pr']}", "info",
              f"the merge queue of {project} paused: {reason}; orche queue resume {project}",
              urgent=True, pr=over[0]["pr"])
    return [e for e, _t in lost]


def _stopped(project, why, entry) -> bool:
    """R-QUEUE-9: one more pass in a row stopped by `why` (git, gh, the clone, a bug; queue.stop; a pass
    that gets through, or `orche queue resume`, sets the count to 0). At MAX_STOPS the queue pauses
    (`by: stops`) and Build and Main get one urgent pointer naming the failure (masked). True when this
    call paused it."""
    mx = MAX_STOPS
    reason = mask.redact(str(why))[0][:200]
    n = queue.stop(project)
    if n < mx or not queue.pause(project, by="stops", reason=f"{n} passes in a row stopped: {reason}", entry=entry,
                                 unless_paused=True):
        return False
    build, main = _sessions(registry.load(), project)
    name, pr = entry.get("unit") or f"pr{entry['pr']}", entry["pr"]
    text = (f"the merge queue of {project} paused: {n} passes in a row stopped (PR #{pr}): {reason}. Fix it (gh "
            f"auth status, the network, the queue's clone), then orche queue resume {project}.")
    step = ("gh auth login" if github.AUTH.search(reason) else
            publish.OWNER_PERMISSION if github.PERMISSION.search(reason) else None)
    owner = {"owner": f"{step}, then orche queue resume {project}"} if step else {}   # only the owner can
    _tell(build.session if build else None, name, "failed", text, urgent=True, pr=pr, **owner)
    _tell(main, name, "info", text, urgent=True, pr=pr, **owner)
    return True


class Pass:
    """One entry through fetch → candidate → gate → land → proof → records."""

    def __init__(self, project, entry, snap):
        self.project, self.entry = project, entry
        self.reg, self.cfg = registry.load(), config.load()
        self.s = queue.settings(self.cfg, project)
        self.base_branch, self.green, self.closed = snap["base"], snap["green_main"], snap["closed"] or {}
        if not self.base_branch:     # never a guess (R12): every add records publish.base
            raise Stop("the queue records no base branch")
        self.head, self.main = _sessions(self.reg, project, entry.get("writer"))
        self.build = _sessions(self.reg, project)[0]          # Build: the queue-wide (pause) notes
        self.pr, self.sha, self.id = int(entry["pr"]), entry["head"], entry["id"]   # sha: the PR's head commit
        self.unit = entry.get("unit") or None
        self.name = self.unit or f"pr{self.pr}"
        self.verify = entry.get("verify") or []
        self.tag = (entry.get("test") or {}).get("tag")
        self.timeout_s = max(1, units.num(self.s.get("timeout_s"), config.DEFAULTS["queue"]["timeout_s"]))
        self.where = self.base = self.cand = self.ctree = self.logp = None

    # pointers
    def to_head(self, status, text, urgent=False, **extra):
        return _tell(self.head.session if self.head else None, self.name, status, text, urgent=urgent,
                     pr=self.pr, **extra)

    def to_build(self, status, text, urgent=False):
        return _tell(self.build.session if self.build else None, self.name, status, text, urgent=urgent,
                     pr=self.pr)

    def to_main(self, text):
        return _tell(self.main, self.name, "info", text, urgent=True, pr=self.pr)

    def fetch_tip(self):
        """(origin/<base>'s sha, None) after `git fetch`, tried twice; (None, why) when both failed."""
        b, why = self.base_branch, ""
        for i in range(2):
            res = shell.git(self.where, "fetch", "--no-tags", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}",
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
            queue.front(self.project, self.id)
            return _res(0, "waiting", self.pr, f"PR #{self.pr}: not enough free memory; back at the front", more=False)
        self.where = checkout(self.project, self.reg)
        _unlock(self.where)
        got = shell.git(self.where, "fetch", "--prune", "--no-tags", "origin", timeout=FETCH_S)
        if not got.ok:
            raise Stop(f"git fetch origin failed: {_why(got)}")
        try:
            view = github.view(self.where, self.pr)
        except github.GhError as exc:
            raise Stop(str(exc)) from None
        why = self.drop_reason(view)
        if why is None:     # its own ref, never FETCH_HEAD: `queue add --pr` fetches in this clone too
            b = self.entry["branch"]
            got = shell.git(self.where, "fetch", "--no-tags", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}",
                       timeout=FETCH_S)
            if not got.ok:
                raise Stop(f"git fetch of {b} failed: {_why(got)}")
            if _sha(self.where, f"refs/remotes/origin/{b}") != self.sha:
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
        if view.get("headRefOid") != self.sha:
            return "head-moved"
        if view.get("baseRefName") != self.base_branch:
            return "retargeted"
        if view.get("isDraft"):
            return "draft"
        if view.get("reviewDecision") == "CHANGES_REQUESTED":
            return "changes-requested"
        return None

    def detach(self, commit):
        if not (shell.git(self.where, "checkout", "-q", "--detach", "--force", commit).ok
                and shell.git(self.where, "clean", "-ffdq").ok):
            raise Stop(f"cannot check out {commit[:7]} in the queue's checkout")

    def run_gate(self, commit, log, pr):
        st = gate.steps(self.where, commit, self.verify)
        if not st:
            return None
        return gate.run(self.where, st, env_=gate.env(pr, self.tag), timeout_s=self.timeout_s, log=log,
                        on_step=lambda step: queue.set_test(self.project, self.id, **step))

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
            queue.reopen(self.project, self.base, by=VERIFIER, base=self.base, reason="main green")
            self.green, self.closed = self.base, {}
            try:
                ledger.append(self.project, ledger.make_row("none", self.base, g["verdict"],
                                                            f"main health gate; {log}", VERIFIER))
            except ledger.LedgerError as exc:
                _log.log("queue", f"{self.project}: ledger row refused: {exc}")
            return "green"
        queue.close(self.project, "main-red", main=self.base, log=log, front=self.id, base=self.base, gate=g["gate"],
                    by=VERIFIER)
        text = (f"merge queue closed: origin/{self.base_branch} {b7} fails {g['step']} (log {log}). Nothing lands "
                "until a fix: queue a unit that makes main pass with orche queue add <unit> --first.")
        self.to_head("failed", text)
        self.to_main(text)
        return "red"

    # §5.3 steps 5-8
    def candidate(self) -> dict:
        self.detach(self.base)
        msg = f"queue: PR #{self.pr} {self.sha[:7]} onto {self.base[:7]}"
        merged = shell.git(self.where, *IDENT, "merge", "--no-ff", "--no-edit", "-m", msg, self.sha)
        if not merged.ok:
            files = shell.git(self.where, "diff", "--name-only", "--diff-filter=U").out.splitlines()
            shell.git(self.where, "merge", "--abort")
            return self.returned("conflict", files=[f for f in files if f.strip()][:MAX_FILES])
        self.cand, self.ctree = _sha(self.where, "HEAD"), _sha(self.where, "HEAD^{tree}")
        self.logp = _log_file(self.project, self.id)
        queue.testing(self.project, self.entry, base=self.base, candidate=self.cand, log=str(self.logp))
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
        if self.not_live(g):
            return self.returned("live", gate_=g)
        return self.land(g)

    def not_live(self, g) -> bool:
        """R-REVIEW-12: a user-visible entry whose gate ran the check script and got less than live-ui-verified.
        No check script in the gate: no level, nothing to hold it to."""
        if registry.team(self.reg, self.project):     # R-TEAM-2: the team's finish step showed it on the issue
            return False
        ran = gate.CI in str(g.get("gate") or "").split(" + ")
        return self.entry.get("surface") == "user" and ran and g.get("verdict") != LIVE

    # §5.5
    def land(self, g) -> dict:
        if self.fetch_base() != self.base:
            return self.restart()
        ok, line = github.merge(self.where, self.pr, self.sha)
        if not ok:
            try:
                after = github.view(self.where, self.pr)
            except github.GhError:
                after = {}
            if str(after.get("state") or "").upper() != "MERGED":
                if after.get("headRefOid") and after["headRefOid"] != self.sha:
                    return self.dropped("head-moved")
                return self.github_refused(line)
        queue.set_test(self.project, self.id, landed={"method": METHOD, "at": util.now_iso(), "ctree": self.ctree,
                                                 "verdict": g.get("verdict"), "gate": g.get("gate"),
                                                 "secs": g.get("secs")})
        return self.prove(g)

    def prove(self, g) -> dict:
        """§5.5's proof: the commit the squash merge made (origin/<base>'s first-parent child of the tested
        base, so commits on top of it never hide it) has the one parent base and the candidate's tree. A fetch that failed twice proves nothing: the entry stays testing (`test.landed`) and the next
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
        parents = shell.git(self.where, "rev-list", "--parents", "-n", "1", m).out.split()[1:] if m else []
        tree = _sha(self.where, f"{m}^{{tree}}") if m else None
        want = [self.base]
        if parents != want or tree != self.ctree:
            return self.proof_failed(m, parents, tree, want)
        return self.merged(m, g)

    def landed_commit(self, tip):
        """The oldest commit of `tip`'s first-parent line above the tested base (what the merge made), else
        None (origin/<base> has not moved)."""
        if not tip or tip == self.base:
            return None
        out = shell.git(self.where, "rev-list", "--first-parent", "--reverse", f"{self.base}..{tip}").out.split()
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
        _unlock(self.where)
        self.base, self.cand, self.ctree = t.get("base"), t.get("candidate"), landed.get("ctree")
        self.logp = Path(t["log"]) if t.get("log") else None
        return self.prove({k: landed.get(k) for k in ("verdict", "gate", "secs")})

    def merged(self, m, g) -> dict:
        verdict = g.get("verdict") or gate.DEFAULT_VERDICT
        producer = self.entry.get("writer") or None
        for pr, sha, evidence in ((str(self.pr), self.sha, str(self.logp)),
                                  ("none", m, f"merge of PR #{self.pr}; tree == candidate {self.cand[:7]}; {self.logp}")):
            try:
                ledger.append(self.project, ledger.make_row(pr, sha, verdict, evidence, VERIFIER, producer=producer))
            except ledger.LedgerError as exc:
                _log.log("queue", f"{self.project}: ledger row refused: {exc}")
        secs = g.get("secs") or 0
        queue.remove(self.project, self.id, "merged", entry=self.entry, base=self.base, candidate=self.cand, merge=m,
                     secs=secs, gate=g.get("gate"), verdict=verdict, rev=self.entry.get("rev"))
        _ev(f"queue merged project={self.project} pr={self.pr} merge={m[:7]} secs={secs} verdict={verdict}")
        queue.reopen(self.project, m, clear=bool(self.entry.get("first")), entry=self.entry, by=VERIFIER, merge=m)
        self.to_head("info", f"merged PR #{self.pr} @{self.sha[:7]} as {m[:7]} ({g.get('gate')} "
                              f"{round(secs / 60)}m, {verdict})", head_sha=self.sha)
        _all_landed(self.project, self.reg, self.entry, self.head)
        return _res(0, "merged", self.pr, f"merged PR #{self.pr} @{self.sha[:7]} as {m[:7]} ({verdict})")

    def restart(self) -> dict:
        mx = units.num(self.s.get("max_restarts"), config.DEFAULTS["queue"]["max_restarts"])
        e = queue.front(self.project, self.id, restarts=True, entry=self.entry, event="restarted", base=self.base,
                        candidate=self.cand, reason=f"origin/{self.base_branch} moved during the gate")
        k = e["restarts"] if e is not None else 0
        if k > mx and queue.pause(self.project, by="restarts", reason="the base keeps moving outside the queue",
                                  unless_paused=True):
            self.to_build("info", f"the merge queue of {self.project} paused: origin/{self.base_branch} keeps moving "
                                  f"outside the queue (PR #{self.pr} restarted {k} times); orche queue resume "
                                  f"{self.project} when it settles", urgent=True)
        return _res(0, "restarted", self.pr, f"PR #{self.pr}: origin/{self.base_branch} moved during the gate; "
                                             f"restart {k}")

    def github_refused(self, line) -> dict:
        queue.pause(self.project, by="github", reason=line, entry=self.entry, front=self.id)
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
        queue.remove(self.project, self.id, "merged", entry=self.entry, base=self.base, candidate=self.cand, merge=m,
                     reason="proof", rev=self.entry.get("rev"))
        queue.close(self.project, "proof", main=m, log=self.logp, entry=self.entry, merge=m, by=VERIFIER)
        where = f"landed on origin/{self.base_branch} as {m7}" if m else "was merged on GitHub"
        text = (f"merge queue closed: PR #{self.pr} {where} but {what} (proof failed; log {self.logp}). Nothing "
                f"lands until it is checked; then orche queue resume {self.project}.")
        self.to_head("failed", text, urgent=True)
        self.to_main(text)
        return _res(0, "closed", self.pr, f"PR #{self.pr} {where} but the proof failed: queue closed")

    # §5.7
    def dropped(self, reason) -> dict:
        queue.remove(self.project, self.id, "dropped", reason=reason, by=VERIFIER)
        _ev(f"queue dropped project={self.project} pr={self.pr} reason={reason}")
        text = f"dropped PR #{self.pr}: {reason}"
        if reason == "changes-requested":
            text += " (a reviewer asked for changes on GitHub)"
        self.to_head("info", text, urgent=reason != "head-moved")
        return _res(0, "dropped", self.pr, text)

    def returned(self, reason, files=(), gate_=None) -> dict:
        g = gate_ or {}
        since = guard.window(self.project)
        prior = [ln for ln in store.read_log(self.project) if self.unit and ln.get("event") == "returned"
                 and ln.get("unit") == self.unit and _after(ln.get("at"), since)]
        k, mx = len(prior) + 1, MAX_RETURNS
        queue.remove(self.project, self.id, "returned", reason=reason, base=self.base, candidate=self.cand, n=k,
                     step=g.get("step"))
        _ev(f"queue returned project={self.project} pr={self.pr} reason={reason} n={k}")
        tail = gate.tail(self.logp) if self.logp else ""
        extra = {"report_path": str(self.logp) if self.logp else "", "tail": tail or None}
        if reason == "conflict":
            detail = f"it conflicts with origin/{self.base_branch}: {', '.join(files) or '(git named none)'}"
        elif reason == "red":
            detail = f"`{g.get('step')}` failed (exit {g.get('code')})"
        elif reason == "live":
            detail = LIVE_REASON.format(level=g.get("verdict"))
        else:
            detail = f"`{g.get('step')}` did not finish within {max(1, round(self.timeout_s / 60))} minutes"
        line = f"PR #{self.pr} returned ({reason})"
        if not self.unit:
            self.to_head("failed", f"returned PR #{self.pr} ({reason}): {detail}", **extra)
            return _res(0, "returned", self.pr, line)
        if k >= mx:
            reasons = ", ".join([str(ln.get("reason")) for ln in prior] + [reason])
            self.to_head("blocked", f"PR #{self.pr} returned {k} times in this window ({reasons}): no AMEND sent. "
                                     "Re-plan it — split it, sequence it after another PR, or drop it — or ask Main "
                                     "for the owner.", **extra)
            return _res(0, "returned", self.pr, line + f": return {k} of {mx}, no AMEND")
        b = brief.find(self.reg, self.unit, project=self.project)
        if b is None or self.head is None:
            self.to_head("failed", f"the queue could not return PR #{self.pr} to {self.unit}: "
                                    f"{'the unit is unknown' if b is None else 'no head'}", **extra)
            return _res(0, "returned", self.pr, line)
        if units.num(self.entry.get("rev"), 0) and b.rev > units.num(self.entry.get("rev"), 0):
            self.to_head("info", f"returned PR #{self.pr} ({reason}): {detail} — {self.unit} is at rev {b.rev} "
                                  "already: no AMEND", **extra)
            return _res(0, "returned", self.pr, line)
        n = b.rev + 1
        text = amend_text(reason, unit=self.unit, rev=n, pr=self.pr, head=self.sha, base_branch=self.base_branch,
                          base=self.base, files=files, step=g.get("step") or "", code=g.get("code"),
                          minutes=max(1, round(self.timeout_s / 60)), tail=tail, log=str(self.logp or ""),
                          level=g.get("verdict") or "")
        try:
            out = dispatch.relay_outcome(roster.Roster(self.reg), [text], self.head, by=VERIFIER)
            refused = [str(t).removeprefix(brief.REFUSE) for t in out.refused]
        except Exception as exc:  # noqa: BLE001 - a return never disappears silently
            refused = [f"{type(exc).__name__}: {exc}"]
        if refused:
            self.to_head("failed", f"the queue could not return PR #{self.pr} to {self.unit}: {refused[0]}", **extra)
            return _res(0, "returned", self.pr, line + f": AMEND refused ({refused[0][:80]})")
        self.to_head("info", f"returned PR #{self.pr} ({reason}) → AMEND {self.unit} rev {n} (return {k} of {mx} in "
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


def _all_landed(project, reg, entry, head) -> None:
    """§5.6: when every child unit of the entry's parent unit (_parent) has its newest revision merged by
    the queue, or is another head's unit (a division: no PR by design) that reported done, or ended
    failed/stopped, and none waits or tests, the head (Build or the workers' division) gets one urgent note."""
    parent, pb = _parent(reg, project, entry)
    if pb is None or head is None:
        return
    from ..flow import lineage
    r = roster.Roster(reg)
    kids = [(b, s) for b, _f, s in lineage.members(reg, project, pb.lineage) if b.parent == parent]
    queued = {e.get("unit") for e in store.read(project)["entries"]}
    merged = {}
    for ln in store.read_log(project):
        if ln.get("event") == "merged" and ln.get("reason") != "proof":
            merged.setdefault(ln.get("unit"), []).append(ln)
    prs, done = [], []
    for b, status in kids:
        if b.unit in queued:
            return
        hit = [ln for ln in merged.get(b.unit, []) if units.num(ln.get("rev"), 0) >= b.rev]
        to = r.member(b.to)
        if hit:
            prs.append(hit[-1]["pr"])
        elif status == "done" and to is not None and to.kind == "div":
            done.append(b.unit)
        elif status not in ("failed", "stopped"):
            return
    if prs or done:
        what = "; ".join(x for x in (prs and f"PR {', '.join(f'#{n}' for n in prs)} merged",
                                     done and f"{', '.join(done)} done") if x)
        _tell(head.session, parent, "info", f"all {len(kids)} units of {parent} landed: {what} — report your "
                                            f"unit to Main", urgent=True)


def _guarded(project, entry, step, back) -> dict:
    """Run one entry's step. A Stop or a bug puts the entry back at the front (`back`; not one GitHub
    merged) and counts toward MAX_STOPS (_stopped); a step that got through clears the count."""
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
            queue.cleared(project)
        return res
    if back:     # one GitHub merged stays testing for its proof
        back = queue.front(project, entry["id"], unless_landed=True) is not None
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
