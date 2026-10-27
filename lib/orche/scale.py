"""Scale presets (design 2026-10-24 §8, `orche hr scale`): one name for every cap that limits how many
agents run at once. A preset sets dotted config keys (config.json) and registry `limits`; scale is
machine-wide, so it is an HR record (event `scale`, HR-log "scale <p> by <who>").

A preset lists only the values that differ from config.DEFAULTS and registry.LIMIT_DEFAULTS (`default`
lists none); target() is its whole set over KEYS and LIMITS, which the rest compares and sets.
current(cfg, reg): the preset every key of which matches, else "custom".
changes(cfg, reg, preset): [(key, old, new)] the preset would change ("limits.<k>" for registry keys).
apply(preset, by, dry_run): write config keys and limits; returns (data, lines).
"""
from . import config, registry, util
from .hr import book

CUSTOM = "custom"
KEYS = ("runner.max_parallel", "runner.max_parallel_total", "runner.min_free_mb", "runner.queue_wait_s",
        "feature_panes", "feature_min_free_disk_mb", "flow.fanout", "flow.depth", "flow.briefs_per_hour",
        "flow.revisions", "flow.runs_per_unit", "flow.max_unit_cost_usd", "flow.max_project_cost_usd")
LIMITS = ("per_project_per_day", "per_project_per_week", "per_request_auto", "global_per_day_freeze",
          "max_awake_sessions")


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


def target(cfg, name) -> tuple:
    """(config keys, limits) preset `name` sets: the defaults of KEYS and LIMITS, its differences over them."""
    p = presets(cfg)[name]
    return ({**{k: get(config.DEFAULTS, k) for k in KEYS}, **(p.get("config") or {})},
            {**{k: registry.LIMIT_DEFAULTS[k] for k in LIMITS}, **(p.get("limits") or {})})


def changes(cfg, reg, name) -> list:
    conf, lims = target(cfg, name)
    limits = {**registry.LIMIT_DEFAULTS, **(reg.get("limits") or {})}
    return ([(k, get(cfg, k), v) for k, v in conf.items() if get(cfg, k) != v]
            + [(f"limits.{k}", limits.get(k), v) for k, v in lims.items() if limits.get(k) != v])


def current(cfg, reg) -> str:
    for name in presets(cfg):
        if not changes(cfg, reg, name):
            return name
    return CUSTOM


def _show(v):
    return "true" if v is True else "false" if v is False else "-" if v is None else str(v)


def summary(cfg, name) -> str:
    """`runs 20/project, 24 total · features headless · spend cap $250 · min free 3072 MB`."""
    c = target(cfg, name)[0]
    panes = "features keep a pane" if c["feature_panes"] else "features headless"
    return (f"runs {c['runner.max_parallel']}/project, {c['runner.max_parallel_total']} total · {panes} · "
            f"spend cap ${_show(c['flow.max_project_cost_usd'])} · min free {c['runner.min_free_mb']} MB")


def cost_line(cfg, name) -> str:
    spend = _show(target(cfg, name)[0]["flow.max_project_cost_usd"])
    est = presets(cfg)[name].get("estimate_usd_per_hour")
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
        limits = target(cfg, name)[1]
        book.record(reg, f"scale {name} by {by}", change=lambda r: r["limits"].update(limits), event={"type": "scale", "project": "*", "unit": "hr", "by": by},
                    note=f"HR scale {name}: {summary(cfg, name)}")
    data = {"preset": name, "was": was, "dry_run": dry_run, "changed": bool(diff) and not dry_run,
            "changes": [{"key": k, "old": a, "new": b} for k, a, b in diff]}
    return data, lines
