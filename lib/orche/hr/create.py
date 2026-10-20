"""Creating units: divisions at `orche init`, feature agents at `orche feat new` (SPEC §2.5, §14.12)."""
from typing import NamedTuple

from .. import cli, orca, registry, roster, setup_flow, util
from . import book, limits, onboarding, panes


class Plan(NamedTuple):
    divisions: list      # to onboard: new ones and resumed `onboarding` ones, catalogue order then custom
    wake: list           # of those, the ones to wake
    skipped: list        # already onboarded (worktree recorded, status not `onboarding`)
    wake_skipped: list   # --wake names among `skipped`


def plan_divisions(reg, key, setup, wake) -> Plan:
    """Which of `setup`'s divisions `orche init` onboards and wakes. `wake` is already checked against
    the setup by the CLI. Raises Refused: a session another project holds or reserves (1), a GATED
    division woken while setup["stage"] is building (3), the awake limit (3).

    A division already onboarded (worktree, status not `onboarding`) is skipped; an
    `onboarding` one is resumed."""
    existing = (reg["projects"].get(key) or {}).get("divisions", {})
    names = setup_flow.names(setup)
    skipped = [d for d in names if (existing.get(d) or {}).get("worktree")
               and existing[d].get("status") != "onboarding"]
    divisions = [d for d in names if d not in skipped]
    for name in divisions:
        session = onboarding.session_name("div", key, name)
        owner = onboarding.session_owner(reg, key, session)
        if owner:
            raise book.Refused(cli.EXIT_USER, f"session {session} is taken by project {owner}; "
                               + ("pick another name" if name in setup["custom"]
                                  else f"leave {name} out, or rename one of the projects"))
    wake_skipped = [d for d in wake if d in skipped]
    wake = [d for d in wake if d in divisions]
    limits.stage_gate(wake, setup["stage"], key)
    limits.need_awake(reg, len(wake), plan=True)
    return Plan(divisions, wake, skipped, wake_skipped)


def onboard_project(reg, key, setup, plan, *, repo_id, root, card, dry_run=False) -> tuple:
    """Register project `key` (or resume it) and onboard `plan.divisions`. Returns (project entry,
    titles of the Main/Liaison panes opened). Raises Refused "init <key> stopped: <why> (re-run orche
    init <key> to resume)" — 3 for an unsafe target, 1 for an Orca, file or JSON error.
    Order: the registry entry and "init <key> (repo <id>)" record (a registered project: the
    "init <key> resumed …" record, saved only when this setup adds custom definitions) ->
    `.orche/project` marker -> info/exclude -> per division: worktree, a provisional `onboarding`
    entry (saved at once), unit files, settings, persona, pane when woken, its `create` record ->
    the liaison persona -> Main and Liaison panes (opened, never switched: init switches to Main
    once, at the very end)."""
    by, custom, proj = roster.actor(), setup["custom"], reg["projects"].get(key)
    if proj is not None:
        if not dry_run:
            # Only additions reach here (see cmd_init._setup); an older entry without the field stays as it is.
            changed = (proj.get("custom_divisions") or {}) != custom
            if changed:
                proj["custom_divisions"] = dict(custom)
            book.record(reg, f"init {key} resumed by {by} (skipping {','.join(plan.skipped) or 'nothing'})",
                        project=key, save=changed)
    else:
        proj = {"repo_id": repo_id, "path": root, "stage": setup["stage"], "aliases": setup["aliases"],
                "main_session": onboarding.session_name("main", key),
                "liaison_session": onboarding.session_name("liaison", key),
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
                              spec=custom.get(name), by=by, dry_run=dry_run)
        onboarding.write_persona([root], "liaison", "liaison", project=key, name="liaison",
                                 session=proj["liaison_session"], model="claude-sonnet-5-5", effort="medium",
                                 main_root=root, dry_run=dry_run)
        opened = []
        for kind in panes.PANE_KINDS:
            data, _ = panes.project_pane(proj, kind, dry_run=dry_run, switch=False)
            if data["pane"] == "opened":
                opened.append(data["session"])
    except onboarding.UnsafeTarget as exc:
        raise book.Refused(cli.EXIT_BLOCKED, f"init {key} stopped: {exc} (re-run orche init {key} to resume)") from None
    except (orca.OrcaError, OSError, util.JsonError) as exc:
        raise book.Refused(cli.EXIT_USER, f"init {key} stopped: {exc} (re-run orche init {key} to resume)") from None
    return proj, opened


def _onboard_division(reg, key, proj, name, *, wake, root, repo_id, spec, by, dry_run) -> None:
    """One division of `onboard_project`: worktree, provisional entry, files, pane, `create` record."""
    session = onboarding.session_name("div", key, name)
    model, effort = (spec["model"], spec["effort"]) if spec else onboarding.DIVISION_MODELS[name]
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
        book.record(reg, f"create {session} ({entry['status']}) by {by}" + (" (resumed)" if resume else ""),
                    event={"type": "create", "project": key, "unit": name, "by": by})


def create_feature(reg, key, name, *, scope, reason, by=None, batch=None, strict=False, dry_run=False) -> tuple:
    """`orche feat new` end to end (SPEC §2.5). `scope`: the globs, split and non-empty (the CLI checks).
    Order: build division (2) -> name is a slug (1) -> session free registry-wide (1) -> no live feature
    of that name (1) -> creation limits (3; the global cap also freezes creation, unless dry_run) ->
    scope overlaps (warning; 3 with `strict`) -> awake limit (3) -> parent-division warning -> child
    worktree of the build worktree, a provisional `onboarding` entry saved at once (a re-run resumes it),
    unit files, settings, persona, pane -> the `create` record and notification -> Orca unread dot
    (a failure is a warning). Returns (data, line)."""
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
    limits.need_awake(reg)
    if build.get("status") != "active":
        cli.warn(f"parent division build is {build.get('status')}")
    model, effort = onboarding.FEATURE_MODEL
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
                                   name=name, model=model, effort=effort, wake=True, parent=build["worktree"],
                                   existing=resume, on_created=provisional, dry_run=dry_run)
        if dry_run:
            orca.worktree("set", "--worktree", f"id:{entry['worktree']}", "--unread", dry_run=True)
            return ({"project": key, "feature": name, "session": session, "dry_run": True},
                    f"would create {session} (dry-run)")
    except onboarding.UnsafeTarget as exc:
        raise book.Refused(cli.EXIT_BLOCKED, f"feat new {name} stopped: {exc}") from None
    except (orca.OrcaError, OSError, util.JsonError) as exc:
        raise book.Refused(cli.EXIT_USER, f"feat new {name} stopped: {exc}") from None
    feature.update(worktree=entry["worktree"], branch=entry["branch"], status="active")
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
    return {"project": key, "feature": feature}, f"{session} active ({entry['branch']})"
