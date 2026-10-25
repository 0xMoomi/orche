"""Scale presets (design 2026-10-24 §8, `orche hr scale`): one name for every cap that limits how many
agents run at once. A preset sets dotted config keys (config.json) and registry `limits`; scale is
machine-wide, so it is an HR record (event `scale`, HR-log "scale <p> by <who>").

current(cfg, reg): the preset every key of which matches, else "custom".
changes(cfg, reg, preset): [(key, old, new)] the preset would change ("limits.<k>" for registry keys).
apply(preset, by, dry_run): write config keys and limits; returns (data, lines).
"""
from . import config, registry, util
from .hr import book

CUSTOM = "custom"


def presets(cfg) -> dict:
    return ((cfg.get("scale") or {}).get("presets")) or config.DEFAULTS["scale"]["presets"]


def get(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _put(cfg, dotted, value):
    *head, last = dotted.split(".")
    cur = cfg
    for part in head:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[last] = value


def changes(cfg, reg, name) -> list:
    p = presets(cfg)[name]
    out = [(k, get(cfg, k), v) for k, v in (p.get("config") or {}).items() if get(cfg, k) != v]
    limits = {**registry.LIMIT_DEFAULTS, **(reg.get("limits") or {})}
    out += [(f"limits.{k}", limits.get(k), v) for k, v in (p.get("limits") or {}).items() if limits.get(k) != v]
    return out


def current(cfg, reg) -> str:
    for name in presets(cfg):
        if not changes(cfg, reg, name):
            return name
    return CUSTOM


def _show(v):
    return "true" if v is True else "false" if v is False else "-" if v is None else str(v)


def summary(cfg, name) -> str:
    """`runs 20/project, 24 total · features headless · spend cap $250 · min free 3072 MB`."""
    c = presets(cfg)[name].get("config") or {}
    panes = "features keep a pane" if c.get("feature_panes", True) else "features headless"
    return (f"runs {c.get('runner.max_parallel')}/project, {c.get('runner.max_parallel_total')} total · {panes} · "
            f"spend cap ${_show(c.get('flow.max_project_cost_usd'))} · min free {c.get('runner.min_free_mb')} MB")


def cost_line(cfg, name) -> str:
    p = presets(cfg)[name]
    spend = _show((p.get("config") or {}).get("flow.max_project_cost_usd"))
    est = p.get("estimate_usd_per_hour")
    head = f"about ${est} an hour API-equivalent at full speed; " if est else ""
    return f"cost: {head}the spend cap stops at ${spend} per window until the owner's next word"


def apply(name, by, *, dry_run=False):
    """Set preset `name`. Returns (data, lines); raises KeyError for an unknown preset."""
    cfg = config.load()
    if name not in presets(cfg):
        raise KeyError(name)
    reg = registry.load()
    was = current(cfg, reg)
    diff = changes(cfg, reg, name)
    keys = " · ".join(f"{k} {_show(a)} → {_show(b)}" for k, a, b in diff) or "nothing to change"
    lines = [f"scale {name} (was {was}): {keys} ({len(diff)} keys)", cost_line(cfg, name)]
    if dry_run:
        lines[0] = "would set " + lines[0] + " (dry-run)"
    elif diff:
        raw = util.read_json(config.path(), default={})
        raw = raw if isinstance(raw, dict) else {}
        for k, _, v in diff:
            if not k.startswith("limits."):
                _put(raw, k, v)
        config.save(raw)
        reg["limits"].update(presets(cfg)[name].get("limits") or {})
        book.record(reg, f"scale {name} by {by}", event={"type": "scale", "project": "*", "unit": "hr", "by": by},
                    note=f"HR scale {name}: {summary(cfg, name)}")
    data = {"preset": name, "was": was, "dry_run": dry_run, "changed": bool(diff) and not dry_run,
            "changes": [{"key": k, "old": a, "new": b} for k, a, b in diff]}
    return data, lines
