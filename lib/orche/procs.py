"""Every process a run started, found by its environment tag `ORCHE_RUN_ID=<unit>@<started_ns>`.

The runner gives claude ORCHE_RUN_ID (headless.env); every process claude starts inherits it,
also one that left claude's process group (`setsid`, a daemon), which a process-group kill misses.

The tag counts only in a process's ENVIRONMENT, never in its arguments (`grep ORCHE_RUN_ID=<id>`
must survive). macOS/BSD: two tables, `ps -axww` (arguments only) and `ps -axwwE` (the environment
follows the arguments, with no separator); a pid's environment is its `-E` line after the exact
arguments-only prefix, and the tag must stand there as a whole token. A pid missing from either
table, listed twice, or whose prefix does not match is no environment match. Linux:
/proc/<pid>/environ, one exact entry. Both tables are read as text decoded with errors="replace"
(shell.py): a non-UTF-8 byte anywhere in the table never stops a sweep.

A process carrying the tag counts, and so do its descendants and the members of a group it leads:
macOS hides the environment of Apple platform binaries (/bin/sh, /bin/sleep, …) from `ps -E`, and
Linux the environment of another user's or a non-dumpable process; those are still found through a
tagged parent or group leader. A lost run's claude (reconcile) is a further root only when it is
that run's claude beyond doubt (see _is_claude).

sweep() kills them: SIGTERM, SIGKILL after `wait_s`. Never this process or its parent. No table
-> nothing is signalled: None when the table is unreadable (a `ps` failed, or timed out with its
full PS_TIMEOUT_S), NoTime raised when the caller's deadline cut the read short (a `ps` that could
not start, or timed out under a shorter timeout): "no time", never "ps broken" (runs/__init__.py counts
only the first toward SWEEP_TRIES). The Linux /proc walk keeps the same rules as one `ps`: it runs
in a daemon thread joined for at most PS_TIMEOUT_S (or what the deadline leaves), so a /proc read
that blocks (a process stuck in the kernel holding its memory lock) never stretches the caller.
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths, shell

ENV = "ORCHE_RUN_ID"
PS_TIMEOUT_S = 2.0   # each of the two `ps` calls (or the whole /proc walk)
TABLE_S = 2 * PS_TIMEOUT_S
WAIT_S = 2.0         # SIGTERM -> SIGKILL
MARGIN_S = 0.05      # a `ps` is started only when it can finish this long before the deadline
PROC = Path("/proc")
ARGS_PS = ["ps", "-axww", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "etime=", "-o", "command="]
ENV_PS = ["ps", "-axwwE", "-o", "pid=", "-o", "command="]


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    args: str        # the command line ('' when unknown)
    env: object      # str (macOS: what follows the arguments) / tuple (Linux: entries); '' / () /
                     # None: unreadable (hidden by the OS, another user's, or not determinable)
    start: object    # epoch seconds the process started, else None


def run_id(unit, started_ns) -> str:
    return f"{unit}@{started_ns}"


def _tag(rid):
    return re.compile(r"(?:^|\s)" + re.escape(f"{ENV}={rid}") + r"(?=\s|$)")


def carries(env, rid) -> bool:
    """`env` (a Proc.env) holds ORCHE_RUN_ID=<rid> exactly."""
    if not rid or not env:
        return False
    if isinstance(env, tuple):
        return f"{ENV}={rid}" in env
    return _tag(rid).search(env) is not None


def budget(deadline) -> float:
    return deadline - time.monotonic() - MARGIN_S


class NoTime(Exception):
    """The table read was cut short by the caller's deadline (not a failure of `ps`)."""


TIMED_OUT = 124     # shell.run's code for a command killed at its timeout


def _ps(argv, deadline):
    """The output; None when `ps` failed or timed out with its full PS_TIMEOUT_S; NoTime when
    the deadline left no time to start it or its shorter timeout ran out."""
    left = budget(deadline)
    if left <= 0:
        raise NoTime
    timeout = min(PS_TIMEOUT_S, left)
    res = shell.run(argv, timeout=timeout)
    if res.ok:
        return res.out or ""
    if res.code == TIMED_OUT and timeout < PS_TIMEOUT_S:
        raise NoTime
    return None


def _etime(text):
    """Seconds of a `ps` etime `[[dd-]hh:]mm:ss`, else None."""
    days, _, rest = text.rpartition("-")
    try:
        parts = [int(x) for x in rest.split(":")]
        secs = int(days) * 86400 if days else 0
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return secs + total


def _etime_start(now, elapsed):
    """Epoch seconds a process started, from its `ps` etime, never earlier than the true start.

    macOS `ps` prints etime as the difference of two WHOLE seconds, its own clock and the start
    (`now.tv_sec - p_starttime.tv_sec`), so it can exceed the true elapsed time by up to 1 s: a
    process started at x.9 and read at (x+1).1 shows 1 s. `now - etime` is then up to 1 s early.
    The true start is before (the start's whole second) + 1 = ps's whole second - etime + 1, and
    ps's whole second is at most that of `now` (taken after `ps` returned), so this is a start in
    whole seconds never earlier than the true one, and at most about 1 s (plus the `ps` call)
    later."""
    return int(now) - elapsed + 1


def _unique(pairs) -> dict:
    """{pid: value}; a pid listed twice is dropped (defensive: macOS `ps` escapes a line break as `\\012`)."""
    out, twice = {}, set()
    for pid, value in pairs:
        if pid in out:
            twice.add(pid)
        out[pid] = value
    for pid in twice:
        out.pop(pid)
    return out


def _table_ps(deadline):
    """[Proc] from `ps -axww` + `ps -axwwE`, else None (either failed); NoTime (see _ps)."""
    args_out = _ps(ARGS_PS, deadline)
    if args_out is None:
        return None
    env_out = _ps(ENV_PS, deadline)
    if env_out is None:
        return None
    now = time.time()
    rows = []
    for line in args_out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        elapsed = _etime(parts[3])
        rows.append((pid, (ppid, pgid, parts[4] if len(parts) == 5 else "",
                           None if elapsed is None else _etime_start(now, elapsed))))
    lines = []
    for line in env_out.splitlines():
        parts = line.split(None, 1)
        try:
            lines.append((int(parts[0]), parts[1] if len(parts) == 2 else ""))
        except (IndexError, ValueError):
            continue
    envs = _unique(lines)
    out = []
    for pid, (ppid, pgid, args, start) in _unique(rows).items():
        line, env = envs.get(pid), None
        if line is not None and line.startswith(args) and (len(line) == len(args) or line[len(args)].isspace()):
            env = line[len(args):].strip()
        out.append(Proc(pid, ppid, pgid, args, env, start))
    return out


def _boot(root):
    """Epoch seconds of boot from /proc/uptime (10 ms steps; `btime` in /proc/stat is cut to whole
    seconds, which could put a start up to 1 s early), else None. Read uptime first, the clock
    after: never earlier than the true boot."""
    try:
        up = float((root / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - up


def _table_proc(deadline, root=PROC):
    """[Proc] from /proc (Linux), else None (unlistable, or the walk outlasted a full
    PS_TIMEOUT_S); NoTime when the deadline left no time to start it or its shorter timeout ran out
    (the same rules as _ps)."""
    left = budget(deadline)
    if left <= 0:
        raise NoTime
    timeout = min(PS_TIMEOUT_S, left)
    box = []

    def walk():
        try:
            box.append(_walk_proc(root))
        except Exception:  # noqa: BLE001 - a walk that breaks is no table, like a failed `ps`
            box.append(None)
    walker = threading.Thread(target=walk, daemon=True)
    walker.start()
    walker.join(timeout)
    if walker.is_alive():
        if timeout < PS_TIMEOUT_S:
            raise NoTime
        return None
    return box[0]


def _walk_proc(root):
    """[Proc] from every /proc/<pid>, else None (unlistable). A start is the END of its clock tick
    on a boot never earlier than the true one: like macOS's etime (_etime_start), never earlier than
    the process really started (procs._is_claude compares it with the lock's started_ns)."""
    try:
        names = [n for n in os.listdir(root) if n.isdigit()]
    except OSError:
        return None
    boot = _boot(root)
    try:
        tick = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        tick = 100
    rows = []
    for name in names:
        try:
            stat = (root / name / "stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()   # after `(comm)`: state ppid pgrp …
            ppid, pgid = int(fields[1]), int(fields[2])
        except (OSError, ValueError, IndexError):
            continue
        try:
            start = boot + (int(fields[19]) + 1) / tick if boot is not None else None
        except (ValueError, IndexError):
            start = None
        try:
            env = tuple(e.decode(errors="replace")
                        for e in (root / name / "environ").read_bytes().split(b"\0") if e)
        except OSError:
            env = None
        try:
            args = (root / name / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            args = ""
        rows.append(Proc(int(name), ppid, pgid, args, env, start))
    return rows


def _linux() -> bool:
    return sys.platform.startswith("linux") and PROC.is_dir()


def table(timeout=TABLE_S):
    """The process table ([Proc]) read within `timeout`, else None."""
    try:
        return _table(time.monotonic() + timeout)
    except NoTime:
        return None


def _table(deadline):
    if budget(deadline) <= 0:
        raise NoTime
    return _table_proc(deadline) if _linux() else _table_ps(deadline)


def _started_s(claude):
    ns = claude.get("started_ns")
    if isinstance(ns, int) and not isinstance(ns, bool) and ns > 0:
        return ns / 1e9
    return None


def _is_claude(proc, claude) -> bool:
    """A lost run's claude whose environment cannot be read (an Apple platform binary; Linux: a
    non-dumpable process): only when
    ALL hold: it is the lock's `child_pid`, still in the lock's `pgid`, its command line carries
    that run's own `--mcp-config …/runner/mcp/<run_id>.json` (headless.marker) as a whole
    token, and it started at or after the lock's `started_ns`. A readable environment decides by
    the tag alone (a different tag: never)."""
    if proc.pid != claude.get("pid") or proc.env:
        return False
    since, marker = _started_s(claude), claude.get("marker")
    return (claude.get("pgid") is not None and proc.pgid == claude.get("pgid")
            and bool(marker) and re.search(r"(?:^|\s)" + re.escape(marker) + r"(?=\s|$)", proc.args) is not None
            and since is not None and proc.start is not None and proc.start >= since)


def _find(rid, deadline, claude=None):
    if not rid and claude is None:
        return None
    rows = _table(deadline)
    if rows is None:
        return None
    found = {p.pid for p in rows if carries(p.env, rid) or (claude is not None and _is_claude(p, claude))}
    grew = True
    while grew:
        more = {p.pid for p in rows if p.pid not in found and (p.ppid in found or p.pgid in found)}
        found |= more
        grew = bool(more)
    return found - {0, 1, os.getpid(), os.getppid()}


def find(rid, timeout=TABLE_S, claude=None):
    """Pids of run `rid` (tagged in their environment, their descendants, members of groups they
    lead) other than this process and its parent; None when the process table could not be read
    in `timeout`. `claude` ({pid, pgid, started_ns, marker} from a lost run's lock) adds that
    run's claude as a root when its environment is hidden (_is_claude)."""
    if timeout is None or timeout <= 0:
        return None
    try:
        return _find(rid, time.monotonic() + timeout, claude)
    except NoTime:
        return None


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def kill(pids, wait_s=WAIT_S) -> list:
    """SIGTERM `pids`, SIGKILL whatever is left after `wait_s`; the pids signalled."""
    hit = []
    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
            hit.append(pid)
        except OSError:
            pass
    deadline = time.monotonic() + max(0.0, wait_s)
    left = list(hit)
    while left and time.monotonic() < deadline:
        time.sleep(0.05)
        left = [pid for pid in left if alive(pid)]
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return hit


def sweep(rid, timeout=TABLE_S, wait_s=WAIT_S, claude=None, deadline=None):
    """Kill every process of run `rid`; the pids signalled, or None when the table could not be
    read; raises NoTime when the deadline cut the read short. With `deadline` (time.monotonic())
    nothing outlasts it: the table read gets what is left (each `ps` only when it can finish
    MARGIN_S before), the SIGTERM->SIGKILL wait what is left after the read."""
    end = time.monotonic() + timeout + wait_s if deadline is None else deadline
    pids = _find(rid, min(end, time.monotonic() + timeout), claude)
    if pids is None:
        return None
    if not pids:
        return []
    return kill(pids, min(wait_s, max(0.0, end - time.monotonic() - MARGIN_S)))


def spawn_detached(argv, log, **extra_env) -> None:
    """Start `argv` detached: its own session (it outlives the caller, a Bash tool call included),
    stdin closed, output appended to `log` (its folder created), the plugin bin first on PATH,
    `extra_env` set and ENV (ORCHE_RUN_ID) dropped, so the end-of-run sweep of a run whose worker
    started it never takes it along. Never waits."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != ENV}
    env.update(extra_env)
    env["PATH"] = f"{paths.PLUGIN_ROOT / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    with open(log, "ab") as fh:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True, env=env)


def orche_argv(*args) -> list:
    """`orche <args>` under this very interpreter (the plugin's bin/orche.py): no launcher has to find
    Python again after a spawn, so a spawned run never dies there without a pointer."""
    return [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), *args]


def spawn_orche(args, log, **extra_env) -> None:
    """spawn_detached of `orche <args>` (orche_argv): every detached self-spawn."""
    spawn_detached(orche_argv(*args), log, **extra_env)


def mem_total_mb():
    """The machine's RAM in MB (macOS `sysctl -n hw.memsize`, Linux /proc/meminfo), or None."""
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                             timeout=PS_TIMEOUT_S).stdout.strip()
        if out.isdigit():
            return int(out) // (1024 * 1024)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _stdout(argv):
    """stdout of `argv` (at most PS_TIMEOUT_S), or None when it cannot run or exits non-zero."""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def parse_sysctl_free(out):
    """MB free from `sysctl -n kern.memorystatus_level hw.memsize`: the kernel's free percentage × RAM."""
    try:
        pct, mem = (int(x) for x in (out or "").split())
    except ValueError:
        return None
    return mem // (1024 * 1024) * pct // 100 if 0 <= pct <= 100 and mem > 0 else None


VM_PAGES = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")


def parse_vm_stat(out):
    """MB free from `vm_stat`: free + inactive + speculative + purgeable pages × the page size."""
    m = re.search(r"page size of (\d+) bytes", out or "")
    if m is None:
        return None
    pages = {}
    for line in (out or "").splitlines():
        name, _, value = line.partition(":")
        if name.strip() in VM_PAGES:
            try:
                pages[name.strip()] = int(value.strip().rstrip("."))
            except ValueError:
                return None
    if "Pages free" not in pages:
        return None
    return sum(pages.values()) * int(m.group(1)) // (1024 * 1024)


def free_mb(run=None):
    """Free memory in MB, or None when unreadable (the runner's memory guard): macOS the
    kernel's free percentage × RAM (`sysctl`), else `vm_stat`; Linux /proc/meminfo MemAvailable.
    `run(argv)` returns stdout or None (tests pass fixed outputs)."""
    run = run or _stdout
    for argv, parse in ((["sysctl", "-n", "kern.memorystatus_level", "hw.memsize"], parse_sysctl_free),
                        (["vm_stat"], parse_vm_stat)):
        found = parse(run(argv))
        if found is not None:
            return found
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None
