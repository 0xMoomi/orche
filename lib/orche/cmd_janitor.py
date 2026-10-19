"""`orche janitor run|report|undo|restore|enable|disable|install-schedule|uninstall-schedule`.

`run` stays a dry run until `ORCHE_HOME/janitor.enabled` exists; `--dry-run` always forces one.
Unless `--dry-run`, `run` first reconciles lost runner runs and prunes `logs/runs/*.log` older than
`runner.log_keep_days` (ORCHE's own state, so not gated on `janitor.enabled`), and afterwards
refreshes the intake of every project that has one (`orche intake <proj> --if-stale`; a failure is a
line in the output, not a failed run). That refresh is deliberately not gated on `janitor.enabled`
either, although it is a paid model call that rewrites the intake block of the user's
PROJECT_CARD.md and the division CONTEXT.md files: the user opted in per project by running the
first intake (a `--no-intake` project has no stamp and is never swept), and `intake.enabled: false`
turns the refresh off.
Logic lives in `orche.janitor`; this module only parses arguments and prints.
"""
import json
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import config, intake, janitor, log, paths, registry, runs, shell, templates, util
from .janitor import archive, trash

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3
LABEL = "com.orche.janitor"
PLIST_TEMPLATE = templates.TEMPLATES_ROOT / "launchd" / f"{LABEL}.plist"
ORCHE_BIN = templates.TEMPLATES_ROOT.parent / "bin" / "orche"


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
    sub.add_parser("install-schedule", help="install the daily 04:00 launchd job").set_defaults(func=cmd_install)
    sub.add_parser("uninstall-schedule", help="remove the launchd job").set_defaults(func=cmd_uninstall)


def _out(args, data, lines):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)


def _fail(code, msg):
    print(f"orche: {msg}", file=sys.stderr)
    return code


def cmd_run(args):
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
    _out(args, data, lines)
    return EXIT_USER if any(c.error for c in cands) else EXIT_OK


def cmd_report(args):
    path = janitor.report_path()
    if not path.is_file():
        return _fail(EXIT_NOT_FOUND, "no janitor report yet; run `orche janitor run`")
    text = path.read_text(encoding="utf-8")
    _out(args, {"report": str(path), "text": text}, [text.rstrip("\n")])
    return EXIT_OK


def cmd_undo(args):
    try:
        m = trash.undo(args.id, dry_run=args.dry_run)
    except trash.TrashError as exc:
        return _fail(exc.code, str(exc))
    verb = "would restore" if args.dry_run else "restored"
    if not args.dry_run:
        log.log("janitor", f"undo {args.id} -> {m['path']}")
    _out(args, {"id": args.id, "path": m["path"], "dry_run": args.dry_run}, [f"{verb} {m['path']}"])
    return EXIT_OK


def cmd_restore(args):
    try:
        restored = archive.restore(args.unit, dry_run=args.dry_run)
    except archive.ArchiveError as exc:
        return _fail(exc.code, str(exc))
    if not args.dry_run:
        log.log("janitor", f"restore {args.unit}: {len(restored)} files")
    verb = "would restore" if args.dry_run else "restored"
    _out(args, {"unit": args.unit, "files": restored, "dry_run": args.dry_run},
         [f"{verb} {len(restored)} files of {args.unit}"] + [f"  {p}" for p in restored])
    return EXIT_OK


def cmd_enable(args):
    flag = janitor.enabled_flag()
    if not args.dry_run:
        util.atomic_write(flag, f"enabled {util.now_iso()}\n")
    _out(args, {"enabled": True, "flag": str(flag), "dry_run": args.dry_run},
         [("would enable" if args.dry_run else "janitor enabled") + f" ({flag})"])
    return EXIT_OK


def cmd_disable(args):
    flag = janitor.enabled_flag()
    if not args.dry_run and flag.exists():
        flag.unlink()
    _out(args, {"enabled": False, "flag": str(flag), "dry_run": args.dry_run},
         [("would disable" if args.dry_run else "janitor disabled (dry-run only)")])
    return EXIT_OK


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render_plist() -> str:
    vals = {"ORCHE_BIN": str(ORCHE_BIN), "ORCHE_HOME": str(paths.orche_home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    return templates.render(PLIST_TEMPLATE.read_text(encoding="utf-8"),
                            **{k: escape(v) for k, v in vals.items()})


def cmd_install(args):
    target = plist_path()
    argv = ["launchctl", "load", "-w", str(target)]
    text = render_plist()
    data = {"plist": str(target), "argv": argv, "dry_run": args.dry_run}
    if args.dry_run:
        _out(args, {**data, "content": text}, [f"would write {target}", "would run: " + " ".join(argv)])
        return EXIT_OK
    paths.ensure_dirs()
    if target.exists():
        shell.run(["launchctl", "unload", str(target)])
    util.atomic_write(target, text, mode=0o644)
    res = shell.run(argv)
    if not res.ok:
        return _fail(EXIT_USER, f"launchctl load failed: {res.err.strip() or res.out.strip()}")
    _out(args, data, [f"installed {target}", "janitor runs daily at 04:00"])
    return EXIT_OK


def cmd_uninstall(args):
    target = plist_path()
    argv = ["launchctl", "unload", "-w", str(target)]
    data = {"plist": str(target), "argv": argv, "dry_run": args.dry_run}
    if args.dry_run:
        _out(args, data, ["would run: " + " ".join(argv), f"would remove {target}"])
        return EXIT_OK
    if not target.exists():
        return _fail(EXIT_NOT_FOUND, f"not installed: {target}")
    res = shell.run(argv)
    if not res.ok:
        print(f"orche: warning: launchctl unload failed: {res.err.strip()}", file=sys.stderr)
    target.unlink()
    _out(args, data, [f"removed {target}"])
    return EXIT_OK
