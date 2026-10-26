"""Creation limits, scope overlaps, the awake limit, the stage gate and the freeze switch.

Creation limits count `create` events whose unit is `<division>/<name>` (workers of any division: one
per-project budget shared by all divisions), by their `at` time:
per project in the last 24 h and 7 days, globally in the last 24 h, and per `batch` id. The
caller applies a `freeze` verdict through `freeze` (set `frozen`, record the event, notify).
"""
import datetime as _dt
import re
from dataclasses import dataclass

from .. import cli, registry, roster, units
from . import book

DAY = _dt.timedelta(days=1)
WEEK = _dt.timedelta(days=7)
UNFREEZE_HINT = "run: orche hr unfreeze"
_WILD = re.compile(r"[*?\[]")
GATED = ("ops",)   # divisions that stay dormant while their project is building (R-STAGE-2)


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    freeze: bool = False


def _feature_creates(reg):
    for ev in reg.get("events", []):
        if ev.get("type") == "create" and "/" in str(ev.get("unit", "")):
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


class AwakeLimit(book.Refused):
    """One more awake session would pass limits.max_awake_sessions (exit 3)."""


def awake(reg) -> tuple:
    """(awake sessions, limits.max_awake_sessions). Awake = active divisions and active features
    (registry.awake_sessions); Main panes do not count."""
    return len(registry.awake_sessions(reg)), reg["limits"]["max_awake_sessions"]


def need_awake(reg, n=1, *, plan=False) -> None:
    """Raise AwakeLimit when `n` more awake sessions would pass the limit (`plan`: init's --wake list).
    n=0 still refuses when the registry is already over the limit, as init does today."""
    count, limit = awake(reg)
    if count + n > limit:
        what = f"would be exceeded ({count}+{n}/{limit})" if plan else f"reached ({count}/{limit})"
        raise AwakeLimit(cli.EXIT_BLOCKED, f"awake limit {what}; sleep a unit first (orche sleep <unit>)")


def stage_gated(reg, unit) -> bool:
    """A unit recorded `gated_by_stage` whose project is building: `orche wake` refuses it."""
    return bool((unit.entry or {}).get("gated_by_stage")) and \
        reg["projects"][unit.project].get("stage") == "building"


def unit_gate(reg, unit) -> None:
    """Refuse waking a unit recorded `gated_by_stage` while its project is building (wake)."""
    if stage_gated(reg, unit):
        raise book.Refused(cli.EXIT_BLOCKED, f"{unit.session} is gated by stage and {unit.project} is "
                                             f"building (run: orche stage set {unit.project} public --yes)")


def stage_gate(names, stage, key=None) -> None:
    """Refuse a planned wake of a GATED division while `stage` is building (init and apply-all --wake).
    `key` adds the run hint (apply-all has no single project)."""
    gated = [n for n in names if n in GATED]
    if gated and stage == "building":
        hint = f" (run: orche stage set {key} public --yes)" if key else ""
        raise book.Refused(cli.EXIT_BLOCKED,
                           f"{gated[0]} is gated by stage: it stays dormant while stage is building{hint}")


def freeze(reg, by, reason) -> None:
    """The global creation cap was reached: set `frozen` and record it (by hr-limits)."""
    reg["frozen"] = True
    book.record(reg, f"freeze by hr-limits ({reason}; requested by {by})",
                event={"type": "freeze", "project": "*", "unit": "hr", "by": "hr-limits"},
                note="HR freeze: global feature-agent creation cap reached")


def set_frozen(reg, frozen, *, dry_run=False) -> tuple:
    """`orche hr freeze|unfreeze`. Returns (data, line); data keys: frozen, changed, dry_run."""
    verb = "freeze" if frozen else "unfreeze"
    changed = reg["frozen"] != frozen
    if changed and not dry_run:
        reg["frozen"] = frozen
        by = roster.actor()
        book.record(reg, f"{verb} by {by}", event={"type": verb, "project": "*", "unit": "hr", "by": by},
                    note=f"HR {verb}: feature-agent creation {'stopped' if frozen else 'allowed'}")
    state = "frozen" if frozen else "unfrozen"
    line = f"already {state}" if not changed else f"would {verb} (dry-run)" if dry_run else state
    return {"frozen": reg["frozen"], "changed": changed and not dry_run, "dry_run": dry_run}, line


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
