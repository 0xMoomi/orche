"""Instance config at ORCHE_HOME/config.json, merged over defaults."""
import copy

from . import log, paths, util

DEFAULTS = {
    "orca_bin": "orca",
    "claude_bin": "claude",
    "notify": True,
    "lang": "en",
    "jev": {"enabled": False, "base_url": "", "model": "jev-1.13.0", "timeout_s": 1.0,
            "api_key_env": "JEV_API_KEY"},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def path():
    return paths.orche_home() / "config.json"


def load() -> dict:
    try:
        data = util.read_json(path(), default={})
    except util.JsonError as exc:
        log.log("config", f"using defaults: {exc}")
        data = {}
    return _merge(DEFAULTS, data if isinstance(data, dict) else {})


def save(cfg: dict) -> None:
    util.write_json(path(), cfg)
