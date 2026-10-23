"""The HR record: refusals, the record of one transition, the HR log, notifications, standing orders."""
import re
import sys
from pathlib import Path

from .. import cli, mask, notify as notice, paths, registry, roster, statusline, templates, util

_ORDER = re.compile(r"^(\d+)\.\s")


class Refused(Exception):
    """A transition refused by policy, or stopped by a failed step. `code` is the CLI exit code."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def log_path() -> Path:
    return paths.orche_home() / "hr-log.md"


def log(text: str) -> None:
    """Append one `- <ts> <text>` line to the HR log (created with a header if missing)."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# HR log\n\n")
        fh.write(f"- {util.now_iso()} {text}\n")


def notify(text: str) -> None:
    """A macOS notification titled ORCHE (notify.send: masked, an argument to a fixed script; no-op
    when config `notify` is off or not on macOS)."""
    notice.send("ORCHE", text, kind="hr")


def record(reg, line, *, event=None, project=None, note=None, save=True) -> None:
    """The HR record of one transition, in this order: `event` (registry.append_event fields, in the
    order they are stored: type, project, unit, by[, batch]) -> save -> HR-log `line` -> the status
    line of `project` (default: the event's; none for "*") -> notification `note`.
    `save=False`: a line without a registry change (a re-run of init that adds nothing)."""
    if event is not None:
        registry.append_event(reg, **event)
    if save:
        registry.save(reg)
    log(line)
    key = project or (event or {}).get("project")
    if key and key != "*":
        statusline.refresh(reg, key)
    if note:
        notify(note)


def next_order(text: str) -> int:
    """The number after the highest `<n>. ` line of a STANDING.md text."""
    return max((int(m.group(1)) for m in map(_ORDER.match, text.splitlines()) if m), default=0) + 1


def correct(reg, key, text, *, by=None, dry_run=False) -> tuple:
    """Append the next numbered standing order to `key`'s STANDING.md (R-STANDING-1) and log it.
    Returns (data, number). Raises Refused(1, "--text is empty") when nothing is left after masking."""
    proj = reg["projects"][key]
    order = " ".join(mask.redact(text)[0].split())  # standing orders reach every brief: mask first
    if not order:
        raise Refused(cli.EXIT_USER, "--text is empty")
    path = Path(proj["path"]) / proj["standing"]
    if path.exists():
        current = path.read_text(encoding="utf-8")
    else:
        tmpl = templates.TEMPLATES_ROOT / "project" / ".orche" / "STANDING.md"
        current = templates.render(tmpl.read_text(encoding="utf-8"), PROJECT=key)
    number, by = next_order(current), by or roster.actor()
    if dry_run:
        print(f"[dry-run] append to {path}: {number}. {order}", file=sys.stderr)
    else:
        sep = "" if not current or current.endswith("\n") else "\n"
        util.atomic_write(path, f"{current}{sep}{number}. {order}\n")
        log(f"correct {key} #{number} by {by}: {order}")
    return {"project": key, "number": number, "path": str(path), "dry_run": dry_run}, number
