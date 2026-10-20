"""`orche stage set <proj> public|building [--yes] [--no-wake]`.

public:   stage -> `stage` event -> wake `ops` (unless --no-wake; the awake limit applies) ->
          print the ops checklist -> hr-log + notify.
building: stage -> `stage` event -> sleep an active `ops` (R-STAGE-5; HANDOFF.md must be fresh —
          a refusal is reported and the stage change is kept). Ops cannot be woken while building.
The real run needs --yes (going public opens a pane).
"""
from . import cli, hr, registry


def register(subparsers):
    stage = subparsers.add_parser("stage", help="project stage (building | public)")
    sub = stage.add_subparsers(dest="stage_command", metavar="<stage-command>", required=True)
    p = sub.add_parser("set", help="set a project's stage")
    p.add_argument("project")
    p.add_argument("stage", choices=registry.STAGES)
    p.add_argument("--yes", action="store_true", help="confirm the real run")
    p.add_argument("--no-wake", action="store_true", help="do not wake ops when going public")
    p.set_defaults(func=cmd_set)


def cmd_set(args):
    reg = registry.load()
    found = registry.project(reg, args.project)
    if found is None:
        return cli.fail(cli.EXIT_NOT_FOUND, f"unknown project: {args.project}")
    key, _ = found
    if not args.yes and not args.dry_run:
        return cli.fail(cli.EXIT_USER, f"setting {key} to {args.stage} has side effects; re-run with --yes")
    cli.out(args, *hr.set_stage(reg, key, args.stage, wake_ops=not args.no_wake, dry_run=args.dry_run))
    return cli.EXIT_OK
