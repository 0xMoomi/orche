"""`orche nudge <session>` and `orche deliver <unit>` — typing into a pane (lib/orche/deliver.py,
design §4.4 and §4.5).

`orche nudge` types the inbox wake line. ORCHE starts it detached whenever a waking pointer is posted
(inbox.wake); run it by hand to wake a pane whose reports wait (`orche status` names it). Prints the
state: nudged, already-nudged, nothing-pending (exit 0); no-pane, busy, dialog, no-agent,
user-typing, refused, nudge-failed, locked (exit 1). An unsafe session name exits 2.

`orche deliver` delivers a dispatched unit. The Stop hook starts it detached for every pane unit;
run it by hand after a failed delivery (the urgent pointer and `orche status` name it). Prints the
state: runner-started, running, already-ran, picked-up, nudged, queued, user-typing (exit 0); no-pane,
needs-wake, wake-requested, dialog, no-agent, refused, nudge-failed, locked, runner-failed (exit 1);
no-brief, unknown-target, retired-target (exit 2).
"""
from . import cli


def register(subparsers):
    p = subparsers.add_parser("nudge", help="type the inbox wake line into <session>'s pane when it has unread "
                                            "reports and is idle; ORCHE starts this by itself")
    p.add_argument("session", help="the recipient session, e.g. main-<proj>")
    p.set_defaults(func=cmd_nudge)
    p = subparsers.add_parser("deliver", help="deliver a dispatched unit: start its run, or type its inbox line "
                                              "into the target pane; ORCHE starts this by itself")
    p.add_argument("unit", help="the unit id")
    p.set_defaults(func=cmd_deliver)


def cmd_nudge(args):
    from . import deliver, inbox
    try:
        inbox.box(args.session)
    except ValueError as exc:
        return cli.fail(cli.EXIT_NOT_FOUND, str(exc))
    if args.dry_run:
        cli.dry(f"would nudge {args.session}")
        return cli.EXIT_OK
    code, state = deliver.nudge(args.session)
    print(state)
    return code


def cmd_deliver(args):
    from . import brief, deliver, registry
    bad = brief.unit_id_error(args.unit)
    if bad:
        return cli.fail(cli.EXIT_NOT_FOUND, bad)
    if args.dry_run:
        cli.dry(f"would deliver {args.unit}")
        return cli.EXIT_OK
    code, state = deliver.deliver_unit(registry.load(), args.unit)
    if code == cli.EXIT_NOT_FOUND:
        return cli.fail(code, f"{state}: {args.unit}")
    print(state)
    return code
