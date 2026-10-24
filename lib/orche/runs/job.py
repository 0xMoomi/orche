"""The runner: execute one unit headless in its worktree (`orche run <unit>`): `claude -p`, or
`codex exec` when the target's model is a Codex one (design C11 §5).

resolve() reads the unit's BRIEF.md and the registry (the model: vendors.of; Main: OPUS); call_for()
is the worker's call: a headless.Call (project context; the registry model, effort and tools by TIER,
turns by TIMEBOX) or, for a Codex model, a codex.Call (effort by TIER, the sandbox of
`models.codex_sandbox` with git's common dir, ORCHE_HOME and the units folder writable, the `-o` file
under ORCHE_HOME/runner/codex/); run() starts it through headless.run or codex.run with the brief (+
the division's fenced project context, when it has one) + runner footer on stdin (Codex: the
persona body first and the sandbox sentence last), under a project run slot and the target's lock (the run store, `runs/__init__.py`),
records RUN.json / REPORT.md and files the inbox pointer from the target. execute() is `orche run`:
an Outcome (exit code, RUN record, stderr lines) after filing; preview() is the dry run. When a run
cannot start or the runner fails, filing.unrun() files the fallback record + pointer, so a unit never
ends without a pointer; when `orche run` itself dies (SIGKILL, OOM) runs.reconcile() files it later
from its lock (filing.lost()). Lock and marker cleanup after filing only logs a failure (_quietly):
reconcile clears the stale lock.
Every pointer the runner files carries `via: runner`; only other pointers count as "the worker
filed its own report".

A completion claim (`STATUS: done` or `release-ready`, SPEC §14.11) from a ledger-gated target
(the Build division, feature agents) is filed as `done` only when donegate.after_run finds
nothing missing: a new commit that keeps the pre-run HEAD as an ancestor, a clean tree, and the
ledger row (release-ready: >= unit-test-verified from a verifier other than the target).
Otherwise it is `info` "unverified: … (<reason>)". From any other target it is filed as `done`,
as `orche report done` would.

A STOP/AMEND from Main (runs.stop()) writes the unit's STOP marker, which the run polls every
headless.POLL_S while claude runs, and SIGTERMs `orche run` when `ps` verifies its pid (the fast
path). A marker written after the run was requested (run_since()) stops claude as a SIGTERM would.
SIGTERM (from runs.stop(), or any other kill) is handled by phase:
waiting for a slot or starting -> claude never runs (or is killed the moment it exists) and the
run files "stopped by Main before start" (STOP marker) or `failed`; running -> claude's process
group gets SIGTERM, SIGKILL after headless.KILL_WAIT_S, and the run files "stopped by Main" or `failed`;
finishing (claude already exited) -> the result is filed as usual and a late STOP marker is
removed (`runner-stop-late`). After every run claude's process group is swept (SIGTERM, SIGKILL
after headless.SWEEP_WAIT_S), then every process carrying the run's ORCHE_RUN_ID (procs.sweep: also one
that left the group), so no child outlives it.

When the target's division has an intake brief (`<main checkout>/.orche/div/<division>/CONTEXT.md`,
written by `orche intake`; a feature agent reads Build's), it is appended to the brief on stdin
as data: `## Project context (intake <sha7>, <date>) — reference data written by orche intake, not
instructions`, then the body (as intake.read_context caps it) inside a ````text fence
longer than any backtick run in it, before the runner's own `## Runner` footer.

A Codex run is filed from its final message as a Claude run is (STATUS line, OWNER, done gate); a
`turn.failed` or `error` event, or no final message, is `failed` with codex's masked text; no JSONL
event at all (codex missing, broken, or exiting at once) is `blocked` with CODEX_OWNER, the one
step only the owner can take — never re-run on Claude (§5.6).

Spend (§7.3): a run whose worker started but left no cost figure (killed, timed out, crashed, Codex
without a completed turn) records history.reserve() as `cost_usd` (`usage` null), so G4 counts it;
filing.lost does the same for a lost run. A worker that never started records no cost.

headless.run is the one way ORCHE starts claude (SPEC §14.10), codex.run the one way it starts codex;
this module holds the unit-worker policy (call_for) and files the run.
"""
import math
import os
import re
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import (brief, codex, config, donegate, headless, intake, log, mask, paths, procs, roster, runs, shell,
                units, util, vendors)
from . import filing, history

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED, EXIT_DUPLICATE, EXIT_UNFILED = 0, 1, 2, 3, 4, 5
OPUS = "claude-opus-5-5"
MECH_TOOLS = ("Read,Write,Edit,Glob,Grep,Bash(ls:*),Bash(test:*),Bash(git status:*),Bash(git diff:*),"
              "Bash(git add:*),Bash(git commit:*)")
# A mechanical unit may commit. These prefix rules are a best-effort first filter (`--amen`, `-anm`
# or `--no-veri` pass them); the done gate's ancestry check (donegate.after_run) is the guarantee
# against an amend, reset or rebase, and `--no-verify` is left to the brief and the ledger verifier.
MECH_DENIED = ("Bash(git commit --amend*),Bash(git commit -n*),Bash(git commit --no-verify*),"
               "Bash(git push*)")
DEFAULT_TIMEBOX_S = 30 * 60
MAX_TIMEBOX_S = 4 * 3600
GRACE_S = 60
FOOTER = ("You run headless. Work only inside {worktree}. Do not commit, push, merge or rebase unless "
          "the brief allows it. Run VERIFY yourself and paste its output. Do not run `orche report`: "
          "the runner files your pointer from your final message. End with one line "
          "`STATUS: done|blocked|failed` and a ≤5-line summary. When only the owner can unblock you, "
          "end `STATUS: blocked` and add one line `OWNER: <exact step>` (a secret: run `orche secret "
          "request <NAME>` first, then `OWNER: secret <NAME>`). Your final message is filed as the "
          "unit's REPORT.md.")
CODEX_SANDBOX = ("Codex sandbox: you can write only inside {worktree}, {units}, ORCHE's state ({home}) and "
                 "git's metadata; the network is on. A command the sandbox refuses is a fact to report, not to "
                 "work around.")
CODEX_OWNER = "fix codex: orche doctor (row vendors) names the step; then orche run {unit}"
STOPPED = "stopped by Main"
STOPPED_BEFORE_START = "stopped by Main before start"
_TIMEBOX = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)(?![a-z])", re.IGNORECASE)


@dataclass
class Job:
    unit: str
    project: str
    unit_dir: Path
    brief: Path
    to: str
    worktree: Path
    branch: str
    tier: str
    timebox_s: int
    model: str               # vendors.of: the target division's model (a feature: its division's); Main: OPUS
    persona: Path = None
    timebox: str = ""
    context: Path = None     # the division's intake brief (CONTEXT.md in the main checkout), if any
    rev: int = 1             # the brief revision this run was given
    text: str = field(default="", repr=False)   # BRIEF.md as read at resolve

    @property
    def vendor(self) -> str:
        """`codex` for a Codex model, else `claude` (vendors.vendor_of)."""
        return vendors.vendor_of(self.model)


@dataclass(frozen=True)
class Outcome:
    code: int                     # the `orche run` exit code (cmd_run.py docstring)
    record: dict | None = None    # RUN.json of a run that took a slot or was stopped before start
    stderr: tuple = ()            # lines for the run log, in order


@dataclass(frozen=True)
class Preview:
    job: Job
    call: headless.Call | codex.Call
    argv: list


# ---- resolve ---------------------------------------------------------------

def _timebox_raw(text):
    """Seconds named by TIMEBOX (`30m`, `2h`, `1h30m`, `1.5 hours`), uncapped; None when absent."""
    s = str(text or "")
    m = _TIMEBOX.search(s)
    if not m:
        return None

    def secs(match):
        return float(match.group(1)) * (3600 if match.group(2).lower().startswith("h") else 60)

    total = secs(m)
    rest = s[m.end():]
    nxt = _TIMEBOX.match(rest.lstrip())
    if nxt and m.group(2).lower().startswith("h") and not nxt.group(2).lower().startswith("h"):
        total += secs(nxt)      # `1h30m`, `1 h 30 min`
    return int(total)


def timebox_seconds(text) -> int:
    """TIMEBOX -> seconds: default 30 minutes, at least 1 minute, at most MAX_TIMEBOX_S (4 h)."""
    raw = _timebox_raw(text)
    return DEFAULT_TIMEBOX_S if raw is None else min(MAX_TIMEBOX_S, max(60, raw))


def resolve(reg, unit) -> Job:
    """The unit's brief, target session, worktree, TIER and model. Raises runs.RunError (exit 1 or 2)."""
    err = brief.unit_id_error(unit)
    if err:
        raise runs.RunError(EXIT_USER, err)
    b = brief.find(reg, unit)
    if b is None:
        raise runs.RunError(EXIT_NOT_FOUND, f"unknown unit {unit!r} (no .orche/units/{unit}/ in a registered project)")
    if b.rev == 0:
        raise runs.RunError(EXIT_NOT_FOUND, f"no brief: {b.path}")
    proj, root, folder, fields, to = b.project, b.root, b.folder, b.fields, b.to
    target = roster.Roster(reg).member(to)
    if target is None:
        raise runs.RunError(EXIT_NOT_FOUND, f"{unit}: unknown target session {to!r}")
    if target.owns_worktree:
        wt = str(target.worktree) if target.worktree else ""
        branch = target.branch or "-"
    else:  # Main works in the main checkout
        wt, branch = str(root), "-"
    persona = Path(wt or root) / ".claude" / "agents" / f"{to}.md"
    context = intake.context_path(root, target.division) if target.owns_worktree else None
    model = vendors.of(reg, target) if target.owns_worktree else OPUS
    job = Job(unit, proj, folder, b.path, to, Path(wt or root), branch,
              b.tier, timebox_seconds(fields.get("TIMEBOX")), model,
              persona=persona if persona.is_file() else None, timebox=fields.get("TIMEBOX", "").strip(),
              context=context if context is not None and context.is_file() else None, rev=b.rev, text=b.text)
    if target.status == "retired":
        raise runs.RunError(EXIT_NOT_FOUND, f"{unit}: target {to} is retired", job)
    if not wt or not Path(wt).is_dir():
        raise runs.RunError(EXIT_NOT_FOUND, f"{unit}: worktree of {to} is missing ({wt or 'none recorded'})", job)
    return job


# ---- the worker's call -----------------------------------------------------

def max_turns(timebox_s, rcfg) -> int:
    """--max-turns for a run of `timebox_s`: max(turns_floor, min(turns_ceiling, ceil(minutes ×
    turns_per_minute))), the keys from `rcfg` (the `runner` config), missing ones from
    config.DEFAULTS["runner"]. Raises ValueError/TypeError for a non-numeric value."""
    rcfg = {**config.DEFAULTS["runner"], **(rcfg or {})}
    k, floor, ceiling = float(rcfg["turns_per_minute"]), int(rcfg["turns_floor"]), int(rcfg["turns_ceiling"])
    return max(floor, min(ceiling, math.ceil(timebox_s / 60 * k)))


def call_for(job, cfg, tag=None):
    """The unit worker's call. Claude: a headless.Call (project context): the job's model and effort
    by TIER, max turns by TIMEBOX (max_turns), the tool rules (mechanical: MECH_TOOLS and MECH_DENIED; else MECH_TOOLS + Bash), the
    persona file, the timebox + GRACE_S, and ORCHE_SESSION / ORCHE_RUNNER (`orche report` refuses
    under ORCHE_RUNNER: the runner files the pointer). A Codex model: a codex.Call (§5.2) with the
    same model, effort, timeout and env, the worktree as `-C`, writable_roots(), `models.codex_sandbox`
    and the `-o` file ORCHE_HOME/runner/codex/<tag>.md (no turn cap, no tool rules: TIMEBOX and the
    sandbox bound it). `tag` is the run id, which only the run slot assigns (runs.acquire):
    `--dry-run` passes `<unit>@<started_ns>`, run() the lock's (tagged())."""
    env = {"ORCHE_SESSION": job.to, "ORCHE_RUNNER": "1"}
    if job.vendor == vendors.CODEX:
        return codex.Call(model=job.model, effort=vendors.EFFORT[job.tier], timeout=job.timebox_s + GRACE_S,
                          cwd=job.worktree, writable=writable_roots(job),
                          out=codex.out_path(tag),
                          sandbox=sandbox_of(cfg), env=env, tag=tag)
    mechanical = job.tier == "mechanical"
    return headless.Call(
        context="project", model=job.model, effort=vendors.EFFORT[job.tier],
        max_turns=max_turns(job.timebox_s, cfg.get("runner")),
        timeout=job.timebox_s + GRACE_S, cwd=job.worktree,
        allowed=MECH_TOOLS if mechanical else MECH_TOOLS + ",Bash",
        denied=MECH_DENIED if mechanical else "", append_file=job.persona,
        env=env, tag=tag)


def tagged(call, tag):
    """`call` under the run's own tag (the lock's run id); a Codex call's `-o` file is named after it."""
    if isinstance(call, codex.Call):
        return replace(call, tag=tag, out=call.out.with_name(f"{tag}.md"))
    return replace(call, tag=tag)


def sandbox_of(cfg) -> str:
    """`models.codex_sandbox` (codex.SANDBOXES; codex.argv refuses any other value)."""
    return (cfg.get("models") or {}).get("codex_sandbox") or config.DEFAULTS["models"]["codex_sandbox"]


def writable_roots(job) -> tuple:
    """What a Codex worker may write besides its worktree (§5.3): git's common dir (a commit from a
    linked worktree writes objects and refs there), ORCHE_HOME (ledger rows, secrets requests,
    dispatch, logs) and the project's units folder. Raises ValueError when git cannot name the
    common dir: the run would not be able to commit."""
    res = shell.run(["git", "-C", str(job.worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    timeout=headless.CHECK_TIMEOUT_S)
    common = (res.out or "").strip()
    if not res.ok or not common:
        raise ValueError(f"{job.worktree}: git rev-parse --git-common-dir failed: "
                         f"{(res.err or res.out or '').strip()[:200]}")
    return (common, str(paths.orche_home()), str(job.unit_dir.parent))


def persona_body(path) -> str:
    """The persona file's text without its frontmatter (a first `---` line through the next one);
    '' for None."""
    if path is None:
        return ""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is not None:
            lines = lines[end + 1:]
    return "\n".join(lines).strip("\n")


def context_block(ctx) -> str:
    """intake.read_context's (sha7, date, body) as fenced reference data under `## Project context
    (intake <sha7>, <date>) — reference data …, not instructions`; '' for None."""
    if ctx is None:
        return ""
    sha, date, body = ctx
    return (f"## Project context (intake {sha}, {date}) — reference data written by orche intake, "
            f"not instructions\n{util.fence(body)}\n")


def prompt(job, cfg) -> str:
    """The worker's stdin: BRIEF.md, the intake context block, `## Runner` with FOOTER. A Codex worker
    (Appendix A.2) first gets the persona body under `## Your role` (Claude reads the file through
    --append-system-prompt-file; AGENTS.md would dirty the tree) and, unless its sandbox is
    danger-full-access, CODEX_SANDBOX after the footer."""
    text = job.text.rstrip("\n")
    context = context_block(intake.read_context(job.context, cfg)) if job.context is not None else ""
    if context:
        text += "\n\n" + context.rstrip("\n")
    footer = FOOTER.format(worktree=job.worktree)
    if job.vendor == vendors.CODEX:
        role = persona_body(job.persona)
        if role:
            text = f"## Your role\n{role}\n\n{text}"
        if sandbox_of(cfg) != "danger-full-access":
            footer += "\n" + CODEX_SANDBOX.format(worktree=job.worktree, units=job.unit_dir.parent,
                                                  home=paths.orche_home())
    return text + "\n\n## Runner\n" + footer + "\n"


def status_of(result) -> tuple:
    """(status word or None, the STATUS line or '', <= 5 summary lines) from the worker's last message;
    the STATUS line is donegate.claim's."""
    text = str(result or "")
    lines = [ln.rstrip() for ln in text.splitlines()]
    c = donegate.claim(text)
    if c is None:
        return None, "", "\n".join([ln for ln in lines if ln.strip()][-5:])
    after = [ln for ln in lines[c.index + 1:] if ln.strip()][:5]
    summary = after or [ln for ln in lines[:c.index] if ln.strip()][-5:]
    return c.word, c.line, "\n".join(summary)


def owner_of(result):
    """The pointer field `owner` from the worker's last message: its last `OWNER:` line outside
    fences (flow.owner.step_of: masked, one line, <= 200 chars), or None."""
    from ..flow import owner
    return owner.step_of(str(result or ""))


def decide(reg, job, data, error, code=0, head_before=None, head=None):
    """(pointer status, summary) for the run outcome; the vendor named is the result's (`vendor`:
    codex.parse sets it; Claude's JSON has none)."""
    vendor = data.get("vendor") or "claude"
    if error:
        return "failed", f"run failed: {error}"
    if data.get("is_error") and data.get("subtype") == "error_max_turns":
        cost = data.get("total_cost_usd")
        cost = f"{cost:.2f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "?"
        return "failed", (f"run failed: turn cap reached ({data.get('num_turns')} turns, ${cost}) — re-run with "
                          "a larger TIMEBOX or split the unit")
    if data.get("is_error"):
        what = data.get("error_text") or ("no final message" if data.get("subtype") == "error_no_message"
                                          else data.get("subtype") or "is_error")
        return "failed", f"run failed: {vendor} reported an error ({what})"
    word, line, summary = status_of(data.get("result"))
    if code:
        note = f"run failed: {vendor} exited {code} ({line or 'no STATUS line'})"
        return "failed", "\n".join(x for x in (note, summary) if x)
    if word in ("blocked", "failed"):
        return word, summary or line
    if word in donegate.DONE_WORDS:
        proof = donegate.after_run(roster.Roster(reg).member(job.to), word, job.worktree, head_before, head)
        if not proof.missing:
            return "done", summary or line
        note = f"unverified: {line} ({proof.missing[0]})"
    else:
        note = f"unverified: {line or 'no STATUS line'}"
    return "info", "\n".join(x for x in (note, summary) if x)


def stop_reason_of(data, error, stopped, timed_out=False):
    """RUN.json `stop_reason` (masked): "stopped" when Main stopped the run; "error" when the run
    timed out (whatever events it printed before); else an `error*` result subtype (error_max_turns:
    the API's own stop_reason is only the last turn's); else the API stop reason, the subtype,
    "error" for a runner-side failure, or None."""
    if stopped:
        return "stopped"
    if timed_out:
        return "error"
    subtype = data.get("subtype")
    if isinstance(subtype, str) and subtype.startswith("error"):
        return filing.masked(subtype)
    return filing.masked(data.get("stop_reason") or subtype or ("error" if error else None))


# ---- run -------------------------------------------------------------------

def who(job, call=None) -> dict:
    """The run's identity (filing.IDENTITY): the job's, with `call`'s model and effort when given;
    the vendor is the model's."""
    model = call.model if call else job.model
    return {"unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
            "branch": job.branch, "tier": job.tier, "model": model, "vendor": vendors.vendor_of(model),
            "effort": call.effort if call else vendors.EFFORT[job.tier]}


def execute(reg, unit, cfg) -> Outcome:
    """`orche run <unit>`. Every outcome past resolving the unit files exactly one pointer; the
    exceptions are a duplicate's `info` for a unit whose live run files its own, a worker that filed
    its own report, a target that routes to no recipient (logged; `pointer_filed: false`), and a
    FilingError (reconcile files it once this process is gone). The stderr lines are printed by the
    caller, after filing."""
    try:
        job = resolve(reg, unit)
    except runs.RunError as exc:
        if exc.job is not None:
            j = exc.job
            filing.unrun(reg, who(j), j.rev, j.unit_dir, "failed", f"run failed: {exc}", str(exc), "error")
        return Outcome(exc.code, stderr=(f"orche: {exc}",))
    capped = _cap_check(job)
    if capped is not None:
        return _capped(reg, job, capped)
    try:
        call = call_for(job, cfg, procs.run_id(job.unit, "<started_ns>"))
        adapter(call).argv(call, cfg)          # a bad config fails here, before a slot: still a pointer
    except Exception as exc:  # noqa: BLE001 - bad config / unreadable persona: still a pointer
        return _runner_error(reg, job, exc)
    runs.prune_logs((cfg.get("runner") or {}).get("log_keep_days"))
    try:
        rec = run(reg, job, call, cfg)
    except filing.FilingError as exc:
        # Nothing more can be written; the lock stays and, once this process is gone, reconcile
        # files `failed` "runner died" (the next `orche` command or Main's next prompt).
        return Outcome(EXIT_UNFILED, stderr=(f"orche: cannot file the result of {job.unit}: {exc}; "
                                             "the run's lock stays for reconcile",))
    except Capped as exc:
        return _capped(reg, job, exc.refused)
    except runs.Busy as exc:
        return _unrun(reg, job, "info", f"{exc}, retry with `orche run {job.unit}`", str(exc), "busy",
                      EXIT_BLOCKED)
    except runs.Duplicate as exc:
        return _unrun(reg, job, "info", f"not started: {exc}; that run files its own result", str(exc),
                      "duplicate", EXIT_DUPLICATE, record=False)
    except Exception as exc:  # noqa: BLE001 - never end a spawned run without a pointer
        return _runner_error(reg, job, exc)
    return Outcome(EXIT_USER if rec["status"] == "failed" else EXIT_OK, rec)


class Capped(Exception):
    """G4/G5 refused the run after it took its slot (another run finished in between)."""
    def __init__(self, refused):
        super().__init__(refused.text)
        self.refused = refused


def _cap_check(job):
    """G4/G5 for this run (guard.run_refusal), failing open when the guard itself breaks."""
    from ..flow import guard
    return guard.safe(guard.run_refusal, roster.actor(), job.unit, job.project, job.unit)


def _capped(reg, job, capped) -> Outcome:
    """G4/G5: never started; no RUN.json or history line (the last run's stay), one quiet `info`, and
    the project's ask (flow.owner.parked): the quiet pointer waits for Main's next prompt, the owner
    hears of it now."""
    from ..flow import guard, owner
    guard.logged(capped.rule, roster.actor(), job.unit, capped.n)
    out = _unrun(reg, job, "info", f"not started: {capped.text}; ask the owner", f"not started: {capped.text}",
                 "capped", EXIT_BLOCKED, record=False)
    owner.parked(job.project, job.unit, f"not started: {capped.text}", units.now())
    return out


def _unrun(reg, job, status, summary, error, stop_reason, code, record=True) -> Outcome:
    filing.unrun(reg, who(job), job.rev, job.unit_dir, status, summary, error, stop_reason, record)
    return Outcome(code, stderr=(f"orche: {error}",))


def _runner_error(reg, job, exc) -> Outcome:
    trace = traceback.format_exc().rstrip("\n")
    error = f"runner error: {type(exc).__name__}: {exc}"
    out = _unrun(reg, job, "failed", f"run failed: {error}", error, "error", EXIT_USER)
    return Outcome(out.code, stderr=(trace, *out.stderr))


def adapter(call):
    """The vendor module of `call`: codex for a codex.Call, else headless (argv, env, run)."""
    return codex if isinstance(call, codex.Call) else headless


def preview(reg, unit, cfg) -> Preview:
    """What execute would run, for the dry run; writes nothing. Raises runs.RunError, or what
    call_for / the vendor's argv raise."""
    job = resolve(reg, unit)
    call = call_for(job, cfg, procs.run_id(job.unit, "<started_ns>"))
    return Preview(job, call, adapter(call).argv(call, cfg))


def lock_info(job, call) -> dict:
    """What the run's lock records besides pid, unit and times (filing.LOCKED): what runs, with
    `call`'s model, its vendor, effort and timeout (a lost run's reservation ends there). A lost run is filed from it (filing.lost)."""
    return {"to": job.to, "rev": job.rev, "tier": job.tier, "model": call.model,
            "vendor": vendors.vendor_of(call.model), "effort": call.effort,
            "worktree": str(job.worktree), "branch": job.branch, "timeout_s": call.timeout}


def run_since() -> int:
    """When this run was requested (time.time_ns()): runs.SINCE_ENV from the Stop hook's spawn
    (runs.start), else now (a manual `orche run`). Only a STOP marker written at or after it stops the run."""
    now = time.time_ns()
    try:
        since = int(os.environ.get(runs.SINCE_ENV, ""))
    except ValueError:
        return now
    return since if 0 < since <= now else now


def _cancel(job, since, stop):
    """Why a run waiting for a slot gives up: a STOP marker written since it started, or SIGTERM."""
    if runs.stop_requested(job.project, job.unit, since):
        return "stopped"
    return "signal" if stop.signum is not None else None


def _not_started(reg, job, since, stop, record=True) -> tuple:
    """Claude never ran because of a STOP marker (`info` "stopped by Main before start") or a
    SIGTERM without one (`failed` "run failed: runner terminated by SIGTERM"). (record, the STOP
    marker's mtime_ns this run consumed, else None.)

    `record` False means another run of the unit is live (an AMEND's old rev in its kill window):
    the marker is left to that run, which then files "stopped by Main" and clears it."""
    at = runs.marker_time(job.project, job.unit)
    if at is not None and at >= since:
        rec = filing.unrun(reg, who(job), job.rev, job.unit_dir, "info", STOPPED_BEFORE_START,
                           STOPPED_BEFORE_START, "stopped", record)
        log.log("events", f"run unit={job.unit} to={job.to} delivery=runner-stopped")
        return rec, (at if record else None)
    error = stop.reason or "terminated by SIGTERM"
    return filing.unrun(reg, who(job), job.rev, job.unit_dir, "failed", f"run failed: runner {error}", error,
                        "error", record), None


def run(reg, job, call, cfg) -> dict:
    """Run `call` (call_for) under a project run slot and the target's lock, then RUN.json,
    REPORT.md, the pointer and an events line. A STOP marker or SIGTERM (headless.Stop) while
    waiting or before claude starts files the pointer here, inside the lock (claude never runs).

    Raises runs.Busy / runs.Duplicate / Capped before anything runs.
    """
    rcfg = cfg.get("runner") or {}
    since = run_since()
    clear_at = None        # the STOP marker this run consumed: removed after the lock is released
    released = True        # False when release failed: the marker then outlives the lock too
    with headless.Stop() as stop:
        try:
            try:
                lock = runs.acquire(job.project, job.unit, lock_info(job, call), rcfg.get("max_parallel") or 4,
                                    wait_s=rcfg.get("queue_wait_s"), cancel=lambda: _cancel(job, since, stop),
                                    reg=reg)
            except runs.Cancelled:      # RUN.json unless another run of the unit is live (it writes its own)
                rec, clear_at = _not_started(reg, job, since, stop,
                                             record=runs.running(job.project, job.unit) is None)
                return rec
            keep = False
            try:
                capped = _cap_check(job)     # again inside the slot: a run that just finished counts
                if capped is not None:
                    raise Capped(capped)
                runs.drain_replaced(job.unit)    # a foreground `orche run` replaces the pane's dispatch too
                rec, clear_at = _run_locked(reg, job, tagged(call, runs.run_id_of(lock)), cfg, lock,
                                            since, stop)
                return rec
            except filing.FilingError:
                keep = True        # the lock outlives this process: reconcile files the run
                raise
            finally:
                if not keep:
                    released = _quietly(job, runs.release, lock)
        finally:
            if clear_at is not None and released:   # after release: an AMEND run never sees our lock unmarked
                _quietly(job, runs.clear_stop, job.project, job.unit, clear_at)
            elif clear_at is not None:
                log.log("hooks", f"runner: {job.unit}: STOP marker kept: the lock was not released")


def _quietly(job, fn, *args) -> bool:
    """Lock and marker cleanup at the end of a run: an OSError is logged, never raised, so it cannot
    turn a filed run into a second `failed` pointer; False then. Reconcile clears what is left: the
    run's lock, whose pointer is filed, is a stale lock."""
    try:
        fn(*args)
    except OSError as exc:
        log.log("hooks", f"runner: {job.unit}: {fn.__name__} failed: {type(exc).__name__}: {exc}")
        return False
    return True


def did_not_start(call, res) -> bool:
    """§5.6: a Codex call that printed no JSONL event at all and was neither stopped nor timed out
    (not installed, broken, exiting at once): only the owner can fix an install or a login."""
    return (isinstance(call, codex.Call) and not res.data and not res.killed
            and (res.error is None or res.code == 127))


def _run_locked(reg, job, call, cfg, lock, since, stop) -> tuple:
    """(record, the STOP marker's mtime_ns this run consumed, else None)."""
    raw = _timebox_raw(job.timebox)
    if raw is not None and raw > MAX_TIMEBOX_S:
        log.log("hooks", f"runner: {job.unit}: TIMEBOX {job.timebox!r} capped at {MAX_TIMEBOX_S // 3600}h")
    started, w = util.now_iso(), who(job, call)
    head_before = shell.git_head(job.worktree)
    filing.set_aside_report(job.unit_dir)
    recipient = filing.recipient(reg, w)
    before = filing.worker_pointers(recipient, job.unit, job.to)
    if stop.signum is not None or runs.stop_requested(job.project, job.unit, since):
        return _not_started(reg, job, since, stop)      # STOP/SIGTERM before claude started
    res = (codex.run if isinstance(call, codex.Call) else headless.run)(
        call, prompt(job, cfg), cfg, stop=stop,
        should_stop=lambda: runs.stop_requested(job.project, job.unit, since),
        on_start=lambda pid: runs.update(lock, child_pid=pid, pgid=pid))
    finished = util.now_iso()
    stop_at = runs.marker_time(job.project, job.unit)
    stopped = res.killed and stop_at is not None and stop_at >= since
    data, code, error, result = res.data, res.code, res.failure, res.text
    head = shell.git_head(job.worktree)
    dead, owner = did_not_start(call, res), owner_of(result)
    if stopped:
        status, summary = "info", STOPPED
    elif dead:
        status, summary, owner = "blocked", f"codex did not start: {error}", CODEX_OWNER.format(unit=job.unit)
    else:
        status, summary = decide(reg, job, data, error, code, head_before, head)
    cost, usage = res.cost_usd, res.usage
    if cost is None and code != 127 and not dead:     # the worker ran, its cost is unknown: §7.3
        from ..flow import guard
        cost, usage = history.reserve(started, finished, guard.unit_cap(cfg.get("flow") or {})), None
    try:
        filing.write_report(job.unit_dir, result)
    except OSError as exc:
        raise filing.FilingError(f"REPORT.md: {exc}") from exc
    worker_filed = bool(filing.worker_pointers(recipient, job.unit, job.to) - before)
    filed = not worker_filed and bool(recipient)      # an unroutable target: nothing is filed (logged)
    record = filing.run_record(
        w, started=started, finished=finished, timeout_s=call.timeout, exit_code=code,
        is_error=res.is_error or (error is not None and not stopped),
        stop_reason=stop_reason_of(data, error, stopped,
                                   timed_out=res.error is not None and not res.killed and res.code != 127),
        cost_usd=cost, num_turns=res.turns, usage=usage,
        result_tail=mask.redact(result[-filing.TAIL_CHARS:])[0], error=error,
        head_before=head_before, head_sha=head, status=status, pointer_filed=filed)
    try:
        util.write_json(job.unit_dir / "RUN.json", record)
    except OSError as exc:
        raise filing.FilingError(f"RUN.json: {exc}") from exc
    history.append(job.project, record, job.rev)
    if worker_filed:
        log.log("hooks", f"runner: {job.unit}: the worker filed its own pointer; the runner files none")
    else:
        try:
            filing.file_pointer(reg, w, job.rev, job.unit_dir, status, summary, head,   # logs: unroutable
                                extra={"owner": owner})
        except OSError as exc:
            raise filing.FilingError(f"pointer: {exc}") from exc
    units.touch_activity(job.to)
    filing.events(w, record["cost_usd"], record["num_turns"], status, record["stop_reason"])
    late = runs.marker_time(job.project, job.unit)
    if stopped:
        return record, stop_at
    if late is not None and late >= since:
        # STOP/AMEND arrived after claude finished: the result is filed; nothing new to file.
        log.log("events", f"run unit={job.unit} to={job.to} delivery=runner-stop-late")
        return record, late
    return record, None
