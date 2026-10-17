"""Per-project visibility counts and the compact status-line segment (registry + file counts only).

No orca calls here: `orche statusline` runs on every prompt render and must stay under 50 ms.
"""
import datetime as _dt
import json

from . import paths, registry, units, util

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
    """{awake, active_features, new, keys, unverified} for project `key`."""
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
    }


def division_cells(proj: dict, active_features: int, labels=None) -> list:
    """One cell per division in fixed order; a missing division renders as asleep."""
    cells = []
    for name in registry.DIVISIONS:
        div = proj.get("divisions", {}).get(name, {})
        glyph = {"active": ACTIVE, "onboarding": ONBOARDING}.get(div.get("status"), ASLEEP)
        cell = (labels or {}).get(name, name) + glyph
        if name == "build" and active_features:
            cell += str(active_features)
        cells.append(cell)
    return cells


def segment(reg: dict, key: str, now=None) -> str:
    """`R&D● RS○ BUILD●3 GTM● OPS○ · new1 · 🔑2 · unverified1` (zero trailing cells omitted)."""
    c = counts(reg, key, now)
    parts = [" ".join(division_cells(reg["projects"][key], c["active_features"], SHORT))]
    for label, n in (("new", c["new"]), ("🔑", c["keys"]), ("unverified", c["unverified"])):
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
