"""The wake file: how Orche starts a turn in a session without typing (R-INBOX-4).

Each pane session (Main, a division, a feature; never the Staff or a headless run) that runs the wake hook
(hooked) watches `inbox/<session>/.wake`: its SessionStart hook returns that path in `watchPaths` and records
WATCH, {session_id, at}, in the same folder ("this pane's current session listens on the wake file"). WATCH is
replaced on every SessionStart and removed at SessionEnd (only by the session it names). Writing the wake
file (ring) fires Claude Code's FileChanged event; the `inbox_wake` hook (asyncRewake) then prints
Orche's own line (due) to stderr and exits 2, which starts a turn in an idle session, or delivers at the
next tool boundary in a busy one. Typing stays the fallback (lib/orche/deliver.py).
"""
import json
import os
import time
from pathlib import Path

from . import inbox, paths, util

WAKE = ".wake"     # inbox/<session>/: the file the session watches; its content changes on every write
WATCH = ".watch"   # inbox/<session>/: {session_id, at}: the Claude session listening on WAKE


def brief_line(unit, rev) -> str:
    """The line that announces a dispatched unit (typed by `orche deliver`, printed by the wake hook)."""
    return f"[orche] inbox: {unit} rev {rev}"


def hooked() -> bool:
    """This Claude session runs the `inbox_wake` hook: the settings.json it loads (`$CLAUDE_CONFIG_DIR`, else
    `~/.claude`) enables the Orche plugin (plugin mode: hooks/hooks.json has the FileChanged entry) or holds
    the FileChanged entry `orche install` writes in settings mode. Unreadable, or an older install without
    that entry: False, and the pane keeps typed wakes until `orche install` or `orche update` runs."""
    from .install import settings
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(base).expanduser() if base else Path.home() / ".claude") / "settings.json"
    try:
        data = util.read_json(path, default={})
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    wanted = [cmd for event, _m, cmd, _f in settings.plugin_hooks(paths.PLUGIN_ROOT) if event == "FileChanged"]
    return settings.plugin_enabled(data) or bool(wanted) and all(
        settings.has_command(data, "FileChanged", cmd) for cmd in wanted)


def wake_path(session) -> Path:
    return inbox.box(session) / WAKE


def arm(session, session_id) -> None:
    """Record that Claude session `session_id` watches `session`'s wake file (replaces any older record)."""
    util.write_json(inbox.box(session) / WATCH, {"session_id": str(session_id), "at": util.now_iso()})


def marker(session):
    """`session`'s WATCH record when it names a Claude session ({session_id, at}), else None. Never raises."""
    try:
        data = util.read_dict(inbox.box(session) / WATCH)
    except (OSError, ValueError):
        return None
    return data if data and isinstance(data.get("session_id"), str) and data["session_id"] else None


def disarm(session, session_id=None) -> bool:
    """Remove `session`'s WATCH record (only when it names `session_id`, if given). True when removed."""
    m = marker(session)
    if m is None or (session_id is not None and m["session_id"] != str(session_id)):
        return False
    try:
        (inbox.box(session) / WATCH).unlink(missing_ok=True)
    except OSError:
        return False
    return True


def ring(session) -> None:
    """Write `session`'s wake file atomically with content that differs on every write, so the watcher
    reports a change each time. Raises what the write raises."""
    util.atomic_write(wake_path(session), json.dumps({"at": util.now_iso(), "n": time.time_ns()}) + "\n")


def due(session):
    """Orche's line for what waits in `session`'s inbox, or None: inbox.WAKE_TEXT when a waking pointer
    (inbox.wakes) is pending, else the brief line of the newest pending `via: dispatch` pointer."""
    items = inbox.pending(session)
    if any(inbox.wakes(p) for _x, p in items):
        return inbox.WAKE_TEXT
    sent = [p for _x, p in items if p.get("via") == "dispatch"]
    if not sent:
        return None
    try:
        rev = int(sent[-1].get("brief_rev") or 1)
    except (TypeError, ValueError):
        rev = 1
    return brief_line(sent[-1].get("unit"), rev)


def changed(file_path, session_id):
    """The FileChanged hook's question: when `file_path` is the wake file of a session whose WATCH names
    `session_id`, Orche's line for it (due), else None. Never reads anything for another file."""
    p = Path(str(file_path or ""))
    if p.name != WAKE or not session_id:
        return None
    root = paths.orche_home() / "inbox"
    if os.path.realpath(p.parent.parent) != os.path.realpath(root):
        return None
    session = p.parent.name
    m = marker(session)
    if m is None or m["session_id"] != str(session_id):
        return None
    return due(session)
