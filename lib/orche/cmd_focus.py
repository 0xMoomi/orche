"""`orche focus <project|session>`: switch Orca to a pane; a click on an ORCHE notification runs it.

A project key or alias names its Main (`main-<key>`, resolved as `dispatch` resolves names); an exact
session name (`main-…`, `div-…`, `feat-…`) names that pane. A connected pane with that title gets
`orca terminal switch` (`switched to <title>`); with none, a Main is reopened with its conversation
(the `orche wake main-<p>` path) and any other session prints `no open pane: <title>` (exit 1).
--dry-run prints what it would do. An Orca error is one stderr line, exit 1.
"""
import argparse
import re

from . import cli, cmd_sleep_wake, orca, registry, roster

SESSION = re.compile(r"^(main|div|feat)-[a-z0-9][a-z0-9-]*$")


def register(subparsers):
    p = subparsers.add_parser("focus", help="switch Orca to a pane (a closed Main is reopened)")
    p.add_argument("target", help="a project key or alias (its Main) or a session name (main-, div-, feat-)")
    p.set_defaults(func=cmd_focus)


def cmd_focus(args):
    m = roster.Roster(registry.load()).resolve_to(args.target, None)
    title = m.session if m is not None else args.target if SESSION.match(args.target) else None
    if title is None:
        return cli.fail(cli.EXIT_NOT_FOUND, f"unknown project or session: {args.target}")
    try:
        term = orca.find_terminal_by_title(title)
    except orca.OrcaError as exc:
        return cli.fail(cli.EXIT_USER, f"could not list Orca panes: {exc}")
    if term is not None and not orca.disconnected(term):
        if args.dry_run:
            print(f"would switch to {title}")
            return cli.EXIT_OK
        try:
            orca.terminal("switch", "--terminal", term["handle"])
        except orca.OrcaError as exc:
            return cli.fail(cli.EXIT_USER, f"could not switch to {title}: {exc}")
        print(f"switched to {title}")
        return cli.EXIT_OK
    if m is not None and m.kind == "main":
        return cmd_sleep_wake.cmd_wake(argparse.Namespace(unit=title, dry_run=args.dry_run, json=False))
    print(f"no open pane: {title}")
    return cli.EXIT_USER
