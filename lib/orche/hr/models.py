"""Changing a division's model: `orche hr model` (design 2026-10-23 §3.2).

The record (`divisions.<d>.model`, and `custom_divisions.<d>.model` for a custom division) is the
model; the personas are re-rendered from it (refresh.refresh_unit; a Build division's features too)
and an HR-log line is written. No registry event (EVENT_TYPES is a strict enum). Panes are never
closed: moving to Codex needs the division (and each Build feature) out of `active` first, and an
active Claude pane switches when it restarts.
"""
import time

from .. import cli, config, paths, roster, vendors
from . import book, onboarding, refresh


def set_model(reg, key, name, model, *, by=None, dry_run=False) -> tuple:
    """Move division `name` of project `key` to `model`. Returns (data, lines); raises Refused:
    unknown project or division (2), a retired or onboarding division (3), a grammar error (1),
    Codex not running or an active unit when moving to Codex (3)."""
    proj = (reg.get("projects") or {}).get(key)
    if proj is None:
        raise book.Refused(cli.EXIT_NOT_FOUND, f"no project {key}")
    entry = (proj.get("divisions") or {}).get(name)
    if entry is None:
        raise book.Refused(cli.EXIT_NOT_FOUND, f"{key} has no division {name}")
    session = entry.get("session") or onboarding.session_name("div", key, name)
    if entry.get("status") in ("retired", "onboarding"):
        raise book.Refused(cli.EXIT_BLOCKED, f"{session} is {entry['status']}")
    errs = vendors.errors(model, config.load())
    if errs:
        raise book.Refused(cli.EXIT_USER, f"model {errs[0]}")
    old = entry.get("model") or vendors.recommend(config.load(), name).model
    data = {"project": key, "division": name, "session": session, "old": old, "model": model,
            "vendor": vendors.vendor_of(model), "changed": False, "dry_run": dry_run}
    if old == model:
        return data, [f"{session} is already on {model}"]
    codex = data["vendor"] == vendors.CODEX
    if codex:
        avail = vendors.available(config.load())
        if not avail.ok:
            raise book.Refused(cli.EXIT_BLOCKED, vendors.refusal(avail))
        awake = [session] if entry.get("status") == "active" else []
        awake += [f.get("session") or onboarding.session_name("feat", key, f["name"])
                  for f in entry.get("features") or []
                  if f.get("status") == "active"]
        if awake:
            raise book.Refused(cli.EXIT_BLOCKED, "sleep its pane first: "
                               + " && ".join(f"orche sleep {s}" for s in awake))
    by = by or roster.actor()
    if not dry_run:
        def change(r):
            p = r["projects"][key]
            p["divisions"][name]["model"] = model
            spec = (p.get("custom_divisions") or {}).get(name)
            if spec is not None:
                spec["model"] = model
        book.write(reg, change)
        out = refresh.Refresh([], [], [])
        backup_dir = paths.orche_home() / "backups" / key / time.strftime("%Y%m%dT%H%M%S")
        for unit in refresh.units_of(proj, name):
            refresh.refresh_unit(key, unit, root=proj["path"], backup_dir=backup_dir, out=out)
        book.record(reg, f"model {session} {old} -> {model} by {by}", project=key)
        data["changed"] = True
        data["personas"] = [str(p) for p in out.changed]
    if codex:
        hint = "(headless only; the next run uses it)"
    elif entry.get("status") == "active":
        hint = f"restart its pane to switch: orche sleep {session} && orche wake {session}"
    else:
        hint = "(the next run uses it)"
    lines = [f"{session}: {old} → {model}" + (" (dry-run)" if dry_run else ""), hint]
    return data, lines
