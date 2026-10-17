"""`orche init <orca-repo-name|path>` — register a project and onboard its divisions.

Order: resolve the repo (`orca repo list`, read-only even under --dry-run) -> project templates +
card symlink -> registry entry -> `.orche/project` marker (the project key, read by the status
line) -> one worktree/unit files/settings/persona per division (all
`dormant` unless `--wake`; `ops` always dormant and gated by stage) -> `main-<proj>` and
`liaison-<proj>` panes in the main checkout when missing -> the two weekly Orca automations
(printed as dry-run lines unless `--automations`). The main checkout's
`.claude/settings.local.json` is never written: Main and Liaison share that checkout.
"""
import os
from pathlib import Path

from . import cmd_sleep_wake, config, orca, paths, registry, templates, units, util
from .hr import EXIT_BLOCKED, EXIT_NOT_FOUND, EXIT_OK, EXIT_USER, PLUGIN_ROOT, dry, fail, onboarding, out, \
    refresh_status_line


def register(subparsers):
    p = subparsers.add_parser("init", help="register an Orca repo as a project and onboard its divisions")
    p.add_argument("repo", help="Orca repo display name, id, or path")
    p.add_argument("--stage", choices=registry.STAGES, default="building")
    p.add_argument("--aliases", default="", help="comma-separated aliases")
    p.add_argument("--divisions", default=",".join(registry.DIVISIONS),
                   help="comma-separated divisions (default: all five)")
    p.add_argument("--wake", default="", help="comma-separated divisions to start now (default: none)")
    p.add_argument("--automations", action="store_true", help="create the Orca automations (default: print them)")
    p.set_defaults(func=cmd_init)


def _csv(text) -> list:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def _find_repo(needle):
    result = orca.repo_list()
    repos = result.get("repos", []) if isinstance(result, dict) else result or []
    want = needle.strip().lower()
    real = os.path.realpath(os.path.expanduser(needle))
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        if want in (str(repo.get("id", "")).lower(), str(repo.get("displayName", "")).lower()) or \
                (repo.get("path") and os.path.realpath(repo["path"]) == real):
            return repo
    return None


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def _link_card(key, repo_root, *, dry_run):
    link = paths.orche_home() / "cards" / f"{key}.md"
    target = Path(repo_root) / "PROJECT_CARD.md"
    if dry_run:
        dry(f"symlink {link} -> {target}")
    elif not link.exists() and not link.is_symlink():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    return link


def write_project_marker(key, repo_root, *, dry_run):
    """`<repo>/.orche/project` = the project key (one line): the status line's fast path walks up to it."""
    marker = paths.project_local(repo_root) / "project"
    if dry_run:
        dry(f"write {marker} ({key})")
        return marker
    try:
        current = marker.read_text(encoding="utf-8") if marker.is_file() else None
    except OSError:
        current = None
    if current != key + "\n":
        util.atomic_write(marker, key + "\n")
    return marker


def automation_argvs(key, proj) -> list:
    audit = ["--name", f"orche-hr-audit-{key}", "--trigger", "weekly", "--day", "1", "--time", "09:00",
             "--provider", "claude", "--workspace", f"path:{PLUGIN_ROOT}", "--prompt",
             "Run `orche hr audit --json` and act on its proposals; apply only sleep proposals with `--apply`."]
    card = ["--name", f"orche-card-refresh-{key}", "--trigger", "weekly", "--day", "1", "--time", "10:00",
            "--provider", "claude", "--workspace", f"path:{proj['path']}", "--prompt",
            f"As liaison-{key}: refresh PROJECT_CARD.md (purpose, stack, entry points, how to ask, last "
            "refreshed) from the repo, keeping it under 2K tokens. First compare HEAD with the base commit "
            "recorded under 'Last refreshed'; if nothing changed since, exit without editing. Never write "
            "dispatch blocks; tell Main anything else with `orche report info|external-request --summary ...`."]
    return [audit, card]


def _panes(key, proj, *, dry_run) -> list:
    """Create `main-<proj>` / `liaison-<proj>` panes in the main checkout when none is open."""
    claude = config.load().get("claude_bin", "claude")
    main, liaison = proj["main_session"], proj["liaison_session"]
    wanted = [(main, f"{cmd_sleep_wake.session_env(main)}{claude} --name {main}"),
              (liaison, f"{cmd_sleep_wake.session_env(liaison)}{claude} --agent liaison --name {liaison}")]
    wt = f"{proj['repo_id']}::{proj['path']}"
    made = []
    for title, command in wanted:
        if orca.find_terminal_by_title(title) is not None:
            continue
        onboarding.open_pane(wt, title, command, dry_run=dry_run)
        made.append(title)
    return made


def _validate(args, reg, key, stage, existing):
    """(divisions to onboard, of which to wake, skipped, skipped wakes) or (None, exit code).

    A division already onboarded (worktree, status not `onboarding`) is skipped; an
    `onboarding` one is resumed.
    """
    divisions, wake = _csv(args.divisions), _csv(args.wake)
    bad = [d for d in divisions + wake if d not in registry.DIVISIONS]
    if bad:
        return None, fail(EXIT_USER, f"unknown division(s): {','.join(bad)} (allowed: {','.join(registry.DIVISIONS)})")
    missing = [d for d in wake if d not in divisions]
    if missing:
        return None, fail(EXIT_USER, f"--wake {','.join(missing)} is not in --divisions")
    skipped = [d for d in divisions if (existing.get(d) or {}).get("worktree")
               and existing[d].get("status") != "onboarding"]
    divisions = [d for d in divisions if d not in skipped]
    wake_skipped = [d for d in wake if d in skipped]
    wake = [d for d in wake if d in divisions]
    if "ops" in wake and stage == "building":
        return None, fail(EXIT_BLOCKED, "ops is gated by stage: it stays dormant while stage is building "
                                        f"(run: orche stage set {key} public --yes)")
    awake, limit = len(registry.awake_sessions(reg)), reg["limits"]["max_awake_sessions"]
    if awake + len(wake) > limit:
        return None, fail(EXIT_BLOCKED, f"awake limit would be exceeded ({awake}+{len(wake)}/{limit}); "
                                        "sleep a unit first (orche sleep <unit>)")
    return (divisions, wake, skipped, wake_skipped), None


def _conflict(reg, key, repo, root):
    """Another key already registered for the same Orca repo id or main checkout path."""
    for other, proj in reg["projects"].items():
        if other != key and (proj.get("repo_id") == repo["id"]
                             or (proj.get("path") and os.path.realpath(proj["path"]) == root)):
            return other
    return None


def cmd_init(args):
    reg = registry.load()
    try:
        repo = _find_repo(args.repo)
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"orca repo list failed: {exc}")
    if repo is None:
        return fail(EXIT_NOT_FOUND, f"no Orca repo matches {args.repo!r} (see: orca repo list)")
    key = util.slugify(repo.get("displayName") or Path(repo["path"]).name)
    if not key:
        return fail(EXIT_USER, f"cannot derive a project key from {repo.get('displayName')!r}")
    root, dry_run = os.path.realpath(repo["path"]), args.dry_run
    other = _conflict(reg, key, repo, root)
    if other:
        return fail(EXIT_USER, f"repo {repo['id']} ({root}) is already registered as project {other}")
    existing = reg["projects"].get(key)
    if existing and (existing.get("repo_id") != repo["id"]
                     or os.path.realpath(existing.get("path") or "/nonexistent") != root):
        return fail(EXIT_USER, f"project key {key} is taken by repo {existing.get('repo_id')} "
                               f"({existing.get('path')}), not {repo['id']} ({root}); rename the Orca repo "
                               "or remove the stale registry entry")
    stage = existing["stage"] if existing else args.stage
    checked, err = _validate(args, reg, key, stage, existing["divisions"] if existing else {})
    if checked is None:
        return err
    divisions, wake, skipped, wake_skipped = checked
    if dry_run:
        dry(f"copy templates/project -> {root} (missing files only, *.tmpl skipped)")
    else:
        paths.ensure_dirs()
        templates.copy_project(root, PROJECT=key)
    link = _link_card(key, root, dry_run=dry_run)
    by = units.actor()
    if existing:
        proj = existing
        if not dry_run:
            units.hr_log(f"init {key} resumed by {by} (skipping {','.join(skipped) or 'nothing'})")
    else:
        proj = {"repo_id": repo["id"], "path": root, "stage": stage,
                "aliases": [a.lower() for a in _csv(args.aliases)],
                "main_session": f"main-{key}", "liaison_session": f"liaison-{key}",
                "card": _tilde(link), "standing": ".orche/STANDING.md", "divisions": {}}
        if not dry_run:
            reg["projects"][key] = proj
            registry.save(reg)
            units.hr_log(f"init {key} (repo {repo['id']}) by {by}")
    try:
        write_project_marker(key, root, dry_run=dry_run)
        onboarding.exclude_settings(root, dry_run=dry_run)  # .orche/units/ never dirties the checkout
        for name in (d for d in registry.DIVISIONS if d in divisions):
            session = f"div-{key}-{name}"
            model, effort = onboarding.DIVISION_MODELS[name]
            base = {"session": session, "model": model, "effort": effort}
            if name == "ops":
                base["gated_by_stage"] = True
            resume = proj["divisions"].get(name) if (proj["divisions"].get(name) or {}).get("status") \
                == "onboarding" else None

            def provisional(wt, name=name, base=base):
                proj["divisions"][name] = {"status": "onboarding", **wt, **base, "last_activity": util.now_iso()}
                registry.save(reg)

            entry = onboarding.onboard(project=key, repo_root=root, repo_id=repo["id"], kind="div", name=name,
                                       session=session, worktree_name=f"div-{name}", branch=f"div/{name}",
                                       model=model, effort=effort, wake=name in wake, existing=resume,
                                       on_created=provisional, dry_run=dry_run)
            if not dry_run:
                proj["divisions"][name] = {**entry, **base}
                registry.append_event(reg, type="create", project=key, unit=name, by=by)
                registry.save(reg)
                units.hr_log(f"create {session} ({entry['status']}) by {by}" + (" (resumed)" if resume else ""))
        onboarding.write_persona([root], "liaison", "liaison", project=key, name="liaison",
                                 session=proj["liaison_session"], model="claude-sonnet-5-5", effort="medium",
                                 main_root=root, dry_run=dry_run)
        panes = _panes(key, proj, dry_run=dry_run)
    except onboarding.UnsafeTarget as exc:
        return fail(EXIT_BLOCKED, f"init {key} stopped: {exc} (re-run orche init {key} to resume)")
    except (orca.OrcaError, OSError, util.JsonError) as exc:
        return fail(EXIT_USER, f"init {key} stopped: {exc} (re-run orche init {key} to resume)")
    autos = automation_argvs(key, proj)
    live = args.automations and not dry_run
    try:
        for argv in autos:
            orca.automations("create", *argv, dry_run=not live)
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"automation create failed: {exc}")
    statuses = {n: ("active" if n in wake else "dormant") for n in registry.DIVISIONS if n in divisions}
    data = {"project": key, "repo_id": repo["id"], "path": root, "stage": stage, "dry_run": dry_run,
            "divisions": statuses, "skipped": skipped, "wake_skipped": wake_skipped, "panes": panes,
            "automations": "created" if live else "printed", "card": proj["card"]}
    lines = [f"{'would init' if dry_run else 'initialized'} {key} ({root}) stage={stage}"]
    lines += [f"{n}: {s}" + (" (gated by stage)" if n == "ops" else "") for n, s in statuses.items()]
    lines += [f"{n}: skipped (already onboarded)" for n in skipped]
    lines += [f"skipped --wake {n} (already onboarded; use `orche wake`)" for n in wake_skipped]
    lines.append(f"panes: {', '.join(panes) if panes else 'main/liaison already open'}")
    lines.append(f"card: {proj['card']}")
    lines.append("automations: " + ("created" if live else "printed only (above); re-run with --automations"))
    if not dry_run:
        refresh_status_line(reg, key)
    out(args, data, lines)
    return EXIT_OK
