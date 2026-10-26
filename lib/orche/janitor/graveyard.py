"""GRAVEYARD ledgers: `<project>/docs/GRAVEYARD.md` and `ORCHE_HOME/graveyard.md` (append-only).

Row: `| date | what | why | recovery | actor |`. Rows are appended before anything is removed
(R-JAN-3) and never edited.
"""
import datetime as _dt
import re
from pathlib import Path

from .. import paths, templates, util

TEMPLATE_REL = Path("docs") / "GRAVEYARD.md"
GLOBAL_HEADER = ("# GRAVEYARD: all projects\n\n"
                 "One line per removed thing, appended before anything is deleted (R-JAN-3). "
                 "Never edit old lines.\n\n"
                 "| date | what | why | recovery | actor |\n|---|---|---|---|---|\n")
_SPLIT = re.compile(r"(?<!\\)\|")
_SEPARATOR = re.compile(r"^:?-+:?$")


def project_file(root) -> Path:
    return Path(root) / TEMPLATE_REL


def global_file() -> Path:
    return paths.orche_home() / "graveyard.md"


def ensure_project_file(root) -> Path:
    """Create `docs/GRAVEYARD.md` from the project template if it is missing."""
    target = project_file(root)
    if not target.exists():
        text = (templates.TEMPLATES_ROOT / "project" / TEMPLATE_REL).read_text(encoding="utf-8")
        util.atomic_write(target, templates.render(text, PROJECT=Path(root).name))
    return target


def cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip() or "-"


def row(date, what, why, recovery, actor) -> str:
    return "| " + " | ".join(cell(x) for x in (date, what, why, recovery, actor)) + " |"


def _append(path: Path, line: str, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    with open(path, "a", encoding="utf-8") as fh:
        if not existing and header:
            fh.write(header)
        elif existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(line + "\n")


def record(project_root, what, why, recovery, actor, *, project="", date=None) -> list:
    """Append one row to the project's GRAVEYARD (if the project dir exists) and the global one."""
    date = (date or _dt.date.today()).isoformat()
    written = []
    if project_root and Path(project_root).is_dir():
        target = ensure_project_file(project_root)
        line = row(date, what, why, recovery, actor)
        _append(target, line)
        written.append(line)
    global_what = f"{project}: {what}" if project else what
    line = row(date, global_what, why, recovery, actor)
    _append(global_file(), line, GLOBAL_HEADER)
    written.append(line)
    return written


def parse_rows(text) -> list:
    """[(line, cells)] for every table body row (header and separator rows skipped)."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in _SPLIT.split(line)[1:]]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if not cells or all(_SEPARATOR.match(c) for c in cells) or cells[0].lower() == "date":
            continue
        out.append((line, cells))
    return out
