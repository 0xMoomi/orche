"""Registry (ORCHE_HOME/registry.json, schema version 1). All reads/writes go through here.

A project's divisions are the standard ones it onboarded (keys of `divisions` that are in
DIVISIONS) plus its custom ones (`custom_divisions`: name -> {label, charter, model, effort});
a custom division's runtime record (worktree, session, status) lives in `divisions` like a
standard one. `divisions_of(proj)` is the one enumeration every command uses.
"""
import copy
import re
import unicodedata

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
STANDARD_LABELS = {"rnd": "R&D", "research": "Research", "build": "Build", "gtm": "GTM", "ops": "Ops"}
# A slug: a-z first, then a-z/0-9, single hyphens between parts (no trailing or doubled `-`).
CUSTOM_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
NAME_MIN, NAME_MAX = 2, 16
RESERVED_NAMES = ("main", "liaison", "feat", "hr", "jev")
CUSTOM_KEYS = ("label", "charter", "model", "effort")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LABEL_MAX, CHARTER_MAX = 24, 400
# `onboarding`: the worktree exists but onboarding has not finished (re-run init / feat new to resume).
DIVISION_STATUSES = ("active", "dormant", "paused", "retired", "onboarding")
FEATURE_STATUSES = ("active", "paused", "retired", "onboarding")
EVENT_TYPES = ("create", "sleep", "wake", "retire", "freeze", "unfreeze", "stage", "scale")

PROJECT_STR_KEYS = ("repo_id", "path", "main_session", "card", "standing")
# Optional `orche_version`: the ORCHE version whose templates last rendered the project's files (orche init).
ORCHE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
DIVISION_STR_KEYS = ("worktree", "branch", "session", "model", "last_activity")
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
    if "pane" in feat and not isinstance(feat["pane"], bool):
        errors.append(f"{where}.pane: must be a boolean")
    scope = feat.get("scope")
    if not _is_str_list(scope) or not scope:
        errors.append(f"{where}.scope: must be a non-empty list of globs")


def custom_name_errors(name) -> list:
    """Why `name` cannot be a custom division name (empty when it can)."""
    if not isinstance(name, str) or not NAME_MIN <= len(name) <= NAME_MAX or not CUSTOM_NAME.match(name):
        return [f"custom division name {name!r} must be a slug of {NAME_MIN}-{NAME_MAX} characters: a-z first, "
                "then a-z or 0-9, single hyphens between parts"]
    if name in DIVISIONS:
        return [f"{name!r} is a standard division; check it in the list instead of adding it as custom"]
    if name in RESERVED_NAMES:
        return [f"{name!r} is reserved ({'|'.join(RESERVED_NAMES)})"]
    return []


def _has_control(text: str) -> bool:
    """Control (line breaks, tabs, escapes) or format (bidi overrides, zero-width) characters."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in text)


def label_errors(label) -> list:
    """Why `label` cannot be a custom division's label (messages without a field prefix)."""
    if not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX:
        return [f"must be one line of 1-{LABEL_MAX} characters"]
    if _has_control(label):
        return ["must not contain line breaks or control characters"]
    taken = {n.lower() for n in DIVISIONS} | {lab.lower() for lab in STANDARD_LABELS.values()}
    if label.strip().lower() in taken:
        return [f"{label.strip()!r} is a standard division's name or label; pick another"]
    return []


def charter_errors(charter) -> list:
    """Why `charter` cannot be a custom division's charter (messages without a field prefix)."""
    if not isinstance(charter, str) or not charter.strip() or "\n" in charter or len(charter) > CHARTER_MAX:
        return [f"must be one paragraph of 1-{CHARTER_MAX} characters"]
    if _has_control(charter):
        return ["must not contain control characters"]
    return []


def model_errors(model) -> list:
    """Why a stored `model` cannot be read (messages without a field prefix): only a non-empty string
    without spaces or control characters is required, so a registry written by an earlier version
    (`sonnet[1m]`) or against another config's `models.catalog` still loads. The grammar and the
    catalog (vendors.errors) are checked where a model is written: `orche hr add`, `orche hr model`,
    `orche init` / `orche setup`."""
    if not isinstance(model, str) or not model \
            or any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in model):
        return ["must be a model id without spaces or control characters"]
    return []


def custom_spec_errors(spec, where="custom division") -> list:
    """Schema errors of one stored custom division spec {label, charter, model, effort}."""
    if not isinstance(spec, dict):
        return [f"{where}: must be an object"]
    errors = [f"{where}.{key}: unknown key (allowed: {'|'.join(CUSTOM_KEYS)})" for key in spec if key not in CUSTOM_KEYS]
    errors += [f"{where}.label: {e}" for e in label_errors(spec.get("label"))]
    errors += [f"{where}.charter: {e}" for e in charter_errors(spec.get("charter"))]
    errors += [f"{where}.model: {e}" for e in model_errors(spec.get("model"))]
    if spec.get("effort") not in EFFORTS:
        errors.append(f"{where}.effort: must be one of {'|'.join(EFFORTS)}, got {spec.get('effort')!r}")
    return errors


def divisions_of(proj: dict) -> list:
    """The project's divisions in onboarding order: its standard ones (catalogue order, the order
    `orche init` onboards them), then its custom ones (in the order they were added)."""
    divisions = proj.get("divisions") or {}
    return [d for d in DIVISIONS if d in divisions] + list(proj.get("custom_divisions") or {})


def upgrade(proj: dict, current: str):
    """{"rendered_by", "now"} when the project's `orche_version` is missing (rendered_by None) or is not
    `current`; else None."""
    rendered_by = proj.get("orche_version")
    return None if rendered_by == current else {"rendered_by": rendered_by, "now": current}


def upgrade_text(key: str, up: dict) -> str:
    """`<key> rendered by <version | an earlier version>, now <version> — run orche init <key>`."""
    return f"{key} rendered by {up['rendered_by'] or 'an earlier version'}, now {up['now']} — run orche init {key}"


def _validate_division(name, div, where, errors, customs=()):
    if name not in DIVISIONS and name not in customs:
        errors.append(f"{where}: unknown division {name!r} (allowed: {'|'.join(DIVISIONS)} "
                      "or a name in custom_divisions)")
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
    if "orche_version" in proj and not (isinstance(proj["orche_version"], str)
                                        and ORCHE_VERSION.fullmatch(proj["orche_version"])):
        errors.append(f"{where}.orche_version: must be a version like 0.6.0")
    customs = proj.get("custom_divisions", {})
    if not isinstance(customs, dict):
        errors.append(f"{where}.custom_divisions: must be an object")
        customs = {}
    for name, spec in customs.items():
        errors += [f"{where}.custom_divisions: {e}" for e in custom_name_errors(name)]
        errors += custom_spec_errors(spec, f"{where}.custom_divisions.{name}")
    divisions = proj.get("divisions")
    if not isinstance(divisions, dict):
        errors.append(f"{where}.divisions: must be an object")
        return
    for name, div in divisions.items():
        _validate_division(name, div, f"{where}.divisions.{name}", errors, customs)


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
    division still counts; a headless feature (`pane: false`) has none and does not. Main sessions
    are not counted.
    """
    out = []
    for proj in reg.get("projects", {}).values():
        for div in proj.get("divisions", {}).values():
            if div.get("status") == "active":
                out.append(div["session"])
            for feat in div.get("features", []):
                if feat.get("status") == "active" and feat.get("pane") is not False:
                    out.append(feat["session"])
    return out
