"""macOS privacy (TCC) and the heartbeat job (0.7.2).

A LaunchAgent-started Python (the `com.orche.reconcile` job) cannot read ~/Desktop, ~/Documents,
~/Downloads or iCloud Drive (~/Library/Mobile Documents) until its Python.app is switched on in
System Settings › Privacy & Security › Full Disk Access; a read there can block (a consent check)
instead of failing. Only the user (or MDM) can grant it, so ORCHE detects, explains, opens the two
places and verifies: this module names the protected folders (string checks only: nothing here
touches a protected path), finds the Python.app to grant, opens Finder and the Full Disk Access pane,
and waits for a pass of the job itself (heartbeat.json `job_at`, reconcile.write_heartbeat).
"""
import os
import sys
import time
from pathlib import Path

from . import paths, shell, units

PROTECTED = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents", "Library/CloudStorage")
VOLUMES = "/Volumes"   # every volume mounted under it (removable and network volumes have their own switches)
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
FIRST_PASS_S = 45      # orche install: the job's first pass within this long, else the guidance
GRANT_QUICK_S = 15     # orche install --grant: already granted? (before anything is opened)
GRANT_S = 300          # ... then this long after Finder and Full Disk Access were opened
KICK_EVERY_S = 30      # ... kickstarting the job this often
POLL_S = 1.0
FIRST_PASS_WAIT = f"checking the heartbeat's first pass… (up to {FIRST_PASS_S} s)"   # printed (flushed) before each wait
INTERRUPTED = "interrupted — check later: orche install --grant"
EXIT_INTERRUPTED = 130   # Ctrl-C during a wait: INTERRUPTED, this exit, no traceback


def blocked(app) -> str:
    """`orche install --grant`'s last line when no readable job pass came."""
    return BLOCKED.format(name=Path(app).name)


def grant_wait(seconds) -> str:
    """`orche install --grant`'s progress line before a wait of `seconds`."""
    return f"checking access… (up to {seconds // 60} min)" if seconds >= 60 else f"checking access… (up to {seconds} s)"
GUIDANCE = (   # {name}: what Finder selects (python_app(): Python.app, or a bare python3.x binary)
    "ORCHE's background job (the heartbeat) cannot read the project folder because of macOS privacy settings. "
    "Drag the item selected in Finder ({name}) into System Settings › Privacy & Security › Full Disk Access, "
    "switch it on, then run in the terminal: orche install --grant",
)
STEPS = (
    "1. Drag the item selected in the Finder window that just opened ({name}) into the Full Disk Access list "
    "(if that fails: + below the list → Cmd+Shift+G → paste the path below).",
    "2. Switch the new entry on (password/Touch ID). If a window says 'Python would like to access files in …', "
    "click 'Allow'.",
    "3. Keep this terminal open. ORCHE checks automatically for up to 5 minutes.",
)
NOTHING_TO_GRANT = "nothing to grant — no project is under a protected folder"
NOT_LOADED = "the heartbeat job is not loaded — run: orche install --mode {mode}"
GRANTED = "granted — heartbeat OK"
BLOCKED = "still blocked — switch {name} on in Full Disk Access, then: orche install --grant"

_sleep = time.sleep       # test seams (a fake clock advances on sleep)
_clock = time.monotonic


def protected_roots(home=None) -> list:
    """The protected folders under `home` (default the user's) and under its real path (a project's
    registered path is a real path)."""
    base = Path(home) if home is not None else Path.home()
    bases = dict.fromkeys([str(base), os.path.realpath(base)])
    return [os.path.join(b, name) for b in bases for name in PROTECTED]


def within(path, roots) -> bool:
    """`path` is one of `roots` or inside one (normalized, case-insensitive like APFS); strings only."""
    p = os.path.normpath(os.path.abspath(os.fspath(path))).casefold()
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep)
               for r in (os.path.normpath(os.fspath(x)).casefold() for x in roots))


def protected(path, home=None) -> bool:
    """`path` is a protected folder or inside one, or on a volume under /Volumes. A string check: the
    path itself is never read."""
    if not path:
        return False
    p = os.path.normpath(os.path.abspath(os.fspath(path))).casefold()
    vol = VOLUMES.casefold() + os.sep
    return (p.startswith(vol) and len(p) > len(vol)) or within(p, protected_roots(home))


def protected_projects(reg, home=None) -> list:
    """[(key, path)] of the registered projects whose path is under a protected folder."""
    out = []
    for key, proj in sorted(((reg or {}).get("projects") or {}).items()):
        path = proj.get("path") if isinstance(proj, dict) else None
        if path and protected(path, home):
            out.append((key, str(path)))
    return out


def job_interpreter():
    """The real path of the interpreter the heartbeat job itself ran (heartbeat.json, else
    last-abort.json `interpreter`, written by its passes), else None."""
    from . import reconcile
    for rec in (reconcile.read_heartbeat() or {}, reconcile.read_abort() or {}):
        exe = rec.get("interpreter")
        if isinstance(exe, str) and exe.startswith("/"):
            return exe
    return None


def python_app(executable=None) -> Path:
    """What to add to Full Disk Access, for `executable` (default: the job's own interpreter, else this
    one): the framework build's `…/Python.framework/Versions/X.Y/Resources/Python.app` (Homebrew's
    python@3.x runs through it, and macOS holds it responsible), found from the interpreter's real
    path; else the interpreter itself (its real path: a bare python3.x of uv, pyenv or conda, whose
    `.name` the texts then show)."""
    real = Path(os.path.realpath(executable or job_interpreter() or sys.executable))
    if "Python.app" in real.parts:
        return Path(*real.parts[:real.parts.index("Python.app") + 1])
    for parent in real.parents:
        if parent.parent.name == "Versions" and parent.parent.parent.name == "Python.framework":
            app = parent / "Resources" / "Python.app"
            return app if app.is_dir() else real
    return real


def reveal(app) -> list:
    """Finder with `app` selected, and System Settings at Full Disk Access: two `open` calls."""
    return [shell.run(["open", "-R", str(app)], timeout=10), shell.run(["open", SETTINGS_URL], timeout=10)]


def steps(app) -> list:
    """`orche install --grant`'s instructions: the three steps, then the path
    to paste (Cmd+Shift+G) when dragging does not work."""
    name = Path(app).name
    return [line.format(name=name) for line in STEPS] + [f"  {name}: {app}"]


def job_pass(since):
    """heartbeat.json when the LaunchAgent itself completed a pass that started at or after `since`
    (an aware datetime) and could list every protected project (no `unreadable`), else None. A
    hook-kicked pass does not count: it runs with Orca's access."""
    from . import reconcile
    hb = reconcile.read_heartbeat() or {}
    at = units.parse_iso(hb.get("job_at"))
    return hb if at is not None and at >= since.replace(microsecond=0) and not hb.get("unreadable") else None


def wait_job_pass(since, timeout_s, kick=None, kick_every_s=None):
    """Poll job_pass(since) for up to `timeout_s`. Each round checks first (a pass already done needs
    no kick), then `kick()`s when due: at the start and every `kick_every_s` after (None: once). The
    heartbeat dict, or None on timeout."""
    start = _clock()
    due = start
    while True:
        hb = job_pass(since)
        if hb is not None:
            return hb
        now = _clock()
        if now - start >= timeout_s:
            return None
        if kick is not None and due is not None and now >= due:
            kick()
            due = now + kick_every_s if kick_every_s else None
        _sleep(POLL_S)


def no_pass(reg, live=True) -> tuple:
    """(hint, lines) when the job's pass did not come: the privacy guidance — Finder and Full Disk
    Access opened (`live`) — when a project is under a protected folder or a privacy abort is
    trusted (reconcile.privacy_abort: at most a day old, newer than the job's last pass); else where
    to look."""
    from . import reconcile
    job_at = units.parse_iso((reconcile.read_heartbeat() or {}).get("job_at"))
    if protected_projects(reg) or reconcile.privacy_abort(units.now(), job_at) is not None:
        app = python_app()
        if live:
            reveal(app)
        return "privacy", [*(g.format(name=app.name) for g in GUIDANCE), f"  {app.name}: {app}"]
    log = paths.orche_home() / "logs" / "reconcile.log"
    return "slow", [f"reconcile job: no pass within {FIRST_PASS_S} s — run orche reconcile and read {log}"]


def interrupted() -> int:
    """Ctrl-C during a wait: one line, exit 130, no traceback."""
    print(INTERRUPTED, file=sys.stderr, flush=True)
    return EXIT_INTERRUPTED


def first_pass_check(started, live=True, progress=None) -> dict:
    """After the job was (re)loaded: wait FIRST_PASS_S for a pass of its own (heartbeat.json
    `job_at` at or after `started`, every protected project readable), checked before any kick, then
    a plain kickstart (a running pass is left alone); the progress line goes to `progress` (default
    stdout) first. {"ok", "took_s", "hint", "lines"}; on a timeout the lines are no_pass's: the
    guidance (Finder and Full Disk Access opened when `live`) when a project is under a protected
    folder or a recent privacy abort says so, else where to look. Ctrl-C raises KeyboardInterrupt."""
    from . import registry
    from .install import launchd
    print(FIRST_PASS_WAIT, file=progress or sys.stdout, flush=True)
    hb = wait_job_pass(started, FIRST_PASS_S, kick=lambda: launchd.kickstart(launchd.RECONCILE))
    if hb is not None:
        return {"ok": True, "took_s": hb.get("took_s"), "hint": None,
                "lines": [f"reconcile job: first pass OK ({hb.get('took_s')} s)"]}
    try:
        reg = registry.load()
    except registry.RegistryError:
        reg = {}
    hint, lines = no_pass(reg, live=live)
    return {"ok": False, "took_s": None, "hint": hint, "lines": lines}
