"""Adapter for the Orca CLI. Every call goes through shell.run and parses the JSON envelope."""
import json
import os
import re
import sys
import tempfile
import time

from . import config, log, paths, shell, util

ALLOWED = {
    "status": None,
    "repo": {"list", "add"},
    "worktree": {"list", "ps", "create", "set", "rm"},
    "terminal": {"list", "create", "send", "rename", "wait", "read", "close", "switch"},
    "orchestration": {"send", "check"},
    "automations": {"create", "list", "remove"},
}


class OrcaError(RuntimeError):
    def __init__(self, message, code="orca_error", argv=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.argv = argv or []


def call(group, sub=None, *args, dry_run=False, timeout=20):
    """Run `orca <group> [sub] [args...] --json` and return the envelope's `result`."""
    if group not in ALLOWED or (ALLOWED[group] is not None and sub not in ALLOWED[group]):
        raise OrcaError(f"unsupported orca command: {group} {sub or ''}".strip(), "unsupported")
    argv = [config.load().get("orca_bin", "orca"), group]
    if sub is not None:
        argv.append(sub)
    argv += [str(a) for a in args] + ["--json"]
    if dry_run:
        print("[dry-run] " + " ".join(argv), file=sys.stderr)
        return {"dryRun": True, "argv": argv}
    if os.environ.get("ORCHE_FAKE_ORCA") == "1" and _fake_allowed():
        return _fake_call(argv, group, sub, timeout)
    res = shell.run(argv, timeout=timeout)
    try:
        env = json.loads(res.out)
    except (ValueError, TypeError):
        log.log("orca", f"unparseable output code={res.code} argv={argv}")
        raise OrcaError((res.err or res.out or "no output").strip()[:300], "bad_output", argv)
    if not isinstance(env, dict) or not env.get("ok"):
        err = (env.get("error") if isinstance(env, dict) else None) or {}
        raise OrcaError(err.get("message", "orca call failed"), err.get("code", "orca_error"), argv)
    return env.get("result")


def _fake_allowed():
    """The test switch only works on a throwaway ORCHE_HOME (under the temp dir, or marked)."""
    home = os.path.realpath(paths.orche_home())
    tmp = os.path.realpath(tempfile.gettempdir())
    if home.startswith(tmp.rstrip(os.sep) + os.sep) or os.path.exists(os.path.join(home, ".orche-test-marker")):
        return True
    log.log("orca", f"ORCHE_FAKE_ORCA ignored: ORCHE_HOME {home} is not a test home")
    return False


def _fake_call(argv, group, sub, timeout):
    """TEST ONLY (ORCHE_FAKE_ORCA=1): record argv to logs/fake-orca.jsonl, answer from fake-orca.json.

    Subprocess tests (hooks) cannot swap shell.RUNNER, so this switch stands in for Orca.
    `ORCHE_HOME/fake-orca.json` maps "<group> <sub>" to the canned `result`; its optional
    `delay_s` makes every call slow (a call slower than `timeout` fails like a real timeout).
    """
    home = paths.orche_home()
    (home / "logs").mkdir(parents=True, exist_ok=True)
    with open(home / "logs" / "fake-orca.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv}) + "\n")
    canned = util.read_json(home / "fake-orca.json", default={}) or {}
    delay = float(canned.get("delay_s", 0) or 0)
    if delay:
        time.sleep(min(delay, timeout))
        if delay >= timeout:
            raise OrcaError(f"timeout after {timeout}s", "timeout", argv)
    key = f"{group} {sub}" if sub else group
    default = {"terminals": []} if key == "terminal list" else {}
    return canned.get(key, default)


def status(**kw):
    return call("status", **kw)


def repo_list(**kw):
    return call("repo", "list", **kw)


def worktree(sub, *args, **kw):
    return call("worktree", sub, *args, **kw)


def terminal(sub, *args, **kw):
    return call("terminal", sub, *args, **kw)


def orchestration(sub, *args, **kw):
    return call("orchestration", sub, *args, **kw)


def automations(sub, *args, **kw):
    return call("automations", sub, *args, **kw)


_LEAD = re.compile(r"^[\W_]+")


def normalize_title(title) -> str:
    """Pane title without leading glyphs/whitespace (Claude Code prefixes titles with e.g. `✳ `)."""
    return _LEAD.sub("", str(title or "")).strip()


def terminals_of(result) -> list:
    terms = result.get("terminals", []) if isinstance(result, dict) else result
    return [t for t in terms or [] if isinstance(t, dict)]


def match_title(terminals, title):
    """First terminal whose normalised title equals the normalised `title`, or None."""
    want = normalize_title(title)
    return next((t for t in terminals if normalize_title(t.get("title")) == want), None)


def disconnected(term) -> bool:
    """A listed terminal whose agent is gone: `orca terminal list` keeps it, title and all, as
    `disconnected` (after claude exited). A terminal that names no connection state is connected."""
    if not isinstance(term, dict):
        return False
    return term.get("connected") is False or any(
        str(term.get(k) or "").lower() == "disconnected" for k in ("status", "state", "connection"))


def find_terminal_by_title(title, *, timeout=20):
    """First terminal whose title matches `title` (leading glyphs ignored), or None."""
    return match_title(terminals_of(terminal("list", timeout=timeout)), title)


DEFAULT_HOOK_BUDGET_S = 3.0


def hook_deadline(started=None) -> float:
    """Monotonic deadline for a fail-open hook: start + $ORCHE_HOOK_BUDGET_S (default 3.0 s)."""
    try:
        budget = float(os.environ.get("ORCHE_HOOK_BUDGET_S") or DEFAULT_HOOK_BUDGET_S)
    except ValueError:
        budget = DEFAULT_HOOK_BUDGET_S
    return (time.monotonic() if started is None else started) + budget


class Terminals:
    """For hooks: one `terminal list` per run, every Orca call bounded by a shared deadline. Hooks never
    type into a pane (deliver.py does, detached)."""

    def __init__(self, deadline):
        self.deadline = deadline
        self._list = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def list(self) -> list:
        if self._list is None:
            self._list = []
            if self.remaining() <= 0:
                return self._list
            try:
                self._list = terminals_of(terminal("list", timeout=self.remaining()))
            except Exception as exc:  # noqa: BLE001 - callers degrade gracefully
                log.log("hooks", f"terminal list failed: {exc}")
        return self._list
