"""`orche feat new|sleep|wake|retire <proj> <name>` — feature-agent lifecycle under the build division.

new:    frozen? -> limits (hr/limits.py; the global cap freezes) -> scope overlap (warn, or exit 3
        with --strict) -> awake limit -> child worktree of the build worktree -> unit files,
        settings, persona -> pane -> registry entry + `create` event -> notify, hr-log,
        status line, Orca unread dot. A provisional `onboarding` entry is saved as soon as the
        worktree exists; re-running `feat new` resumes it without a second worktree.
sleep/wake: the `orche sleep|wake` flow (cmd_sleep_wake).
retire: refuses an onboarding entry, a dirty worktree, a HANDOFF.md without `final:`, the main
        checkout, or an open pane titled with the session; lists git-ignored files the removal
        destroys; needs --yes. copy the unit memory files to the archive dir -> tag `archive/feat-<name>-<yyyymmdd>`
        -> `orca worktree rm` -> archive the unit's transcripts -> GRAVEYARD row -> status
        `retired` + event + hr-log.
"""
import shlex
import shutil
import sys

from . import cmd_hr, cmd_sleep_wake, janitor, orca, registry, shell, units, util
from .hr import EXIT_BLOCKED, EXIT_NOT_FOUND, EXIT_OK, EXIT_USER, dry, fail, limits, onboarding, out, \
    refresh_status_line, warn
from .janitor import archive, discover, graveyard, rules

FINAL_LINES = 20


def register(subparsers):
    feat = subparsers.add_parser("feat", help="create, sleep, wake or retire a feature agent")
    sub = feat.add_subparsers(dest="feat_command", metavar="<feat-command>", required=True)
    p = sub.add_parser("new", help="create a feature agent under the build division")
    p.add_argument("project")
    p.add_argument("name")
    p.add_argument("--scope", required=True, help="comma-separated globs the feature owns")
    p.add_argument("--reason", required=True)
    p.add_argument("--by", help="requesting session (default: $ORCHE_SESSION or user)")
    p.add_argument("--batch-id", help="groups automatic creations from one request")
    p.add_argument("--strict", action="store_true", help="refuse (exit 3) on scope overlap")
    p.set_defaults(func=cmd_new)
    for name, func, text in (("sleep", cmd_sleep, "pause a feature agent"),
                             ("wake", cmd_wake, "resume a feature agent"),
                             ("retire", cmd_retire, "archive and remove a finished feature agent")):
        p = sub.add_parser(name, help=text)
        p.add_argument("project")
        p.add_argument("name")
        if name == "retire":
            p.add_argument("--yes", action="store_true", help="confirm the real run")
        p.set_defaults(func=func)


def _project(reg, name):
    found = registry.project(reg, name)
    if found is None:
        return None, fail(EXIT_NOT_FOUND, f"unknown project: {name}")
    return found, None


def _session(key, name):
    return f"feat-{key}-{name}"


def _freeze(reg, by, reason):
    reg["frozen"] = True
    registry.append_event(reg, type="freeze", project="*", unit="hr", by="hr-limits")
    registry.save(reg)
    units.hr_log(f"freeze by hr-limits ({reason}; requested by {by})")
    cmd_hr.notify("HR freeze: global feature-agent creation cap reached")


def _check_new(args, reg, key, proj, by):
    """None when creation may proceed, else the exit code (message already printed)."""
    build = proj["divisions"].get("build")
    if build is None:
        return fail(EXIT_NOT_FOUND, f"{key} has no build division (run orche init first)")
    if util.slugify(args.name) != args.name:
        return fail(EXIT_USER, f"feature name must be lowercase [a-z0-9-]: {args.name!r}")
    if any(f["name"] == args.name and f["status"] not in ("retired", "onboarding")
           for f in build.get("features", [])):
        return fail(EXIT_USER, f"feature {args.name} already exists in {key}")
    verdict = limits.check_create(reg, key, batch=args.batch_id)
    if not verdict.ok:
        if verdict.freeze and not args.dry_run:
            _freeze(reg, by, verdict.reason)
        return fail(EXIT_BLOCKED, verdict.reason)
    scope = [s.strip() for s in args.scope.split(",") if s.strip()]
    for name, theirs, mine in limits.overlaps(scope, build.get("features", [])):
        if args.strict:
            return fail(EXIT_BLOCKED, f"scope {mine} overlaps feature {name} ({theirs})")
        warn(f"scope {mine} overlaps feature {name} ({theirs})")
    awake, cap = len(registry.awake_sessions(reg)), reg["limits"]["max_awake_sessions"]
    if awake + 1 > cap:
        return fail(EXIT_BLOCKED, f"awake limit reached ({awake}/{cap}); sleep a unit first (orche sleep <unit>)")
    if build.get("status") != "active":
        warn(f"parent division build is {build.get('status')}")
    return None


def cmd_new(args):
    reg = registry.load()
    found, err = _project(reg, args.project)
    if found is None:
        return err
    key, proj = found
    by = args.by or units.actor()
    if not [s for s in args.scope.split(",") if s.strip()]:
        return fail(EXIT_USER, "--scope needs at least one glob")
    code = _check_new(args, reg, key, proj, by)
    if code is not None:
        return code
    build, session = proj["divisions"]["build"], _session(key, args.name)
    model, effort = onboarding.FEATURE_MODEL
    features = build.setdefault("features", [])
    resume = next((f for f in features if f["name"] == args.name and f["status"] == "onboarding"), None)
    now = util.now_iso()
    feature = {"name": args.name, "scope": [s.strip() for s in args.scope.split(",") if s.strip()],
               "session": session, "created": resume["created"] if resume else now, "last_activity": now, "reason": args.reason}

    def provisional(wt):
        features.append({**feature, **wt, "status": "onboarding"})
        registry.save(reg)
    try:
        entry = onboarding.onboard(project=key, repo_root=proj["path"], repo_id=proj["repo_id"], kind="feat",
                                   name=args.name, session=session, worktree_name=f"feat-{args.name}",
                                   branch=f"feat/{args.name}", model=model, effort=effort, wake=True,
                                   parent=build["worktree"], existing=resume, on_created=provisional,
                                   dry_run=args.dry_run)
        if args.dry_run:
            orca.worktree("set", "--worktree", f"id:{entry['worktree']}", "--unread", dry_run=True)
            out(args, {"project": key, "feature": args.name, "session": session, "dry_run": True},
                f"would create {session} (dry-run)")
            return EXIT_OK
    except onboarding.UnsafeTarget as exc:
        return fail(EXIT_BLOCKED, f"feat new {args.name} stopped: {exc}")
    except (orca.OrcaError, OSError, util.JsonError) as exc:
        return fail(EXIT_USER, f"feat new {args.name} stopped: {exc}")
    feature.update(worktree=entry["worktree"], branch=entry["branch"], status="active")
    features[:] = [f for f in features if not (f["name"] == args.name and f["status"] == "onboarding")]
    features.append(feature)
    event = {"type": "create", "project": key, "unit": f"build/{args.name}", "by": by}
    if args.batch_id:
        event["batch"] = args.batch_id
    registry.append_event(reg, **event)
    registry.save(reg)
    units.hr_log(f"create {session} by {by}: {args.reason}")
    refresh_status_line(reg, key)
    cmd_hr.notify(f"New feature agent {session}: {args.reason}")
    try:
        orca.worktree("set", "--worktree", f"id:{entry['worktree']}", "--unread")
    except orca.OrcaError as exc:
        warn(f"could not mark the worktree unread: {exc}")
    out(args, {"project": key, "feature": feature}, f"{session} active ({entry['branch']})")
    return EXIT_OK


def _feature_unit(reg, key, name):
    unit = units.find_unit(reg, _session(key, name))
    if unit is None or unit.kind != "feat":
        return None, fail(EXIT_NOT_FOUND, f"unknown feature {name} in {key}")
    return unit, None


def _lifecycle(args, action):
    reg = registry.load()
    found, err = _project(reg, args.project)
    if found is None:
        return err
    return cmd_sleep_wake.run_unit(args, action, _session(found[0], args.name))


def cmd_sleep(args):
    return _lifecycle(args, cmd_sleep_wake.sleep_unit)


def cmd_wake(args):
    return _lifecycle(args, cmd_sleep_wake.wake_unit)


def has_final(handoff) -> bool:
    try:
        with open(handoff, encoding="utf-8") as fh:
            head = [next(fh, "") for _ in range(FINAL_LINES)]
    except OSError:
        return False
    return any(line.strip().lower().startswith("final:") for line in head)


def _onboarding_file(rel, unit) -> bool:
    """Files `feat new` writes into the worktree (ignored by the dirty check while untracked)."""
    unit_dir = f".orche/{unit.kind}/{unit.name}/"
    return rel.startswith(unit_dir) or rel in (f".claude/agents/{unit.session}.md", onboarding.EXCLUDE_LINE)


def _retire_problem(unit, proj):
    wt = str(unit.worktree_path)
    if not wt or units.is_within(proj["path"], wt) or discover.real(wt) == discover.real(proj["path"]):
        return f"{unit.session}'s worktree is the project's main checkout; refusing to remove it"
    if not has_final(unit.handoff):
        return f"HANDOFF.md has no 'final:' line in its first {FINAL_LINES} lines: {unit.handoff}"
    res = shell.run(["git", "-C", wt, "status", "--porcelain", "--untracked-files=all"], timeout=60)
    if not res.ok:
        return f"cannot read git status of {wt}"
    lines = [ln for ln in res.out.splitlines() if ln.strip()]
    dirty = [ln for ln in lines if not rules.is_record_file(discover.porcelain_path(ln))
             and not (ln.startswith("??") and _onboarding_file(discover.porcelain_path(ln), unit))]
    if dirty:
        return f"{wt} has {len(dirty)} uncommitted change(s); commit or discard them first"
    return None


def _transcripts(unit, proj):
    root = janitor.configured_dir(janitor.settings()["claude_projects"])
    wt, main = discover.real(str(unit.worktree_path)), discover.real(proj["path"])

    def owns(cwd):
        real = discover.real(cwd)
        return bool(real and wt and real != main and units.is_within(real, wt))
    files = discover.session_files(root, unit.session, owns)
    keep = {proj.get("main_session"), proj.get("liaison_session")}
    files = [f for f in files if not any(part.removesuffix(".jsonl") in keep for part in f.parts)]
    return root, files


def _pane_problem(unit, key, name):
    """Refusal text while a pane titled with the session is open (or Orca cannot be listed)."""
    try:
        term = orca.find_terminal_by_title(unit.session)
    except orca.OrcaError as exc:
        return f"cannot list Orca panes to check {unit.session} is closed: {exc}"
    if term is not None:
        return f"pane open — run `orche feat sleep {key} {name}` first"
    return None


def _ignored_files(wt) -> list:
    """Git-ignored files in the worktree (outside `.orche/`) that `orca worktree rm` destroys."""
    res = shell.run(["git", "-C", wt, "ls-files", "--others", "--ignored", "--exclude-standard"], timeout=60)
    if not res.ok:
        return []
    return [ln for ln in res.out.splitlines() if ln.strip() and not ln.startswith(".orche/")]


def cmd_retire(args):
    reg = registry.load()
    found, err = _project(reg, args.project)
    if found is None:
        return err
    key, proj = found
    unit, err = _feature_unit(reg, key, args.name)
    if unit is None:
        return err
    if unit.entry["status"] == "retired":
        out(args, {"feature": args.name, "status": "retired", "changed": False}, f"{unit.session} is already retired")
        return EXIT_OK
    if unit.entry["status"] == "onboarding":
        return fail(EXIT_BLOCKED, f"{unit.session} is still onboarding; finish it with "
                                  f"`orche feat new {key} {args.name}` (or remove the entry) before retiring")
    problem = _retire_problem(unit, proj)
    if problem:
        return fail(EXIT_BLOCKED, problem)
    if args.dry_run:
        dry(f"check no Orca pane is titled {unit.session}")
    pane = _pane_problem(unit, key, args.name)
    if pane:
        return fail(EXIT_BLOCKED, pane)
    if not args.yes and not args.dry_run:
        return fail(EXIT_USER, f"retiring {unit.session} removes its worktree; re-run with --yes")
    wt, branch = str(unit.worktree_path), unit.entry["branch"]
    tag = f"archive/feat-{args.name}-{units.now().strftime('%Y%m%d')}"
    tag_argv = ["git", "-C", wt, "tag", tag, branch]
    claude_root, files = _transcripts(unit, proj)
    unit_dir, kept = unit.handoff.parent, archive.unit_dir(unit.session) / "unit-files" / tag
    if kept.is_dir() and any(kept.iterdir()):
        return fail(EXIT_USER, f"{kept} already holds the unit files of an earlier retirement of {unit.session} "
                               f"(tag {tag}); refusing to overwrite it; retry tomorrow or move it aside")
    doomed = _ignored_files(wt)
    if doomed:
        line = (f"{len(doomed)} git-ignored file(s) will be destroyed by worktree rm: "
                + ", ".join(doomed[:10]) + (" …" if len(doomed) > 10 else ""))
        (dry if args.dry_run else warn)(line)
    if args.dry_run:
        dry(f"copy {unit_dir} -> {kept}")
        dry(shlex.join(tag_argv))
        orca.worktree("rm", "--worktree", f"id:{unit.entry['worktree']}", dry_run=True)
        dry(f"archive {len(files)} transcript file(s) of {unit.session}" if files
            else f"no transcripts found for {unit.session}")
        dry(f"append GRAVEYARD row in {graveyard.project_file(proj['path'])}")
        out(args, {"feature": args.name, "dry_run": True, "tag": tag, "transcripts": len(files)},
            f"would retire {unit.session} (dry-run)")
        return EXIT_OK
    try:
        shutil.copytree(unit_dir, kept, dirs_exist_ok=True)  # an empty leftover dir is reused
    except (OSError, shutil.Error) as exc:
        return fail(EXIT_USER, f"could not keep the unit files of {unit.session} in {kept}: {exc}; nothing removed")
    print(f"orche: unit files of {unit.session} kept in {kept}", file=sys.stderr)
    res = shell.run(tag_argv, timeout=30)
    if not res.ok:
        return fail(EXIT_USER, f"git tag {tag} failed: {res.err.strip()}")
    try:
        orca.worktree("rm", "--worktree", f"id:{unit.entry['worktree']}")
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"orca worktree rm failed: {exc} (tag {tag} kept)")
    by, now, archived = units.actor(), units.now(), None
    if files:
        def record(m):
            graveyard.record(proj["path"], f"archived {m['count']} session files of {unit.session}",
                             "feature retired", f"orche janitor restore {unit.session}", by, project=key,
                             date=now.date())
        try:
            archived = archive.archive_sessions(unit.session, files, claude_root, trigger="retired",
                                                reason="feature retired", project=key, now=now,
                                                record=record)["archive"]
        except (archive.ArchiveError, OSError) as exc:
            warn(f"transcript archive failed: {exc}; the janitor will retry")
    else:
        units.hr_log(f"retire {unit.session}: no transcripts to archive")
    try:
        graveyard.record(proj["path"], f"feature {args.name} (worktree {wt}, branch {branch})",
                         "retired: HANDOFF final", f"git checkout -b {branch} {tag}; unit files: {kept}", by,
                         project=key,
                         date=now.date())
    except OSError as exc:
        gy = graveyard.project_file(proj["path"])
        units.hr_log(f"retire {unit.session}: GRAVEYARD row failed ({exc}); add it by hand to {gy}")
        warn(f"could not write the GRAVEYARD row ({exc}); add by hand to {gy}: feature {args.name}, "
             f"recovery: git checkout -b {branch} {tag}; unit files: {kept}")
    unit.entry["status"] = "retired"
    registry.append_event(reg, type="retire", project=key, unit=unit.event_unit, by=by)
    registry.save(reg)
    units.hr_log(f"retire {unit.session} by {by} (tag {tag})")
    refresh_status_line(reg, key)
    cmd_hr.notify(f"Feature agent {unit.session} retired")
    out(args, {"feature": args.name, "status": "retired", "tag": tag, "archive": archived},
        f"{unit.session} retired (tag {tag})")
    return EXIT_OK
