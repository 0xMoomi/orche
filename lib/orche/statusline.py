"""Per-project visibility counts and the compact status-line segment (registry + file counts only).

No orca calls here: `orche statusline` runs on every prompt render and must stay under 50 ms.
"""
import datetime as _dt
import json

from . import paths, registry, runs, units, util

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


def session_owners(reg: dict) -> dict:
    """Map every registered session name to its project key."""
    out = {}
    for key, proj in reg.get("projects", {}).items():
        for name in ("main_session", "liaison_session"):
            if proj.get(name):
                out[proj[name]] = key
        for div in proj.get("divisions", {}).values():
            out[div.get("session")] = key
            for feat in div.get("features", []):
                out[feat.get("session")] = key
    return out


def counts(reg: dict, key: str, now=None) -> dict:
    """{awake, active_features, new, keys, unverified, running} for project `key` (running = live
    `orche run` lock files)."""
    now = now or units.now()
    proj = reg["projects"][key]
    build = proj.get("divisions", {}).get("build", {})
    new = 0
    for ev in reg.get("events", []):
        at = units.parse_iso(ev.get("at"))
        if ev.get("type") == "create" and ev.get("project") == key and at and now - at <= NEW_WINDOW:
            new += 1
    keys = sum(1 for _, req in _json_files("secrets/requests/*.json")
               if req.get("status") == "pending" and req.get("project", key) == key)
    owners = session_owners(reg)
    unverified = 0
    for path, ptr in _json_files("inbox/*/*.json"):
        target = owners.get(path.parent.name) or owners.get(ptr.get("to")) or owners.get(ptr.get("from"))
        if target == key and ptr.get("status") == "done" and ptr.get("ledger_row") is False:
            unverified += 1
    return {
        "awake": len(registry.awake_sessions({"projects": {key: proj}})),
        "active_features": sum(1 for f in build.get("features", []) if f.get("status") == "active"),
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


def short_label(proj: dict, name: str) -> str:
    """Status-line name of a division: SHORT for a standard one, else its `custom_cells` entry."""
    if name in SHORT:
        return SHORT[name]
    return custom_cells(proj).get(name) or name.upper()


def division_cells(proj: dict, active_features: int, short=False) -> list:
    """One cell per division of the project (`registry.divisions_of`); a custom division whose
    record is missing renders as asleep. `short` uses the status-line names (`short_label`)."""
    cells, names = [], {**custom_cells(proj), **SHORT} if short else {}
    for name in registry.divisions_of(proj):
        div = proj.get("divisions", {}).get(name, {})
        glyph = {"active": ACTIVE, "onboarding": ONBOARDING}.get(div.get("status"), ASLEEP)
        cell = names.get(name, name) + glyph
        if name == "build" and active_features:
            cell += str(active_features)
        cells.append(cell)
    return cells


def segment(reg: dict, key: str, now=None) -> str:
    """`R&D● RS○ BUILD●3 GTM● OPS○ · run2 · new1 · 🔑2 · unverified1` (zero trailing cells omitted)."""
    c = counts(reg, key, now)
    cells = division_cells(reg["projects"][key], c["active_features"], short=True)
    parts = [" ".join(cells)] if cells else []
    for label, n in (("run", c["running"]), ("new", c["new"]), ("🔑", c["keys"]),
                     ("unverified", c["unverified"])):
        if n:
            parts.append(f"{label}{n}")
    return " · ".join(parts)


def refresh(reg: dict, key: str) -> None:
    """Rewrite ORCHE_HOME/status/<proj>.line after a registry change (best effort, never raises).

    The one helper every state change uses: init, sleep/wake (also feat and `hr audit --apply`),
    feat new/retire, stage set.
    """
    try:
        util.atomic_write(paths.orche_home() / "status" / f"{key}.line", segment(reg, key) + "\n")
    except (OSError, KeyError):
        pass


def run(cwd, as_json=False) -> int:
    """Resolve the project from `cwd`, write status/<proj>.line, print it. Silent (exit 0) when unresolved."""
    try:
        reg = registry.load()
    except registry.RegistryError:
        return 0
    key = units.project_from_cwd(reg, cwd)
    if key is None:
        return 0
    line = segment(reg, key)
    try:
        util.atomic_write(paths.orche_home() / "status" / f"{key}.line", line + "\n")
    except OSError:
        pass
    print(json.dumps({"project": key, "line": line}, ensure_ascii=False) if as_json else line)
    return 0
