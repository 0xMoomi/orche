"""`orche sleep <unit>` / `orche wake <unit>` for division and feature-agent sessions.

sleep: `hr.sleep` — HANDOFF.md must be fresh (< 30 min) -> close the pane -> status `paused` -> event.
wake:  `hr.wake` — respect the stage gate and `limits.max_awake_sessions` -> open a pane resuming
       the session (`hr/panes.py`; a never-started dormant unit starts fresh) -> status `active` -> event.
       An `active` unit with no pane titled with its session gets its pane reopened;
       with a pane it stays "already active". Both honor --dry-run (orca argv printed, nothing mutated).
       `main-<proj>` opens or switches to the project pane (`hr.project_pane`).
`staff-<proj>` (that project's CEO Staff, lib/orche/staff.py; from a Main, `staff` is its own project's):
wake records it awake and opens or switches to its pane; sleep records it asleep (nothing opens it
automatically again) and closes the pane.
"""
from . import cli, hr, orca, registry, roster, staff


def register(subparsers):
    for name, func, text in (("sleep", cmd_sleep, "close a unit's pane and mark it paused"),
                             ("wake", cmd_wake, "resume a unit's session in a new pane")):
        p = subparsers.add_parser(name, help=text)
        p.add_argument("unit", help="div-<proj>-<division>, feat-<proj>-<feature> or staff-<proj> "
                                    "(wake also takes main-<proj>)")
        p.set_defaults(func=func)


def _is_staff(unit) -> bool:
    return unit == "staff" or staff.is_staff(unit)


def _staff(args, verb) -> int:
    """`verb` (staff.wake or staff.sleep) on the project's Staff `args.unit` names: `staff-<proj>`, or
    `staff` from a Main (ORCHE_SESSION `main-<proj>`)."""
    reg = registry.load()
    session = args.unit
    if session == "staff":
        me = roster.Roster(reg).member(roster.env_session())
        if me is None or me.kind != "main":
            return cli.fail(cli.EXIT_USER, "name the project's Staff: staff-<project>")
        session = staff.session_of(me.project)
    key = staff.project_of(reg, session)
    if key is None:
        return cli.fail(cli.EXIT_NOT_FOUND, f"no project {session.removeprefix(staff.PREFIX)!r}: "
                                            "a Staff is staff-<project> of a registered project")
    try:
        data, line = verb(key, reg["projects"][key], dry_run=args.dry_run)
    except orca.OrcaError as exc:
        return cli.fail(cli.EXIT_USER, f"could not open or close the {session} pane: {exc}")
    except registry.Busy as exc:
        return cli.fail(cli.EXIT_BLOCKED, str(exc))
    cli.out(args, data, line)
    return cli.EXIT_OK


def cmd_sleep(args):
    if _is_staff(args.unit):
        return _staff(args, staff.sleep)
    try:
        with registry.lock():
            return _sleep(args)
    except registry.Busy as exc:
        return cli.fail(cli.EXIT_BLOCKED, str(exc))


def _sleep(args):
    reg = registry.load()
    m = roster.Roster(reg).member(args.unit)
    if m is not None and not m.owns_worktree:
        return cli.fail(cli.EXIT_USER, f"{m.session} is a project pane: close it in Orca")
    return hr.run_verb(args, hr.sleep, args.unit, reg)


def cmd_wake(args):
    if _is_staff(args.unit):
        return _staff(args, staff.wake)
    try:
        with registry.lock():
            return _wake(args)
    except registry.Busy as exc:
        return cli.fail(cli.EXIT_BLOCKED, str(exc))


def _wake(args):
    reg = registry.load()
    m = roster.Roster(reg).member(args.unit)
    if m is not None and not m.owns_worktree:   # Main: no awake limit, stage gate or onboarding check
        try:
            data, line = hr.project_pane(m.project, reg["projects"][m.project], dry_run=args.dry_run)
        except orca.OrcaError as exc:
            return cli.fail(cli.EXIT_USER, f"could not open or switch to {m.session}: {exc}")
        cli.out(args, data, [line] + ([data["staff"]] if data.get("staff") else []))
        return cli.EXIT_OK
    return hr.run_verb(args, hr.wake, args.unit, reg)
