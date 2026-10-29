"""The runner: execute one unit headless in its worktree (`orche run <unit>`): `claude -p`, or
`codex exec` when the target's model is a Codex one.

resolve() reads the unit's BRIEF.md and the registry (the ceiling: vendors.of; Main: OPUS) and picks the
model and effort from the TIER's rung (ladder.py; a MODEL pin replaces the model); call_for()
is the worker's call: a headless.Call (project context; the rung's model and effort, tools by TIER,
turns by TIMEBOX) or, for a Codex model, a codex.Call (the rung's effort, the sandbox of
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

A Claude attempt that ended normally but unverified (no STATUS line, or a done-gate gap) gets one
finishing continuation before anything climbs or files (_finish): the same session resumed
(`--resume`, FINISH_TURNS turns, FINISH_PROMPT naming the gap) within what is left of the TIMEBOX,
after G4; never twice, never for Codex, never after a STOP. Its history line (`kind` finish) and
RUN.json count only its own cost: a resumed result reports the whole conversation's total.

In a team repository (registry.team) a verified gated done publishes nothing and starts no head review:
it is filed with publish.ready_line and `pr: none`; `orche queue add <unit>` publishes on the owner's word.
When the team plugin is installed for this user, its rules text reaches the prompt (`## Team rules`) and its
PreToolUse gates join ORCHE's (headless.Call.team).

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

from .. import (brief, codex, config, donegate, headless, intake, ladder, log, mask, paths, procs, publish,
                registry, roster, runs, shell, templates, units, util, vendors)
from ..flow import usage as holds
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
CODEX_OWNER = "fix codex: orche doctor (row vendors) names the step; then orche run {unit}"
STOPPED = "stopped by Main"
STOPPED_BEFORE_START = "stopped by Main before start"
FINISH_TURNS = 8
FINISH_MIN_S = 60       # TIMEBOX left below which no finishing continuation starts
FINISH_PROMPT = ("Your run ended without a valid completion: {gap}. Do only the closing steps your brief requires "
                 "(commit, ledger row from your VERIFY output, REPORT.md, the final STATUS line). Start no new work. "
                 "If you cannot finish, end with STATUS: blocked and one OWNER line.")
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
    model: str               # the rung's model (ladder.rung), at most the division's (vendors.of; Main: OPUS)
    persona: Path = None
    timebox: str = ""
    context: Path = None     # the division's intake brief (CONTEXT.md in the main checkout), if any
    rev: int = 1             # the brief revision this run was given
    text: str = field(default="", repr=False)   # BRIEF.md as read at resolve
    root: Path = None        # the project's main checkout (publish.base reads origin/HEAD there)
    kind: str = ""           # the target's roster kind: a `feat` run starts with publish.sync
    gated: bool = False      # donegate.gated(target): templates/run/commit-gated.md, and a verified done publishes
    sync_note: str = ""      # publish.sync's `## Branch sync` note for the prompt, set at the run's start
    effort: str = "medium"   # the rung's effort (ladder.rung)
    base: str = ""           # the ceiling: the division's model (Main: OPUS)
    pinned: str = ""         # the brief's MODEL, if any (and still valid at run time)
    pin_note: str = ""       # why a MODEL pin no longer runs (ladder.pin_error at run time): the rung runs
    team: str = ""           # the team plugin the project declares (registry.team): a gated done publishes nothing
    plugin: Path = None      # that plugin's folder when installed for this user: its rules and gates reach the worker

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

def timebox_raw(text):
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
    raw = timebox_raw(text)
    return DEFAULT_TIMEBOX_S if raw is None else min(MAX_TIMEBOX_S, max(60, raw))


def resolve(reg, unit, cfg=None) -> Job:
    """The unit's brief, target session, worktree, TIER, and the TIER's rung (model and effort) under
    the division's model. Raises runs.RunError (exit 1 or 2)."""
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
    base = vendors.of(reg, target) if target.owns_worktree else OPUS
    cfg = cfg if cfg is not None else config.load()
    pin, pin_note = (fields.get("MODEL") or "").strip(), ""
    if pin:   # R-LADDER-3 again at run time: the division's model or Codex may have changed since dispatch
        err = ladder.pin_error(cfg, pin, base, to)
        if err:
            pin, pin_note = "", f"{err}: ran the {b.tier if b.tier in brief.TIERS else 'standard'} rung instead"
    r = ladder.rung(cfg, base, b.tier, pin or None)
    job = Job(unit, proj, folder, b.path, to, Path(wt or root), branch,
              b.tier, timebox_seconds(fields.get("TIMEBOX")), r.model, effort=r.effort, base=base, pinned=pin,
              pin_note=pin_note,
              persona=persona if persona.is_file() else None, timebox=fields.get("TIMEBOX", "").strip(),
              context=context if context is not None and context.is_file() else None, rev=b.rev, text=b.text,
              root=Path(root), kind=target.kind, gated=donegate.gated(target), team=registry.team(reg, proj) or "")
    if job.team:
        from ..hr import panes
        job.plugin = panes.plugin_install_path(job.team)
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
        return codex.Call(model=job.model, effort=job.effort, timeout=job.timebox_s + GRACE_S,
                          cwd=job.worktree, writable=writable_roots(job),
                          out=codex.out_path(tag),
                          sandbox=sandbox_of(cfg), env=env, tag=tag)
    mechanical = job.tier == "mechanical"
    return headless.Call(
        context="project", model=job.model, effort=job.effort,
        max_turns=max_turns(job.timebox_s, cfg.get("runner")),
        timeout=job.timebox_s + GRACE_S, cwd=job.worktree,
        allowed=MECH_TOOLS if mechanical else MECH_TOOLS + ",Bash",
        denied=MECH_DENIED if mechanical else "", append_file=job.persona,
        env=env, tag=tag, team=job.plugin)


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
    res = shell.git(job.worktree, "rev-parse", "--path-format=absolute", "--git-common-dir",
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


def team_rules(plugin) -> str:
    """The installed team plugin's rules text (`<plugin>/hooks/team-rules.md`, read at call time) under
    `## Team rules`; '' without a plugin or that file."""
    if plugin is None:
        return ""
    try:
        text = (Path(plugin) / "hooks" / "team-rules.md").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return f"## Team rules\n{text}" if text else ""


def prompt(job, cfg) -> str:
    """The worker's stdin: BRIEF.md, the intake context block, in a team repository whose plugin is installed
    its `## Team rules` (team_rules), `## Runner` with templates/run/footer.md. A Codex worker
    first gets the persona body under `## Your role` (Claude reads the file through
    --append-system-prompt-file; AGENTS.md would dirty the tree) and, unless its sandbox is
    danger-full-access, templates/run/codex-sandbox.md after the footer."""
    text = job.text.rstrip("\n")
    context = context_block(intake.read_context(job.context, cfg)) if job.context is not None else ""
    if context:
        text += "\n\n" + context.rstrip("\n")
    rules = team_rules(job.plugin)
    if rules:
        text += "\n\n" + rules
    if job.sync_note:
        text += "\n\n" + job.sync_note
    footer = templates.load("run/footer.md", WORKTREE=job.worktree,
                            COMMIT=templates.load("run/commit-gated.md" if job.gated else "run/commit.md"))
    if job.vendor == vendors.CODEX:
        role = persona_body(job.persona)
        if role:
            text = f"## Your role\n{role}\n\n{text}"
        if sandbox_of(cfg) != "danger-full-access":
            footer += "\n" + templates.load("run/codex-sandbox.md", WORKTREE=job.worktree, UNITS=job.unit_dir.parent,
                                             HOME=paths.orche_home())
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
            "effort": call.effort if call else job.effort}


def execute(reg, unit, cfg) -> Outcome:
    """`orche run <unit>`. Every outcome past resolving the unit files exactly one pointer; the
    exceptions are a duplicate's `info` for a unit whose live run files its own, a worker that filed
    its own report, a target that routes to no recipient (logged; `pointer_filed: false`), and a
    FilingError (reconcile files it once this process is gone). The stderr lines are printed by the
    caller, after filing."""
    try:
        job = resolve(reg, unit, cfg)
    except runs.RunError as exc:
        if exc.job is not None:
            j = exc.job
            filing.unrun(reg, who(j), j.rev, j.unit_dir, "failed", f"run failed: {exc}", str(exc), "error")
        return Outcome(exc.code, stderr=(f"orche: {exc}",))
    capped = _cap_check(job)
    if capped is not None:
        return _capped(reg, job, capped)
    hold = holds.active(job.vendor)
    if hold is not None:
        return _held(reg, job, hold)
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
    except Held as exc:
        return _held(reg, job, exc.hold)
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


class Held(Exception):
    """§8.5: the job's vendor became held while the run waited for its slot (flow/usage.py)."""
    def __init__(self, hold):
        super().__init__("held")
        self.hold = hold


def _held(reg, job, hold) -> Outcome:
    """§8.5: a held vendor's run never starts: RUN.json `stop_reason: held` (not a run for G5), one
    quiet `info` "held: <vendor> usage limit until HH:MM; this unit runs then", the unit joins the
    hold; the heartbeat (class `held`) re-runs it once the hold is over. Exit 0, never `failed`."""
    holds.join(job.vendor, job.project, job.unit, "work")
    text = holds.held_text(hold, job.vendor)
    return _unrun(reg, job, "info", text, text, "held", EXIT_OK)


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
    job = resolve(reg, unit, cfg)
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
                                    reg=reg, total=rcfg.get("max_parallel_total") or 8,
                                    min_free_mb=rcfg.get("min_free_mb"))
            except runs.Cancelled:      # RUN.json unless another run of the unit is live (it writes its own)
                rec, clear_at = _not_started(reg, job, since, stop,
                                             record=runs.running(job.project, job.unit) is None)
                return rec
            keep = False
            try:
                capped = _cap_check(job)     # again inside the slot: a run that just finished counts
                if capped is not None:
                    raise Capped(capped)
                hold = holds.active(job.vendor)     # a hold that began while this run waited
                if hold is not None:
                    raise Held(hold)
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


def _publish(reg, job, summary, owner, head, result, cfg=None) -> tuple:
    """A gated done that passed the gate: push the branch and ensure its PR (publish.publish). Returns
    (status, summary, owner, RUN.json `pr` or None, the REPORT.md text with its `PR:` line); a gap
    files `info` "unverified: <gap>" with the gap's OWNER step, if any."""
    member = roster.Roster(reg).member(job.to)
    b = brief.find(reg, job.unit, project=job.project)
    title, body = publish.pr_text(member, job.root or job.worktree, goal=b.fields.get("GOAL") if b is not None
                                  else job.unit, report=result, cfg=cfg)
    out = publish.publish(member, job.worktree, job.root or job.worktree, unit=job.unit, title=title, body=body,
                          cfg=cfg)
    if isinstance(out, publish.Unchanged):     # R5: nothing against the base; filed without a PR or a review
        line = out.note()
        return "done", f"{summary}\n{line}".lstrip("\n"), owner, None, f"{str(result or '').rstrip()}\n\n{line}\n"
    if isinstance(out, publish.Gap):
        log.log("hooks", f"runner: {job.unit}: publish gap: {out.text}")
        return "info", f"unverified: {out.text}\n{summary}".rstrip(), out.owner or owner, None, result
    line = f"PR: #{out.pr} {out.url} head {out.head}"
    return "done", f"{summary}\n{line}".lstrip("\n"), owner, out.record(), f"{str(result or '').rstrip()}\n\n{line}\n"


@dataclass
class _Try:
    """One worker call inside a run (an attempt) and how it ended."""
    kind: str
    call: object
    started: str
    finished: str
    res: object
    stopped: bool
    stop_at: object
    dead: bool
    owner: object
    status: str
    summary: str
    head: object
    cost: object
    usage: object

    @property
    def timed_out(self) -> bool:
        return self.res.error is not None and not self.res.killed and self.res.code != 127


def _attempt(reg, job, call, cfg, lock, since, stop, head_before, text, kind="work", prior=None) -> _Try:
    """Run the worker once with `text` on stdin and decide its outcome. `prior`: the resumed session's
    total so far (a finish), taken off this call's reported total."""
    started = util.now_iso()
    res = adapter(call).run(
        call, text, cfg, stop=stop,
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
        status, summary = decide(reg, replace(job, model=call.model), data, error, code, head_before, head)
    cost = history.cost(res, started, finished, cfg, ran=not dead)     # the worker ran, its cost unknown: §7.3
    usage = res.usage if cost is res.cost_usd else None
    if prior is not None and cost is res.cost_usd and isinstance(cost, (int, float)):
        cost, usage = round(max(0.0, cost - prior), 4), None
    return _Try(kind, call, started, finished, res, stopped, stop_at, dead, owner, status, summary, head, cost, usage)


def _audit(job, cfg) -> None:
    """Jev's TIER audit (ladder.audit): logs only, and never fails the run."""
    try:
        ladder.audit(job.unit, job.tier, job.text, cfg)
    except Exception as exc:  # noqa: BLE001 - the audit is advisory
        log.log("hooks", f"runner: {job.unit}: tier audit skipped ({type(exc).__name__}: {exc})")


def _sum(values):
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(nums), 4) if nums else None


def _spend_hit(job, cfg, tries):
    """G4 before the paid escalation attempt: the unit's and the project's spend inside W, this run's calls so
    far included (history has them only once the run is filed). Fails open."""
    return history.spend_hit(job.project, job.unit, cfg, _sum(x.cost for x in tries) or 0.0, tag=f"runner: {job.unit}")


def _hold_of(job, cfg, tries) -> tuple:
    """§8.5: (hold, attempt) for the first of `tries` whose error names a usage limit — the vendor's
    hold recorded with this unit joined (flow/usage.hit) — else (None, None)."""
    for x in tries:
        if not x.stopped and holds.is_limit(x.res.limit_text, cfg):
            hold = holds.hit(vendors.vendor_of(x.call.model), x.res.limit_text, cfg, project=job.project,
                             unit=job.unit, stage="work")
            if hold is not None:
                return hold, x
    return None, None


def _finish(reg, job, call, cfg, lock, since, stop, head_before, tries, recipient, before):
    """The one finishing continuation of the last attempt in `tries`, or None when it does not qualify:
    a Claude call that ended normally (no error, exit 0, not stopped, timed out, at its turn cap or at a
    usage limit) and was decided `unverified`, with a session id, no pointer of the worker's own, no
    STOP, at least FINISH_MIN_S of the TIMEBOX left and G4 not hit."""
    t = tries[-1]
    res = t.res
    if (not isinstance(call, headless.Call) or t.kind == "finish" or t.status != "info"
            or not str(t.summary).startswith("unverified:") or t.stopped or t.dead or t.timed_out
            or res.error is not None or res.code or res.is_error or holds.is_limit(res.limit_text, cfg)
            or res.data.get("subtype") == "error_max_turns" or res.session_id is None
            or filing.worker_pointers(recipient, job.unit, job.to) - before
            or stop.signum is not None or runs.stop_requested(job.project, job.unit, since)):
        return None
    began = units.parse_iso(tries[0].started)
    left = call.timeout - ((units.now() - began).total_seconds() if began is not None else 0)
    if left < FINISH_MIN_S:
        return None
    capped = _spend_hit(job, cfg, tries)
    if capped:
        log.log("hooks", f"runner: {job.unit}: no finishing continuation: spend cap ({capped})")
        return None
    gap = str(t.summary).splitlines()[0].removeprefix("unverified:").strip()
    log.log("hooks", f"runner: {job.unit}: ended unverified ({gap}); resuming once to finish")
    more = replace(call, resume=res.session_id, max_turns=FINISH_TURNS, timeout=left)
    prior = res.cost_usd if isinstance(res.cost_usd, (int, float)) and not isinstance(res.cost_usd, bool) else None
    return _attempt(reg, job, more, cfg, lock, since, stop, head_before, FINISH_PROMPT.format(gap=gap),
                    kind="finish", prior=prior)


def _run_locked(reg, job, call, cfg, lock, since, stop) -> tuple:
    """(record, the STOP marker's mtime_ns this run consumed, else None). An attempt that ends
    unverified gets one finishing continuation first (_finish); a mechanical attempt that then still
    fails or ends unverified re-runs once at the standard rung (ladder.escalate), in this run."""
    raw = timebox_raw(job.timebox)
    if raw is not None and raw > MAX_TIMEBOX_S:
        log.log("hooks", f"runner: {job.unit}: TIMEBOX {job.timebox!r} capped at {MAX_TIMEBOX_S // 3600}h")
    head_before = shell.git_head(job.worktree)
    if job.kind == "feat":     # R28: start in sync with the base; head_before is read after the merge
        job.sync_note = publish.sync(job.worktree, publish.base(job.root or job.worktree, cfg, job.worktree, job.project)) or ""
        head_before = shell.git_head(job.worktree)
    filing.set_aside_report(job.unit_dir)
    recipient = filing.recipient(reg, who(job, call))
    before = filing.worker_pointers(recipient, job.unit, job.to)
    if stop.signum is not None or runs.stop_requested(job.project, job.unit, since):
        return _not_started(reg, job, since, stop)      # STOP/SIGTERM before claude started
    _audit(job, cfg)
    base_prompt = prompt(job, cfg)
    tries = [_attempt(reg, job, call, cfg, lock, since, stop, head_before, base_prompt)]
    more = _finish(reg, job, call, cfg, lock, since, stop, head_before, tries, recipient, before)
    if more is not None:
        tries.append(more)
    t = tries[-1]
    limited = holds.is_limit(t.res.limit_text, cfg)      # §8.5: a usage limit holds the unit; no climb
    if (not limited
            and ladder.escalate(cfg, job.tier, bool(job.pinned), t.status, t.summary, stopped=t.stopped,
                                timed_out=t.timed_out, turn_cap=t.res.data.get("subtype") == "error_max_turns",
                                dead=t.dead)
            and not filing.worker_pointers(recipient, job.unit, job.to) - before
            and stop.signum is None and not runs.stop_requested(job.project, job.unit, since)):
        capped = _spend_hit(job, cfg, tries)      # G4 before the paid escalation attempt
        if capped:
            log.log("hooks", f"runner: {job.unit}: no escalation: spend cap ({capped})")
            t.summary = f"{t.summary}\nno escalation: spend cap ({capped})".strip()
        else:
            std = ladder.rung(cfg, job.base or job.model, "standard")
            up = replace(call, model=std.model, effort=std.effort)
            log.log("hooks", f"runner: {job.unit}: {call.model} {call.effort} ended {t.status}; "
                             f"escalating once to {up.model} {up.effort}")
            text = base_prompt + ladder.previous(call.model, call.effort, t.status, mask.redact(t.res.text or "")[0])
            tries.append(_attempt(reg, job, up, cfg, lock, since, stop, head_before, text, kind="escalation"))
    t = tries[-1]
    stopped, stop_at, owner, status, summary, head, result = t.stopped, t.stop_at, t.owner, t.status, t.summary, t.head, t.res.text
    pr = review = held_vendor = held_stage = None
    quiet = False
    hold, hit = (None, None) if stopped else _hold_of(job, cfg, tries)
    if hold is None and status == "done" and job.gated and job.team:   # team mode: the owner's "push" publishes
        line = publish.ready_line(job.unit, job.branch)
        from .. import inbox
        summary = "\n".join(str(summary or "").splitlines()[:inbox.MAX_LINES - 1] + [line]).lstrip("\n")
        result = f"{str(result or '').rstrip()}\n\n{line}\n"
        t.summary = summary
    elif hold is None and status == "done" and job.gated:   # once, after the final attempt: a failed first attempt never publishes
        status, summary, owner, pr, result = _publish(reg, job, summary, owner, head, result, cfg)
        t.status, t.summary = status, summary
        if pr is not None:                 # the head review alone; a cross-check runs only on request
            from ..review import build as _br
            if _br.after_publish(job.project, pr["number"], job.unit):     # A.11: the review's pointer wakes
                review, quiet = {"build": {"verdict": "pending"}}, True
    if hold is not None:      # §8.5: never `failed`; the heartbeat re-runs the unit once the hold is over
        held_vendor, held_stage = vendors.vendor_of(hit.call.model), "work"
        status, summary = "info", f"{holds.held_text(hold, held_vendor)} (stage {held_stage})"
        hit.status, hit.summary = status, summary
    if job.pin_note:
        summary = f"{summary}\n{job.pin_note}".strip()
    t = tries[-1]          # the last attempt: exit code, error, stop reason
    call, started, finished, res = t.call, tries[0].started, tries[-1].finished, t.res
    data, code, error = res.data, res.code, res.failure
    w = who(job, call)
    cost = _sum(x.cost for x in tries) if len(tries) > 1 else t.cost
    usage = t.usage
    try:
        filing.write_report(job.unit_dir, result)
    except OSError as exc:
        raise filing.FilingError(f"REPORT.md: {exc}") from exc
    worker_filed = bool(filing.worker_pointers(recipient, job.unit, job.to) - before)
    filed = not worker_filed and bool(recipient)      # an unroutable target: nothing is filed (logged)
    held = filing.holds_of(job.unit_dir)     # §8.5 repeat guard: this run held → +1; a done pointer → 0
    held = held + 1 if hold is not None else 0 if status == "done" else held
    record = filing.run_record(      # a hold: its stage and THIS vendor, whose hold the heartbeat waits for
        w, started=started, finished=finished, timeout_s=call.timeout, exit_code=code,
        is_error=res.is_error or (error is not None and not stopped),
        stop_reason="usage_limit" if hold is not None else stop_reason_of(data, error, stopped, timed_out=t.timed_out),
        cost_usd=cost, num_turns=_sum(x.res.turns for x in tries) if len(tries) > 1 else res.turns, usage=usage,
        result_tail=mask.redact(result[-filing.TAIL_CHARS:])[0], error=error,
        head_before=head_before, head_sha=head, status=status, pointer_filed=filed, pr=pr, review=review,
        stage=held_stage, held_vendor=held_vendor, holds=held)
    try:
        util.write_json(job.unit_dir / "RUN.json", record)
    except OSError as exc:
        raise filing.FilingError(f"RUN.json: {exc}") from exc
    for x in tries:       # the model calls: one history line each
        history.attempt(job.project, job.rev, unit=job.unit, to=job.to, model=x.call.model, effort=x.call.effort, res=x.res, started=x.started,
                        finished=x.finished, status=x.status, kind=x.kind, cfg=cfg, stop_reason=record["stop_reason"],
                        ran=not x.dead, spent=x.cost)
    if worker_filed:
        log.log("hooks", f"runner: {job.unit}: the worker filed its own pointer; the runner files none")
    else:
        try:
            filing.file_pointer(reg, w, job.rev, job.unit_dir, status, summary, head,   # logs: unroutable
                                extra={"owner": owner, "pr": pr["number"] if pr else None,
                                       **({"review": review} if review else {}),
                                       **({"quiet": True} if hold is not None or quiet else {})})
        except OSError as exc:
            raise filing.FilingError(f"pointer: {exc}") from exc
    units.touch_activity(job.to)
    filing.events(w, record["cost_usd"], record["num_turns"], status, record["stop_reason"],
                  rung=f"{job.tier}:{call.model}:{call.effort}", attempts=len(tries),
                  escalated=any(x.kind == "escalation" for x in tries))
    late = runs.marker_time(job.project, job.unit)
    if stopped:
        return record, stop_at
    if late is not None and late >= since:
        # STOP/AMEND arrived after claude finished: the result is filed; nothing new to file.
        log.log("events", f"run unit={job.unit} to={job.to} delivery=runner-stop-late")
        return record, late
    return record, None
