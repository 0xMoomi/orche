"""Re-rendering a registered project's ORCHE-owned unit files at `orche init` (SPEC §8.4).

Ownership is the registry: every live unit's session names its two personas (`.claude/agents/<session>.md`
in the main checkout and in its worktree) and its worktree's `.claude/settings.local.json`; nothing else
is regenerated. Units: every division of `registry.divisions_of(proj)` and every Build feature, except
`retired` and `onboarding` ones (resuming those stays init's onboarding path) and features without a
worktree. A unit whose persona template does not resolve is `kept` (never the minimal fallback over a real
persona); a missing worktree keeps its copy and refreshes the main one. An overwritten persona is first
copied to `ORCHE_HOME/backups/<key>/<ts>/`; a dry run writes nothing (no backups either).
"""
import os
from typing import NamedTuple

from .. import config, paths, registry, units, vendors
from . import book, onboarding

SKIP = ("retired", "onboarding")


class Refresh(NamedTuple):
    changed: list   # paths written (a dry run: would write), in unit order
    kept: list      # (path, why) left as they are
    restart: list   # sessions of `active` units whose persona changed


def _units(proj):
    """(kind, name, entry, model, effort, custom spec) of every unit `refresh_project` re-renders.
    A division's model is its record's; its pane effort the custom spec's, else the recommendation's.
    A feature runs on its division's model at FEATURE_EFFORT."""
    specs, cfg = proj.get("custom_divisions") or {}, config.load()
    for name in registry.divisions_of(proj):
        entry = (proj.get("divisions") or {}).get(name)
        if not entry or entry.get("status") in SKIP or not entry.get("worktree"):
            continue
        spec, rec = specs.get(name), vendors.recommend(cfg, name)
        model = entry.get("model") or (spec["model"] if spec else rec.model)
        yield "div", name, entry, model, spec["effort"] if spec else rec.effort, spec
        for feat in entry.get("features") or []:
            if feat.get("status") in SKIP or not feat.get("worktree"):
                continue
            yield "feat", feat["name"], feat, model, vendors.FEATURE_EFFORT, None


def stamp_version(reg, key, version) -> bool:
    """Record `version` as project `key`'s `orche_version`, saving the registry only when it differs.
    True when it changed."""
    proj = reg["projects"][key]
    if proj.get("orche_version") == version:
        return False
    book.write(reg, lambda r: r["projects"][key].update(orche_version=version))
    return True


def refresh_project(reg, key, *, root, ts, dry_run=False) -> Refresh:
    """Re-render the personas and worktree settings of project `key`'s live units from the current
    templates. Raises onboarding.UnsafeTarget (a target resolving elsewhere), OSError or util.JsonError."""
    proj = reg["projects"].get(key)
    if proj is None:   # a dry run of a new project: nothing registered yet
        return Refresh([], [], [])
    backup_dir = paths.orche_home() / "backups" / key / ts
    out = Refresh([], [], [])
    for unit in _units(proj):
        refresh_unit(key, unit, root=root, backup_dir=backup_dir, out=out, dry_run=dry_run)
    return out


def units_of(proj, division):
    """The `_units` rows of `division` and its workers (a division's row comes before its workers')."""
    current = None
    for unit in _units(proj):
        current = unit[1] if unit[0] == "div" else current
        if current == division:
            yield unit


def refresh_unit(key, unit, *, root, backup_dir, out, dry_run=False) -> None:
    """Re-render one `_units` row's worktree settings (and Entire's commit linking) and personas (main checkout and worktree),
    appending to `out` (a Refresh)."""
    kind, name, entry, model, effort, spec = unit
    changed, kept, restart = out
    role = "division" if kind == "div" else "feature"
    session = entry.get("session") or onboarding.session_name(kind, key, name)
    wt = units.worktree_path(entry["worktree"])
    dirs = [root]
    if os.path.isdir(wt):
        dirs.append(wt)
        path, wrote = onboarding.write_settings(wt, session, main_root=root, dry_run=dry_run)
        if wrote:
            changed.append(path)
        path, wrote = onboarding.link_entire(root, wt, dry_run=dry_run)
        if wrote:
            changed.append(path)
    else:
        kept.append((wt, "missing"))
    if onboarding.persona_template(role, name, custom=bool(spec)) is None:
        kept += [(os.path.join(d, ".claude", "agents", f"{session}.md"), "no persona template") for d in dirs]
        return
    custom = {"label": spec["label"], "charter": spec["charter"]} if spec else None
    written = onboarding.write_persona(dirs, session, role, project=key, name=name, session=session,
                                       model=model, effort=effort, main_root=root, dry_run=dry_run,
                                       custom=custom, backup_dir=None if dry_run else backup_dir)
    changed += written
    if written and entry.get("status") == "active":
        restart.append(session)
