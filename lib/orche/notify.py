"""The one notification path, the bell: `ring(project, *, kind) -> bool` writes one BEL byte to the
terminal of the project's Main pane, and Orca shows its own notification for it (source `terminal-bell`:
`Bell in <workspace>`, `<repo> · Attention requested`; a click focuses that pane) while Orca's Settings >
Notifications > Terminal Bell is on, except while that workspace is the active one in a focused Orca
window. macOS credited every osascript notification to Script Editor, inside a tell to Orca too (osascript
runs the command itself), and Orca has no command that posts a notification with Orche's text: a ring
carries no text, which stays where Orche recorded it (flow.owner's ask and notices, the status line, Main's
inbox).

Main's terminal: Claude Code runs its hooks without a controlling terminal, so Main's own hooks
(roster.note_pane) walk up from the hook's parent to the first process that has one, the `claude` in
Main's pane (terminal), and record it in `inbox/<main-p>/.pane` {pid, tty, at} (PANE). `ring` writes only
to a recorded path `/dev/tty<name>` that `ps` still names as that pid's terminal (alive, the same
terminal), opened O_WRONLY|O_NOCTTY|O_NONBLOCK: never a terminal Main did not record, never a wait.

Gated by the top-level config `notify` (default true). Never raises. One events line per try, never text:
`notify kind=<kind> project=<p> ok=0|1`, plus ` reason=off|no-pane|error` when it did not ring. Callers:
flow.owner (the ask's reminders, a parked guard refusal, the heartbeat's notices) and a Keymaster request
that opened no dialog (cmd_secret).
"""
import os
import re
import time

from . import config, log, shell, units, util

PANE = ".pane"      # inbox/<main-p>/: {pid, tty, at}: the terminal of Main's `claude` (roster.note_pane)
TTY = re.compile(r"^/dev/tty[a-z0-9]+$")
PS_S = 0.5          # a `ps` call, and the whole walk up from a hook, take at most this long
UP = 8              # processes walked up from a hook's parent at most


def enabled(cfg=None) -> bool:
    """Notifications are on: config `notify` (default true)."""
    return bool((cfg if cfg is not None else config.load()).get("notify", True))


def _ps(pid, timeout):
    """(ppid, tty) of `pid` from `ps -o ppid= -o tty= -p <pid>`: `tty` the terminal's path (`/dev/ttys013`),
    "" when it has none (`??`); None when ps fails, times out or lists no such process."""
    res = shell.run(["ps", "-o", "ppid=", "-o", "tty=", "-p", str(int(pid))], timeout=timeout)
    parts = res.out.split() if res.ok else []
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), "" if not parts[1].strip("?-") else "/dev/" + parts[1]


def _bell(path) -> None:
    """One BEL byte to the terminal at `path`, opened O_WRONLY|O_NOCTTY|O_NONBLOCK: it never becomes this
    process's terminal and never waits (a full queue is an error)."""
    fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        if os.write(fd, b"\a") != 1:
            raise OSError("short write")
    finally:
        os.close(fd)


PS, BELL = _ps, _bell   # the ps call and the terminal write (seams for tests)


def terminal(pid=None, now=None):
    """{pid, tty, at} of the first process from `pid` (default: this process's parent) up that has a
    controlling terminal, when that is a /dev/tty<name> path: from a hook, the `claude` whose hook it is.
    None when none is found within UP processes and PS_S, or `ps` cannot say."""
    pid = os.getppid() if pid is None else int(pid)
    end = time.monotonic() + PS_S
    for _ in range(UP):
        left = end - time.monotonic()
        row = PS(pid, left) if pid > 1 and left > 0 else None
        if row is None:
            return None
        if row[1]:
            if not TTY.fullmatch(row[1]):
                return None
            return {"pid": pid, "tty": row[1], "at": (now or units.now()).isoformat(timespec="seconds")}
        pid = row[0]
    return None


def _pane(project):
    """The terminal path recorded for `project`'s Main (PANE) while `ps` still names it that pid's
    terminal; None otherwise (no Main, no record, a path that is not /dev/tty<name>, a dead pid)."""
    from . import inbox, registry, roster
    main = roster.Roster(registry.load()).main(project)
    rec = (util.read_dict(inbox.box(main.session) / PANE) if main is not None else None) or {}
    pid, tty = rec.get("pid"), rec.get("tty")
    if type(pid) is not int or not isinstance(tty, str) or not TTY.fullmatch(tty):
        return None
    row = PS(pid, PS_S)
    return tty if row is not None and row[1] == tty else None


def ring(project, *, kind) -> bool:
    """Ring `project`'s Main pane: one BEL byte to its terminal (_pane), so Orca shows its own notification.
    True when written; False when config `notify` is off (`off`), no live terminal is recorded for Main
    (`no-pane`) or the write failed (`error`; hooks.log names the exception type, never a path or text)."""
    reason = None
    try:
        path = _pane(project) if enabled() else False
        if not path:
            reason = "off" if path is False else "no-pane"
        else:
            BELL(path)
    except Exception as exc:  # noqa: BLE001 - a notification never fails its caller
        reason = "error"
        log.log("hooks", f"notify {kind}: {type(exc).__name__}")
    log.log("events", f"notify kind={kind} project={project} ok={int(reason is None)}"
                      + (f" reason={reason}" if reason else ""))
    return reason is None
