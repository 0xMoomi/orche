"""The gate (R-QUEUE-4): `scripts/verify/ci.sh` when the tested commit tracks it as
executable, then the entry's committed VERIFY scripts. Each step is an argv (no shell) run in the
checkout, in its own process group, inside one time budget for the whole gate, its output appended to
the gate log. The verdict is ci.sh's last `verdict:` line, else type-check-only. Model-free; it never
reads CI status or commit messages, and a red gate is never re-run here (R14).

The gate is the project's own code run as the user, with the network: nothing sandboxes it beyond its
environment (env(): no secrets, no session, run or home names) and its process group. Every process of
one take carries ORCHE_QUEUE_RUN=<tag> in its environment; a lost integrator's gate is stopped through
that tag and through the step it recorded, verified beyond doubt (lost_pids), and nothing else is
signalled (a recorded pgid alone may be anyone's after a reboot)."""
import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

from .. import mask, procs
from . import repo

CI = "scripts/verify/ci.sh"
VERDICT_RE = re.compile(r"^\s*verdict:\s*(type-check-only|unit-test-verified|live-ui-verified)\s*$")
DEFAULT_VERDICT = "type-check-only"
TAG = "ORCHE_QUEUE_RUN"
DROP_ENV = ("ORCHE_SESSION", "ORCHE_RUNNER", "ORCHE_RUN_ID", "ORCHE_HOME", "CODEX_HOME", "ORCHE_QUEUE_PR", TAG)
DROP_PREFIXES = ("CLAUDE", "ANTHROPIC", "JEV_")
SECRET_RE = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|(?:^|_)AUTH(?:_|$)|_PWD$", re.IGNORECASE)
START_SLACK_S = 3       # a recorded step start and `ps`'s start of that process (whole seconds) agree within this
TAIL_LINES = 40
TAIL_BYTES = 64 * 1024
KILL_WAIT_S = 2.0


def steps(where, commit, verify=()) -> list:
    """[(name, argv)] the gate runs on `commit`: ci.sh (mode 100755 there), then each VERIFY command
    whose first token is a file tracked in `commit` (ci.sh named alone again is not run twice)."""
    out = []
    if repo.tree_mode(where, commit, CI) == "100755":
        out.append((CI, ["./" + CI]))
    for cmd in verify or ():
        try:
            argv = shlex.split(str(cmd))
        except ValueError:
            continue
        if not argv:
            continue
        rel = argv[0].removeprefix("./")
        if rel == CI and len(argv) == 1 and out and out[0][0] == CI:
            continue
        if repo.tree_mode(where, commit, rel):
            out.append((str(cmd), ["./" + rel, *argv[1:]]))
    return out


def secret(name) -> bool:
    """A name the gate never gets (A.13): a secret by its name (TOKEN, SECRET, KEY, PASSWORD, PASSWD,
    CREDENTIAL, an AUTH word such as SSH_AUTH_SOCK, *_PWD; any case), a CLAUDE*/ANTHROPIC*/JEV_* name,
    or ORCHE's and Codex's own (session, runner, run, ORCHE_HOME, CODEX_HOME, the queue's names)."""
    up = str(name).upper()
    return up in DROP_ENV or up.startswith(DROP_PREFIXES) or SECRET_RE.search(up) is not None


def env(pr=None, tag=None) -> dict:
    """The integrator's environment without every secret() name (PATH, HOME, LANG, TMPDIR kept), plus
    CI=1, ORCHE_QUEUE=1, ORCHE_QUEUE_PR=<N> (none for main's health gate) and ORCHE_QUEUE_RUN=<tag>."""
    out = {k: v for k, v in os.environ.items() if not secret(k)}
    out.update({"CI": "1", "ORCHE_QUEUE": "1"})
    if pr is not None:
        out["ORCHE_QUEUE_PR"] = str(pr)
    if tag:
        out[TAG] = str(tag)
    return out


def killpg(pgid) -> None:
    """SIGTERM the process group, SIGKILL what is left after KILL_WAIT_S. Never raises. Only for a
    step of this process (its group cannot be anyone else's yet); a lost gate goes through sweep()."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(int(pgid), sig)
        except (ProcessLookupError, PermissionError, ValueError, TypeError):
            return
        end = time.monotonic() + KILL_WAIT_S
        while time.monotonic() < end:
            try:
                os.killpg(int(pgid), 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)


def _token(text, item) -> bool:
    return bool(item) and re.search(r"(?:^|\s)" + re.escape(item) + r"(?=\s|$)", text or "") is not None


def carries(env_, tag) -> bool:
    """A procs.Proc env holds ORCHE_QUEUE_RUN=<tag> exactly (Linux: an entry; macOS: a whole token)."""
    if not tag or not env_:
        return False
    item = f"{TAG}={tag}"
    return item in env_ if isinstance(env_, tuple) else _token(env_, item)


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def is_leader(p, test) -> bool:
    """`p` (a procs.Proc) is the step the lost take recorded, beyond doubt: its pid and its group are the
    recorded pgid, it started within START_SLACK_S of the recorded start, its command line carries the
    step's command as a whole token, and an environment that can be read carries the take's tag (another
    tag or none: never). It covers a step whose environment the OS hides (/bin/sh on macOS)."""
    pgid, at = _int(test.get("pgid")), test.get("pgid_at")
    if pgid is None or pgid <= 1 or p.pid != pgid or p.pgid != pgid:
        return False
    if p.env and not carries(p.env, test.get("tag")):
        return False
    if not isinstance(at, (int, float)) or isinstance(at, bool) or p.start is None or abs(p.start - at) > START_SLACK_S:
        return False
    return _token(p.args, str(test.get("pgid_cmd") or ""))


def lost_pids(rows, test) -> set:
    """The pids a lost take's gate left running, from a process table ([procs.Proc]): those carrying its
    tag, its verified step (is_leader), their descendants and the members of groups they lead; never
    pid 0 or 1, this process or its parent."""
    found = {p.pid for p in rows if carries(p.env, test.get("tag")) or is_leader(p, test)}
    grew = bool(found)
    while grew:
        more = {p.pid for p in rows if p.pid not in found and (p.ppid in found or p.pgid in found)}
        found |= more
        grew = bool(more)
    return found - {0, 1, os.getpid(), os.getppid()}


def sweep(test, timeout=procs.TABLE_S):
    """Stop what a lost take's gate left running: each group led by one of lost_pids (a verified group,
    so also what it forks after the table was read) with killpg, then every pid of lost_pids (SIGTERM,
    SIGKILL after the wait). The pids found, or None when the process table could not be read (nothing
    is signalled then)."""
    if not (test.get("tag") or test.get("pgid")):
        return []
    rows = procs.table(timeout)
    if rows is None:
        return None
    pids = lost_pids(rows, test)
    for leader in sorted({p.pid for p in rows if p.pid in pids and p.pgid == p.pid}):
        killpg(leader)
    if pids:
        procs.kill(pids)
    return sorted(pids)


def _verdict(log, offset):
    try:
        with open(log, "rb") as fh:
            fh.seek(offset)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = [m.group(1) for m in (VERDICT_RE.match(ln) for ln in text.splitlines()) if m]
    return found[-1] if found else None


def run(where, steps_, *, env_, timeout_s, log, on_step=None) -> dict:
    """Run `steps_` in order. {"result": green|red|timeout, "step", "code", "verdict", "secs", "gate",
    "log"}; red/timeout name the step that stopped it. `on_step({"pgid", "pgid_at", "pgid_cmd"})` is told
    each step's process group, its start (epoch seconds) and command as it starts, and the same keys set
    to None when it ends (the integrator records them so a lost take's step can be verified)."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    deadline = began + max(1, int(timeout_s))
    verdict, res = None, {"result": "green", "step": None, "code": 0}
    with open(log, "ab") as fh:
        for name, argv in steps_:
            fh.write(f"$ {name}\n".encode())
            fh.flush()
            offset = log.stat().st_size
            left = deadline - time.monotonic()
            if left <= 0:
                res = {"result": "timeout", "step": name, "code": None}
                break
            try:
                proc = subprocess.Popen(argv, cwd=str(where), env=env_, stdin=subprocess.DEVNULL, stdout=fh,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                fh.write(f"{exc}\n".encode())
                res = {"result": "red", "step": name, "code": 127}
                break
            if on_step:
                on_step({"pgid": proc.pid, "pgid_at": time.time(), "pgid_cmd": argv[0]})
            try:
                code = proc.wait(timeout=left)
            except subprocess.TimeoutExpired:
                killpg(proc.pid)
                proc.wait()
                fh.write(f"[orche queue] {name}: the gate's {int(timeout_s)} s ran out\n".encode())
                res = {"result": "timeout", "step": name, "code": None}
                break
            finally:
                if on_step:
                    on_step({"pgid": None, "pgid_at": None, "pgid_cmd": None})
            killpg(proc.pid)            # whatever the step left running in its group
            if code != 0:
                res = {"result": "red", "step": name, "code": code}
                break
            if name == CI:
                verdict = _verdict(log, offset)
    res.update({"verdict": (verdict or DEFAULT_VERDICT) if res["result"] == "green" else None,
                "secs": int(time.monotonic() - began), "gate": " + ".join(n for n, _a in steps_) or "-",
                "log": str(log)})
    return res


def tail(log, n=TAIL_LINES) -> str:
    """The log's last `n` lines, masked ("" when unreadable)."""
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return ""
    lines = data.decode("utf-8", "replace").splitlines()[-n:]
    return mask.redact("\n".join(lines))[0]
