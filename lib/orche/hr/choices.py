"""`orche hr choices` (design 2026-10-23 §10.3, Appendix D): the division kinds and models of a
project, and the owner's questions built from them (`ask`), each one element of AskUserQuestion's
`questions` plus the value of every label. Read-only; the only subprocesses are `codex --version` and
the RAM probe of `ask.scale` (`procs.mem_total_mb`).

Guardrails (pinned by tests/test_choices.py): never an unavailable model as an option (it is named
in the question instead), the recommendation first and marked, a header of at most 12 characters,
2-4 options with unique labels free of ", ", and an entry with fewer than 2 options is None.
Question wording lives here (`TEXTS`, it is UI); descriptions live in config (`kinds`,
`models.catalog`: owner-editable data)."""
import os
import shutil

from .. import procs, registry, scale, vendors

DEFAULT_SET = ("rnd", "research", "build", "gtm")
LIGHT_SET = ("rnd", "build")
HIDDEN = ("retired", "onboarding")
SCALES = ("default", "20", "30")
TIGHT_RAM_MB, TIGHT_DISK_MB = 32 * 1024, 30 * 1024

TEXTS = {
    "ko": {
        "add_q": "어떤 부서를 추가할까요? (여러 개 골라도 돼요)", "add_h": "부서",
        "new_label": "새 이름으로 만들기", "new_desc": "원하는 부서 이름과 맡을 일을 적어 주세요",
        "change_q": "어느 부서의 모델을 바꿀까요?", "change_h": "부서",
        "change_more": " (그 밖: {labels} — 이름을 적어 주세요)",
        "now_rec": "지금 {model} (권장 모델)", "now_other": "지금 {model} · 권장 {rec}",
        "model_new": "{label} 부서는 어떤 모델로 돌릴까요?", "model_star": "새 부서는 어떤 모델로 돌릴까요?",
        "model_change": "{label} 부서를 어떤 모델로 바꿀까요? (지금: {model})", "model_h": "모델",
        "rec": " (권장)", "keep_label": "그대로 두기", "keep_desc": "지금 모델({model}) 유지",
        "cost": " · 비용 {dots}", "estimate": " (추정)",
        "codex_absent": "Codex는 설치되지 않아 못 골라요(설치: npm install -g @openai/codex 후 codex login)",
        "codex_broken": "Codex는 설치됐지만 실행되지 않아 못 골라요(고치기: npm install -g @openai/codex@latest)",
        "codex_active": "Codex는 이 부서 패널이 켜져 있어 지금은 못 골라요(패널을 재운 뒤 다시)",
        "setup_q": "이 프로젝트를 어떤 부서로 시작할까요?", "setup_h": "부서",
        "preset_default": ("기본 {n}개 (권장)", "{labels}: 시제품부터 출시까지"),
        "preset_light": ("가볍게 2개", "{labels}: 시제품과 개발만"),
        "preset_pick": ("직접 고르기", "{extras}까지 하나씩 고르기"),
        "setup_models_q": "부서별 모델은 어떻게 할까요?", "setup_models_h": "모델",
        "models_fitted": ("부서에 맞춰서 (권장)", "{groups}"),
        "models_all": ("모두 {model}", "가장 강한 모델로 통일 · 비용 {dots}"),
        "models_pick": ("부서마다 고르기", "다음 질문에서 부서별로 고르기"),
        "pick_std_q": "기본 부서 중 무엇을 둘까요? (여러 개 골라도 돼요)", "pick_std_h": "기본 부서",
        "pick_extra_q": "더 둘 부서가 있나요? (여러 개 골라도 돼요)", "pick_extra_h": "추가 부서",
        "none": ("없음", "기본 부서만"),
        "status": {"active": "켜짐", "dormant": "쉬는 중", "paused": "멈춤", "onboarding": "만드는 중",
                   "retired": "은퇴"},
        "scale_q": "에이전트를 몇 개까지 동시에 쓸까요?{machine}", "scale_h": "규모", "scale_now": " (지금)",
        "scale_machine": " (이 Mac: RAM {ram}GB · 디스크 여유 {disk}GB — 30개면 부족할 수 있어요)",
        "scale": {"default": ("기본", "동시 실행 4 · 기능 에이전트는 창과 함께 · 자율 지출 상한 $30"),
                  "20": ("20개까지", "동시 실행 12 · 기능 에이전트는 창 없이 · 자율 지출 상한 $150 (API 환산 시간당 약 $40)"),
                  "30": ("30개까지", "동시 실행 20 · 자율 지출 상한 $250 (시간당 약 $60) · Claude 구독은 사용량 한도에 걸려요: "
                                    "API 키나 Codex를 섞으세요")},
        "rules": "이름: 영문 소문자로 시작하는 2–16자(소문자·숫자·하이픈, 예: marketing) · 라벨: 24자 이하 · "
                 "맡을 일: 한 문단, 400자 이하",
    },
    "en": {
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
        "setup_q": "Which divisions should this project start with?", "setup_h": "Divisions",
        "preset_default": ("The default {n} (recommended)", "{labels}: prototype to launch"),
        "preset_light": ("Light: 2", "{labels}: prototypes and building only"),
        "preset_pick": ("Pick them", "one by one, {extras} included"),
        "setup_models_q": "Which models should the divisions use?", "setup_models_h": "Models",
        "models_fitted": ("Fitted to each (recommended)", "{groups}"),
        "models_all": ("All {model}", "the strongest for all · cost {dots}"),
        "models_pick": ("Per division", "one question per division next"),
        "pick_std_q": "Which standard divisions? (pick any)", "pick_std_h": "Standard",
        "pick_extra_q": "Any more? (pick any)", "pick_extra_h": "Extras",
        "none": ("None", "standard ones only"),
        "status": {"active": "active", "dormant": "dormant", "paused": "paused", "onboarding": "onboarding",
                   "retired": "retired"},
        "scale_q": "How many agents should run at once?{machine}", "scale_h": "Scale", "scale_now": " (now)",
        "scale_machine": " (this Mac: {ram} GB RAM · {disk} GB free disk — tight for 30)",
        "scale": {"default": ("Default", "4 runs at once · features keep a pane · spend cap $30"),
                  "20": ("Up to 20", "12 runs at once · features without panes · spend cap $150 "
                                     "(about $40/h API-equivalent)"),
                  "30": ("Up to 30", "20 runs at once · spend cap $250 (about $60/h) · a Claude subscription hits "
                                     "its usage window: mix in an API key or Codex")},
        "rules": "name: 2–16 characters, a-z first, then a-z, 0-9 or single hyphens (e.g. marketing) · "
                 "label: ≤ 24 characters · charter: one paragraph, ≤ 400 characters",
    },
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


def _active(div, key):
    if div.get("status") == "active":
        return True
    return key == "build" and any(f.get("status") == "active" for f in div.get("features") or [])


def _model_entry(t, cfg, lang, models, kind, question, current=None, active=False):
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
        cur = vendors.catalog(cfg, current, lang)["label"]
        keep_rec = current == rec
        keep = (t["keep_label"] + (t["rec"] if keep_rec else ""),
                t["keep_desc"].format(model=cur) + (f" · {kind['why']}" if keep_rec and kind["why"] else ""),
                None, "keep")
        opts = [keep] + opts[:3] if keep_rec else opts[:3] + [keep]
    note = _codex_note(t, kind.get("_avail"))
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
            opts.append((lab + (t["scale_now"] if name == cur else ""), desc, name, name))
    return cur, _entry(t["scale_q"].format(machine=note), t["scale_h"], False, opts)


def build(reg, cfg, key, lang, avail=None, machine=None) -> dict:
    """The choices JSON (Appendix D.1) of project `key` (None: a project not under ORCHE yet);
    `machine` {ram_mb, free_disk_mb} defaults to machine_info() of the project's path."""
    t = TEXTS.get(lang) or TEXTS["en"]
    proj = ((reg or {}).get("projects") or {}).get(key) or {} if key else {}
    divs = proj.get("divisions") or {}
    customs = proj.get("custom_divisions") or {}
    stage = proj.get("stage") or "building"
    catalog = vendors._catalog(cfg)
    if avail is None and any(vendors.vendor_of(m) == vendors.CODEX for m in catalog):
        avail = vendors.available(cfg)

    models = []
    for m in catalog:
        row = vendors.catalog(cfg, m, lang)
        codex = vendors.vendor_of(m) == vendors.CODEX
        ok = not codex or (avail is not None and avail.ok)
        models.append({"model": m, "vendor": vendors.vendor_of(m), "label": row["label"], "desc": row["desc"],
                       "cost": row["dots"] + (t["estimate"] if codex and row["dots"] else ""),
                       "available": ok, "reason": None if ok else _codex_note(t, avail),
                       "headless_only": codex})
    mlabel = {m["model"]: m["label"] for m in models}

    kinds = []
    for name in _kind_order(cfg, proj):
        info = vendors.kind(cfg, name, lang)
        custom = customs.get(name) or {}
        div = divs.get(name)
        status = (div or {}).get("status")
        exists = div is not None and status != "retired"
        if key:
            suggested = not exists and status != "retired" and (
                name in DEFAULT_SET or (name == "ops" and stage == "public"))
        else:
            suggested = name in DEFAULT_SET
        kinds.append({"key": name, "standard": name in registry.DIVISIONS,
                      "label": custom.get("label") or (info["label"] if info["listed"] else name),
                      "desc": info["desc"], "charter": custom.get("charter") or info["charter"] or None,
                      "exists": exists, "status": status,
                      "status_text": t["status"].get(status) if status else None,
                      "session": (div or {}).get("session"), "model": (div or {}).get("model") if div else None,
                      "recommended": info["model"], "pane_effort": info["effort"], "why": info["why"],
                      "suggested": suggested})
    star = vendors.kind(cfg, "custom", lang)
    custom = {"recommended": star["model"], "pane_effort": star["effort"], "why": star["why"],
              "rules": t["rules"]}

    ask = {"add": None, "change": None, "model": {}, "setup": None, "pick": None}
    if machine is None:
        machine = machine_info(proj.get("path"))
    cur_scale, ask["scale"] = _scale_entry(t, cfg, reg, machine)
    for k in kinds:
        k["_avail"] = avail
        if k["status"] in HIDDEN:
            ask["model"][k["key"]] = None
        elif k["exists"]:
            cur = k["model"] or k["recommended"]
            q = t["model_change"].format(label=k["label"], model=mlabel.get(cur) or cur)
            ask["model"][k["key"]] = _model_entry(t, cfg, lang, models, k, q, cur, _active(divs[k["key"]], k["key"]))
        else:
            ask["model"][k["key"]] = _model_entry(t, cfg, lang, models, k, t["model_new"].format(label=k["label"]))
    star_kind = {"recommended": custom["recommended"], "why": custom["why"], "_avail": avail}
    ask["model"]["*"] = _model_entry(t, cfg, lang, models, star_kind, t["model_star"])

    if key:
        addable = [k for k in kinds if k["status"] is None]
        addable.sort(key=lambda k: not k["suggested"])
        opts = [(k["label"], k["desc"], k["key"], k["key"]) for k in addable[:3]]
        opts.append((t["new_label"], t["new_desc"], "*", "*"))
        ask["add"] = _entry(t["add_q"], t["add_h"], True, opts)
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
        ask["change"] = _entry(q, t["change_h"], False, opts)
    else:
        by = {k["key"]: k for k in kinds}
        dlabels = [by[n]["label"] for n in DEFAULT_SET if n in by]
        extras = [k for k in kinds if k["key"] not in DEFAULT_SET]
        groups = {}
        for n in DEFAULT_SET:
            if n in by:
                groups.setdefault(by[n]["recommended"], []).append(by[n]["label"])
        group_text = ", ".join(f"{'·'.join(labs)} {mlabel.get(m) or m}" for m, labs in groups.items())
        strongest = custom["recommended"]
        sdots = next((m["cost"] for m in models if m["model"] == strongest), "")
        (dl, dd), (ll, ld), (pl, pd) = t["preset_default"], t["preset_light"], t["preset_pick"]
        (fl, fd), (al, ad), (ml, md) = t["models_fitted"], t["models_all"], t["models_pick"]
        # "All <strongest>" only when that model runs here (a Codex recommendation needs Codex)
        s_ok = vendors.vendor_of(strongest) != vendors.CODEX or (avail is not None and avail.ok)
        mopts = [(fl, fd.format(groups=group_text), "recommended", "recommended")]
        if s_ok:
            mopts.append((al.format(model=mlabel.get(strongest) or strongest), ad.format(dots=sdots), strongest, "all"))
        mopts.append((ml, md, "pick", "pick"))
        s_note = "" if s_ok else _codex_note(t, avail)
        ask["setup"] = [
            _entry(t["setup_q"], t["setup_h"], False, [
                (dl.format(n=len(DEFAULT_SET)), dd.format(labels="·".join(dlabels)), list(DEFAULT_SET), "default"),
                (ll, ld.format(labels="·".join(by[n]["label"] for n in LIGHT_SET)), list(LIGHT_SET), "light"),
                (pl, pd.format(extras="·".join(k["label"] for k in extras)), "pick", "pick")]),
            _entry(t["setup_models_q"] + (f" {s_note}" if s_note else ""), t["setup_models_h"], False, mopts)]
        nl, nd = t["none"]
        ask["pick"] = [
            _entry(t["pick_std_q"], t["pick_std_h"], True,
                   [(by[n]["label"] + t["rec"], by[n]["desc"], n, n) for n in DEFAULT_SET if n in by]),
            _entry(t["pick_extra_q"], t["pick_extra_h"], True,
                   [(k["label"], k["desc"], k["key"], k["key"]) for k in extras[:3]] + [(nl, nd, None, "none")])]
    for k in kinds:
        k.pop("_avail", None)
    return {"project": key, "lang": lang, "stage": stage, "kinds": kinds, "custom": custom, "models": models,
            "scale": {"current": cur_scale, "presets": list(scale.presets(cfg)), "machine": machine}, "ask": ask}
