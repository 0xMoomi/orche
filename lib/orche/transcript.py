"""Read Claude Code transcript JSONL: the last assistant message and the session's first timestamp."""
import json
import os

from . import units

TAIL_BYTES = 2 * 1024 * 1024


def _lines(path, tail_only):
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - TAIL_BYTES) if tail_only else 0
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 and lines else lines


def _entries_reversed(path):
    for tail_only in (True, False):
        found = False
        for line in reversed(_lines(path, tail_only)):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "assistant":
                found = True
                yield entry
        if found or os.path.getsize(path) <= TAIL_BYTES:
            return


def last_assistant(path):
    """(message_id, text) of the last assistant message; text blocks of one message id are joined."""
    msg_id, parts = None, []
    try:
        for entry in _entries_reversed(path):
            msg = entry.get("message") or {}
            this_id = msg.get("id") or entry.get("uuid")
            if msg_id is None:
                msg_id = this_id
            elif this_id != msg_id:
                break
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                parts.append("\n".join(t for t in texts if t))
    except OSError:
        return None, ""
    return msg_id, "\n".join(p for p in reversed(parts) if p)


def last_assistant_text(path) -> str:
    return last_assistant(path)[1]


def first_timestamp(path):
    """Aware datetime of the first line carrying a `timestamp`, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("timestamp"):
                    return units.parse_iso(str(entry["timestamp"]).replace("Z", "+00:00"))
    except OSError:
        return None
    return None
