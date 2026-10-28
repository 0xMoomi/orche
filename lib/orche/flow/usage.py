"""Usage-limit holds (R20): a vendor's usage or rate limit pauses that vendor's new
attempts and reviews, across projects, until its reset. The merge queue keeps going (no model).

detect(text, vendor, cfg) reads an attempt's problem text (headless.Result.limit_text): None when it
names no limit (`runner.usage_limit.patterns`, case-insensitive), else (until, source): the first of
`reset_patterns` that parses ("reset"), else the backoff ladder `backoff_min` by the hold's step
("backoff"; the step climbs while holds repeat within REPEAT_S, then stays on the last rung). The
strings the CLIs print are unverified (U4), hence a config list.

The hold: ORCHE_HOME/flow/usage-<vendor>.json {vendor, at, until, source, step, text (masked, at most
TEXT_MAX), project (the first unit's), units: [{project, unit, stage, at}], notified, resumed},
written under flow/.usage-<vendor>.lock. A run start of a held vendor joins the hold (job._held); an
attempt that hits a limit records it (hit). The heartbeat (reconcile, heartbeat()) sends the owner
notice once per hold, logs `usage resume` once `until` passed and re-runs the held units (class `held`)."""
import contextlib
import datetime as _dt
import fcntl
import functools
import re

from .. import config, log, mask, paths, units, util

VENDORS = ("claude", "codex")
HELD = ("held", "usage_limit")       # RUN.json stop reasons of a unit a hold stopped
KEYS = ("patterns", "reset_patterns", "backoff_min")
TEXT_MAX = 160
REPEAT_MAX = 3                       # holds of one unit without a done run between; then none (hit)
REPEAT_S = 24 * 3600                # a hold this soon after the last one climbs the backoff ladder
AHEAD = _dt.timedelta(days=8)        # a parsed reset further out than this is not believed
_warned = set()


def settings(cfg) -> dict:
    """runner.usage_limit with every key valid: a missing or malformed one is config.DEFAULTS' (one
    `config` hooks-log line per process)."""
    base = config.DEFAULTS["runner"]["usage_limit"]
    got = ((cfg or {}).get("runner") or {}).get("usage_limit")
    got = got if isinstance(got, dict) else {}
    out = {}
    for key in KEYS:
        value = got.get(key, base[key])
        kind = (int, float) if key == "backoff_min" else str
        ok = (isinstance(value, list) and value and all(isinstance(x, kind) and not isinstance(x, bool) for x in value)
              and (key != "backoff_min" or all(x > 0 for x in value)))
        if not ok and key not in _warned:
            _warned.add(key)
            log.log("hooks", f"config: runner.usage_limit.{key} is malformed; the default is used")
        out[key] = value if ok else base[key]
    return out


@functools.lru_cache(maxsize=32)
def _compiled(patterns: tuple) -> tuple:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:
            log.log("hooks", f"config: runner.usage_limit pattern {p!r} skipped: {exc}")
    return tuple(out)


def is_limit(text, cfg) -> bool:
    """`text` names a usage or rate limit (one of `patterns`)."""
    return bool(text) and any(rx.search(text) for rx in _compiled(tuple(settings(cfg)["patterns"])))


def _num(groups, key):
    v = groups.get(key)
    return int(v) if isinstance(v, str) and v.isdigit() else None


def _when(g, now):
    """The time one reset_patterns match names, else None."""
    if _num(g, "epoch") is not None:
        return _dt.datetime.fromtimestamp(_num(g, "epoch"), _dt.timezone.utc).astimezone(now.tzinfo)
    if _num(g, "seconds") is not None:
        return now + _dt.timedelta(seconds=_num(g, "seconds"))
    if any(_num(g, k) is not None for k in ("days", "hours", "minutes")):
        return now + _dt.timedelta(days=_num(g, "days") or 0, hours=_num(g, "hours") or 0,
                                   minutes=_num(g, "minutes") or 0)
    if g.get("iso"):                                              # without an offset: UTC
        return units.parse_iso(g["iso"].replace(" ", "T"))
    hour, minute, ampm = _num(g, "hour"), _num(g, "minute") or 0, (g.get("ampm") or "").lower()
    if hour is None or (g.get("minute") is None and not ampm):    # "reset 5" names no clock time
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + _dt.timedelta(days=1)       # "resets 3pm": the next 15:00


def reset_time(text, cfg, now):
    """The first reset time a `reset_patterns` match names, later than now and within AHEAD; else None."""
    for rx in _compiled(tuple(settings(cfg)["reset_patterns"])):
        for m in rx.finditer(text or ""):
            at = _when(m.groupdict(), now)
            if at is not None and now < at <= now + AHEAD:
                return at
    return None


def backoff(cfg, step) -> int:
    """Minutes of backoff step `step` (0-based; past the ladder its last rung)."""
    ladder = settings(cfg)["backoff_min"]
    return ladder[min(max(0, int(step)), len(ladder) - 1)]


def _next_step(last, now) -> int:
    at = units.parse_iso((last or {}).get("at"))
    if at is None or (now - at).total_seconds() >= REPEAT_S:
        return 0
    return int((last or {}).get("step") or 0) + 1


def detect(text, vendor, cfg, now=None, step=None):
    """(until, "reset" | "backoff") when `text` names a usage limit, else None. `step` (default: from
    `vendor`'s last hold) picks the backoff rung."""
    if not is_limit(text, cfg):
        return None
    now = now or units.now()
    at = reset_time(text, cfg, now)
    if at is not None:
        return at, "reset"
    step = _next_step(read(vendor), now) if step is None else step
    return now + _dt.timedelta(minutes=backoff(cfg, step)), "backoff"


# ---- the hold store ------------------------------------------------------------------------------

def path(vendor):
    return paths.orche_home() / "flow" / f"usage-{util.slugify(vendor)}.json"


@contextlib.contextmanager
def _locked(vendor):
    lock = path(vendor).with_name(f".usage-{util.slugify(vendor)}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def read(vendor):
    """`vendor`'s hold record (active or past), or None."""
    try:
        data = util.read_json(path(vendor), default=None)
    except (OSError, util.JsonError):
        return None
    return data if isinstance(data, dict) else None


def until_of(hold):
    return units.parse_iso((hold or {}).get("until"))


def is_active(hold, now) -> bool:
    until = until_of(hold)
    return until is not None and until > now


def active(vendor, now=None):
    """`vendor`'s hold while its `until` is ahead, else None."""
    hold = read(vendor)
    return hold if is_active(hold, now or units.now()) else None


def holds() -> list:
    """Every vendor's hold record on file (active or past)."""
    return [h for h in (read(v) for v in VENDORS) if h is not None]


def hhmm(at, now=None) -> str:
    """`15:00` (local), with the date when not today: `10-26 15:00`; `?` when unknown."""
    if at is None:
        return "?"
    now, local = now or units.now(), at.astimezone()
    return local.strftime("%H:%M") if local.date() == now.astimezone().date() else local.strftime("%m-%d %H:%M")


def _join(hold, project, unit, stage, now) -> None:
    rows = [u for u in hold.get("units") or [] if isinstance(u, dict)
            and not (u.get("project") == project and u.get("unit") == unit)]
    hold["units"] = rows + [{"project": project, "unit": unit, "stage": stage, "at": units.iso(now)}]


def repeats(project, unit) -> int:
    """The runs of `unit` a usage limit stopped since its last done pointer: its RUN.json `holds` (the
    runner counts every held run; 0 without one)."""
    from .. import brief, registry
    from ..runs import filing
    root = units.project_root(registry.load(), project)
    return filing.holds_of(root / brief.UNITS / unit) if root is not None else 0


def hit(vendor, text, cfg, *, project, unit, stage, now=None):
    """An attempt's (or review's) problem `text`: when it names a usage limit, record it — a new hold,
    or `unit` joining the active one (whose `until` moves only to a later parsed reset) — and return
    the hold; else None. One `usage hold` events line per new hold. A unit that a limit already
    stopped REPEAT_MAX times without a `done` run between is not held again (a deterministic false
    positive): None, one `usage hold refused unit=<u> reason=repeat` events line, and the run files
    its own status (`failed`)."""
    if not is_limit(text, cfg):
        return None
    if repeats(project, unit) >= REPEAT_MAX:
        log.log("hooks", f"usage: {project}/{unit}: {REPEAT_MAX} holds without a successful run; "
                         f"not held again: {mask.redact(text)[0][:TEXT_MAX]}")
        log.log("events", f"usage hold refused unit={unit} reason=repeat")
        return None
    now = now or units.now()
    with _locked(vendor):
        hold = read(vendor)
        if is_active(hold, now):
            at = reset_time(text, cfg, now)
            if at is not None and at > until_of(hold):
                hold.update(until=units.iso(at), source="reset")
            _join(hold, project, unit, stage, now)
            util.write_json(path(vendor), hold)
            return hold
        step = _next_step(hold, now)
        until, source = detect(text, vendor, cfg, now, step=step)
        hold = {"vendor": vendor, "at": units.iso(now), "until": units.iso(until), "source": source, "step": step,
                "text": mask.redact(text)[0][:TEXT_MAX], "project": project, "units": [], "notified": None,
                "resumed": None}
        _join(hold, project, unit, stage, now)
        util.write_json(path(vendor), hold)
    log.log("events", f"usage hold vendor={vendor} until={hhmm(until, now)} source={source} step={step} units=1")
    return hold


def join(vendor, project, unit, stage, now=None):
    """`unit` joins `vendor`'s active hold (a run start while held); the hold, or None when none is active."""
    now = now or units.now()
    with _locked(vendor):
        hold = read(vendor)
        if not is_active(hold, now):
            return None
        _join(hold, project, unit, stage, now)
        util.write_json(path(vendor), hold)
    return hold


def mark(vendor, **fields) -> None:
    """Set `fields` (notified, resumed) on `vendor`'s hold record under its lock."""
    with _locked(vendor):
        hold = read(vendor)
        if hold is not None:
            hold.update(fields)
            util.write_json(path(vendor), hold)


def held_units(hold, project=None) -> list:
    return [u for u in (hold or {}).get("units") or []
            if isinstance(u, dict) and (project is None or u.get("project") == project)]


def held_text(hold, vendor, now=None) -> str:
    """The quiet `info` of a held unit: `held: claude usage limit until 15:00; this unit runs then`."""
    return f"held: {vendor} usage limit until {hhmm(until_of(hold), now)}; this unit runs then"


def _row(action, project, target, line, failed=None) -> dict:
    """A heartbeat pass row (reconcile), as owner.heartbeat's."""
    return {"action": action, "class": "usage", "project": project, "target": target, "rev": None, "attempt": 1,
            "cap": 1, "line": line, "failed": failed}


def heartbeat(now, dry_run) -> list:
    """The heartbeat's class `usage` (reconcile): an active hold nobody told yet -> the owner notice
    (owner.usage_notice) once, marked first; a hold past its `until` -> `usage resume
    vendor=<v>` once. Rows as owner.heartbeat's."""
    from . import owner
    rows = []
    for hold in holds():
        vendor, until, n = str(hold.get("vendor") or "?"), until_of(hold), len(held_units(hold))
        project, at = str(hold.get("project") or ""), hhmm(until, now)
        if is_active(hold, now) and not hold.get("notified") and project:
            failed = None
            if not dry_run:
                try:
                    mark(vendor, notified=units.iso(now))      # marked first: at most one notice per hold
                    owner.usage_notice(project, vendor, at, n, now)
                except Exception as exc:  # noqa: BLE001 - a notice never stops the pass
                    failed = type(exc).__name__
                    log.log("hooks", f"reconcile: usage notice {vendor}: {failed}: {exc}")
            rows.append(_row("usage-notice", project, vendor, f"{'would tell' if dry_run else 'told'} the "
                                                            f"owner: {vendor} usage limit until {at} ({n} units held)", failed))
        elif until is not None and not is_active(hold, now) and not hold.get("resumed"):
            if not dry_run:
                try:
                    mark(vendor, resumed=units.iso(now))
                except OSError as exc:
                    log.log("hooks", f"reconcile: usage resume {vendor}: {exc}")
                    continue
                log.log("events", f"usage resume vendor={vendor}")
            rows.append(_row("usage-resume", project or "-", vendor,
                             f"{vendor} usage hold over at {at}: its {n} held units re-run"))
    return rows
