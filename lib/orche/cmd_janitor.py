"""`orche janitor run|report|undo|restore|enable|disable`.

`run` stays a dry run until `ORCHE_HOME/janitor.enabled` exists; `--dry-run` always forces one. The
heartbeat starts it daily (janitor.heartbeat); one pass at a time (`janitor/.pass.lock`, exit 3 while held).
Unless `--dry-run`, `run` first reconciles lost runner runs and prunes `logs/runs/*.log` older than
`runner.log_keep_days` (ORCHE's own state, so not gated on `janitor.enabled`), and afterwards
refreshes the intake of every project that has one (`orche intake <proj> --if-stale`; a failure is a
line in the output, not a failed run). That refresh is deliberately not gated on `janitor.enabled`
either, although it is a paid model call that rewrites the intake block of the user's
PROJECT_CARD.md and the division CONTEXT.md files: the user opted in per project by running the
first intake (a `--no-intake` project has no stamp and is never swept), and `intake.enabled: false`
turns the refresh off. Then, once a week (`--weekly`: now), the weekly pass (janitor/weekly.py): the audit's
new mistake classes into STANDING.md with one pointer to Main, and one verify-skill drift unit per project
to Build; not on `--dry-run`.
Logic lives in `orche.janitor`; this module only parses arguments and prints.
"""
import sys

from . import cli, config, intake, janitor, log, registry, runs, util
from .janitor import archive, trash

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3


def register(subparsers):
    p = subparsers.add_parser("janitor", help="state-based cleanup: archive retired sessions, trash discarded files")
    sub = p.add_subparsers(dest="janitor_cmd", metavar="<action>")
    p.set_defaults(func=lambda a: (p.print_help(sys.stderr), EXIT_USER)[1])
    sp = sub.add_parser("run", help="plan (and, when enabled, perform) one cleanup pass; also refreshes "
                                    "stale project intakes (`orche intake --if-stale`, a paid read-only "
                                    "sweep each), even while the cleanup itself is a dry run")
    sp.add_argument("--apply-removed", action="store_true",
                    help="retire units whose worktree Orca no longer lists")
    sp.add_argument("--claude-projects", help="Claude transcript root (default ~/.claude/projects)")
    sp.add_argument("--weekly", action="store_true", help="run the weekly pass now (audit, verify-skill drift)")
    sp.set_defaults(func=cmd_run)
    sub.add_parser("report", help="print the last run's report").set_defaults(func=cmd_report)
    sp = sub.add_parser("undo", help="move a trashed path back")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_undo)
    sp = sub.add_parser("restore", help="unarchive a unit's sessions to their original paths")
    sp.add_argument("unit")
    sp.set_defaults(func=cmd_restore)
    sub.add_parser("enable", help="let `janitor run` act (not only report)").set_defaults(func=cmd_enable)
    sub.add_parser("disable", help="back to dry-run only").set_defaults(func=cmd_disable)


def cmd_run(args):
    with janitor.pass_lock() as held:
        if not held:
            return cli.fail(EXIT_BLOCKED, f"another janitor pass is running ({janitor.lock_path()})")
        return _run(args)


def _run(args):
    runs.reconcile_cli(registry.load(), args)
    if not args.dry_run:      # ORCHE's own run logs: pruned whether or not the janitor is enabled
        runs.prune_logs((config.load().get("runner") or {}).get("log_keep_days"))
    res = janitor.run(dry_run=args.dry_run, apply_removed=args.apply_removed,
                      claude_projects=args.claude_projects)
    cands = res["candidates"]
    data = {**{k: v for k, v in res.items() if k not in ("candidates", "text")},
            "candidates": [c.to_dict() for c in cands]}
    lines = [res["text"].rstrip("\n")]
    if not args.dry_run:      # refresh stamped projects' intake (never a reason for a failed run)
        refreshed = intake.refresh_all(registry.load(), config.load())
        data["intake"] = refreshed
        lines += [r["line"] for r in refreshed]
        from .janitor import weekly     # the dispatch relay and the audit: only for a run
        state = janitor.load_state()
        week = weekly.run(registry.load(), state, force=args.weekly)
        if week:
            janitor.save_state(state)
        data["weekly"], lines = week, lines + week
    cli.out(args, data, lines)
    return EXIT_USER if any(c.error for c in cands) else EXIT_OK


def cmd_report(args):
    path = janitor.report_path()
    if not path.is_file():
        return cli.fail(EXIT_NOT_FOUND, "no janitor report yet; run `orche janitor run`")
    text = path.read_text(encoding="utf-8")
    cli.out(args, {"report": str(path), "text": text}, [text.rstrip("\n")])
    return EXIT_OK


def cmd_undo(args):
    try:
        m = trash.undo(args.id, dry_run=args.dry_run)
    except trash.TrashError as exc:
        return cli.fail(exc.code, str(exc))
    verb = "would restore" if args.dry_run else "restored"
    if not args.dry_run:
        log.log("janitor", f"undo {args.id} -> {m['path']}")
    cli.out(args, {"id": args.id, "path": m["path"], "dry_run": args.dry_run}, [f"{verb} {m['path']}"])
    return EXIT_OK


def cmd_restore(args):
    try:
        restored = archive.restore(args.unit, dry_run=args.dry_run)
    except archive.ArchiveError as exc:
        return cli.fail(exc.code, str(exc))
    if not args.dry_run:
        log.log("janitor", f"restore {args.unit}: {len(restored)} files")
    verb = "would restore" if args.dry_run else "restored"
    cli.out(args, {"unit": args.unit, "files": restored, "dry_run": args.dry_run},
         [f"{verb} {len(restored)} files of {args.unit}"] + [f"  {p}" for p in restored])
    return EXIT_OK


def cmd_enable(args):
    flag = janitor.enabled_flag()
    if not args.dry_run:
        util.atomic_write(flag, f"enabled {util.now_iso()}\n")
    cli.out(args, {"enabled": True, "flag": str(flag), "dry_run": args.dry_run},
         [("would enable" if args.dry_run else "janitor enabled") + f" ({flag})"])
    return EXIT_OK


def cmd_disable(args):
    flag = janitor.enabled_flag()
    if not args.dry_run and flag.exists():
        flag.unlink()
    cli.out(args, {"enabled": False, "flag": str(flag), "dry_run": args.dry_run},
         [("would disable" if args.dry_run else "janitor disabled (dry-run only)")])
    return EXIT_OK

