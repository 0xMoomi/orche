"""`orche hr choices` (design 2026-10-23 §10.3, Appendix D): the division kinds and models of a
project, and the owner's questions built from them (`ask`), each one element of AskUserQuestion's
`questions` plus the value of every label. Read-only; the only subprocesses are `codex --version`, the RAM
probe of `ask.scale` (`procs.mem_total_mb`) and, for a project not under ORCHE yet, local git reads of the
folder the owner is in (detect).

Guardrails (pinned by tests/test_choices.py): never an unavailable model as an option (it is named
in the question instead), the recommendation first and marked, a header of at most 12 characters,
2-4 options with unique labels free of ", ", and an entry with fewer than 2 options is None.
Question wording lives here (`TEXTS`, it is UI, in English: Main asks in the owner's language and
maps the picked label through `values`); descriptions live in config (`kinds`, `models.catalog`:
owner-editable data)."""
import os
import shutil

from .. import procs, registry, scale, setup_flow, vendors

PRESETS = setup_flow.PRESETS
HIDDEN = ("retired", "onboarding")
SCALES = ("default", "20", "30")
TIGHT_RAM_MB, TIGHT_DISK_MB = 32 * 1024, 30 * 1024

TEXTS = {
    "add_q": "Which divisions should I add? (pick any)", "add_h": "Division",
    "new_label": "A new one", "new_desc": "type its name and what it should do",
    "change_q": "Which division's model should change?", "change_h": "Division",
    "change_more": " (also: {labels} — type the name)",
    "now_rec": "now {model} (recommended)", "now_other": "now {model} · recommended {rec}",
    "model_new": "Which model should {label} run on?",
    "model_star": "Which model should the new division run on?",
    "model_change": "Which model should {label} switch to? (now: {model})", "model_h": "Model",
    "rec": " (recommended)", "keep_label": "Keep", "keep_desc": "stay on {model}",
    "cost": " · cost {dots}", "estimate": " (estimate)",
    "codex_absent": "Codex is not installed (install: npm install -g @openai/codex, then codex login)",
    "codex_broken": "Codex is installed but does not start (fix: npm install -g @openai/codex@latest)",
    "codex_active": "Codex is not offered while this division's pane is open (sleep it first)",
    "setup_q": "How will this project run?", "setup_h": "Preset",
    "preset": {"build": ("Build-focused", "{groups}; Build's workers specialise in front, back and infra. For a "
                                          "product that is being built."),
               "operations": ("Operations", "{names}. For a service that is live.")},
    "preset_pick": ("Pick them", "one by one, {extras} included"),
    "setup_models_q": "Which models should the divisions use?", "setup_models_h": "Models",
    "models_fitted": ("Fitted to each (recommended)", "{groups}"),
    "models_all": ("All {model}", "the strongest for all · cost {dots}"),
    "models_pick": ("Per division", "one question per division next"),
    "pick_std_q": "Which divisions? (pick any)", "pick_std_h": "Divisions",
    "pick_extra_q": "Any more? (pick any)", "pick_extra_h": "Extras",
    "none": ("None", "only the ones above"),
    "status": {"active": "active", "dormant": "dormant", "paused": "paused", "onboarding": "onboarding",
               "retired": "retired"},
    "scale_q": "How many agents should run at once?{machine}", "scale_h": "Scale", "scale_now": " (now)",
    "scale_machine": " (this Mac: {ram} GB RAM · {disk} GB free disk — tight for 30)",
    "scale": {"default": ("Default", "{par} runs at once · features headless · spend cap ${cap}"),
              "20": ("Up to 20", "{par} runs at once · features without panes · spend cap ${cap} "
                                 "(about ${hour}/h API-equivalent)"),
              "30": ("Up to 30", "{par} runs at once · spend cap ${cap} (about ${hour}/h) · a Claude subscription hits "
                                 "its usage window: mix in an API key or Codex")},
    "where_q": "Where is the project?", "where_h": "Project",
    "where_new": ("Create a new repository in this folder ({name})", "git init and a first commit here; then GitHub "
                  "and Orca"),
    "where_other": ("Use another folder (type its path)", "an existing repository or an empty folder elsewhere"),
    "github_q": "Should the new repository go to GitHub?", "github_h": "GitHub",
    "github": {"private": ("Private", "a private repository under your gh account"),
               "public": ("Public", "anyone can see it"),
               "local": ("Local only (no GitHub yet)", 'publish it later: say "publish this repository to GitHub"')},
    "rules": "name: 2–16 characters, a-z first, then a-z, 0-9 or single hyphens (e.g. marketing) · "
             "label: ≤ 24 characters · charter: one paragraph, ≤ 400 characters",
}


def _entry(question, header, multi, opts):
    """One `ask` entry from `opts` [(label, description, value, key)]: labels made unique (a clash
    gains " (<key>)") and free of ", "; None below 2 options; at most 4 (the caller trims)."""
    if len(opts) < 2:
        return None
    seen, options, values = set(), [], {}
    for lab, desc, value, key in opts[:4]:
        lab = lab.replace(", ", " ")
        if lab in seen:
            lab = f"{lab} ({key})"
        seen.add(lab)
        options.append({"label": lab, "description": desc})
        values[lab] = value
    return {"tool": {"question": question, "header": header[:12], "multiSelect": multi, "options": options},
            "values": values}


def _kind_order(cfg, proj):
    keys = list(registry.DIVISIONS)
    for name in list((proj or {}).get("custom_divisions") or {}) + list((proj or {}).get("divisions") or {}):
        if name not in keys:
            keys.append(name)
    for name in cfg.get("kinds") or {}:
        if name != "custom" and name not in keys:
            keys.append(name)
    return keys


def _codex_note(t, avail):
    if avail is None or avail.ok:
        return ""
    return t["codex_absent"] if avail.state == "absent" else t["codex_broken"]


def _active(div):
    if div.get("status") == "active":
        return True
    return any(f.get("status") == "active" for f in div.get("features") or [])


def _model_entry(t, cfg, models, kind, question, avail, current=None, active=False):
    """`ask.model.<kind>`: available models, the recommendation first and marked; for an existing
    division (`current`) the current model goes and Keep (None) comes."""
    rec = kind["recommended"]
    cands = [m for m in models if m["available"] and m["model"] != current
             and not (active and m["vendor"] == vendors.CODEX)]
    cands.sort(key=lambda m: m["model"] != rec)
    opts = []
    for m in cands:
        mark = m["model"] == rec
        desc = (kind["why"] if mark and kind["why"] else m["desc"])
        if m["cost"]:
            desc += t["cost"].format(dots=m["cost"])
        opts.append((m["label"] + (t["rec"] if mark else ""), desc, m["model"], m["model"]))
    if current is not None:
        cur = vendors.catalog(cfg, current)["label"]
        keep_rec = current == rec
        keep = (t["keep_label"] + (t["rec"] if keep_rec else ""),
                t["keep_desc"].format(model=cur) + (f" · {kind['why']}" if keep_rec and kind["why"] else ""),
                None, "keep")
        opts = [keep] + opts[:3] if keep_rec else opts[:3] + [keep]
    note = _codex_note(t, avail)
    if active and not note:
        note = t["codex_active"] if any(m["vendor"] == vendors.CODEX and m["available"] for m in models) else ""
    return _entry(question + (f" {note}" if note else ""), t["model_h"], False, opts)


def machine_info(path=None) -> dict:
    """RAM and free disk (MB) of this machine; None where unknown."""
    try:
        disk = shutil.disk_usage(path or os.path.expanduser("~")).free // (1024 * 1024)
    except OSError:
        disk = None
    return {"ram_mb": procs.mem_total_mb(), "free_disk_mb": disk}


def _scale_entry(t, cfg, reg, machine):
    cur = scale.current(cfg, reg or {})
    tight = [v for v, lim in ((machine.get("ram_mb"), TIGHT_RAM_MB), (machine.get("free_disk_mb"), TIGHT_DISK_MB))
             if v is not None and v < lim]
    note = ""
    if tight:
        gb = lambda v: "?" if v is None else str(round(v / 1024))  # noqa: E731
        note = t["scale_machine"].format(ram=gb(machine.get("ram_mb")), disk=gb(machine.get("free_disk_mb")))
    avail = scale.presets(cfg)
    opts = []
    for name in SCALES:
        if name in avail:
            lab, desc = t["scale"][name]
            conf = scale.target(cfg, name)[0]
            desc = desc.format(par=conf["runner.max_parallel"], cap=conf["flow.max_project_cost_usd"],
                               hour=avail[name].get("estimate_usd_per_hour"))
            opts.append((lab + (t["scale_now"] if name == cur else ""), desc, name, name))
    return cur, _entry(t["scale_q"].format(machine=note), t["scale_h"], False, opts)


def _models(t, cfg, catalog, avail) -> list:
    """The catalog's models, each with its label, cost dots and whether it runs here."""
    models = []
    for m in catalog:
        row = vendors.catalog(cfg, m)
        codex = vendors.vendor_of(m) == vendors.CODEX
        ok = not codex or (avail is not None and avail.ok)
        models.append({"model": m, "vendor": vendors.vendor_of(m), "label": row["label"], "desc": row["desc"],
                       "cost": row["dots"] + (t["estimate"] if codex and row["dots"] else ""),
                       "available": ok, "reason": None if ok else _codex_note(t, avail),
                       "headless_only": codex})
    return models


def _kinds(t, cfg, key, proj, rec) -> list:
    """Every division kind of the project (or of a new one, suggested from preset `rec`), in display order."""
    divs = proj.get("divisions") or {}
    customs = proj.get("custom_divisions") or {}
    stage = proj.get("stage") or "building"
    kinds = []
    for name in _kind_order(cfg, proj):
        info = vendors.kind(cfg, name)
        custom = customs.get(name) or {}
        div = divs.get(name)
        status = (div or {}).get("status")
        exists = div is not None and status != "retired"
        if key:
            suggested = not exists and status != "retired" and (
                name in PRESETS["operations"] or (name == "ops" and stage == "public"))
        else:
            suggested = name in PRESETS[rec]
        kinds.append({"key": name, "standard": name in registry.DIVISIONS,
                      "label": custom.get("label") or (info["label"] if info["listed"] else name),
                      "desc": info["desc"], "charter": custom.get("charter") or info["charter"] or None,
                      "exists": exists, "status": status,
                      "status_text": t["status"].get(status) if status else None,
                      "session": (div or {}).get("session"), "model": (div or {}).get("model") if div else None,
                      "recommended": info["model"], "pane_effort": info["effort"], "why": info["why"],
                      "suggested": suggested})
    return kinds


def _project_asks(t, kinds, mlabel) -> tuple:
    """`ask.add` and `ask.change` of a project under ORCHE."""
    addable = [k for k in kinds if k["status"] is None]
    addable.sort(key=lambda k: not k["suggested"])
    opts = [(k["label"], k["desc"], k["key"], k["key"]) for k in addable[:3]]
    opts.append((t["new_label"], t["new_desc"], "*", "*"))
    add = _entry(t["add_q"], t["add_h"], True, opts)
    live = [k for k in kinds if k["exists"] and k["status"] not in HIDDEN]
    opts = []
    for k in live:
        cur = k["model"] or k["recommended"]
        text = t["now_rec"] if cur == k["recommended"] else t["now_other"]
        opts.append((k["label"], text.format(model=mlabel.get(cur) or cur,
                                             rec=mlabel.get(k["recommended"]) or k["recommended"]),
                     k["key"], k["key"]))
    q = t["change_q"]
    if len(live) > 4:
        q += t["change_more"].format(labels=", ".join(k["label"] for k in live[4:]))
    return add, _entry(q, t["change_h"], False, opts)


def _and(words) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _preset_option(t, name, by, mlabel, rec) -> tuple:
    """One preset of `ask.setup`: its label (the recommendation marked), its description and its value, a setup
    file fragment (`preset`, the divisions, a custom one as {})."""
    members = [n for n in PRESETS[name] if n in by]
    groups = {}
    for n in members:
        groups.setdefault(by[n]["recommended"], []).append(n)
    lab, desc = t["preset"][name]
    value = {"preset": name, "divisions": members,
             "custom": {n: {} for n in members if n not in registry.DIVISIONS}}
    return (lab + (t["rec"] if name == rec else ""),
            desc.format(groups=" and ".join(f"{', '.join(ns)} on {mlabel.get(m) or m}" for m, ns in groups.items()),
                        names=_and(members)), value, name)


def _setup_asks(t, kinds, models, mlabel, custom, avail, rec) -> tuple:
    """`ask.setup` and `ask.pick` of a project not under ORCHE yet; `rec` is the recommended preset."""
    by = {k["key"]: k for k in kinds}
    chosen = [n for n in PRESETS[rec] if n in by]
    extras = [k for k in kinds if k["key"] not in chosen]
    groups = {}
    for n in chosen:
        groups.setdefault(by[n]["recommended"], []).append(by[n]["label"])
    group_text = ", ".join(f"{'·'.join(labs)} {mlabel.get(m) or m}" for m, labs in groups.items())
    strongest = custom["recommended"]
    sdots = next((m["cost"] for m in models if m["model"] == strongest), "")
    pl, pd = t["preset_pick"]
    (fl, fd), (al, ad), (ml, md) = t["models_fitted"], t["models_all"], t["models_pick"]
    # "All <strongest>" only when that model runs here (a Codex recommendation needs Codex)
    s_ok = vendors.vendor_of(strongest) != vendors.CODEX or (avail is not None and avail.ok)
    mopts = [(fl, fd.format(groups=group_text), "recommended", "recommended")]
    if s_ok:
        mopts.append((al.format(model=mlabel.get(strongest) or strongest), ad.format(dots=sdots), strongest, "all"))
    mopts.append((ml, md, "pick", "pick"))
    s_note = "" if s_ok else _codex_note(t, avail)
    order = sorted(PRESETS, key=lambda name: name != rec)
    setup = [
        _entry(t["setup_q"], t["setup_h"], False,
               [_preset_option(t, name, by, mlabel, rec) for name in order]
               + [(pl, pd.format(extras="·".join(k["label"] for k in extras)), "pick", "pick")]),
        _entry(t["setup_models_q"] + (f" {s_note}" if s_note else ""), t["setup_models_h"], False, mopts)]
    nl, nd = t["none"]
    pick = [
        _entry(t["pick_std_q"], t["pick_std_h"], True,
               [(by[n]["label"] + t["rec"], by[n]["desc"], n, n) for n in chosen]),
        _entry(t["pick_extra_q"], t["pick_extra_h"], True,
               [(k["label"], k["desc"], k["key"], k["key"]) for k in extras[:3]] + [(nl, nd, None, "none")])]
    return setup, pick


def _where_asks(t, here) -> list:
    """`ask.where` of a folder without a git repository: where the project is, then (for a new one) GitHub."""
    (nl, nd), (ol, od) = t["where_new"], t["where_other"]
    where = _entry(t["where_q"], t["where_h"], False, [
        (nl.format(name=os.path.basename(os.path.realpath(here))) + t["rec"], nd, {"new": True}, "new"),
        (ol, od, {"path": "*"}, "path")])
    gh = [(lab + (t["rec"] if vis == "private" else ""), desc, vis, vis) for vis, (lab, desc) in t["github"].items()]
    return [where, _entry(t["github_q"], t["github_h"], False, gh)]


def build(reg, cfg, key, avail=None, machine=None, here=None) -> dict:
    """The choices JSON (Appendix D.1) of project `key` (None: a project not under ORCHE yet);
    `machine` {ram_mb, free_disk_mb} defaults to machine_info() of the project's path; `here` (a project not
    under ORCHE yet): the folder the owner is in (no git repository there: `ask.where` first)."""
    t = TEXTS
    proj = ((reg or {}).get("projects") or {}).get(key) or {} if key else {}
    divs = proj.get("divisions") or {}
    stage = proj.get("stage") or "building"
    catalog = vendors.catalog_config(cfg)
    if avail is None and any(vendors.vendor_of(m) == vendors.CODEX for m in catalog):
        avail = vendors.available(cfg)
    models = _models(t, cfg, catalog, avail)
    mlabel = {m["model"]: m["label"] for m in models}
    from .. import detect
    top = detect.toplevel(here) if here and not key else None
    rec = detect.stage(top) if top else "build"
    kinds = _kinds(t, cfg, key, proj, rec)
    star = vendors.kind(cfg, "custom")
    custom = {"recommended": star["model"], "pane_effort": star["effort"], "why": star["why"],
              "rules": t["rules"]}

    ask = {"where": _where_asks(t, here) if here and not key and not top else None,
           "add": None, "change": None, "model": {}, "setup": None, "pick": None}
    if machine is None:
        machine = machine_info(proj.get("path"))
    cur_scale, ask["scale"] = _scale_entry(t, cfg, reg, machine)
    for k in kinds:
        if k["status"] in HIDDEN:
            ask["model"][k["key"]] = None
        elif k["exists"]:
            cur = k["model"] or k["recommended"]
            q = t["model_change"].format(label=k["label"], model=mlabel.get(cur) or cur)
            ask["model"][k["key"]] = _model_entry(t, cfg, models, k, q, avail, cur, _active(divs[k["key"]]))
        else:
            ask["model"][k["key"]] = _model_entry(t, cfg, models, k, t["model_new"].format(label=k["label"]), avail)
    star_kind = {"recommended": custom["recommended"], "why": custom["why"]}
    ask["model"]["*"] = _model_entry(t, cfg, models, star_kind, t["model_star"], avail)
    if key:
        ask["add"], ask["change"] = _project_asks(t, kinds, mlabel)
    else:
        ask["setup"], ask["pick"] = _setup_asks(t, kinds, models, mlabel, custom, avail, rec)
    return {"project": key, "stage": stage, "recommended_preset": None if key else rec, "kinds": kinds, "custom": custom, "models": models,
            "scale": {"current": cur_scale, "presets": list(scale.presets(cfg)), "machine": machine}, "ask": ask}
