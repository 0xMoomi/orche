"""Every `codex exec` ORCHE starts (design C11 §5.2-5.6, §7.2, Appendix A.1): one call shape, one
environment rule, the process lifecycle of headless.spawn(), one JSONL parser, one cost estimate.

argv() and env() have no side effects: what a dry run prints is what runs. run() starts the call,
waits, kills, sweeps, parses and removes the `-o` file.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import headless, procs

SANDBOXES = ("workspace-write", "danger-full-access")
EFFORTS = ("low", "medium", "high")
ERROR_MAX = 300


def out_path(tag) -> Path:
    """The `-o` file of run `tag` (<unit>@<started_ns>): ORCHE_HOME/runner/codex/<tag>.md."""
    from . import paths
    return paths.orche_home() / "runner" / "codex" / f"{tag}.md"


@dataclass(frozen=True)
class Call:
    model: str                       # "codex" (Codex's own default) or "codex:<id>" (-m <id>)
    effort: str                      # one of EFFORTS: -c model_reasoning_effort
    timeout: float                   # seconds until the group is killed, > 0
    cwd: Path                        # -C: the worktree
    writable: tuple                  # sandbox_workspace_write.writable_roots (workspace-write only)
    out: Path                        # -o: the final message file, removed by run()
    sandbox: str = "workspace-write"  # one of SANDBOXES (models.codex_sandbox)
    env: dict = field(default_factory=dict)  # the caller's own variables, set last; never ORCHE_RUN_ID
    tag: str | None = None           # run tag (procs.run_id): ORCHE_RUN_ID, the tag sweep


def _validate(call):
    if call.model != "codex" and not (call.model.startswith("codex:") and call.model[6:]):
        raise ValueError(f"codex model must be codex or codex:<id>, not {call.model!r}")
    if call.effort not in EFFORTS:
        raise ValueError(f"codex effort must be one of {', '.join(EFFORTS)}, not {call.effort!r}")
    if call.sandbox not in SANDBOXES:
        raise ValueError(f"codex sandbox must be one of {', '.join(SANDBOXES)}, not {call.sandbox!r}")
    if not call.timeout or call.timeout <= 0:
        raise ValueError("codex timeout must be > 0")


def _bin(cfg) -> str:
    return str((cfg.get("models") or {}).get("codex_bin") or "codex")


def argv(call: Call, cfg: dict) -> list:
    """Appendix A.1, one argv entry per element (never a shell string)."""
    _validate(call)
    cmd = [_bin(cfg), "exec", "--json", "--color", "never", "--skip-git-repo-check", "--ignore-user-config",
           "-C", str(call.cwd), "-s", call.sandbox]
    if call.sandbox == "workspace-write":
        roots = json.dumps([str(p) for p in call.writable], separators=(",", ":"))
        cmd += ["-c", f"sandbox_workspace_write.writable_roots={roots}",
                "-c", "sandbox_workspace_write.network_access=true"]
    cmd += ["-c", 'approval_policy="never"', "-c", 'shell_environment_policy.inherit="all"']
    if call.model.startswith("codex:"):
        cmd += ["-m", call.model[6:]]
    cmd += ["-c", f'model_reasoning_effort="{call.effort}"', "-o", str(call.out), "-"]
    return cmd


def env(call: Call, cfg: dict) -> dict:
    """headless.base_env without Claude's auth (no CLAUDE* name, no stored token), then call.env,
    then a tagged call's ORCHE_RUN_ID. Codex's own variables (CODEX_HOME, …) pass as set."""
    out = headless.base_env(cfg, claude_auth=False)
    out.update({str(k): str(v) for k, v in call.env.items()})
    out.pop(procs.ENV, None)
    if call.tag:
        out[procs.ENV] = call.tag
    return out


_MALFORMED = set()   # price keys already logged as malformed (once per process)


def _price(prices, key):
    """models.prices[key] as three floats; None when missing or malformed (logged once per key)."""
    entry = prices.get(key) if isinstance(prices, dict) else None
    if entry is None:
        return None
    if isinstance(entry, (list, tuple)) and len(entry) == 3 \
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in entry):
        return tuple(float(x) for x in entry)
    if key not in _MALFORMED:
        _MALFORMED.add(key)
        from . import log
        log.log("events", f"codex prices malformed model={key}")
    return None


def cost(usage, model: str, cfg: dict):
    """§7.2: API-equivalent USD (4 places) from models.prices; None without usage or a price. A
    malformed entry counts as an unlisted id: the `codex` rate (logged once, never raises)."""
    if not isinstance(usage, dict) or not usage:
        return None
    prices = (cfg.get("models") or {}).get("prices") or {}
    model_id = model[6:] if model.startswith("codex:") else ""
    price = (_price(prices, model_id) if model_id else None) or _price(prices, "codex")
    if not price:
        return None
    p_in, p_cached, p_out = price
    num = lambda key: usage.get(key) if isinstance(usage.get(key), (int, float)) else 0  # noqa: E731
    inp, cached = num("input_tokens"), num("cached_input_tokens")
    uncached = inp - cached if cached <= inp else inp
    out = num("output_tokens") + num("reasoning_output_tokens")
    return round((uncached * p_in + cached * p_cached + out * p_out) / 1e6, 4)


def _error_text(text) -> str:
    from . import mask
    return mask.redact(str(text or "codex reported an error"))[0][:ERROR_MAX]


def parse(stdout: str, out_file, model: str, cfg: dict) -> dict:
    """§5.6: the JSONL events as Claude's result keys; {} when stdout held no event at all.

    A line that is not a JSON object is skipped. Usage is summed over turn.completed; the last
    agent_message is kept; the final text is the non-empty `-o` file, else that last message."""
    events = []
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            events.append(event)
    if not events:
        return {}
    usage, turns, last, failed, errored, error_text = {}, 0, "", False, False, None
    for event in events:
        kind = event["type"]
        if kind == "turn.completed":
            turns += 1
            for key, value in (event.get("usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0) + value
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                last = item["text"]
        elif kind == "turn.failed":
            failed = True
            error_text = _error_text((event.get("error") or {}).get("message"))
        elif kind == "error":
            errored = True
            error_text = error_text or _error_text(event.get("message"))
    final = ""
    try:
        final = Path(out_file).read_text(encoding="utf-8", errors="replace") if out_file else ""
    except OSError:
        pass
    if not final.strip():
        final = last
    if failed:
        subtype = "error_turn_failed"
    elif errored and not turns:
        subtype = "error_event"
    elif not final.strip():
        subtype = "error_no_message"
    else:
        subtype = "success"
    return {"type": "result", "vendor": "codex", "result": final, "is_error": subtype != "success",
            "subtype": subtype, "stop_reason": "completed" if subtype == "success" else subtype,
            "total_cost_usd": cost(usage, model, cfg), "num_turns": turns, "usage": usage,
            "error_text": error_text}


def run(call: Call, stdin: str, cfg: dict, *, stop=None, should_stop=None, on_start=None) -> headless.Result:
    """headless.spawn() + parse(); the `-o` file is gone afterwards. Never raises for anything codex
    does; raises ValueError for an invalid call, OSError when the `-o` folder cannot be made."""
    from . import mask
    cmd, call_env = argv(call, cfg), env(call, cfg)
    out_file = Path(call.out)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        code, out, err, error, killed = headless.spawn(
            cmd, call_env, call.cwd, stdin, timeout=call.timeout, tag=call.tag, stop=stop,
            should_stop=should_stop, on_start=on_start)
        data = parse(out, out_file, call.model, cfg)
    finally:
        try:
            out_file.unlink(missing_ok=True)
        except OSError:
            pass
    if error is None and killed:
        error = (stop.reason if stop is not None else None) or "stopped by STOP marker"
    if error is not None:
        failure = mask.redact(error)[0]
    elif not data:
        tail = next((ln.strip() for text in (err, out) for ln in reversed(text.splitlines()) if ln.strip()),
                    "no output")
        failure = mask.redact(f"codex exited {code}: {tail}"[:ERROR_MAX])[0]
    else:
        failure = None
    return headless.Result(code, data, error, failure, killed)
