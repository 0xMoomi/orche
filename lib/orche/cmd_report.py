"""`orche report <status> --summary TEXT [--unit ID] [--to SESSION] [--report-path PATH] [--urgent]`.

A worker files its completion (or block, question, stop) as one inbox pointer:
- from: $ORCHE_SESSION, else the session owning the cwd;
- to: the project's Main; a feature session reports to its Build division (override with --to);
- unit: the newest `.orche/units/*/BRIEF.md` addressed (TO) to `from`; brief_rev from its `rev:` line;
  with no such brief, `external-request|question|info` use `adhoc-<from>-<yyyymmdd>` (others need one);
- head_sha: git HEAD of the reporter's worktree (Main: the cwd); ledger_row: whether the canonical
  ledger has a row for it;
- report_path: --report-path, else the unit's REPORT.md when it exists.
The recipient must be a session the registry knows; the summary is masked (mask.redact).
`done` from a ledger-gated member (the Build division, a feature agent) needs a canonical ledger row
for that HEAD with a verdict >= type-check-only (donegate.for_report; exit 3 otherwise).
Under the runner (ORCHE_RUNNER set by `orche run`) it refuses with exit 2: the runner files the pointer.
"""
import json
import os
import sys
from pathlib import Path

from . import brief, donegate, inbox, mask, registry, roster, units, util

EXIT_OK, EXIT_USER, EXIT_RUNNER, EXIT_BLOCKED = 0, 1, 2, 3
ADHOC_STATUSES = ("external-request", "question", "info")


def register(subparsers):
    p = subparsers.add_parser("report", help="file a completion/blocker as an inbox pointer (to Main or Build)")
    p.add_argument("status", help="|".join(inbox.STATUSES))
    p.add_argument("--summary", required=True, help="what happened, <= 5 lines")
    p.add_argument("--unit", help="unit id (default: the newest brief addressed to this session)")
    p.add_argument("--to", help="recipient session (default: Main; a feature reports to Build)")
    p.add_argument("--report-path", help="REPORT.md path (default: the unit's REPORT.md if present)")
    p.add_argument("--urgent", action="store_true", help="mark the pointer urgent (drained at Main's next prompt; does not wake a pane)")
    p.set_defaults(func=cmd_report)


def _fail(msg):
    print(f"orche: {msg}", file=sys.stderr)
    return EXIT_USER


def unidentified_message(r, place) -> str:
    """The refusal for an unidentified reporter, naming the Main of the cwd's project when known."""
    main = r.main(place.project)
    return (f"cannot tell which session is reporting: set ORCHE_SESSION={main.session if main else 'main-<proj>'} "
            "(ORCHE panes set it automatically) or run inside a unit worktree")


def cmd_report(args):
    if os.environ.get("ORCHE_RUNNER"):
        print("orche: report refused: this unit runs under `orche run`, which files your pointer from "
              "your final message", file=sys.stderr)
        return EXIT_RUNNER
    if args.status not in inbox.STATUSES:
        return _fail(f"status must be one of {'|'.join(inbox.STATUSES)}, got {args.status!r}")
    err = brief.unit_id_error(args.unit) if args.unit else None
    if err:
        return _fail(err)
    reg = registry.load()
    cwd = os.getcwd()
    r = roster.Roster(reg)
    who = r.identify(cwd)
    if who.session is None:
        return _fail(unidentified_message(r, who.place))
    if who.member is None:
        return _fail("cannot tell which session is reporting; set ORCHE_SESSION or run inside a unit")
    me, key, default_to = who.member.session, who.member.project, r.recipient(who.member.session)
    to = args.to or default_to
    if not to:
        return _fail(f"no recipient for {me}; pass --to")
    target = r.member(to)
    if target is None:
        return _fail(f"unknown target session {to!r} (not in the registry)")
    if target.status == "retired":
        return _fail(f"target session {to!r} is retired")
    summary = inbox.clip(mask.redact(args.summary)[0])
    if not summary:
        return _fail("--summary is empty")
    if args.unit:
        unit = args.unit
        b = brief.find(reg, unit, project=key)
        if b is None or b.rev == 0:
            return _fail(f"unknown unit {unit!r} (no .orche/units/{unit}/BRIEF.md)")
    else:
        b = brief.newest(reg, key, me)
        if b is None and args.status not in ADHOC_STATUSES:
            return _fail(f"no brief addressed to {me}; pass --unit")
        unit = b.unit if b else f"adhoc-{util.slugify(me)}-{units.now():%Y%m%d}"
    if args.report_path:
        report = str(Path(args.report_path).expanduser().resolve())
    else:
        default = b.folder / "REPORT.md" if b else None
        report = str(default) if default and default.is_file() else ""
    proof = donegate.for_report(who.member, cwd, args.status)
    if proof.missing:
        print(proof.message, file=sys.stderr)
        return EXIT_BLOCKED
    try:
        ptr = inbox.make(unit, me, to, args.status, report_path=report, head_sha=proof.head or "",
                         ledger_row=proof.row is not None,
                         brief_rev=b.rev if b else 1,
                         urgent=args.urgent, summary=summary)
        path = None if args.dry_run else inbox.write(ptr)
        if not args.dry_run:
            units.touch_activity(me)
    except ValueError as exc:
        return _fail(str(exc))
    if args.json:
        print(json.dumps(ptr, indent=2, ensure_ascii=False))
    elif args.dry_run:
        print(f"[dry-run] would write pointer {unit} {args.status} -> {to}")
    else:
        print(f"reported {unit} {args.status} -> {to}: {path}")
    return EXIT_OK
