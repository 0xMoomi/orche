"""The runner: execute one unit headless with `claude -p` in its worktree (`orche run <unit>`).

resolve() reads the unit's BRIEF.md and the registry; build_argv() picks model, turns and tools
by TIER; execute() runs claude with the brief + runner footer on stdin; run() takes a project
run slot and the target's lock (runs.py), records RUN.json / REPORT.md and files the inbox pointer
from the target. file_unrun() is the fallback record + pointer when a run cannot start or the
runner fails, so a unit never ends without a pointer; when `orche run` itself dies (SIGKILL, OOM)
runs.reconcile() files it later through file_lost().
Every pointer the runner files carries `via: runner`; only other pointers count as "the worker
filed its own report".

`STATUS: done` from a gated target (dispatch.gated_sender: the Build division, feature agents) is
filed as `done` only when the run produced a new commit, the worktree is clean and the ledger gate
(cmd_report.done_gate) passes for the new HEAD, otherwise `info` "unverified: … (<reason>)"; from
any other target it is filed as `done`, as `orche report done` would.

A STOP/AMEND from Main (runs.stop()) writes the unit's STOP marker, which the run polls every
STOP_POLL_S while claude runs, and SIGTERMs `orche run` when `ps` verifies its pid (the fast
path). A marker written after the run was requested (run_since()) stops claude as a SIGTERM would.
SIGTERM (from runs.stop(), or any other kill) is handled by phase:
waiting for a slot or starting -> claude never runs (or is killed the moment it exists) and the
run files "stopped by Main before start" (STOP marker) or `failed`; running -> claude's process
group gets SIGTERM, SIGKILL after KILL_WAIT_S, and the run files "stopped by Main" or `failed`;
finishing (claude already exited) -> the result is filed as usual and a late STOP marker is
removed (`runner-stop-late`). After every run claude's process group is swept (SIGTERM, SIGKILL
after SWEEP_WAIT_S), then every process carrying the run's ORCHE_RUN_ID (procs.sweep: also one
that left the group), so no child outlives it.

Unlike the rest of ORCHE this module calls subprocess directly (shell.run has no cwd/env and
cannot kill a process group on timeout).
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from . import cmd_report, config, dispatch, inbox, ledger, log, mask, paths, procs, runs, shell, units, util

EXIT_USER, EXIT_NOT_FOUND = 1, 2
OPUS = "claude-opus-5-5"
HARD_MAX_TURNS = 40
MECH_TOOLS = ("Read,Write,Edit,Glob,Grep,Bash(ls:*),Bash(test:*),Bash(git status:*),Bash(git diff:*),"
              "Bash(git add:*),Bash(git commit:*)")
# A mechanical unit may commit. These prefix rules are a best-effort first filter (`--amen`, `-anm`
# or `--no-veri` pass them); the done gate's ancestry check (history_rewritten) is the guarantee
# against an amend, reset or rebase, and `--no-verify` is left to the brief and the ledger verifier.
MECH_DENIED = ("Bash(git commit --amend*),Bash(git commit -n*),Bash(git commit --no-verify*),"
               "Bash(git push*)")
DEFAULT_TIMEBOX_S = 30 * 60
MAX_TIMEBOX_S = 4 * 3600
GRACE_S = 60
TAIL_CHARS = 2000
KILL_WAIT_S = 5
SWEEP_WAIT_S = 2
STOP_POLL_S = 2.0    # while claude runs, the unit's STOP marker is checked this often
VIA = "runner"       # pointer field `via` on every pointer the runner files
FOOTER = ("You run headless. Work only inside {worktree}. Do not commit, push, merge or rebase unless "
          "the brief allows it. Run VERIFY yourself and paste its output. Do not run `orche report`: "
          "the runner files your pointer from your final message. End with one line "
          "`STATUS: done|blocked|failed` and a ≤5-line summary. Your final message is filed as the "
          "unit's REPORT.md.")
STOPPED = "stopped by Main"
STOPPED_BEFORE_START = "stopped by Main before start"
# CLAUDE_CODE_* variables that carry authentication / provider choice: the worker keeps them.
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
            "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE")
# Main's own variables that never reach the worker (ORCHE_RUN_ID is set anew for every run).
DROPPED_ENV = ("CLAUDECODE", "CLAUDE_PROJECT_DIR", "ORCA_TERMINAL_HANDLE", "ORCHE_RUN_SINCE", procs.ENV)
_TO = re.compile(r"^to:\s*(\S+)", re.MULTILINE)
_TIMEBOX = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)(?![a-z])", re.IGNORECASE)
# The word must not run on into `|`: the footer's template `STATUS: done|blocked|failed` is no claim.
_STATUS = re.compile(r"^\W*STATUS:\s*([A-Za-z-]+)(?![\w|-])", re.MULTILINE | re.IGNORECASE)


class RunError(Exception):
    def __init__(self, code, message, job=None):
        super().__init__(message)
        self.code = code
        self.job = job     # set when the target is known (a pointer can still be filed)


class FilingError(Exception):
    """REPORT.md, RUN.json or the pointer of a finished run could not be written (ENOSPC, EACCES,
    …). run() keeps the lock: once this process is gone, runs.reconcile() files the run."""


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
    persona: Path = None
    timebox: str = ""


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
    """The unit's brief, target session, worktree and TIER. Raises RunError (exit 1 or 2)."""
    if not dispatch.UNIT_ID.match(unit or ""):
        raise RunError(EXIT_USER, f"bad unit id {unit!r} (want <proj>-<slug>-<yyyymmdd>-<n>)")
    proj, root = dispatch.repo_of_unit(reg, unit)
    if proj is None:
        raise RunError(EXIT_NOT_FOUND, f"unknown unit {unit!r} (no .orche/units/{unit}/ in a registered project)")
    folder = dispatch.unit_dir(root, unit)
    brief = folder / "BRIEF.md"
    try:
        text = brief.read_text(encoding="utf-8")
    except OSError:
        raise RunError(EXIT_NOT_FOUND, f"no brief: {brief}") from None
    fields = dispatch.read_brief_fields(root, unit)
    m = _TO.search(text)
    to = m.group(1) if m else fields.get("TO", "").strip()
    if not to or to not in units.all_sessions(reg):
        raise RunError(EXIT_NOT_FOUND, f"{unit}: unknown target session {to!r}")
    target = units.find_unit(reg, to)
    if target is not None:
        wt = units.worktree_path(target.entry.get("worktree", ""))
        branch = target.entry.get("branch") or "-"
    else:  # Main / liaison work in the main checkout
        wt, branch = str(root), "-"
    tier = fields.get("TIER", "").strip().lower()
    persona = Path(wt or root) / ".claude" / "agents" / f"{to}.md"
    job = Job(unit, proj, folder, brief, to, Path(wt or root), branch,
              tier if tier in dispatch.TIERS else "standard", timebox_seconds(fields.get("TIMEBOX")),
              persona if persona.is_file() else None, fields.get("TIMEBOX", "").strip())
    if target is not None and target.entry.get("status") == "retired":
        raise RunError(EXIT_NOT_FOUND, f"{unit}: target {to} is retired", job)
    if not wt or not Path(wt).is_dir():
        raise RunError(EXIT_NOT_FOUND, f"{unit}: worktree of {to} is missing ({wt or 'none recorded'})", job)
    return job


# ---- claude argv -----------------------------------------------------------

PROBE_TIMEOUT_S = 10


def claude_version(claude_bin, timeout=PROBE_TIMEOUT_S) -> str:
    res = shell.run([claude_bin, "--version"], timeout=timeout)
    return (res.out or "").strip() if res.ok else ""


def probe(claude_bin, version="", timeout=PROBE_TIMEOUT_S) -> dict:
    """Flags this `claude` lists in `--help` (read-only). claude 2.1.x lists the file form of the
    system-prompt flag only as `--append-system-prompt[-file]` inside another option's text."""
    res = shell.run([claude_bin, "--help"], timeout=timeout)
    text = (res.out or "") + (res.err or "")
    return {"bin": claude_bin, "version": version, "ok": res.ok,
            "system_prompt_file": "--append-system-prompt-file" in text or "--append-system-prompt[-file]" in text,
            "setting_sources": "--setting-sources" in text,
            "tools": bool(re.search(r"^\s*--tools\b", text, re.MULTILINE))}   # triage's `--tools ""`


def probe_path() -> Path:
    return paths.orche_home() / "runner" / "probe.json"


def claude_flags(cfg, write=True, timeout=PROBE_TIMEOUT_S) -> dict:
    """probe() once per `claude_bin` + `claude --version`, cached in ORCHE_HOME/runner/probe.json
    (its own file: the user's config.json is never rewritten; not on a dry run, and not when
    `claude --version` fails); a new claude version probes again. `timeout` bounds each call."""
    claude_bin = cfg.get("claude_bin") or "claude"
    version = claude_version(claude_bin, timeout)
    try:
        cached = util.read_json(probe_path(), default={}) or {}
    except util.JsonError:
        cached = {}
    if (isinstance(cached, dict) and cached.get("bin") == claude_bin and cached.get("ok") and version
            and cached.get("version") == version and "tools" in cached):
        return cached
    found = probe(claude_bin, version, timeout)
    if write and found["ok"] and version:
        try:
            util.write_json(probe_path(), found)
        except OSError as exc:
            log.log("hooks", f"runner: cannot cache claude --help probe: {exc}")
    return found


MCP_FLAG = "--mcp-config"


def claude_marker(run_id) -> str:
    """The fragment of run `run_id`'s claude command line naming its own empty MCP config
    (runs.mcp_config_path): no other run's claude has it. Reconcile uses it for a lost run's claude
    only when that process's environment is hidden, together with its pgid and start time
    (procs._is_claude)."""
    return f"{MCP_FLAG} {runs.mcp_config_path(run_id)}"


def with_run_id(argv, run_id) -> list:
    """`argv` (build_argv) with the run's own `--mcp-config` path, once the lock gave it its id."""
    out = list(argv)
    out[out.index(MCP_FLAG) + 1] = str(runs.mcp_config_path(run_id))
    return out


def model_for(tier, cfg) -> str:
    if tier == "mechanical":
        return (cfg.get("runner") or {}).get("model_mechanical") or "claude-sonnet-5-5"
    return OPUS


def max_turns_for(tier, cfg) -> int:
    return HARD_MAX_TURNS if tier == "hard" else int((cfg.get("runner") or {}).get("max_turns") or 20)


def tools_for(tier) -> str:
    return MECH_TOOLS if tier == "mechanical" else MECH_TOOLS + ",Bash"


def build_argv(job, cfg, flags) -> list:
    """claude's argv. Its `--mcp-config` path names the run id, which only the run slot assigns
    (runs.acquire): here it reads `<unit>@<started_ns>` (what `--dry-run` prints) until run()
    puts the real one in (with_run_id)."""
    argv = [cfg.get("claude_bin") or "claude", "-p", "--model", model_for(job.tier, cfg),
            "--output-format", "json", "--max-turns", str(max_turns_for(job.tier, cfg)),
            "--setting-sources", "project", "--strict-mcp-config",
            MCP_FLAG, str(runs.mcp_config_path(procs.run_id(job.unit, "<started_ns>"))),
            "--allowedTools", tools_for(job.tier)]
    if job.tier == "mechanical":
        argv += ["--disallowedTools", MECH_DENIED]
    if job.persona is not None:
        if flags.get("system_prompt_file"):
            argv += ["--append-system-prompt-file", str(job.persona)]
        else:   # fallback for a claude without the file flag (the persona is then visible in `ps`)
            argv += ["--append-system-prompt", job.persona.read_text(encoding="utf-8")]
    return argv


def prompt(job) -> str:
    text = job.brief.read_text(encoding="utf-8")
    return text.rstrip("\n") + "\n\n## Runner\n" + FOOTER.format(worktree=job.worktree) + "\n"


def _dropped(key) -> bool:
    """Main's Claude Code session variables never reach the worker (auth variables do)."""
    return key in DROPPED_ENV or (key.startswith("CLAUDE_CODE_") and key not in AUTH_ENV)


def env_for(job, run_id=None) -> dict:
    """The worker's environment; `run_id` becomes ORCHE_RUN_ID, the tag procs.sweep() kills by."""
    env = {k: v for k, v in os.environ.items() if not _dropped(k)}
    env["ORCHE_SESSION"] = job.to
    env["ORCHE_RUNNER"] = "1"       # `orche report` refuses: the runner files the pointer
    env["PATH"] = f"{paths.PLUGIN_ROOT / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    if run_id:
        env[procs.ENV] = run_id
    return env


# ---- execute ---------------------------------------------------------------

class _Signals:
    """SIGTERM state of this `orche run` (one run per process). `phase` is waiting | starting |
    running | finishing; `signum` is set by any SIGTERM; `killed` when it stopped a live claude."""

    def __init__(self):
        self.phase, self.signum, self.killed, self.pgid, self.timer = "idle", None, False, None, None
        self.clear_at = None    # the STOP marker (mtime_ns) this run consumed; removed after release


SIG = _Signals()


def _killpg(pgid, sig) -> None:
    """Signal claude's process group; never our own."""
    if not pgid or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


_TERM_LOCK = threading.RLock()   # the SIGTERM handler (main thread) and the STOP watcher share _terminate


def _terminate(pgid) -> None:
    """SIGTERM the group now, SIGKILL after KILL_WAIT_S (a timer: a signal handler never waits)."""
    with _TERM_LOCK:
        SIG.killed = True
        _killpg(pgid, signal.SIGTERM)
        if SIG.timer is None:
            SIG.timer = threading.Timer(KILL_WAIT_S, _killpg, (pgid, signal.SIGKILL))
            SIG.timer.daemon = True
            SIG.timer.start()


def _watch_stop(pgid, should_stop, done) -> None:
    """While claude runs: every STOP_POLL_S, `should_stop()` (the unit's STOP marker) -> the same
    kill as a SIGTERM to `orche run`. Ends when `done` is set or after the kill."""
    while not done.wait(STOP_POLL_S):
        try:
            hit = should_stop()
        except Exception as exc:  # noqa: BLE001 - a failed check never ends the watch
            log.log("hooks", f"runner: STOP marker check failed: {exc}")
            continue
        if hit and SIG.phase == "running" and not done.is_set():
            _terminate(pgid)
            return


def _on_sigterm(signum, frame):
    """Never raises: records the signal; while claude runs, also starts killing its group."""
    SIG.signum = signum
    if SIG.phase == "running" and SIG.pgid:
        _terminate(SIG.pgid)


def _group_alive(pgid) -> bool:
    try:
        os.killpg(pgid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def sweep_group(pgid) -> None:
    """After claude exited: SIGTERM whatever is left of its process group, SIGKILL after
    SWEEP_WAIT_S if anything remains."""
    if not pgid or pgid == os.getpgrp() or not _group_alive(pgid):
        return
    _killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + SWEEP_WAIT_S
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    _killpg(pgid, signal.SIGKILL)


def sweep_run(run_id) -> None:
    """After the group sweep: kill every process still carrying ORCHE_RUN_ID=<run_id> (one that
    left claude's process group, e.g. with setsid), SIGKILL after procs.WAIT_S."""
    if not run_id:
        return
    try:
        hit = procs.sweep(run_id)
    except procs.NoTime:
        hit, why = None, "no time left to read the process table"
    except Exception as exc:  # noqa: BLE001 - the sweep never fails the run
        hit, why = None, f"{type(exc).__name__}: {exc}"
    else:
        why = "the process table could not be read (`ps` failed or timed out)"
    unit = run_id.rsplit("@", 1)[0]
    if hit is None:
        log.log("hooks", f"runner: {unit}: run sweep skipped: {why}")
    elif hit:
        log.log("events", f"run unit={unit} runner-swept pids={','.join(map(str, hit))}")


def _kill_group(proc):
    """Timeout: SIGTERM claude's process group, SIGKILL after KILL_WAIT_S; (stdout, stderr)."""
    out = err = ""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _killpg(proc.pid, sig)
        try:
            out, err = proc.communicate(timeout=KILL_WAIT_S)
            break
        except subprocess.TimeoutExpired:
            out, err = "", ""
    return out or "", err or ""


def execute(argv, text, cwd, env, timeout, on_start=None, should_stop=None):
    """(exit code, stdout, stderr, error or None). The process group is killed on timeout, on a
    SIGTERM to `orche run`, and when `should_stop()` (polled every STOP_POLL_S by a watcher thread
    while claude runs) turns true; SIG.killed is then set. `on_start(proc)` runs once claude is up.
    On every exit path the group is swept afterwards, then every process tagged with the run's
    ORCHE_RUN_ID (env[procs.ENV]; sweep_run).

    The poll lives in a thread so that stdin goes through one `communicate()` call: retrying
    `communicate()` after a timeout can drop input not yet written (older CPython, e.g. 3.9)."""
    SIG.phase = "starting"     # a SIGTERM from here on is only recorded until claude's pgid is known
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as exc:
        return 127, "", "", f"cannot start {argv[0]}: {exc}"
    done, watcher = threading.Event(), None
    try:
        if on_start is not None:
            on_start(proc)
        SIG.pgid = proc.pid
        SIG.phase = "running"
        if SIG.signum is not None:     # arrived while starting: kill the child the moment it exists
            _terminate(proc.pid)
        if should_stop is not None:
            thread = threading.Thread(target=_watch_stop, args=(proc.pid, should_stop, done), daemon=True)
            thread.start()
            watcher = thread           # only a started thread is joined (a failed start must not mask the kill)
        try:
            out, err = proc.communicate(text, timeout=timeout)
            error = None
        except subprocess.TimeoutExpired:
            out, err = _kill_group(proc)
            error = f"timeout after {timeout}s"
    finally:
        done.set()
        if watcher is not None:
            watcher.join(timeout=STOP_POLL_S)
        SIG.phase = "finishing"        # claude is done: a SIGTERM now lets the result be filed
        if proc.returncode is None:    # an exception above: never leave claude running
            _kill_group(proc)
        if SIG.timer is not None:
            SIG.timer.cancel()
        sweep_group(proc.pid)
        sweep_run((env or {}).get(procs.ENV))
    code = proc.returncode if proc.returncode is not None else 124
    if SIG.killed and error is None:
        why = f"terminated by {signal.Signals(SIG.signum).name}" if SIG.signum is not None else "stopped by STOP marker"
        return code, out or "", err or "", why
    return code, out or "", err or "", error


def _as_result(data) -> dict:
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def parse_output(stdout) -> dict:
    """The `--output-format json` result object ({} when none is found).

    Noise around it (an update banner, a project hook echoing to stdout) is skipped: the whole
    stream first, then the last non-empty line, then the last JSON object with `"type": "result"`
    starting on a line of its own (single-line or running to the end of the stream).
    """
    text = stdout or ""
    try:
        return _as_result(json.loads(text))
    except ValueError:
        pass
    lines = text.splitlines(keepends=True)
    last = next((ln for ln in reversed(lines) if ln.strip()), "")
    try:
        fallback = _as_result(json.loads(last))
    except ValueError:
        fallback = {}
    pos = len(text)
    for line in reversed(lines):
        pos -= len(line)
        if not line.lstrip().startswith(("{", "[")):
            continue
        for chunk in (line, text[pos:]):
            try:
                data = _as_result(json.loads(chunk))
            except ValueError:
                continue
            if data.get("type") == "result":
                return data
    return fallback


def status_of(result) -> tuple:
    """(status word or None, the STATUS line or '', <= 5 summary lines) from the worker's last message."""
    lines = [ln.rstrip() for ln in str(result or "").splitlines()]
    hits = [i for i, ln in enumerate(lines) if _STATUS.match(ln)]
    if not hits:
        return None, "", "\n".join([ln for ln in lines if ln.strip()][-5:])
    i = hits[-1]
    after = [ln for ln in lines[i + 1:] if ln.strip()][:5]
    summary = after or [ln for ln in lines[:i] if ln.strip()][-5:]
    return _STATUS.match(lines[i]).group(1).lower(), lines[i].strip(), "\n".join(summary)


def worktree_clean(job) -> bool:
    """`git status --porcelain` shows nothing but the untracked files ORCHE itself writes."""
    from .cmd_feat import orche_owned          # lazy: cmd_feat pulls in the HR stack
    from .janitor.discover import porcelain_path
    res = shell.run(["git", "-C", str(job.worktree), "status", "--porcelain", "--untracked-files=all"],
                    timeout=30)
    if not res.ok:
        return False
    me = SimpleNamespace(session=job.to)
    return not [ln for ln in res.out.splitlines()
                if ln.strip() and not (ln.startswith("??") and orche_owned(porcelain_path(ln), me))]


def gated(reg, job) -> bool:
    """The target carries the `done` gate (Build division, feature agents), as in `orche report`."""
    return dispatch.gated_sender(units.find_unit(reg, job.to))


def history_rewritten(worktree, head_before, head):
    """None when the pre-run HEAD is an ancestor of `head` (the run only added commits); else why.
    This, not the mechanical deny list, is what catches an amend, reset or rebase in any spelling."""
    if not head_before:
        return "history rewritten (HEAD before the run unknown)"
    res = shell.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", head_before, head], timeout=30)
    if res.ok:
        return None
    return f"history rewritten ({head_before[:7]} is not an ancestor of HEAD)"


def done_problem(reg, job, head_before, head, clean):
    """Why a `STATUS: done` is not verified (None: it is). Ungated targets (Research, R&D, GTM,
    Ops, Main, …) need nothing more, as `orche report done` would; gated ones, at every tier, need a
    new commit that keeps the pre-run HEAD as an ancestor (history_rewritten), a clean tree and the
    ledger gate (cmd_report.done_gate) passing for the new HEAD."""
    if not gated(reg, job):
        return None
    if not head or head == head_before:
        return "no commit"
    rewritten = history_rewritten(job.worktree, head_before, head)
    if rewritten:
        return rewritten
    if not clean:
        return "uncommitted changes"
    if cmd_report.done_gate(reg, job.project, job.to, head):
        row = ledger.row_for_sha(job.project, head)
        if row is None:
            return f"no ledger row for {head[:7]}"
        return f"ledger verdict {row.get('verdict')} for {head[:7]}"
    return None


def decide(reg, job, data, error, code=0, head_before=None, head=None, clean=False):
    """(pointer status, summary) for the run outcome."""
    if error:
        return "failed", f"run failed: {error}"
    if data.get("is_error"):
        return "failed", f"run failed: claude reported an error ({data.get('subtype') or 'is_error'})"
    word, line, summary = status_of(data.get("result"))
    if code:
        note = f"run failed: claude exited {code} ({line or 'no STATUS line'})"
        return "failed", "\n".join(x for x in (note, summary) if x)
    if word in ("blocked", "failed"):
        return word, summary or line
    if word == "done":
        why = done_problem(reg, job, head_before, head, clean)
        if why is None:
            return "done", summary or line
        note = f"unverified: {line} ({why})"
    else:
        note = f"unverified: {line or 'no STATUS line'}"
    return "info", "\n".join(x for x in (note, summary) if x)


# ---- run -------------------------------------------------------------------

def _masked(value):
    return mask.redact(value)[0] if isinstance(value, str) else value


def _set_aside_report(job) -> None:
    """An earlier run's REPORT.md becomes REPORT.prev.md, so this run never links a stale one."""
    path = job.unit_dir / "REPORT.md"
    if path.is_file():
        os.replace(path, job.unit_dir / "REPORT.prev.md")


def _write_report(job, result) -> None:
    """REPORT.md = the masked result, unless the worker wrote one during this run."""
    path = job.unit_dir / "REPORT.md"
    if not path.exists() and str(result or "").strip():
        util.atomic_write(path, mask.redact(str(result))[0].rstrip("\n") + "\n")


def target_pointers(job, recipient) -> set:
    """Names of the pointers for this unit from the target in `recipient`'s inbox (pending or
    drained), leaving out the ones a runner filed (`via: runner`: a duplicate or stopped run).

    Taken before and after claude runs: a new name means the worker ran `orche report` despite the
    footer, and the runner then files none (one pointer per run)."""
    if not recipient:
        return set()
    folder = inbox.box(recipient)
    found = set()
    for path in list(folder.glob("*.json")) + list((folder / inbox.DRAINED).glob("*.json")):
        try:
            ptr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(ptr, dict) and ptr.get("unit") == job.unit and ptr.get("from") == job.to
                and ptr.get("via") != VIA):
            found.add(path.name)
    return found


def _recipient(reg, job):
    routed = cmd_report.route(reg, job.to)
    return routed[2] if routed else None


def _file_pointer(reg, job, status, summary, head, link_report=True) -> bool:
    """File the run's pointer from the target; False (logged) when the target routes nowhere."""
    to = _recipient(reg, job)
    if not to:
        log.log("hooks", f"runner: no recipient for {job.to}; pointer for {job.unit} not filed")
        return False
    report = job.unit_dir / "REPORT.md"
    linked = link_report and report.is_file()
    inbox.write(inbox.make(job.unit, job.to, to, status, report_path=str(report) if linked else "",
                           head_sha=head or "",
                           ledger_row=bool(head) and ledger.row_for_sha(job.project, head) is not None,
                           brief_rev=cmd_report.brief_rev(job.brief),
                           summary=cmd_report.clip(mask.redact(summary)[0]) or status, via=VIA))
    return True


def _events(job, model, cost, turns, status) -> None:
    log.log("events", f"run unit={job.unit} to={job.to} model={model} cost={cost} turns={turns} status={status}")


def file_unrun(reg, job, model, status, summary, error, stop_reason, record=True) -> dict:
    """Minimal RUN.json (unless `record` is False: another run of the unit is live and writes its
    own) + pointer when claude never ran (busy, duplicate, stopped before start) or the runner
    itself failed. The pointer never links a REPORT.md (it would be an earlier run's). Best effort:
    every step is attempted and logged. `pointer_filed` is False when the target routes nowhere."""
    now = util.now_iso()
    try:
        routed = bool(_recipient(reg, job))
    except Exception:  # noqa: BLE001 - _file_pointer below logs the same failure
        routed = False
    rec = {"unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
           "branch": job.branch, "tier": job.tier, "model": model, "started": now, "finished": now,
           "timeout_s": None, "exit_code": None, "is_error": status == "failed", "stop_reason": stop_reason,
           "cost_usd": None, "num_turns": None, "usage": None, "result_tail": "",
           "error": _masked(error), "head_before": None, "head_sha": None, "status": status,
           "pointer_filed": routed}
    steps =[lambda: _file_pointer(reg, job, status, summary, None, link_report=False),
             lambda: units.touch_activity(job.to),
             lambda: _events(job, model, None, None, status)]
    if record:
        steps.insert(0, lambda: util.write_json(job.unit_dir / "RUN.json", rec))
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - the pointer matters more than any single step
            log.log("hooks", f"runner: {job.unit}: cannot record the failed run: {exc}")
    return rec


def _lost_job(reg, data):
    """The Job of a lost run's unit (its target from the lock), else None (unit unknown)."""
    unit = data.get("unit")
    try:
        job = resolve(reg, unit)
    except RunError as exc:
        job = exc.job
    if job is None:
        proj, root = dispatch.repo_of_unit(reg, unit) if dispatch.UNIT_ID.match(unit or "") else (None, None)
        if proj is None:
            return None
        folder = dispatch.unit_dir(root, unit)
        job = Job(unit, proj, folder, folder / "BRIEF.md", data.get("to") or "", Path(root), "-",
                  "standard", DEFAULT_TIMEBOX_S)
    if data.get("to") and data["to"] != job.to:
        job = replace(job, to=data["to"])
    return job


def _pointer_since(job, recipient, started) -> bool:
    """A pointer for the unit from the target — the runner's (`via: runner`) or one the worker
    filed itself (`orche report`) — filed at or after `started`, is in `recipient`'s inbox
    (pending or drained): Main already heard from this run."""
    if not recipient:
        return False
    since = units.parse_iso(started)
    folder = inbox.box(recipient)
    for path in list(folder.glob("*.json")) + list((folder / inbox.DRAINED).glob("*.json")):
        try:
            ptr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not (isinstance(ptr, dict) and ptr.get("unit") == job.unit and ptr.get("from") == job.to):
            continue
        at = units.parse_iso(ptr.get("at"))
        if since is None or (at is not None and at >= since):
            return True
    return False


def _record_since(job, started) -> bool:
    """RUN.json already holds this run (its `started` is at or after the lock's)."""
    try:
        rec = util.read_json(job.unit_dir / "RUN.json", default=None)
    except util.JsonError:
        return False
    since, at = units.parse_iso(started), units.parse_iso((rec or {}).get("started"))
    return isinstance(rec, dict) and since is not None and at is not None and at >= since


def file_lost(reg, data) -> str:
    """A lost run (runs.reconcile: its `orche run` died without filing) from its lock record
    `data`. "stale-lock": a pointer of this run (the runner's or the worker's own) is already
    filed; "unknown-unit": no registered project has the unit, nothing to file; "unroutable": the
    target routes to no recipient: the minimal RUN.json (`pointer_filed: false`) and no pointer
    (logged); "reconciled": a minimal RUN.json (`stop_reason: lost`; kept when the run had written
    its own) and `failed` "runner died: …" from the target to Main (feature: Build), events
    `runner-reconciled`, activity.
    Raises OSError when a write fails (the caller keeps the lock and retries later)."""
    job = _lost_job(reg, data)
    if job is None:
        log.log("hooks", f"runner: lost run of unknown unit {data.get('unit')!r}: nothing to file")
        return "unknown-unit"
    started, pid = data.get("started"), data.get("pid")
    recipient = _recipient(reg, job)
    if _pointer_since(job, recipient, started):
        return "stale-lock"
    error = f"runner process {pid} died without filing"
    if not _record_since(job, started):
        cfg = config.load()
        util.write_json(job.unit_dir / "RUN.json", {
            "unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
            "branch": job.branch, "tier": job.tier, "model": model_for(job.tier, cfg),
            "started": started, "finished": util.now_iso(), "timeout_s": None, "exit_code": None,
            "is_error": True, "stop_reason": "lost", "cost_usd": None, "num_turns": None, "usage": None,
            "result_tail": "", "error": error, "head_before": None, "head_sha": None, "status": "failed",
            "pointer_filed": bool(recipient)})
    if not _file_pointer(
            reg, job, "failed", f"runner died: {job.unit} (pid {pid}, started {runs._clock(started)}); "
                                f"re-run with `orche run {job.unit}`", None, link_report=False):
        log.log("events", f"run unit={job.unit} to={job.to} runner-unroutable pid={pid} filed=false")
        return "unroutable"
    units.touch_activity(job.to)
    log.log("events", f"run unit={job.unit} to={job.to} runner-reconciled pid={pid}")
    return "reconciled"


def _handle_sigterm():
    """Install the phase-aware SIGTERM handler (main thread only); returns the previous handler."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signal.SIGTERM, _on_sigterm)


def run_since() -> int:
    """When this run was requested (time.time_ns()): ORCHE_RUN_SINCE from the Stop hook's spawn,
    else now (a manual `orche run`). Only a STOP marker written at or after it stops the run."""
    now = time.time_ns()
    try:
        since = int(os.environ.get("ORCHE_RUN_SINCE", ""))
    except ValueError:
        return now
    return since if 0 < since <= now else now


def _cancel(job, since):
    """Why a run waiting for a slot gives up: a STOP marker written since it started, or SIGTERM."""
    if runs.stop_requested(job.project, job.unit, since):
        return "stopped"
    return "signal" if SIG.signum is not None else None


def _not_started(reg, job, model, since, record=True) -> dict:
    """Claude never ran because of a STOP marker (`info` "stopped by Main before start") or a
    SIGTERM without one (`failed` "run failed: runner terminated by SIGTERM").

    `record` False means another run of the unit is live (an AMEND's old rev in its kill window):
    the marker is left to that run, which then files "stopped by Main" and clears it."""
    at = runs.marker_time(job.project, job.unit)
    if at is not None and at >= since:
        rec = file_unrun(reg, job, model, "info", STOPPED_BEFORE_START, STOPPED_BEFORE_START, "stopped", record)
        log.log("events", f"run unit={job.unit} to={job.to} delivery=runner-stopped")
        if record:
            SIG.clear_at = at
        return rec
    error = f"terminated by {signal.Signals(SIG.signum or signal.SIGTERM).name}"
    return file_unrun(reg, job, model, "failed", f"run failed: runner {error}", error, "error", record)


def run(reg, job, argv, cfg, timeout=None) -> dict:
    """Execute under a project run slot and the target's lock, then RUN.json, REPORT.md, the
    pointer and an events line. A STOP marker or SIGTERM while waiting or before claude starts
    files the pointer here, inside the lock (claude never runs).

    Raises runs.Busy / runs.Duplicate before anything runs.
    """
    rcfg = cfg.get("runner") or {}
    model = argv[argv.index("--model") + 1]
    since = run_since()
    global SIG
    SIG = _Signals()
    previous = _handle_sigterm()
    SIG.phase = "waiting"
    try:
        try:
            lock = runs.acquire(job.project, job.unit, {"to": job.to}, rcfg.get("max_parallel") or 4,
                                wait_s=rcfg.get("queue_wait_s"), cancel=lambda: _cancel(job, since), reg=reg)
        except runs.Cancelled:      # RUN.json unless another run of the unit is live (it writes its own)
            return _not_started(reg, job, model, since, record=runs.running(job.project, job.unit) is None)
        SIG.phase = "starting"
        keep = False
        rid = runs.run_id_of(lock)
        try:
            return _run_locked(reg, job, with_run_id(argv, rid), lock, rid, timeout, since)
        except FilingError:
            keep = True        # the lock outlives this process: reconcile files the run
            raise
        finally:
            runs.drop_mcp_config(rid)      # claude is gone; its path in the lock's argv still names it
            if not keep:
                runs.release(lock)
    finally:
        SIG.phase = "idle"
        if SIG.clear_at is not None:   # after release: a waiting AMEND run never sees our lock unmarked
            runs.clear_stop(job.project, job.unit, SIG.clear_at)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _run_locked(reg, job, argv, lock, rid, timeout, since) -> dict:
    util.write_json(runs.mcp_config_path(rid), {"mcpServers": {}})   # --strict-mcp-config: none
    if timeout is None:
        timeout = job.timebox_s + GRACE_S
        raw = _timebox_raw(job.timebox)
        if raw is not None and raw > MAX_TIMEBOX_S:
            log.log("hooks", f"runner: {job.unit}: TIMEBOX {job.timebox!r} capped at {MAX_TIMEBOX_S // 3600}h")
    started, model = util.now_iso(), argv[argv.index("--model") + 1]
    head_before = dispatch.git_head(job.worktree)
    _set_aside_report(job)
    recipient = _recipient(reg, job)
    before = target_pointers(job, recipient)
    if SIG.signum is not None or runs.stop_requested(job.project, job.unit, since):
        return _not_started(reg, job, model, since)      # STOP/SIGTERM before claude started
    code, out, err, error = execute(argv, prompt(job), job.worktree, env_for(job, rid), timeout,
                                    on_start=lambda proc: runs.update(lock, child_pid=proc.pid, pgid=proc.pid),
                                    should_stop=lambda: runs.stop_requested(job.project, job.unit, since))
    finished = util.now_iso()
    stop_at = runs.marker_time(job.project, job.unit)
    stopped = SIG.killed and stop_at is not None and stop_at >= since
    data = parse_output(out)
    if error is None and not data:
        error = f"exit {code}: " + ((err or out or "no output").strip().splitlines() or ["no output"])[-1][:300]
    error = _masked(error)
    result = data.get("result") if isinstance(data.get("result"), str) else ""
    head = dispatch.git_head(job.worktree)
    if stopped:
        status, summary = "info", STOPPED
    else:
        clean = worktree_clean(job) if head and head != head_before else False
        status, summary = decide(reg, job, data, error, code, head_before, head, clean)
    try:
        _write_report(job, result)
    except OSError as exc:
        raise FilingError(f"REPORT.md: {exc}") from exc
    worker_filed = bool(target_pointers(job, recipient) - before)
    filed = not worker_filed and bool(recipient)      # an unroutable target: nothing is filed (logged)
    record = {"unit": job.unit, "project": job.project, "to": job.to, "worktree": str(job.worktree),
              "branch": job.branch, "tier": job.tier, "model": model,
              "started": started, "finished": finished, "timeout_s": timeout,
              "exit_code": code, "is_error": bool(data.get("is_error")) or (error is not None and not stopped),
              "stop_reason": "stopped" if stopped else _masked(
                  data.get("stop_reason") or data.get("subtype") or ("error" if error else None)),
              "cost_usd": data.get("total_cost_usd", data.get("cost_usd")),
              "num_turns": data.get("num_turns"), "usage": data.get("usage"),
              "result_tail": mask.redact(result[-TAIL_CHARS:])[0], "error": error,
              "head_before": head_before, "head_sha": head, "status": status, "pointer_filed": filed}
    try:
        util.write_json(job.unit_dir / "RUN.json", record)
    except OSError as exc:
        raise FilingError(f"RUN.json: {exc}") from exc
    if worker_filed:
        log.log("hooks", f"runner: {job.unit}: the worker filed its own pointer; the runner files none")
    else:
        try:
            _file_pointer(reg, job, status, summary, head)    # logs a target that routes nowhere
        except OSError as exc:
            raise FilingError(f"pointer: {exc}") from exc
    units.touch_activity(job.to)
    _events(job, model, record["cost_usd"], record["num_turns"], status)
    late = runs.marker_time(job.project, job.unit)
    if stopped:
        SIG.clear_at = stop_at
    elif late is not None and late >= since:
        # STOP/AMEND arrived after claude finished: the result is filed; nothing new to file.
        SIG.clear_at = late
        log.log("events", f"run unit={job.unit} to={job.to} delivery=runner-stop-late")
    return record
