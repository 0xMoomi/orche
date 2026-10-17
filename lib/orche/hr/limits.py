"""Feature-agent creation limits and scope-overlap checks (no side effects).

Counts `create` events whose unit starts with `build/` (feature agents), by their `at` time:
per project in the last 24 h and 7 days, globally in the last 24 h, and per `batch` id. The
caller applies a `freeze` verdict (set `frozen`, record the event, notify).
"""
import datetime as _dt
import re
from dataclasses import dataclass

from .. import units

DAY = _dt.timedelta(days=1)
WEEK = _dt.timedelta(days=7)
UNFREEZE_HINT = "run: orche hr unfreeze"
_WILD = re.compile(r"[*?\[]")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    freeze: bool = False


def _feature_creates(reg):
    for ev in reg.get("events", []):
        if ev.get("type") == "create" and str(ev.get("unit", "")).startswith("build/"):
            at = units.parse_iso(ev.get("at"))
            if at is not None:
                yield ev, at


def counts(reg, project, *, batch=None, now=None) -> dict:
    now = now or units.now()
    out = {"project_day": 0, "project_week": 0, "global_day": 0, "batch": 0}
    for ev, at in _feature_creates(reg):
        age = now - at
        if age < DAY:
            out["global_day"] += 1
        if ev.get("project") == project:
            out["project_day"] += age < DAY
            out["project_week"] += age < WEEK
        if batch and ev.get("batch") == batch:
            out["batch"] += 1
    return out


def check_create(reg, project, *, batch=None, now=None) -> Verdict:
    """Whether one more feature agent may be created in `project` now."""
    if reg.get("frozen"):
        return Verdict(False, f"feature-agent creation is frozen; {UNFREEZE_HINT}")
    lim, c = reg["limits"], counts(reg, project, batch=batch, now=now)
    if c["global_day"] >= lim["global_per_day_freeze"]:
        return Verdict(False, f"global limit reached ({c['global_day']}/{lim['global_per_day_freeze']} "
                              f"in 24h); creation is now frozen; {UNFREEZE_HINT}", freeze=True)
    if c["project_day"] >= lim["per_project_per_day"]:
        return Verdict(False, f"{project}: daily limit reached ({c['project_day']}/{lim['per_project_per_day']} "
                              "in 24h)")
    if c["project_week"] >= lim["per_project_per_week"]:
        return Verdict(False, f"{project}: weekly limit reached ({c['project_week']}/"
                              f"{lim['per_project_per_week']} in 7 days)")
    if batch and c["batch"] >= lim["per_request_auto"]:
        return Verdict(False, f"batch {batch}: per-request limit reached ({c['batch']}/{lim['per_request_auto']})")
    return Verdict(True)


def _prefix(pattern: str) -> str:
    """Literal directory prefix of a glob, up to the last `/` before the first wildcard."""
    m = _WILD.search(pattern)
    if m is None:
        return pattern
    head = pattern[:m.start()]
    return head[:head.rfind("/") + 1]


def globs_overlap(a: str, b: str) -> bool:
    """Conservative: True when some path could match both globs."""
    if a == b or units.scope_match(a, b) or units.scope_match(b, a):
        return True
    if not _WILD.search(a) and not _WILD.search(b):
        return False
    pa, pb = _prefix(a), _prefix(b)
    if _WILD.search(a) and _WILD.search(b):
        return pa.startswith(pb) or pb.startswith(pa)
    literal, prefix = (a, pb) if not _WILD.search(a) else (b, pa)
    return literal.startswith(prefix)


def overlaps(scope, features, skip=None) -> list:
    """[(feature name, its glob, new glob)] for active/paused features whose scope overlaps `scope`."""
    found = []
    for feat in features:
        if feat.get("status") not in ("active", "paused") or feat.get("name") == skip:
            continue
        for theirs in feat.get("scope", []):
            for mine in scope:
                if globs_overlap(mine, theirs):
                    found.append((feat["name"], theirs, mine))
    return found
