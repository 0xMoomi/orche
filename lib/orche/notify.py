"""The one macOS notification path (C10 §5.3, §7): `send(title, body, sound=False)`.

The script is fixed words — `on run argv`, a `display notification` that reads its body and title
from `item 1` and `item 2 of argv` (plus the constant ` sound name "Glass"` when asked), `end run` —
and the body and the title travel as osascript arguments: nothing Main, a worker or the owner wrote
is ever AppleScript. Both are masked and made one line (control characters dropped). Gated by the
top-level config `notify`; macOS only (PLATFORM). Never raises. One events line per notification
tried, naming the caller's `kind` and the outcome, never the text.

Callers: Main's closing line and a parked guard refusal (flow.owner), HR (hr.book), the Keymaster
(secrets.store) and the heartbeat's escalations (reconcile).
"""
import sys
import unicodedata

from . import config, log, mask, shell

PLATFORM = sys.platform     # the notification is macOS only (a seam for tests)
TIMEOUT_S = 5
SCRIPT = ("on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run")
SOUND = ' sound name "Glass"'


def enabled(cfg=None) -> bool:
    """Notifications are on: macOS and config `notify` (default true)."""
    return PLATFORM == "darwin" and bool((cfg if cfg is not None else config.load()).get("notify", True))


def clean(text) -> str:
    """`text` masked, control characters dropped, whitespace runs made one space: one line."""
    safe = mask.redact(str(text or ""))[0]
    return " ".join("".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in safe).split())


def arg(text) -> str:
    """clean(text) as an osascript argument: a leading `-` gets a space before it (never an option)."""
    flat = clean(text)
    return " " + flat if flat.startswith("-") else flat


def argv(title, body, sound=False) -> list:
    """The osascript argv: the fixed script lines, then the body and the title as arguments."""
    script = (SCRIPT[0], SCRIPT[1] + (SOUND if sound else ""), SCRIPT[2])
    return ["osascript", *(x for line in script for x in ("-e", line)), arg(body), arg(title)]


def send(title, body, sound=False, *, kind="-") -> bool:
    """Show one notification. True when osascript exited 0; False when notifications are off, off
    macOS, or osascript failed (hooks.log names the exit code, never the text)."""
    if not enabled():
        return False
    try:
        res = shell.run(argv(title, body, sound), timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - best-effort; the type only (a message may hold the argv)
        log.log("hooks", f"notify {kind}: {type(exc).__name__}")
        return False
    log.log("events", f"notify kind={kind} ok={int(res.ok)}")
    if not res.ok:
        log.log("hooks", f"notify {kind}: osascript exit {res.code}")
    return res.ok
