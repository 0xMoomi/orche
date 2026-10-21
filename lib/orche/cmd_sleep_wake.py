"""`orche sleep <unit>` / `orche wake <unit>` for division and feature-agent sessions.

sleep: `hr.sleep` — HANDOFF.md must be fresh (< 30 min) -> close the pane -> status `paused` -> event.
wake:  `hr.wake` — respect the stage gate and `limits.max_awake_sessions` -> open a pane resuming
       the session (`hr/panes.py`; a never-started dormant unit starts fresh) -> status `active` -> event.
       An `active` unit with no pane titled with its session gets its pane reopened;
       with a pane it stays "already active". Both honor --dry-run (orca argv printed, nothing mutated).
       `main-<proj>` opens or switches to the project pane (`hr.project_pane`).
"""
from . import cli, hr, orca, registry, roster


def register(subparsers):
    for name, func, text in (("sleep", cmd_sleep, "close a unit's pane and mark it paused"),
                             ("wake", cmd_wake, "resume a unit's session in a new pane")):
        p = subparsers.add_parser(name, help=text)
        p.add_argument("unit", help="div-<proj>-<division> or feat-<proj>-<feature> "
                                    "(wake also takes main-<proj>)")
        p.set_defaults(func=func)


def run(args, verb, name, reg) -> int:
    """CLI glue of `orche sleep|wake` and `orche feat sleep|wake`: resolve, run, print."""
    m = roster.Roster(reg).member(name)
    if m is None or not m.owns_worktree:
        return cli.fail(cli.EXIT_NOT_FOUND, f"unknown unit: {name} (expected div-<proj>-<division>, "
                                            "feat-<proj>-<feature> or main-<proj>)")
    try:
        data, line = verb(reg, m, dry_run=args.dry_run)
    except hr.Refused as exc:
        return cli.fail(exc.code, exc.msg)
    cli.out(args, data, line)
    return cli.EXIT_OK


def cmd_sleep(args):
    reg = registry.load()
    m = roster.Roster(reg).member(args.unit)
    if m is not None and not m.owns_worktree:
        return cli.fail(cli.EXIT_USER, f"{m.session} is a project pane: close it in Orca")
    return run(args, hr.sleep, args.unit, reg)


def cmd_wake(args):
    reg = registry.load()
    m = roster.Roster(reg).member(args.unit)
    if m is not None and not m.owns_worktree:   # Main: no awake limit, stage gate or onboarding check
        try:
            data, line = hr.project_pane(reg["projects"][m.project], dry_run=args.dry_run)
        except orca.OrcaError as exc:
            return cli.fail(cli.EXIT_USER, f"could not open or switch to {m.session}: {exc}")
        cli.out(args, data, line)
        return cli.EXIT_OK
    return run(args, hr.wake, args.unit, reg)
