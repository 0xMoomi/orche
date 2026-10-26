"""The one macOS notification path (C10 §5.3, §7): `send(title, body, sound=False, *, project=None)`.

Two deliveries, chosen at send time; both mask the body and the title and make each one line (control
characters dropped), and neither ever builds a script or a shell string from text Main, a worker or
the owner wrote:

- `terminal-notifier` on PATH: `-title <title> -message <body> [-sound Glass] -activate com.stablyai.orca`,
  plus, for a project key (`^[a-z0-9][a-z0-9-]{0,63}$`), `-execute '<this checkout>/bin/orche' focus <key>`:
  a click brings Orca forward and switches it to the project's Main tab (`orche focus`). A key that does
  not match gets no `-execute` (hooks.log `notify <kind>: project key refused`). macOS asks once to allow
  its notifications: until the owner allows them it exits non-zero (3 once refused) or blocks on the
  prompt, so the run gets its own short timeout (NOTIFIER_TIMEOUT_S), and any failure falls back to the
  osascript path below (hooks.log `notify <kind>: terminal-notifier exit <code>, osascript fallback`):
  a refused permission must never cost the owner every notification.
- otherwise osascript with fixed words: `on run argv`, a `display notification` that reads its body and
  title from `item 1` and `item 2 of argv` (plus the constant ` sound name "Glass"` when asked), `end run`;
  body and title are osascript arguments. macOS credits an osascript notification to Script Editor, so a
  click opened Script Editor; the line is sent inside `tell application id "com.stablyai.orca"` so the
  notification is Orca's and a click opens Orca. AppleScript launches a target that is not running, which
  a background pass must never do: the tell is used only when `pgrep -xq Orca` finds Orca running.

Gated by the top-level config `notify`; macOS only (PLATFORM). Never raises. One events line per
notification tried, naming the caller's `kind` and the outcome, never the text.

Callers: Main's closing line, a parked guard refusal and the heartbeat's project escalations (flow.owner,
with the project), HR (hr.book), the Keymaster (secrets.store) and the heartbeat's global notice (reconcile).
"""
import re
import shlex
import shutil
import sys
import unicodedata
from pathlib import Path

from . import config, log, mask, shell

PLATFORM = sys.platform     # the notification is macOS only (a seam for tests)
WHICH = shutil.which        # finds terminal-notifier (a seam for tests)
TIMEOUT_S = 5
NOTIFIER_TIMEOUT_S = 3      # a permission prompt that blocks must not hold a hook for long
ORCA_ID = "com.stablyai.orca"
ORCHE_BIN = Path(__file__).resolve().parents[2] / "bin" / "orche"
PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
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


def notifier_argv(title, body, sound=False, project=None) -> list:
    """The terminal-notifier argv; `project` (a checked key or None) adds the click's `orche focus`."""
    out = ["terminal-notifier", "-title", arg(title), "-message", arg(body)]
    out += ["-sound", "Glass"] if sound else []
    out += ["-activate", ORCA_ID]
    if project:
        out += ["-execute", f"{shlex.quote(str(ORCHE_BIN))} focus {project}"]
    return out


def send(title, body, sound=False, *, kind="-", project=None) -> bool:
    """Show one notification. True when the tool exited 0; False when notifications are off, off macOS,
    or the tool failed (hooks.log names the tool and its exit code, never the text). A failed
    terminal-notifier falls back to osascript; the result then describes the osascript attempt."""
    if not enabled():
        return False
    if project is not None and not PROJECT.match(str(project)):
        log.log("hooks", f"notify {kind}: project key refused")
        project = None
    try:
        if WHICH("terminal-notifier"):
            try:
                res = shell.run(notifier_argv(title, body, sound, project), timeout=NOTIFIER_TIMEOUT_S)
                why = None if res.ok else f"exit {res.code}"
            except Exception as exc:  # noqa: BLE001 - the type only (a message may hold the argv)
                why = type(exc).__name__
            if why is None:
                log.log("events", f"notify kind={kind} ok=1")
                return True
            log.log("hooks", f"notify {kind}: terminal-notifier {why}, osascript fallback")
        cmd = argv(title, body, sound, orca=orca_running())
        res = shell.run(cmd, timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - best-effort; the type only (a message may hold the argv)
        log.log("hooks", f"notify {kind}: {type(exc).__name__}")
        return False
    log.log("events", f"notify kind={kind} ok={int(res.ok)}")
    if not res.ok:
        log.log("hooks", f"notify {kind}: {cmd[0]} exit {res.code}")
    return res.ok
