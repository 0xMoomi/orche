"""Re-rendering a registered project's ORCHE-owned unit files at `orche init` (SPEC §8.4, design §15.2).

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

from .. import paths, registry, units
from . import onboarding

SKIP = ("retired", "onboarding")


class Refresh(NamedTuple):
    changed: list   # paths written (a dry run: would write), in unit order
    kept: list      # (path, why) left as they are
    restart: list   # sessions of `active` units whose persona changed


def _units(proj):
    """(kind, name, entry, model, effort, custom spec) of every unit `refresh_project` re-renders."""
    specs = proj.get("custom_divisions") or {}
    for name in registry.divisions_of(proj):
        entry = (proj.get("divisions") or {}).get(name)
        if not entry or entry.get("status") in SKIP or not entry.get("worktree"):
            continue
        spec = specs.get(name)
        model, effort = (spec["model"], spec["effort"]) if spec else onboarding.DIVISION_MODELS[name]
        yield "div", name, entry, entry.get("model") or model, effort, spec
        if name != "build":
            continue
        for feat in entry.get("features") or []:
            if feat.get("status") in SKIP or not feat.get("worktree"):
                continue
            yield "feat", feat["name"], feat, onboarding.FEATURE_MODEL[0], onboarding.FEATURE_MODEL[1], None


def stamp_version(reg, key, version) -> bool:
    """Record `version` as project `key`'s `orche_version`, saving the registry only when it differs.
    True when it changed."""
    proj = reg["projects"][key]
    if proj.get("orche_version") == version:
        return False
    proj["orche_version"] = version
    registry.save(reg)
    return True


def refresh_project(reg, key, *, root, ts, dry_run=False) -> Refresh:
    """Re-render the personas and worktree settings of project `key`'s live units from the current
    templates. Raises onboarding.UnsafeTarget (a target resolving elsewhere), OSError or util.JsonError."""
    proj = reg["projects"].get(key)
    if proj is None:   # a dry run of a new project: nothing registered yet
        return Refresh([], [], [])
    backup_dir = paths.orche_home() / "backups" / key / ts
    changed, kept, restart = [], [], []
    for kind, name, entry, model, effort, spec in _units(proj):
        role = "division" if kind == "div" else "feature"
        session = entry.get("session") or onboarding.session_name(kind, key, name)
        wt = units.worktree_path(entry["worktree"])
        dirs = [root]
        if os.path.isdir(wt):
            dirs.append(wt)
            path, wrote = onboarding.write_settings(wt, session, main_root=root, dry_run=dry_run)
            if wrote:
                changed.append(path)
        else:
            kept.append((wt, "missing"))
        if onboarding.persona_template(role, name, custom=bool(spec)) is None:
            kept += [(os.path.join(d, ".claude", "agents", f"{session}.md"), "no persona template") for d in dirs]
            continue
        custom = {"label": spec["label"], "charter": spec["charter"]} if spec else None
        written = onboarding.write_persona(dirs, session, role, project=key, name=name, session=session,
                                           model=model, effort=effort, main_root=root, dry_run=dry_run,
                                           custom=custom, backup_dir=None if dry_run else backup_dir)
        changed += written
        if written and entry.get("status") == "active":
            restart.append(session)
    return Refresh(changed, kept, restart)
