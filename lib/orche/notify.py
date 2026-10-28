"""The one macOS notification path: `send(title, body, sound=False, *, kind="-", project=None)`.

osascript with fixed words: `on run argv`, a `display notification` that reads its body and title from
`item 1` and `item 2 of argv` (plus the constant ` sound name "Glass"` when asked), `end run`; body and
title are osascript arguments, masked and made one line (control characters dropped), so no script or
shell string is ever built from text Main, a worker or the owner wrote. macOS credits an osascript
notification to Script Editor, so a click opened Script Editor; the line is sent inside
`tell application id "com.stablyai.orca"` so the notification is Orca's and a click opens Orca. AppleScript
launches a target that is not running, which a background pass must never do: the tell is used only when
`pgrep -xq Orca` finds Orca running. `project` is accepted from callers and not used.

Gated by the top-level config `notify`; macOS only (PLATFORM). Never raises. One events line per
notification tried, naming the caller's `kind` and the outcome, never the text.

Callers: a parked guard refusal and the heartbeat's reminders and project escalations (flow.owner, with the
project; Orca's own notification covers Main's closing line), HR (hr.book), the Keymaster (secrets.store)
and the heartbeat's global notice (reconcile).
"""
import sys
import unicodedata

from . import config, log, mask, shell

PLATFORM = sys.platform     # the notification is macOS only (a seam for tests)
TIMEOUT_S = 5
ORCA_ID = "com.stablyai.orca"
SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")
TELL = (f'tell application id "{ORCA_ID}"', "end tell")
SOUND = ' sound name "Glass"'


def enabled(cfg=None) -> bool:
    """Notifications are on: macOS and config `notify` (default true)."""
    return PLATFORM == "darwin" and bool((cfg if cfg is not None else config.load()).get("notify", True))


def clean(text) -> str:
    """`text` masked, control characters dropped, whitespace runs made one space: one line."""
    safe = mask.redact(str(text or ""))[0]
    return " ".join("".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in safe).split())


def arg(text) -> str:
    """clean(text) as an argument: a leading `-` gets a space before it (never an option)."""
    flat = clean(text)
    return " " + flat if flat.startswith("-") else flat


def orca_running() -> bool:
    """Orca is running (`pgrep -xq Orca`): only then may the script address it (a seam for tests)."""
    return shell.run(["pgrep", "-xq", "Orca"], timeout=TIMEOUT_S).ok


def argv(title, body, sound=False, *, orca=False) -> list:
    """The osascript argv: the fixed script lines (inside a tell to Orca when `orca`), then the body and
    the title as arguments."""
    line = SCRIPT[1] + (SOUND if sound else "")
    script = (SCRIPT[0], TELL[0], line, TELL[1], SCRIPT[2]) if orca else (SCRIPT[0], line, SCRIPT[2])
    return ["osascript", *(x for s in script for x in ("-e", s)), arg(body), arg(title)]


def send(title, body, sound=False, *, kind="-", project=None) -> bool:
    """Show one notification. True when osascript exited 0; False when notifications are off, off macOS,
    or osascript failed (hooks.log names the tool and its exit code, never the text)."""
    del project   # callers name the project; a click opens Orca either way
    if not enabled():
        return False
    try:
        cmd = argv(title, body, sound, orca=orca_running())
        res = shell.run(cmd, timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - best-effort; the type only (a message may hold the argv)
        log.log("hooks", f"notify {kind}: {type(exc).__name__}")
        return False
    log.log("events", f"notify kind={kind} ok={int(res.ok)}")
    if not res.ok:
        log.log("hooks", f"notify {kind}: {cmd[0]} exit {res.code}")
    return res.ok
