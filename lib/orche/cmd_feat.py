"""`orche feat new|sleep|wake|retire <proj> <name>` — feature-agent lifecycle under the build division.

new:    session `feat-<proj>-<name>` free registry-wide (hr.session_owner; else exit 1) ->
        frozen? -> limits (hr/limits.py; the global cap freezes) -> scope overlap (warn, or exit 3
        with --strict) -> awake limit -> child worktree of the build worktree -> unit files,
        settings, persona -> pane -> registry entry + `create` event -> notify, hr-log,
        status line, Orca unread dot. A provisional `onboarding` entry is saved as soon as the
        worktree exists; re-running `feat new` resumes it without a second worktree.
sleep/wake: the `orche sleep|wake` flow (hr.sleep / hr.wake through cmd_sleep_wake.run).
retire: hr.retire (hr/retire.py).
"""
from . import cmd_sleep_wake, hr, registry, roster
from .cli import EXIT_NOT_FOUND, EXIT_OK, EXIT_USER, fail, out


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


def cmd_new(args):
    reg = registry.load()
    found, err = _project(reg, args.project)
    if found is None:
        return err
    key, _ = found
    scope = [s.strip() for s in args.scope.split(",") if s.strip()]
    if not scope:
        return fail(EXIT_USER, "--scope needs at least one glob")
    try:
        data, line = hr.create_feature(reg, key, args.name, scope=scope, reason=args.reason, by=args.by,
                                       batch=args.batch_id, strict=args.strict, dry_run=args.dry_run)
    except hr.Refused as exc:
        return fail(exc.code, exc.msg)
    out(args, data, line)
    return EXIT_OK


def _feature_unit(reg, key, name):
    unit = roster.Roster(reg).member(hr.session_name("feat", key, name))
    if unit is None or unit.kind != "feat":
        return None, fail(EXIT_NOT_FOUND, f"unknown feature {name} in {key}")
    return unit, None


def _lifecycle(args, verb):
    reg = registry.load()
    found, err = _project(reg, args.project)
    if found is None:
        return err
    return cmd_sleep_wake.run(args, verb, hr.session_name("feat", found[0], args.name), reg)


def cmd_sleep(args):
    return _lifecycle(args, hr.sleep)


def cmd_wake(args):
    return _lifecycle(args, hr.wake)


def cmd_retire(args):
    reg = registry.load()
    found, err = _project(reg, args.project)
    if found is None:
        return err
    unit, err = _feature_unit(reg, found[0], args.name)
    if unit is None:
        return err
    try:
        data, line = hr.retire(reg, unit, yes=args.yes, dry_run=args.dry_run)
    except hr.Refused as exc:
        return fail(exc.code, exc.msg)
    out(args, data, line)
    return EXIT_OK
