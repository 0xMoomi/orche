"""`orche reconcile` — one heartbeat pass (lib/orche/reconcile.py, design c10 §2.1).

The LaunchAgent `com.orche.reconcile` (installed by `orche install`) runs it every reconcile.every_s
seconds; a hook starts it when the heartbeat is stale. One bounded, idempotent pass per call: lost
runs are settled, unread reports are nudged and undelivered briefs re-delivered on a backoff
(10, 20, 40 min), then escalated once. `--dry-run` prints the plan and writes nothing; `--project P`
limits the pass to one project; `--json` prints one document (the actions taken or planned). Exit 0,
also when another pass holds the lock; 1 when the registry cannot be read; 2 for an unknown project.
"""
import sys

from . import cli


def register(subparsers):
    p = subparsers.add_parser("reconcile", help="one heartbeat pass: settle lost runs, re-nudge unread reports, "
                                                "re-deliver stuck briefs; the LaunchAgent runs it every 5 min")
    p.add_argument("--project", default=None, metavar="KEY", help="only this registered project (default all)")
    p.set_defaults(func=cmd_reconcile)


def cmd_reconcile(args):
    from . import reconcile
    keys = {args.project} if getattr(args, "project", None) else None
    try:
        return reconcile.run_pass(dry_run=bool(getattr(args, "dry_run", False)), keys=keys,
                                  as_json=bool(getattr(args, "json", False)))
    except Exception as exc:  # noqa: BLE001 - an internal error is exit 1, never a traceback for launchd
        print(f"orche: reconcile: {type(exc).__name__}: {exc}", file=sys.stderr)
        return cli.EXIT_USER
