"""Small shared helpers: slugs, timestamps, atomic JSON IO, dispatch-block parsing."""
import datetime as _dt
import json
import os
import re
import tempfile
from pathlib import Path

DISPATCH_KEYS = ("TO", "GOAL", "SCOPE", "CONTEXT", "ACCEPT", "VERIFY", "TIMEBOX", "FORBID",
                 "REPORT", "TIER", "ROLLBACK", "AMEND", "STOP")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class JsonError(ValueError):
    """A JSON file exists but cannot be parsed."""

    def __init__(self, path, reason):
        super().__init__(f"{path}: invalid JSON ({reason})")
        self.path = str(path)
        self.reason = str(reason)


def _default_mode(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def atomic_write(path, text: str, mode=None) -> None:
    """Write via temp file + fsync + os.replace.

    Keeps an existing file's mode; new files get 0o666 minus the umask.
    Pass `mode` (e.g. 0o600) to force permissions, as for secret files.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    final_mode = _default_mode(path) if mode is None else mode
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), final_mode)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_row(path, header: str, line: str) -> None:
    """Append one line with a single O_APPEND write, so concurrent appenders never drop a row.

    A missing (or empty) file is first created holding just `header` atomically (temp file +
    hard link, which never replaces a file another appender created meanwhile).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        empty = path.stat().st_size == 0
    except FileNotFoundError:
        empty = None
    if empty is None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(header.rstrip("\n") + "\n")
                os.fchmod(fh.fileno(), _default_mode(path))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)
    elif empty:
        atomic_write(path, header.rstrip("\n") + "\n")
    data = line.rstrip("\n") + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        if os.lseek(fd, 0, os.SEEK_END) > 0:
            with open(path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    data = "\n" + data
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def read_json(path, default=None):
    """Parsed JSON, `default` if the file is missing, JsonError if it is corrupt."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeDecodeError) as exc:
        raise JsonError(path, exc) from None


def write_json(path, data) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def unit_id(proj: str, slug: str, n: int, date=None) -> str:
    date = date or _dt.date.today()
    return f"{slugify(proj)}-{slugify(slug)}-{date.strftime('%Y%m%d')}-{int(n)}"


def parse_kv_block(text: str):
    """Parse a dispatch block body. Returns (fields, unknown).

    `KEY<spaces>VALUE` starts a field; a line starting with two spaces continues
    the previous field (joined with a newline). Keys outside DISPATCH_KEYS are
    returned in `unknown` (in order) and their continuation lines are dropped.
    """
    fields, unknown = {}, []
    current = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  "):
            if current is not None:
                piece = raw[2:].rstrip()
                fields[current] = f"{fields[current]}\n{piece}" if fields[current] else piece
            continue
        key, _, value = raw.strip().partition(" ")
        if key in DISPATCH_KEYS:
            current = key
            fields[key] = value.strip()
        else:
            current = None
            unknown.append(key)
    return fields, unknown


_FENCE_LINE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")
# Blocks whose continuation lines may hold their own fenced snippets (the dispatch grammar indents them).
TRAILER_INFOS = ("dispatch", "amend", "stop")


def fence_spans(lines, nest=TRAILER_INFOS) -> list:
    """[(start, end or None, info)] for the fenced blocks in `lines`, in order (CommonMark-like).

    A fence is 3+ backticks or 3+ tildes (a backtick fence's info string has no backtick). A block is
    closed only by a bare fence of the same character and at least the opener's length; a fence line
    of the other character, or a shorter one, is content. Inside a block whose info word is in `nest`
    (```dispatch, ~~~amend, …) only a closer indented no deeper than its opener closes it, so a fenced
    snippet in an indented continuation line stays inside. `end` is None for an unclosed block.
    """
    spans, cur = [], None
    for i, line in enumerate(lines):
        m = _FENCE_LINE.match(line)
        if not m:
            continue
        indent, fence, info = len(m.group(1)), m.group(2), m.group(3).strip()
        if cur is None:
            if fence[0] == "`" and "`" in info:
                continue
            cur = (i, fence[0], len(fence), indent, info)
            continue
        start, char, size, depth, opened = cur
        word = opened.split()[0].lower() if opened else ""
        if fence[0] == char and len(fence) >= size and not info and (word not in nest or indent <= depth):
            spans.append((start, i, opened))
            cur = None
    if cur is not None:
        spans.append((cur[0], None, cur[4]))
    return spans


def fenced_bodies(text: str, lang: str) -> list:
    """Bodies of the closed fenced blocks (``` or ~~~) whose info string is exactly `lang`, in order."""
    lines = (text or "").splitlines()
    return ["\n".join(lines[s + 1:e]).rstrip("\n") for s, e, info in fence_spans(lines)
            if e is not None and info == lang]


def find_fenced_block(text: str, lang: str):
    """Body of the last ```<lang> (or ~~~<lang>) fenced block in `text`, or None."""
    bodies = fenced_bodies(text, lang)
    return bodies[-1] if bodies else None
