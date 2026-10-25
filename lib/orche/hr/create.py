"""Creating units: divisions at `orche init`, feature agents at `orche feat new` (SPEC §2.5, §14.12)."""
import shutil
from typing import NamedTuple

from .. import cli, config, orca, registry, roster, scale, setup_flow, units, util, vendors
from . import book, limits, onboarding, panes


class Plan(NamedTuple):
    divisions: list      # to onboard: new ones and resumed `onboarding` ones, catalogue order then custom
    wake: list           # of those, the ones to wake
    skipped: list        # already onboarded (worktree recorded, status not `onboarding`)
    wake_skipped: list   # --wake names among `skipped`


def division_model(setup, name, models=None) -> tuple:
    """(model, pane effort) a new division `name` onboards with: `models[name]`, else its custom spec's
    model, else the recommendation's; the effort is the custom spec's, else the recommendation's."""
    spec, rec = setup["custom"].get(name), vendors.recommend(config.load(), name)
    model = (models or {}).get(name) or (spec["model"] if spec else rec.model)
    return model, spec["effort"] if spec else rec.effort


def _check_defaults(setup, divisions, models) -> None:
    """Refuse (1), before anything is written, a division that would onboard on a config `kinds`
    default that is not a model string (vendors.errors): `kinds.rnd.model: gpt-5` is named with the
    grammar. A model chosen in the setup or a custom spec is checked by setup_flow."""
    cfg = config.load()
    table = cfg.get("kinds") or {}
    for name in divisions:
        if (models or {}).get(name) or name in setup["custom"]:
            continue
        model = vendors.recommend(cfg, name).model
        errs = vendors.errors(model, cfg)
        if errs:
            row = name if isinstance(table.get(name), dict) and table[name].get("model") else "custom"
            raise book.Refused(cli.EXIT_USER, f"config kinds.{row}.model: {errs[0]}")


def plan_divisions(reg, key, setup, wake, models=None) -> Plan:
    """Which of `setup`'s divisions `orche init` onboards and wakes. `wake` is already checked against
    the setup by the CLI. Raises Refused: a config `kinds` default that is not a model string (1), a
    session another project holds or reserves (1), a Codex division woken (3: codex divisions run
    headless only), a GATED division woken while setup["stage"] is building (3), the awake limit (3).

    A division already onboarded (worktree, status not `onboarding`) is skipped; an
    `onboarding` one is resumed."""
    existing = (reg["projects"].get(key) or {}).get("divisions", {})
    names = setup_flow.names(setup)
    skipped = [d for d in names if (existing.get(d) or {}).get("worktree")
               and existing[d].get("status") != "onboarding"]
    divisions = [d for d in names if d not in skipped]
    _check_defaults(setup, divisions, models)
    for name in divisions:
        session = onboarding.session_name("div", key, name)
        owner = onboarding.session_owner(reg, key, session)
        if owner:
            raise book.Refused(cli.EXIT_USER, f"session {session} is taken by project {owner}; "
                               + ("pick another name" if name in setup["custom"]
                                  else f"leave {name} out, or rename one of the projects"))
    wake_skipped = [d for d in wake if d in skipped]
    wake = [d for d in wake if d in divisions]
    for name in wake:
        if vendors.vendor_of(division_model(setup, name, models)[0]) == vendors.CODEX:
            raise book.Refused(cli.EXIT_BLOCKED, f"{name}: codex divisions run headless only (v1); "
                               "briefs reach them through orche run")
    limits.stage_gate(wake, setup["stage"], key)
    limits.need_awake(reg, len(wake), plan=True)
    return Plan(divisions, wake, skipped, wake_skipped)


def onboard_project(reg, key, setup, plan, *, repo_id, root, card, dry_run=False, models=None,
                    resume_line=True) -> tuple:
    """Register project `key` (or resume it) and onboard `plan.divisions`. Returns (project entry,
    titles of the Main pane opened). Raises Refused "init <key> stopped: <why> (re-run orche
    init <key> to resume)" — 3 for an unsafe target, 1 for an Orca, file or JSON error.
    Order: the registry entry and "init <key> (repo <id>)" record (a registered project: the
    "init <key> resumed …" record, saved only when this setup adds custom definitions) ->
    `.orche/project` marker -> info/exclude -> per division: worktree, a provisional `onboarding`
    entry (saved at once), unit files, settings, persona, pane when woken, its `create` record ->
    the Main pane (opened, never switched: init switches to Main once, at the very end).
    `models` ({division: model string}) overrides division_model's default; `resume_line=False`
    leaves out the "init <key> resumed …" record (a caller adding one division writes its own)."""
    by, custom, proj = roster.actor(), setup["custom"], reg["projects"].get(key)
    if proj is not None:
        if not dry_run:
            # Only additions reach here (see cmd_init._setup); an older entry without the field stays as it is.
            changed = (proj.get("custom_divisions") or {}) != custom
            if changed:
                proj["custom_divisions"] = dict(custom)
            if resume_line:
                book.record(reg, f"init {key} resumed by {by} (skipping {','.join(plan.skipped) or 'nothing'})",
                            project=key, save=changed)
            elif changed:
                registry.save(reg)
    else:
        proj = {"repo_id": repo_id, "path": root, "stage": setup["stage"], "aliases": setup["aliases"],
                "main_session": onboarding.session_name("main", key),
                "card": card, "standing": ".orche/STANDING.md", "divisions": {},
                "custom_divisions": dict(custom)}
        if not dry_run:
            reg["projects"][key] = proj
            book.record(reg, f"init {key} (repo {repo_id}) by {by}", project=key)
    try:
        onboarding.write_project_marker(key, root, dry_run=dry_run)
        onboarding.exclude_settings(root, dry_run=dry_run)  # .orche/units/ never dirties the checkout
        for name in plan.divisions:   # standard ones in catalogue order, then custom ones
            _onboard_division(reg, key, proj, name, wake=name in plan.wake, root=root, repo_id=repo_id,
                              spec=custom.get(name), by=by, dry_run=dry_run,
                              model=division_model(setup, name, models))
        data, _ = panes.project_pane(proj, dry_run=dry_run, switch=False)
        opened = [data["session"]] if data["pane"] == "opened" else []
    except onboarding.UnsafeTarget as exc:
        raise book.Refused(cli.EXIT_BLOCKED, f"init {key} stopped: {exc} (re-run orche init {key} to resume)") from None
    except (orca.OrcaError, OSError, util.JsonError) as exc:
        raise book.Refused(cli.EXIT_USER, f"init {key} stopped: {exc} (re-run orche init {key} to resume)") from None
    return proj, opened


def _onboard_division(reg, key, proj, name, *, wake, root, repo_id, spec, by, dry_run, model) -> None:
    """One division of `onboard_project`: worktree, provisional entry, files, pane, `create` record.
    `model`: the (model, pane effort) of division_model."""
    session = onboarding.session_name("div", key, name)
    model, effort = model
    base = {"session": session, "model": model}
    if name in limits.GATED:
        base["gated_by_stage"] = True
    resume = proj["divisions"].get(name) if (proj["divisions"].get(name) or {}).get("status") \
        == "onboarding" else None

    def provisional(wt):
        proj["divisions"][name] = {"status": "onboarding", **wt, **base, "last_activity": util.now_iso()}
        registry.save(reg)

    entry = onboarding.onboard(project=key, repo_root=root, repo_id=repo_id, kind="div", name=name,
                               model=model, effort=effort, wake=wake, existing=resume,
                               on_created=provisional, dry_run=dry_run,
                               custom={"label": spec["label"], "charter": spec["charter"]} if spec else None)
    if not dry_run:
        proj["divisions"][name] = {**entry, **base}
        book.record(reg, f"create {session} ({entry['status']}) by {by} on {model}" + (" (resumed)" if resume else ""),
                    event={"type": "create", "project": key, "unit": name, "by": by})


def add_division(reg, key, name, *, model=None, label=None, charter=None, wake=False, dry_run=False) -> tuple:
    """Add one division to registered project `key` (`orche hr add`, design 2026-10-23 §3.1).
    Returns (data, lines): the `recommended for …` / `choices: …` / codex lines (printed under a dry
    run too), then `created <session> (<status>) on <model>`. Raises Refused: unknown project (2);
    a bad or taken name, label/charter missing for a custom name or given for a standard one, a
    grammar error (1); Codex not running, or woken (3)."""
    proj = (reg.get("projects") or {}).get(key)
    if proj is None:
        raise book.Refused(cli.EXIT_NOT_FOUND, f"no project {key}")
    standard = name in registry.DIVISIONS
    if not standard:
        errs = registry.custom_name_errors(name)
        if errs:
            raise book.Refused(cli.EXIT_USER, errs[0])
    if name in (proj.get("divisions") or {}) or name in (proj.get("custom_divisions") or {}):
        raise book.Refused(cli.EXIT_USER, f"{key} already has division {name}; change its model: "
                           f"orche hr model {key} {name} <model>")
    if standard and (label is not None or charter is not None):
        raise book.Refused(cli.EXIT_USER, f"{name} is a standard division: --label and --charter are for custom ones")
    cfg = config.load()
    known = vendors.kind(cfg, name, "en")
    if not standard and not (known["listed"] and known["charter"]) and (not label or not charter):
        raise book.Refused(cli.EXIT_USER, f"custom division {name} needs --label and --charter")
    rec = vendors.recommend(cfg, name)
    model = model or rec.model
    errs = vendors.errors(model, cfg)
    if errs:
        raise book.Refused(cli.EXIT_USER, f"model {errs[0]}")
    avail = vendors.available(cfg)
    lines = [f"recommended for {name}: {rec.model} — {rec.why}",
             "choices: " + " · ".join(vendors.choice_labels(cfg, avail))]
    if not avail.ok:
        lines.append(vendors.unavailable_line(avail))
    codex = vendors.vendor_of(model) == vendors.CODEX
    if codex and name == "build":
        lines.append("note: commits and pushes under the Codex sandbox depend on probe P1; a permission "
                     "error → set models.codex_sandbox to danger-full-access")
    if codex and not avail.ok:
        raise book.Refused(cli.EXIT_BLOCKED, vendors.refusal(avail))
    if codex and wake:
        raise book.Refused(cli.EXIT_BLOCKED, "codex divisions run headless only (v1); briefs reach them "
                           "through orche run")
    setup = setup_flow.of_project(proj)
    if standard:
        setup["divisions"].append(name)
    else:
        try:
            setup["custom"][name] = setup_flow.custom_spec(
                name, {"label": label, "charter": charter, "model": model})
        except setup_flow.SetupError as exc:
            raise book.Refused(cli.EXIT_USER, exc.errors[0]) from None
    models = {name: model}
    plan = plan_divisions(reg, key, setup, [name] if wake else [], models)
    session = onboarding.session_name("div", key, name)
    data = {"project": key, "division": name, "session": session, "status": "active" if wake else "dormant",
            "model": model, "vendor": vendors.vendor_of(model), "recommended": rec.model, "dry_run": dry_run}
    if dry_run:
        return data, lines + [f"would create {session} ({data['status']}) on {model} (dry-run)"]
    onboard_project(reg, key, setup, plan, repo_id=proj["repo_id"], root=proj["path"], card=proj.get("card"),
                    models=models, resume_line=False)
    data["status"] = proj["divisions"][name]["status"]
    return data, lines + [f"created {session} ({data['status']}) on {model}"]


def create_feature(reg, key, name, *, scope, reason, by=None, batch=None, strict=False, dry_run=False) -> tuple:
    """`orche feat new` end to end (SPEC §2.5). `scope`: the globs, split and non-empty (the CLI checks).
    Order: build division (2) -> name is a slug (1) -> session free registry-wide (1) -> no live feature
    of that name (1) -> creation limits (3; the global cap also freezes creation, unless dry_run) ->
    scope overlaps (warning; 3 with `strict`) -> free disk >= `feature_min_free_disk_mb` (3) -> awake limit
    (3; not for a headless feature: Codex, or `feature_panes` false) -> parent-division warning -> child
    worktree of the build worktree, a provisional `onboarding` entry saved at once (a re-run resumes it),
    unit files, settings, persona, pane -> the `create` record and notification -> Orca unread dot
    (a failure is a warning). A headless Claude feature is `active` with `pane: false` (no pane, no
    awake slot; `orche feat wake` opens one); a Codex feature is `paused`. Returns (data, line)."""
    proj = reg["projects"][key]
    by = by or roster.actor()
    session = onboarding.session_name("feat", key, name)
    build = proj["divisions"].get("build")
    if build is None:
        raise book.Refused(cli.EXIT_NOT_FOUND, f"{key} has no build division (run orche init first)")
    if util.slugify(name) != name:
        raise book.Refused(cli.EXIT_USER, f"feature name must be lowercase [a-z0-9-]: {name!r}")
    owner = onboarding.session_owner(reg, key, session)
    if owner:
        raise book.Refused(cli.EXIT_USER, f"session {session} is taken by project {owner}; pick another name")
    if any(f["name"] == name and f["status"] not in ("retired", "onboarding")
           for f in build.get("features", [])):
        raise book.Refused(cli.EXIT_USER, f"feature {name} already exists in {key}")
    verdict = limits.check_create(reg, key, batch=batch)
    if not verdict.ok:
        if verdict.freeze and not dry_run:
            limits.freeze(reg, by, verdict.reason)
        raise book.Refused(cli.EXIT_BLOCKED, verdict.reason)
    for other, theirs, mine in limits.overlaps(scope, build.get("features", [])):
        if strict:
            raise book.Refused(cli.EXIT_BLOCKED, f"scope {mine} overlaps feature {other} ({theirs})")
        cli.warn(f"scope {mine} overlaps feature {other} ({theirs})")
    cfg = config.load()
    _disk_guard(cfg, reg, proj["path"])
    model, effort = build.get("model") or vendors.recommend(cfg, "build").model, vendors.FEATURE_EFFORT
    codex = vendors.vendor_of(model) == vendors.CODEX   # headless only: no pane, no awake slot, `paused`
    headless = codex or cfg.get("feature_panes", True) is False
    if not headless:
        limits.need_awake(reg)
    if build.get("status") != "active":
        cli.warn(f"parent division build is {build.get('status')}")
    features = build.setdefault("features", [])
    resume = next((f for f in features if f["name"] == name and f["status"] == "onboarding"), None)
    now = util.now_iso()
    feature = {"name": name, "scope": list(scope), "session": session,
               "created": resume["created"] if resume else now, "last_activity": now, "reason": reason}

    def provisional(wt):
        features.append({**feature, **wt, "status": "onboarding"})
        registry.save(reg)
    try:
        entry = onboarding.onboard(project=key, repo_root=proj["path"], repo_id=proj["repo_id"], kind="feat",
                                   name=name, model=model, effort=effort, wake=not headless, parent=build["worktree"],
                                   existing=resume, on_created=provisional, dry_run=dry_run)
        if dry_run:
            orca.worktree("set", "--worktree", f"id:{entry['worktree']}", "--unread", dry_run=True)
            return ({"project": key, "feature": name, "session": session, "dry_run": True},
                    f"would create {session} (dry-run)")
    except onboarding.UnsafeTarget as exc:
        raise book.Refused(cli.EXIT_BLOCKED, f"feat new {name} stopped: {exc}") from None
    except (orca.OrcaError, OSError, util.JsonError) as exc:
        raise book.Refused(cli.EXIT_USER, f"feat new {name} stopped: {exc}") from None
    from .. import publish   # here, not at the top: hr stays free of the git/gh publish layer at import
    publish.guard_base(units.worktree_path(entry["worktree"]), publish.base(proj["path"]))   # R29
    status = "paused" if codex else "active"
    feature.update(worktree=entry["worktree"], branch=entry["branch"], status=status)
    if headless and not codex:
        feature["pane"] = False
    features[:] = [f for f in features if not (f["name"] == name and f["status"] == "onboarding")]
    features.append(feature)
    event = {"type": "create", "project": key, "unit": f"build/{name}", "by": by}
    if batch:
        event["batch"] = batch
    book.record(reg, f"create {session} by {by}: {reason}", event=event,
                note=f"New feature agent {session}: {reason}")
    try:
        orca.worktree("set", "--worktree", f"id:{entry['worktree']}", "--unread")
    except orca.OrcaError as exc:
        cli.warn(f"could not mark the worktree unread: {exc}")
    return {"project": key, "feature": feature}, f"{session} {status} ({entry['branch']})"


def _disk_guard(cfg, reg, path):
    """Refuse (3) a feature worktree when the project's disk has less than `feature_min_free_disk_mb`."""
    need = cfg.get("feature_min_free_disk_mb", config.DEFAULTS["feature_min_free_disk_mb"])
    if isinstance(need, bool) or not isinstance(need, (int, float)) or need <= 0:
        return
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return
    if free < need * 1024 * 1024:
        raise book.Refused(cli.EXIT_BLOCKED,
                           f"disk: {free / 1024 ** 3:.1f} GiB free < {need / 1024:g} GiB "
                           f"(scale {scale.current(cfg, reg)}); free space or pick a smaller scale")
