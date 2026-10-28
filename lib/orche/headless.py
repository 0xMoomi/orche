"""Every headless `claude -p` ORCHE starts (SPEC §14.10): one call shape, one environment rule,
one kill-and-sweep, one result parser. Callers bring only their policy.

A `Call` names a context:

- `project`: the unit worker. Project settings, `--permission-mode bypassPermissions` (a headless
  worker cannot answer prompts; the gates and the deny list still block), the run's own empty MCP
  config when tagged, the caller's tool rules, a persona file, ORCHE's PreToolUse gates through
  `--settings` (gates()), then, in a team repository, the team plugin's own (`Call.team`).
- `sealed`: intake and the Liaison. `--restricted`, `--tools` derived from the allowed rules, no
  session kept.
- `blank`: triage. No tools, no MCP config, no session kept, its own system prompt.

argv() and env() have no side effects: what a dry run prints is what runs. run() is the one process
lifecycle (start, wait, kill, sweep, clean up, parse); Stop turns a SIGTERM to this process into
a kill of the claude in progress. check() gives doctor's rows.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import log, paths, procs, registry, shell, util

CONTEXTS = ("project", "sealed", "blank")
FLAGS = ("--model", "--effort", "--output-format", "--setting-sources", "--permission-mode",
         "--restricted", "--tools", "--no-session-persistence", "--strict-mcp-config", "--mcp-config",
         "--allowedTools", "--disallowedTools", "--append-system-prompt-file", "--system-prompt",
         "--settings")
# CLAUDE_CODE_* variables that carry authentication or the provider choice: every call keeps them.
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
            "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE")
# The owned-namespace variables that pass (with AUTH_ENV); every other one is dropped.
PASS_ENV = ("ORCHE_HOME", "ORCHE_FAKE_ORCA", "ORCHE_SCRATCH_ROOT", "ORCHE_HOOK_BUDGET_S",
            "CLAUDE_CONFIG_DIR", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
OWNED = ("ORCHE_", "ORCA_", "CLAUDE", "JEV_")
_MCP_FLAG = "--mcp-config"
FILE_FLAG = "--append-system-prompt-file"
CHECK_TIMEOUT_S = 10
POLL_S, KILL_WAIT_S, SWEEP_WAIT_S = 2.0, 5, 2   # should_stop() poll; SIGTERM->SIGKILL grace; group sweep


@dataclass(frozen=True)
class Call:
    context: str                     # one of CONTEXTS
    model: str                       # --model, passed as given
    max_turns: int                   # --max-turns, >= 1
    timeout: float                   # seconds until the group is killed, > 0
    cwd: Path | None = None          # project/sealed: required; blank: None (run() makes a scratch dir)
    effort: str | None = None        # one of registry.EFFORTS; None: no --effort
    allowed: str = ""                # --allowedTools rules; sealed: also --tools (the names before "(")
    denied: str = ""                 # --disallowedTools rules
    append_file: Path | None = None  # --append-system-prompt-file (project, sealed)
    system: str | None = None        # --system-prompt (blank: required; others: never)
    env: dict = field(default_factory=dict)  # the caller's own variables, set last; never ORCHE_RUN_ID
    tag: str | None = None           # run tag (procs.run_id): ORCHE_RUN_ID, the marker, the tag sweep
    team: Path | None = None         # project: the installed team plugin, whose PreToolUse gates join ORCHE's


def _validate(call):
    if call.context not in CONTEXTS:
        raise ValueError(f"context must be one of {'|'.join(CONTEXTS)}, got {call.context!r}")
    if call.effort is not None and call.effort not in registry.EFFORTS:
        raise ValueError(f"effort must be one of {'|'.join(registry.EFFORTS)} or None, got {call.effort!r}")
    if call.max_turns < 1:
        raise ValueError(f"max_turns must be >= 1, got {call.max_turns!r}")
    if call.timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {call.timeout!r}")
    if procs.ENV in call.env:
        raise ValueError(f"{procs.ENV} comes from the tag, not from env")
    if call.context == "blank":
        extra = [name for name in ("cwd", "tag", "allowed", "denied", "append_file") if getattr(call, name)]
        if extra:
            raise ValueError(f"a blank call takes no {', '.join(extra)}")
        if not call.system:
            raise ValueError("a blank call needs system")
        return
    if call.cwd is None:
        raise ValueError(f"a {call.context} call needs cwd")
    if call.system is not None:
        raise ValueError(f"a {call.context} call takes no system (use append_file)")
    if call.context == "sealed" and not call.allowed:
        raise ValueError("a sealed call needs allowed")


def _rules(allowed):
    """The comma-separated rules of `allowed`; a comma inside parentheses belongs to its rule."""
    rules, depth, start = [], 0, 0
    for i, ch in enumerate(allowed):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            rules.append(allowed[start:i])
            start = i + 1
    rules.append(allowed[start:])
    return [r.strip() for r in rules if r.strip()]


def _tool_names(allowed) -> str:
    """`--tools` of a sealed call: each rule's tool name (before "("), unique, in order."""
    names = []
    for rule in _rules(allowed):
        name = rule.split("(", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return ",".join(names)


@dataclass(frozen=True)
class Result:
    code: int              # exit status; negative: killed by that signal; 124: none read; 127: not started
    data: dict             # the --output-format json result object; {} when stdout held none
    error: str | None      # why run() ended or never started claude; None if claude exited itself
    failure: str | None    # masked: error, else "exit N: <last line>" when data is {}, else None
    killed: bool           # run() killed claude for a Stop signal or should_stop()

    @property
    def text(self) -> str:
        value = self.data.get("result")
        return value if isinstance(value, str) else ""

    @property
    def is_error(self) -> bool:
        return bool(self.data.get("is_error"))

    @property
    def cost_usd(self):
        return self.data.get("total_cost_usd")

    @property
    def turns(self):
        return self.data.get("num_turns")

    @property
    def usage(self):
        return self.data.get("usage")

    @property
    def problem(self) -> str:
        """Why the call failed, '' when it did not: failure, an is_error result, a nonzero exit; the
        vendor named is the result's (`vendor`, set by codex.parse), else claude."""
        vendor = self.data.get("vendor") or "claude"
        if self.failure:
            return self.failure
        if self.is_error:
            return (f"{vendor} reported an error "
                    f"({self.data.get('error_text') or self.data.get('subtype') or 'is_error'})")
        if self.code:
            return f"{vendor} exited {self.code}"
        return ""

    @property
    def limit_text(self) -> str:
        """The text a usage or rate limit shows in (flow/usage.py, design §8.5): the failure line and,
        for an is_error result, Claude's result text or Codex's `error_text` (never Codex's final
        message: that is the model's). '' for a call that did not fail, was killed or hit its turn cap."""
        if self.killed or not (self.failure or self.is_error or self.code) \
                or self.data.get("subtype") == "error_max_turns":
            return ""
        own = (self.data.get("error_text") if self.data.get("vendor") == "codex"
               else self.text if self.is_error else "")
        return "\n".join(x for x in (self.failure, own) if isinstance(x, str) and x)


def _pre_tool_use(root) -> list:
    """The PreToolUse list of `<root>/hooks/hooks.json`, `${CLAUDE_PLUGIN_ROOT}` in each command replaced
    by `root`. Raises OSError, ValueError, KeyError or TypeError when it cannot be read."""
    data = json.loads((Path(root) / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    for entry in entries:
        for hook in entry["hooks"]:
            hook["command"] = hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(root))
    return entries


def gates(team=None) -> str:
    """The `--settings` value of a project call: compact `{"hooks": {"PreToolUse": [...]}}`, the
    PreToolUse list of hooks/hooks.json as it is, `${CLAUDE_PLUGIN_ROOT}` in each command replaced
    by the plugin root, then (`team`: the installed team plugin's folder) the team plugin's own
    PreToolUse entries, resolved against its folder. Read on every call, no cache. Headless calls load
    no user settings, so without this a worker runs no gate (design D8); only PreToolUse is passed,
    never a lifecycle hook. An unreadable ORCHE hooks.json or a missing PreToolUse list raises (OSError,
    ValueError, KeyError): a project worker without its gates must not start. An unreadable team
    hooks.json leaves ORCHE's gates alone."""
    entries = _pre_tool_use(paths.PLUGIN_ROOT)
    if team is not None:
        try:
            entries += _pre_tool_use(team)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log.log("hooks", f"headless: the team plugin's gates at {team} are unreadable; ORCHE's gates alone")
    return json.dumps({"hooks": {"PreToolUse": entries}}, separators=(",", ":"))


def argv(call: Call, cfg: dict) -> list:
    """The exact command line: bin from cfg["claude_bin"] or "claude". ValueError for an invalid
    call. No side effects: what dry runs print. A project call reads hooks.json for its gates
    (gates(), which raises when it cannot).

    The three shapes, after `-p --model M [--effort E] --output-format json --max-turns N`:
    project `--setting-sources project --permission-mode bypassPermissions` (a headless worker
    cannot answer prompts; the gates and the deny list still block); sealed `--restricted --tools
    <names>` (`--restricted` refuses bypass); blank `--setting-sources project --tools ""`. Then
    `--strict-mcp-config`, the tagged `--mcp-config`, the tool rules, `--no-session-persistence`
    (sealed, blank), the prompt file or system prompt, and the project call's `--settings`."""
    _validate(call)
    out = [cfg.get("claude_bin") or "claude", "-p", "--model", call.model]
    if call.effort is not None:
        out += ["--effort", call.effort]
    out += ["--output-format", "json", "--max-turns", str(call.max_turns)]
    if call.context == "project":
        out += ["--setting-sources", "project", "--permission-mode", "bypassPermissions"]
    elif call.context == "sealed":
        out += ["--restricted", "--tools", _tool_names(call.allowed)]
    else:
        out += ["--setting-sources", "project", "--tools", ""]
    out.append("--strict-mcp-config")
    if call.tag:
        out += [_MCP_FLAG, str(marker_path(call.tag))]
    if call.allowed:
        out += ["--allowedTools", call.allowed]
    if call.denied:
        out += ["--disallowedTools", call.denied]
    if call.context != "project":
        out.append("--no-session-persistence")
    if call.append_file is not None:
        out += [FILE_FLAG, str(call.append_file)]
    if call.system is not None:
        out += ["--system-prompt", call.system]
    if call.context == "project":
        out += ["--settings", gates(call.team)]
    return out


def _owned(name) -> bool:
    return name.startswith(OWNED) and name not in PASS_ENV and name not in AUTH_ENV


def base_env(cfg: dict, claude_auth: bool = True) -> dict:
    """The environment every headless vendor call starts from, from os.environ at call time.

    Everything inherited passes, except the owned namespaces (OWNED): there only PASS_ENV and
    AUTH_ENV pass. The configured Jev key variable never passes. The plugin's bin/ goes first on
    PATH. claude_auth: CLAUDE_CODE_OAUTH_TOKEN inherited, else the one `orche auth set` stored,
    else none (the run uses the login); reading the store is the only lookup, nothing is written.
    claude_auth=False (another vendor): no CLAUDE* or ANTHROPIC* name at all and no stored token (C5).
    Last, config `team.env` (the team's hook switches; config.load refused the owned names)."""
    from . import config, oauth  # lazy: hooks import this module for argv()/env() only
    jev_key_env = str((cfg.get("jev") or {}).get("api_key_env") or "JEV_API_KEY")
    out = {k: v for k, v in os.environ.items() if not _owned(k) and k != jev_key_env}
    if claude_auth:
        oauth.inject(out)
    else:
        out = {k: v for k, v in out.items()
               if not k.startswith(("CLAUDE", "ANTHROPIC")) and k not in AUTH_ENV}
    out["PATH"] = f"{paths.PLUGIN_ROOT / 'bin'}{os.pathsep}{out.get('PATH', '')}"
    out.update(config.team_env(cfg))
    return out


def env(call: Call, cfg: dict) -> dict:
    """The exact environment of a Claude call. No side effects: base_env(cfg), then call.env
    applies, then a tagged call gets ORCHE_RUN_ID (an untagged one has none)."""
    out = base_env(cfg)
    out.update({str(k): str(v) for k, v in call.env.items()})
    out.pop(procs.ENV, None)
    if call.tag:
        out[procs.ENV] = call.tag
    return out


def marker_dir() -> Path:
    """ORCHE_HOME/runner/mcp: where tagged calls keep their own empty MCP config."""
    return paths.orche_home() / "runner" / "mcp"


def marker_path(tag) -> Path:
    """The tagged call's own empty `--mcp-config` file (`{"mcpServers": {}}` while it runs)."""
    return marker_dir() / f"{tag}.json"


def marker(tag) -> str:
    """`--mcp-config <marker_path(tag)>`: the fragment of a tagged claude's command line that no
    other call has (procs._is_claude matches it for a lost run)."""
    return f"{_MCP_FLAG} {marker_path(tag)}"


def _listed(flag, text) -> bool:
    if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text):
        return True
    return flag == FILE_FLAG and "--append-system-prompt[-file]" in text


def check(cfg: dict) -> list:
    """Doctor rows (level, name, detail). Runs only `--version` and `--help` (10 s each); never raises."""
    claude_bin = cfg.get("claude_bin") or "claude"
    found = (claude_bin if os.path.isabs(claude_bin) and os.access(claude_bin, os.X_OK)
             else shutil.which(claude_bin))
    if not found:
        return [("WARN", "claude", f"{claude_bin} not found (config claude_bin); "
                                   "orche run, intake and triage cannot start claude")]
    res = shell.run([found, "--version"], timeout=CHECK_TIMEOUT_S)
    version = (res.out or "").strip().splitlines()[:1]
    rows = [("OK", "claude", f"{found} {version[0]}") if res.ok and version else
            ("WARN", "claude", f"{found} --version failed: {(res.err or res.out or '').strip()[:200]}")]
    res = shell.run([found, "--help"], timeout=CHECK_TIMEOUT_S)
    if not res.ok:
        rows.append(("WARN", "claude flags",
                     f"{found} --help failed: {(res.err or res.out or '').strip()[:200]}"))
        return rows
    text = (res.out or "") + (res.err or "")
    missing = [flag for flag in FLAGS if not _listed(flag, text)]
    rows.append(("OK", "claude flags", f"all {len(FLAGS)} listed in claude --help") if not missing else
                ("WARN", "claude flags", f"not listed in claude --help: {', '.join(missing)}; headless "
                                         "calls that use them fail (update Claude Code)"))
    return rows


def _killpg(pgid, sig) -> None:
    """Signal claude's process group; never our own."""
    if not pgid or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


class Stop:
    """`with Stop() as stop:` A SIGTERM to this process (main thread only) is recorded in
    stop.signum and kills the claude of any run(..., stop=stop) in progress (SIGTERM, then
    SIGKILL after KILL_WAIT_S; a blank call: SIGKILL). The previous handler comes back on exit.
    One per process at a time. Outside the main thread it installs and records nothing.

    It also holds the state of the call in progress (its group, the SIGKILL timer, whether it was
    killed): run() makes a private one when it is given none."""

    def __init__(self):
        self.signum = None
        self._lock = threading.RLock()   # the handler (main thread) and the STOP watcher share _terminate
        self._pgid, self._blank, self._timer, self._killed = None, False, None, False
        self._previous, self._installed = None, False

    @property
    def reason(self):
        """`terminated by <SIGNAME>` once a signal arrived (Result.error's text), else None."""
        if self.signum is None:
            return None
        try:
            return f"terminated by {signal.Signals(self.signum).name}"
        except ValueError:
            return f"terminated by signal {self.signum}"

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            self._previous = signal.signal(signal.SIGTERM, self._on_signal)
            self._installed = True
        return self

    def __exit__(self, *exc):
        if self._installed:
            signal.signal(signal.SIGTERM, self._previous if self._previous is not None else signal.SIG_DFL)
            self._installed = False
        return False

    def _on_signal(self, signum, frame):
        """Never raises; waits only for an in-flight kill (the lock): records the signal; while a
        call runs, starts killing it."""
        self.signum = signum
        if self._pgid:
            self._terminate()

    def _attach(self, pgid, blank) -> None:
        with self._lock:
            # _pgid last: a SIGTERM between assignments must not see the new group with the old flags.
            self._blank, self._timer, self._killed = blank, None, False
            self._pgid = pgid
            if self.signum is not None:     # arrived while starting: kill the child the moment it exists
                self._terminate()

    def _detach(self):
        """(killed, SIGKILL timer or None); a SIGTERM from now on is only recorded."""
        with self._lock:
            out = (self._killed, self._timer)
            self._pgid, self._timer = None, None
            return out

    def _terminate(self) -> None:
        """Blank: SIGKILL the group. Else SIGTERM it now, SIGKILL after KILL_WAIT_S (a timer)."""
        with self._lock:
            if not self._pgid:
                return
            self._killed = True
            if self._blank:
                _killpg(self._pgid, signal.SIGKILL)
                return
            _killpg(self._pgid, signal.SIGTERM)
            if self._timer is None:
                self._timer = threading.Timer(KILL_WAIT_S, _killpg, (self._pgid, signal.SIGKILL))
                self._timer.daemon = True
                self._timer.start()


def _watch(stop, should_stop, done) -> None:
    """While claude runs: every POLL_S, should_stop() -> the same kill as a Stop signal."""
    while not done.wait(POLL_S):
        try:
            hit = should_stop()
        except Exception as exc:  # noqa: BLE001 - a failed check never ends the watch
            log.log("hooks", f"runner: STOP marker check failed: {exc}")
            continue
        if hit and not done.is_set():
            stop._terminate()
            return


def _group_alive(pgid) -> bool:
    try:
        os.killpg(pgid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill(proc, blank):
    """Kill claude's group and collect (stdout, stderr). Blank: SIGKILL, reaped for 1 s. Else
    SIGTERM, SIGKILL after KILL_WAIT_S."""
    if blank:
        # If this 1 s reap times out, the second _kill in _wait's finally adds up to 1 s more and
        # run() reports code 124 (no exit status read).
        _killpg(proc.pid, signal.SIGKILL)
        try:
            out, err = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return out or "", err or ""
    out = err = ""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _killpg(proc.pid, sig)
        try:
            out, err = proc.communicate(timeout=KILL_WAIT_S)
            break
        except subprocess.TimeoutExpired:
            out, err = "", ""
    return out or "", err or ""


def _sweep_group(pgid, blank) -> None:
    """After claude exited: kill what is left of its group (blank: SIGKILL; else SIGTERM, SIGKILL
    after SWEEP_WAIT_S if anything remains)."""
    if not pgid or pgid == os.getpgrp() or not _group_alive(pgid):
        return
    if blank:
        _killpg(pgid, signal.SIGKILL)
        return
    _killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + SWEEP_WAIT_S
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    _killpg(pgid, signal.SIGKILL)


def _sweep_tag(tag) -> None:
    """Kill every process still carrying ORCHE_RUN_ID=<tag> (one that left claude's group)."""
    if not tag:
        return
    try:
        hit = procs.sweep(tag)
    except procs.NoTime:
        hit, why = None, "no time left to read the process table"
    except Exception as exc:  # noqa: BLE001 - the sweep never fails the call
        hit, why = None, f"{type(exc).__name__}: {exc}"
    else:
        why = "the process table could not be read (`ps` failed or timed out)"
    unit = tag.rsplit("@", 1)[0]
    if hit is None:
        log.log("hooks", f"runner: {unit}: run sweep skipped: {why}")
    elif hit:
        log.log("events", f"run unit={unit} runner-swept pids={','.join(map(str, hit))}")


def _scratch_dir() -> str:
    """A fresh empty directory outside $HOME (Claude Code's upward CLAUDE.md discovery finds nothing)."""
    base = tempfile.gettempdir()
    real, home = os.path.realpath(base), os.path.realpath(str(Path.home()))
    if real == home or real.startswith(home.rstrip(os.sep) + os.sep):
        base = "/tmp"
    return tempfile.mkdtemp(prefix="orche-blank-", dir=base)


def _as_result(data) -> dict:
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def _parse_output(stdout) -> dict:
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


def _wait(proc, stdin, timeout, blank, tag, stop, should_stop, on_start):
    """Steps 4-6 of run(): (stdout, stderr, timeout error or None, killed by stop/should_stop)."""
    done, watcher, error, out, err = threading.Event(), None, None, "", ""
    try:
        if on_start is not None:
            on_start(proc.pid)
        stop._attach(proc.pid, blank)
        if should_stop is not None:
            thread = threading.Thread(target=_watch, args=(stop, should_stop, done), daemon=True)
            thread.start()
            watcher = thread           # only a started thread is joined (a failed start must not mask the kill)
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            out, err = _kill(proc, blank)
            error = f"timeout after {timeout}s"
    finally:
        done.set()
        if watcher is not None:
            watcher.join(timeout=POLL_S)
        killed, timer = stop._detach()
        if proc.returncode is None:    # an exception above: never leave claude running
            _kill(proc, blank)
        if timer is not None:
            timer.cancel()
        _sweep_group(proc.pid, blank)
        _sweep_tag(tag)
    return out or "", err or "", error, killed


def spawn(cmd, env, cwd, stdin, *, timeout, blank=False, tag=None, stop=None, should_stop=None,
          on_start=None):
    """The one process lifecycle: start `cmd` in its own group, feed stdin, wait up to `timeout`,
    kill (blank: SIGKILL at once), sweep the group and the tag. (code, stdout, stderr, error,
    killed); code 127 with the error when it could not start, 124 when no exit status was read."""
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                                errors="replace", start_new_session=True)
    except OSError as exc:
        return 127, "", "", f"cannot start {cmd[0]}: {exc}", False
    out, err, error, killed = _wait(proc, stdin, timeout, blank, tag, stop if stop is not None else Stop(),
                                    should_stop, on_start)
    return (proc.returncode if proc.returncode is not None else 124), out, err, error, killed


def run(call: Call, stdin: str, cfg: dict, *, stop=None, should_stop=None, on_start=None) -> Result:
    """Start the call, feed stdin, wait up to call.timeout, kill, sweep, clean up, parse.

    Never raises for anything claude does. Raises ValueError (an invalid call), OSError (the
    marker or scratch dir could not be made), and, after claude is killed, KeyboardInterrupt or
    its own machinery's errors. `on_start(pid)` runs once claude is up; `should_stop()` is polled
    every POLL_S; a Stop kills the call on a SIGTERM to this process."""
    from . import mask  # lazy: hooks that import headless only for argv()/env() keep a small import closure
    cmd, call_env = argv(call, cfg), env(call, cfg)
    mcp = marker_path(call.tag) if call.tag else None
    scratch = None
    if mcp is not None:
        util.write_json(mcp, {"mcpServers": {}})      # --strict-mcp-config: none
    try:
        if call.context == "blank":
            scratch = _scratch_dir()
        code, out, err, error, killed = spawn(cmd, call_env, scratch or call.cwd, stdin, timeout=call.timeout,
                                              blank=call.context == "blank", tag=call.tag, stop=stop,
                                              should_stop=should_stop, on_start=on_start)
    finally:
        try:
            if mcp is not None:
                mcp.unlink(missing_ok=True)
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
        except OSError:
            pass
    data = _parse_output(out)
    if error is None and killed:
        error = (stop.reason if stop is not None else None) or "stopped by STOP marker"
    if error is not None:
        failure = mask.redact(error)[0]
    elif not data:
        tail = next((ln.strip() for text in (err, out) for ln in reversed(text.splitlines()) if ln.strip()),
                    "no output")
        failure = mask.redact(f"exit {code}: {tail}"[:300])[0]
    else:
        failure = None
    return Result(code, data, error, failure, killed)
