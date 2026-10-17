"""Registry (ORCHE_HOME/registry.json, schema version 1). All reads/writes go through here."""
import copy

from . import paths, util

VERSION = 1
LIMIT_DEFAULTS = {
    "per_project_per_day": 3,
    "per_project_per_week": 10,
    "per_request_auto": 2,
    "global_per_day_freeze": 20,
    "idle_days_to_sleep": 14,
    "max_awake_sessions": 12,
}
STAGES = ("building", "public")
DIVISIONS = ("rnd", "research", "build", "gtm", "ops")
# `onboarding`: the worktree exists but onboarding has not finished (re-run init / feat new to resume).
DIVISION_STATUSES = ("active", "dormant", "paused", "retired", "onboarding")
FEATURE_STATUSES = ("active", "paused", "retired", "onboarding")
EVENT_TYPES = ("create", "sleep", "wake", "retire", "freeze", "unfreeze", "stage")

PROJECT_STR_KEYS = ("repo_id", "path", "main_session", "liaison_session", "card", "standing")
DIVISION_STR_KEYS = ("worktree", "branch", "session", "model", "effort", "last_activity")
FEATURE_STR_KEYS = ("name", "worktree", "branch", "session", "created", "last_activity", "reason")
EVENT_STR_KEYS = ("at", "project", "unit", "by")
SECRET_BACKENDS = ("gcp", "vercel")
SECRET_ENVS = ("dev", "preview", "prod")
SECRET_KEYS = ("backend", "gcp_project", "vercel_project", "gh_repo", "cache_local")


class RegistryError(ValueError):
    def __init__(self, errors, where=None):
        prefix = f"invalid registry {where}" if where else "invalid registry"
        super().__init__(prefix + ": " + "; ".join(errors))
        self.errors = list(errors)


def path():
    return paths.orche_home() / "registry.json"


def empty() -> dict:
    return {"version": VERSION, "limits": dict(LIMIT_DEFAULTS), "frozen": False, "projects": {}, "events": []}


def load() -> dict:
    try:
        reg = util.read_json(path())
    except util.JsonError as exc:
        raise RegistryError([f"invalid JSON ({exc.reason})"], where=path()) from None
    if reg is None:
        return empty()
    errors = validate(reg)
    if errors:
        raise RegistryError(errors, where=path())
    return reg


def save(reg: dict) -> None:
    errors = validate(reg)
    if errors:
        raise RegistryError(errors)
    util.write_json(path(), reg)


def _need_str(obj, keys, where, errors):
    for key in keys:
        if not isinstance(obj.get(key), str):
            errors.append(f"{where}.{key}: missing or not a string")


def _need_enum(obj, key, allowed, where, errors):
    if obj.get(key) not in allowed:
        errors.append(f"{where}.{key}: must be one of {'|'.join(allowed)}, got {obj.get(key)!r}")


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _validate_feature(feat, where, errors):
    if not isinstance(feat, dict):
        errors.append(f"{where}: must be an object")
        return
    _need_str(feat, FEATURE_STR_KEYS, where, errors)
    _need_enum(feat, "status", FEATURE_STATUSES, where, errors)
    scope = feat.get("scope")
    if not _is_str_list(scope) or not scope:
        errors.append(f"{where}.scope: must be a non-empty list of globs")


def _validate_division(name, div, where, errors):
    if name not in DIVISIONS:
        errors.append(f"{where}: unknown division {name!r} (allowed: {'|'.join(DIVISIONS)})")
        return
    if not isinstance(div, dict):
        errors.append(f"{where}: must be an object")
        return
    _need_str(div, DIVISION_STR_KEYS, where, errors)
    _need_enum(div, "status", DIVISION_STATUSES, where, errors)
    if "gated_by_stage" in div and not isinstance(div["gated_by_stage"], bool):
        errors.append(f"{where}.gated_by_stage: must be a boolean")
    if "features" in div:
        if name != "build":
            errors.append(f"{where}.features: only allowed under build")
        elif not isinstance(div["features"], list):
            errors.append(f"{where}.features: must be a list")
        else:
            for i, feat in enumerate(div["features"]):
                _validate_feature(feat, f"{where}.features[{i}]", errors)


def _validate_secrets(sec, where, errors):
    """Optional per-project Keymaster config; every key optional, backend defaults to gcp."""
    if not isinstance(sec, dict):
        errors.append(f"{where}: must be an object")
        return
    for key in sec:
        if key not in SECRET_KEYS:
            errors.append(f"{where}.{key}: unknown key (allowed: {'|'.join(SECRET_KEYS)})")
    if "backend" in sec:
        _need_enum(sec, "backend", SECRET_BACKENDS, where, errors)
    if "gcp_project" in sec:
        ids = sec["gcp_project"]
        if not isinstance(ids, dict):
            errors.append(f"{where}.gcp_project: must be an object of env -> project id")
        else:
            for env, value in ids.items():
                if env not in SECRET_ENVS:
                    errors.append(f"{where}.gcp_project.{env}: unknown env (allowed: {'|'.join(SECRET_ENVS)})")
                elif not isinstance(value, str) or not value:
                    errors.append(f"{where}.gcp_project.{env}: must be a non-empty string")
    if "vercel_project" in sec and not isinstance(sec["vercel_project"], str):
        errors.append(f"{where}.vercel_project: must be a string")
    if "gh_repo" in sec and not (isinstance(sec["gh_repo"], str)
                                 and len(sec["gh_repo"].split("/")) == 2 and all(sec["gh_repo"].split("/"))):
        errors.append(f"{where}.gh_repo: must be owner/repo")
    if "cache_local" in sec and not isinstance(sec["cache_local"], bool):
        errors.append(f"{where}.cache_local: must be a boolean")


def _validate_project(key, proj, errors):
    where = f"projects.{key}"
    if not isinstance(proj, dict):
        errors.append(f"{where}: must be an object")
        return
    _need_str(proj, PROJECT_STR_KEYS, where, errors)
    _need_enum(proj, "stage", STAGES, where, errors)
    if not _is_str_list(proj.get("aliases")):
        errors.append(f"{where}.aliases: must be a list of strings")
    if "secrets" in proj:
        _validate_secrets(proj["secrets"], f"{where}.secrets", errors)
    divisions = proj.get("divisions")
    if not isinstance(divisions, dict):
        errors.append(f"{where}.divisions: must be an object")
        return
    for name, div in divisions.items():
        _validate_division(name, div, f"{where}.divisions.{name}", errors)


def validate(reg) -> list:
    """Return a list of human-readable schema errors (empty when valid)."""
    if not isinstance(reg, dict):
        return ["registry: must be an object"]
    errors = []
    if reg.get("version") != VERSION:
        errors.append(f"version: must be {VERSION}")
    limits = reg.get("limits")
    if not isinstance(limits, dict):
        errors.append("limits: must be an object")
    else:
        for key in LIMIT_DEFAULTS:
            value = limits.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"limits.{key}: must be a non-negative integer")
    if not isinstance(reg.get("frozen"), bool):
        errors.append("frozen: must be a boolean")
    projects = reg.get("projects")
    if not isinstance(projects, dict):
        errors.append("projects: must be an object")
    else:
        for key, proj in projects.items():
            _validate_project(key, proj, errors)
    events = reg.get("events")
    if not isinstance(events, list):
        errors.append("events: must be a list")
    else:
        for i, ev in enumerate(events):
            where = f"events[{i}]"
            if not isinstance(ev, dict):
                errors.append(f"{where}: must be an object")
                continue
            _need_str(ev, EVENT_STR_KEYS, where, errors)
            _need_enum(ev, "type", EVENT_TYPES, where, errors)
            if "batch" in ev and not isinstance(ev["batch"], str):
                errors.append(f"{where}.batch: must be a string")
    return errors


def project(reg: dict, key_or_alias: str):
    """Resolve a project by key or alias (case-insensitive). Returns (key, project) or None."""
    needle = (key_or_alias or "").strip().lower()
    projects = reg.get("projects", {})
    for key, proj in projects.items():
        if key.lower() == needle:
            return key, proj
    for key, proj in projects.items():
        if needle in (a.lower() for a in proj.get("aliases", [])):
            return key, proj
    return None


def append_event(reg: dict, **fields) -> dict:
    """Append one event (append-only). `at` defaults to now. Returns the event."""
    event = {"at": util.now_iso(), **fields}
    errors = []
    _need_str(event, EVENT_STR_KEYS, "event", errors)
    _need_enum(event, "type", EVENT_TYPES, "event", errors)
    if errors:
        raise RegistryError(errors)
    reg.setdefault("events", []).append(copy.deepcopy(event))
    return event


def awake_sessions(reg: dict) -> list:
    """Sessions of active divisions and of every active feature, whatever its division's status.

    The limit is about live sessions/panes, so an active feature under a paused or dormant
    division still counts. Main/liaison sessions are not counted.
    """
    out = []
    for proj in reg.get("projects", {}).values():
        for div in proj.get("divisions", {}).values():
            if div.get("status") == "active":
                out.append(div["session"])
            for feat in div.get("features", []):
                if feat.get("status") == "active":
                    out.append(feat["session"])
    return out
