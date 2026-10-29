"""Per-project visibility counts and the compact status-line segment (registry + file counts only).

No orca calls here: `orche statusline` must stay under 50 ms. The status line itself only prints the
cached ORCHE_HOME/status/<proj>.line (templates/statusline-segment.sh): its first line is the segment;
a second line `spent <YYYY-MM-DD> <usd|n/a>` keeps the day's spend (project.spent_today) for the
refreshes that do not read transcripts, and is dropped the next day.
"""
import datetime as _dt
import json

from . import paths, registry, roster, runs, units, util

ACTIVE, ASLEEP, ONBOARDING = "●", "○", "◐"
SHORT = {"rnd": "R&D", "research": "RS", "build": "BUILD", "gtm": "GTM", "ops": "OPS"}
NEW_WINDOW = _dt.timedelta(hours=24)


def _json_files(pattern):
    for path in sorted(paths.orche_home().glob(pattern)):
        try:
            data = util.read_json(path)
        except (util.JsonError, OSError):
            continue
        if isinstance(data, dict):
            yield path, data


def counts(reg: dict, key: str, now=None, r=None) -> dict:
    """{awake, active_features, workers, new, keys, unverified, running} for project `key` (running = live
    `orche run` lock files; workers = {division: active workers}, active_features their sum). `r` is the
    caller's Roster of `reg`, built here when absent."""
    now = now or units.now()
    proj = reg["projects"][key]
    workers = {name: n for name, div in (proj.get("divisions") or {}).items()
               if (n := sum(1 for f in (div or {}).get("features") or [] if f.get("status") == "active"))}
    new = 0
    for ev in reg.get("events", []):
        at = units.parse_iso(ev.get("at"))
        if ev.get("type") == "create" and ev.get("project") == key and at and now - at <= NEW_WINDOW:
            new += 1
    keys = sum(1 for _, req in _json_files("secrets/requests/*.json")
               if req.get("status") == "pending" and req.get("project", key) == key)
    r = r if r is not None else roster.Roster(reg)

    def owner(session):
        member = r.member(session)
        return member.project if member else None

    unverified = 0
    for path, ptr in _json_files("inbox/*/*.json"):
        target = owner(path.parent.name) or owner(ptr.get("to")) or owner(ptr.get("from"))
        if target == key and ptr.get("status") == "done" and ptr.get("ledger_row") is False:
            unverified += 1
    return {
        "awake": len(registry.awake_sessions({"projects": {key: proj}})),
        "active_features": sum(workers.values()),
        "workers": workers,
        "new": new,
        "keys": keys,
        "unverified": unverified,
        "running": runs.count(key),
    }


def custom_cells(proj: dict) -> dict:
    """{custom division: status-line name}, unique within the project and never a standard cell.

    From the label's letters, upper case (the name's when the label has none): the first 3, else 4,
    else 5 letters, whichever is still free, else the 3-letter form plus the first free digit from 2
    (`Design` → DES, then `Designers` → DESI; a second `QA` → QA2). Cells are assigned in the order
    the divisions were added, so an earlier division keeps its cell."""
    taken, cells = set(SHORT.values()), {}
    for name, spec in (proj.get("custom_divisions") or {}).items():
        label = (spec or {}).get("label") if isinstance(spec, dict) else None
        letters = "".join(c for c in (label or "") if c.isalpha()).upper() \
            or "".join(c for c in name if c.isalpha()).upper()
        cell = next((letters[:n] for n in (3, 4, 5) if letters[:n] not in taken), None)
        digit = 2
        while cell is None:
            cell = f"{letters[:3]}{digit}" if f"{letters[:3]}{digit}" not in taken else None
            digit += 1
        taken.add(cell)
        cells[name] = cell
    return cells


def division_cells(proj: dict, workers: dict, short=False) -> list:
    """One cell per division of the project (`registry.divisions_of`), with its active worker count
    from `workers` ({division: n}, `counts`); a custom division whose record is missing renders as
    asleep. `short` uses the status-line names (SHORT, else `custom_cells`)."""
    cells, names = [], {**custom_cells(proj), **SHORT} if short else {}
    for name in registry.divisions_of(proj):
        div = proj.get("divisions", {}).get(name, {})
        glyph = {"active": ACTIVE, "onboarding": ONBOARDING}.get(div.get("status"), ASLEEP)
        cell = names.get(name, name) + glyph
        if workers.get(name):
            cell += str(workers[name])
        cells.append(cell)
    return cells


def waiting(reg: dict, key: str, now=None) -> tuple:
    """(what waits on the owner: the open ask plus the units blocked on the owner, the PAUSED reason or
    None: a spend cap or an owner pause that holds)."""
    from . import project                # light at import: the status line loads only what it uses
    from .flow import guard, owner
    n = len(project.blocked(reg, key, now)) + (1 if owner.status(key, now) else 0)
    s, p = guard.spend_status(key, now), guard.pause_status(key)
    return n, (f"spend cap ${s['cap']:g} reached" if s else "by the owner" if p else None)


def segment(reg: dict, key: str, now=None, r=None, spent=None) -> str:
    """`waiting3 · R&D● RS○ BUILD●3 DES●2 GTM● OPS○ · run2 · 🔑2 · $4.20 today` (zero cells omitted;
    `spent`: today's USD, "n/a" when unreadable, None when not read today), or, while a spend cap or a
    pause holds, `PAUSED <why> · say "resume" in Main`."""
    n, paused = waiting(reg, key, now)
    if paused:
        return f'PAUSED {paused} · say "resume" in Main'
    c = counts(reg, key, now, r)
    cells = division_cells(reg["projects"][key], c["workers"], short=True)
    parts = ([f"waiting{n}"] if n else []) + ([" ".join(cells)] if cells else [])
    for label, k in (("run", c["running"]), ("🔑", c["keys"])):
        if k:
            parts.append(f"{label}{k}")
    if spent == "n/a":
        parts.append("spend n/a")
    elif spent:
        parts.append(f"${spent:.2f} today")
    return " · ".join(parts)


def _path(key):
    return paths.orche_home() / "status" / f"{key}.line"


def _today(now=None) -> str:
    return (now or units.now()).date().isoformat()


def kept_spend(key, now=None):
    """Today's spend the cached line keeps (float, or "n/a"), else None (none, or another day's)."""
    try:
        lines = _path(key).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    word, day, value = (lines[1].split() + ["", "", ""])[:3] if len(lines) > 1 else ("", "", "")
    if word != "spent" or day != _today(now):
        return None
    try:
        return "n/a" if value == "n/a" else float(value)
    except ValueError:
        return None


def _write(reg, key, now=None, r=None, spent=False) -> str:
    """Render and cache the segment; `spent` (a number or None for unreadable) replaces the kept spend."""
    spent = kept_spend(key, now) if spent is False else ("n/a" if spent is None else spent)
    line = segment(reg, key, now, r, spent)
    tail = f"spent {_today(now)} {spent if spent == 'n/a' else f'{spent:.6f}'}\n" if spent is not None else ""
    try:
        util.atomic_write(_path(key), line + "\n" + tail)
    except OSError:
        pass
    return line


def refresh(reg: dict, key: str, spent=False) -> None:
    """Rewrite ORCHE_HOME/status/<proj>.line after a change (best effort, never raises); `spent`, when
    given, is today's spend (project.spent_today: a number, None when unreadable).

    The one helper every state change uses: init, sleep/wake (also feat and `hr audit --apply`),
    feat new/retire, stage set, a report to Main, the owner's ask, flow pause/resume, `orche status`
    and the heartbeat (which reads the transcripts: the spend is at most one pass old).
    """
    try:
        _write(reg, key, spent=spent)
    except Exception:  # noqa: BLE001 - the status line never fails its caller
        pass


def touch(key: str) -> None:
    """refresh() for a caller without the registry at hand (best effort, never raises)."""
    try:
        reg = registry.load()
        if key in reg.get("projects", {}):
            _write(reg, key)
    except Exception:  # noqa: BLE001 - the status line never fails its caller
        pass


def run(cwd, as_json=False) -> int:
    """Resolve the project from `cwd`, write status/<proj>.line, print it. Silent (exit 0) when unresolved."""
    try:
        reg = registry.load()
    except registry.RegistryError:
        return 0
    r = roster.Roster(reg)
    key = r.place(cwd).project
    if key is None:
        return 0
    line = _write(reg, key, r=r)
    print(json.dumps({"project": key, "line": line}, ensure_ascii=False) if as_json else line)
    return 0
