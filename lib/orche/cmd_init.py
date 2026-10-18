"""`orche init <orca-repo-name|path>` — register a project and onboard its divisions.

The divisions come from a setup (`setup_flow`): `--setup-file`, else `--divisions`/`--custom`/
`--stage`/`--aliases`, else — on a terminal — the interactive checklist, else the setup defaults
(every standard division but `ops`; a registered project keeps what it has). Re-running adds only
divisions that are new; nothing is ever removed. `--print-setup` prints the setup JSON and exits.

Order: resolve the repo (`orca repo list`, read-only even under --dry-run) -> setup -> project templates +
card symlink -> registry entry -> `.orche/project` marker (the project key, read by the status
line) -> one worktree/unit files/settings/persona per division (all
`dormant` unless `--wake`; `ops` always dormant and gated by stage) -> `main-<proj>` and
`liaison-<proj>` panes in the main checkout when missing -> the two weekly Orca automations
(printed as dry-run lines unless `--automations`). The main checkout's
`.claude/settings.local.json` is never written: Main and Liaison share that checkout.
"""
import json
import os
import sys
from pathlib import Path

from . import cmd_sleep_wake, config, orca, paths, registry, setup_flow, templates, units, util
from .hr import EXIT_BLOCKED, EXIT_NOT_FOUND, EXIT_OK, EXIT_USER, PLUGIN_ROOT, dry, fail, onboarding, out, \
    refresh_status_line, warn

EXIT_INTERRUPTED = 130   # a setup given wrongly exits EXIT_USER (1), like argparse usage errors here
SETUP_FLAGS = ("--divisions", "--custom", "--setup-file", "--stage", "--aliases", "--wake")


def register(subparsers):
    p = subparsers.add_parser("init", help="register an Orca repo as a project and onboard its divisions")
    p.add_argument("repo", help="registry project key or alias, or Orca repo display name, id, or path")
    p.add_argument("--stage", choices=registry.STAGES, default=None, help="default: building")
    p.add_argument("--aliases", default=None, help="comma-separated aliases")
    p.add_argument("--divisions", default=None,
                   help="comma-separated standard divisions (default: all but ops; a registered project "
                        "keeps its own)")
    p.add_argument("--custom", action="append", default=[], metavar="NAME=LABEL:CHARTER[:MODEL[:EFFORT]]",
                   help="add a custom division (repeatable; model default claude-opus-5-5, effort high)")
    p.add_argument("--setup-file", default=None, metavar="JSON",
                   help='{"divisions": [...], "custom": {name: {label, charter[, model, effort]}}, '
                        '"stage": ..., "aliases": [...]}')
    p.add_argument("--print-setup", action="store_true",
                   help="print the project's setup JSON (the defaults for an unregistered repo) and exit")
    p.add_argument("--wake", default="", help="comma-separated divisions to start now (default: none)")
    p.add_argument("--automations", action="store_true", help="create the Orca automations (default: print them)")
    p.set_defaults(func=cmd_init)


def _csv(text) -> list:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


class RepoMissing(Exception):
    """A registry project matched but its Orca repo is not in `orca repo list`."""


def _find_repo(needle, reg):
    """(Orca repo, registry key or None) for `needle`, else (None, None).

    A registry project key or alias wins (matched to its Orca repo by id or path, and the project
    keeps its registered key); then an Orca repo id, display name or path. A registry match whose
    Orca repo is not listed raises RepoMissing(key) instead of falling through to the Orca names.
    """
    result = orca.repo_list()
    repos = [r for r in (result.get("repos", []) if isinstance(result, dict) else result or [])
             if isinstance(r, dict)]
    found = registry.project(reg, needle)
    if found:
        key, proj = found
        proj_real = os.path.realpath(proj["path"]) if proj.get("path") else None
        for repo in repos:
            if (proj.get("repo_id") and repo.get("id") == proj["repo_id"]) or \
                    (proj_real and repo.get("path") and os.path.realpath(repo["path"]) == proj_real):
                return repo, key
        # Never fall through to an Orca name match on the same word: another repo could be
        # onboarded under this project's alias.
        raise RepoMissing(key)
    want = needle.strip().lower()
    real = os.path.realpath(os.path.expanduser(needle))
    for repo in repos:
        if want in (str(repo.get("id", "")).lower(), str(repo.get("displayName", "")).lower()) or \
                (repo.get("path") and os.path.realpath(repo["path"]) == real):
            return repo, None
    return None, None


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


def _is_tty() -> bool:
    return bool(sys.stdin) and sys.stdin.isatty()


def _interactive(args) -> bool:
    """The checklist runs on a terminal, without --json and without --divisions/--custom/--setup-file."""
    return args.divisions is None and not args.custom and not args.setup_file and not args.json and _is_tty()


def _new_count(setup, existing) -> int:
    have = (existing or {}).get("divisions", {})
    return sum(1 for n in setup_flow.names(setup) if n not in have)


def _summary(setup, existing, wake) -> str:
    return setup_flow.summary(setup, new=_new_count(setup, existing), registered=bool(existing), wake=wake)


def session_owner(reg, key, name):
    """The other project holding or reserving division `name`'s session `div-<key>-<name>`, else None."""
    return registry.session_owner(reg, key, f"div-{key}-{name}")


def _setup(args, key, existing, reg):
    """(setup, None) or (None, exit code). Interactive only on a terminal, without --json and
    without any of --divisions/--custom/--setup-file. A registered project keeps every division,
    custom definition, stage and aliases it has; init never removes or changes them."""
    base = setup_flow.of_project(existing) if existing else setup_flow.defaults()
    try:
        if args.setup_file:
            if args.divisions is not None or args.custom or args.stage or args.aliases is not None:
                return None, fail(EXIT_USER, "--setup-file holds the whole setup; drop --divisions, --custom, "
                                              "--stage and --aliases")
            setup = setup_flow.from_data(setup_flow.read_file(args.setup_file), base)
        else:
            setup = setup_flow.from_flags(args.divisions, args.custom, base)
            if args.stage:
                setup["stage"] = args.stage
            if args.aliases is not None:
                setup["aliases"] = [a.lower() for a in _csv(args.aliases)]
    except setup_flow.SetupError as exc:
        return None, fail(EXIT_USER, str(exc))
    if existing:
        for name, spec in (existing.get("custom_divisions") or {}).items():
            if setup["custom"].get(name) != spec:
                return None, fail(EXIT_USER, f"custom division {name} already exists with another definition; "
                                             "changing it is out of scope (edit the registry and its persona by hand)")
        dropped = [d for d in base["divisions"] if d not in setup["divisions"]]
        if dropped:
            warn(f"{','.join(dropped)} stay(s) onboarded: init never removes a division "
                 f"(sleep it: orche sleep div-{key}-<division>, or remove it by hand)")
        if setup["stage"] != existing["stage"]:
            warn(f"stage stays {existing['stage']} for registered project {key} "
                 f"(change it with: orche stage set {key} {setup['stage']} --yes)")
        if setup["aliases"] != base["aliases"]:
            warn(f"aliases of registered project {key} are not changed by init")
        setup = {**setup, "divisions": [d for d in registry.DIVISIONS
                                        if d in setup["divisions"] or d in base["divisions"]],
                 "stage": existing["stage"], "aliases": base["aliases"]}
    if _interactive(args):
        locked = setup_flow.names(base) if existing else []
        checklist = setup_flow.Checklist(key, setup, locked, sys.stdin, sys.stdout, wake=_csv(args.wake),
                                         session_owner=lambda name: session_owner(reg, key, name))
        try:
            setup = checklist.run(bool(existing), lambda s: _summary(s, existing, _csv(args.wake)))
        except setup_flow.Aborted:
            print("aborted; nothing written")
            return None, EXIT_OK
        except KeyboardInterrupt:
            print("\naborted; nothing written")
            return None, EXIT_INTERRUPTED
    return setup, None


def _print_setup(args, reg) -> int:
    """`--print-setup`: a registered project's setup, else (the repo must exist in Orca) the defaults."""
    found = registry.project(reg, args.repo)
    if found:
        setup = setup_flow.of_project(found[1])
    else:
        try:
            repo, _ = _find_repo(args.repo, reg)
        except orca.OrcaError as exc:
            return fail(EXIT_USER, f"orca repo list failed: {exc}")
        if repo is None:
            return fail(EXIT_NOT_FOUND, f"no registry project or Orca repo matches {args.repo!r} "
                                        "(see: orche projects, orca repo list)")
        setup = setup_flow.defaults()
    print(json.dumps(setup, indent=2, ensure_ascii=False))
    return EXIT_OK


def _validate(setup, wake_text, reg, key, stage, existing):
    """(divisions to onboard, of which to wake, skipped, skipped wakes) or (None, exit code).

    A division already onboarded (worktree, status not `onboarding`) is skipped; an
    `onboarding` one is resumed.
    """
    divisions, wake = setup_flow.names(setup), _csv(wake_text)
    bad = [d for d in wake if d not in registry.DIVISIONS and d not in setup["custom"]]
    if bad:
        return None, fail(EXIT_USER, f"unknown division(s): {','.join(bad)} (allowed: {','.join(registry.DIVISIONS)} "
                                      "or a custom division)")
    missing = [d for d in wake if d not in divisions]
    if missing:
        return None, fail(EXIT_USER, f"--wake {','.join(missing)} is not in --divisions")
    skipped = [d for d in divisions if (existing.get(d) or {}).get("worktree")
               and existing[d].get("status") != "onboarding"]
    divisions = [d for d in divisions if d not in skipped]
    for name in divisions:
        owner = session_owner(reg, key, name)
        if owner:
            return None, fail(EXIT_USER, f"session div-{key}-{name} is taken by project {owner}; "
                                         + ("pick another name" if name in setup["custom"]
                                            else f"leave {name} out, or rename one of the projects"))
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
    if args.print_setup:
        given = [f for f, v in zip(SETUP_FLAGS, (args.divisions is not None, args.custom, args.setup_file,
                                                 args.stage, args.aliases is not None, args.wake)) if v]
        if given:
            return fail(EXIT_USER, f"--print-setup only prints; drop {', '.join(given)}")
        return _print_setup(args, reg)
    try:
        repo, key = _find_repo(args.repo, reg)
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"orca repo list failed: {exc}")
    except RepoMissing as exc:
        return fail(EXIT_NOT_FOUND, f"{args.repo!r} is registered as {exc} but its Orca repo is not in "
                                    "`orca repo list`")
    if repo is None:
        return fail(EXIT_NOT_FOUND, f"no registry project or Orca repo matches {args.repo!r} "
                                    "(see: orche projects, orca repo list)")
    key = key or util.slugify(repo.get("displayName") or Path(repo["path"]).name)
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
    interactive = _interactive(args)
    setup, err = _setup(args, key, existing, reg)
    if setup is None:
        return err
    stage = setup["stage"]
    checked, err = _validate(setup, args.wake, reg, key, stage, existing["divisions"] if existing else {})
    if checked is None:
        return err
    divisions, wake, skipped, wake_skipped = checked
    custom = setup["custom"]
    summary = _summary(setup, existing, wake)   # before onboarding mutates `existing`
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
            # Only additions reach here (see _setup); an older entry without the field stays as it is.
            if (proj.get("custom_divisions") or {}) != custom:
                proj["custom_divisions"] = dict(custom)
                registry.save(reg)
    else:
        proj = {"repo_id": repo["id"], "path": root, "stage": stage, "aliases": setup["aliases"],
                "main_session": f"main-{key}", "liaison_session": f"liaison-{key}",
                "card": _tilde(link), "standing": ".orche/STANDING.md", "divisions": {},
                "custom_divisions": dict(custom)}
        if not dry_run:
            reg["projects"][key] = proj
            registry.save(reg)
            units.hr_log(f"init {key} (repo {repo['id']}) by {by}")
    try:
        write_project_marker(key, root, dry_run=dry_run)
        onboarding.exclude_settings(root, dry_run=dry_run)  # .orche/units/ never dirties the checkout
        for name in divisions:   # standard ones in catalogue order, then custom ones
            session = f"div-{key}-{name}"
            spec = custom.get(name)
            model, effort = (spec["model"], spec["effort"]) if spec else onboarding.DIVISION_MODELS[name]
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
                                       on_created=provisional, dry_run=dry_run,
                                       custom={"label": spec["label"], "charter": spec["charter"]} if spec else None)
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
    statuses = {n: ("active" if n in wake else "dormant") for n in divisions}
    data = {"project": key, "repo_id": repo["id"], "path": root, "stage": stage, "dry_run": dry_run,
            "divisions": statuses, "custom": [n for n in divisions if n in custom], "skipped": skipped,
            "wake_skipped": wake_skipped, "panes": panes, "automations": "created" if live else "printed",
            "card": proj["card"], "summary": summary}
    lines = [f"{'would init' if dry_run else 'initialized'} {key} ({root}) stage={stage}"]
    if not interactive:   # the checklist already showed it
        lines.append(summary)
    lines += [f"{n}: {s}" + (" (gated by stage)" if n == "ops" else " (custom)" if n in custom else "")
              for n, s in statuses.items()]
    lines += [f"{n}: skipped (already onboarded)" for n in skipped]
    lines += [f"skipped --wake {n} (already onboarded; use `orche wake`)" for n in wake_skipped]
    lines.append(f"panes: {', '.join(panes) if panes else 'main/liaison already open'}")
    lines.append(f"card: {proj['card']}")
    lines.append("automations: " + ("created" if live else "printed only (above); re-run with --automations"))
    if not dry_run:
        refresh_status_line(reg, key)
    out(args, data, lines)
    return EXIT_OK
