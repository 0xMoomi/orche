"""`orche errand`: the CEO Staff's errands (lib/orche/errand.py).

  orche errand --goal TEXT [--context TEXT] [--timebox 10m]   start an errand (a headless run; the result
                                                              comes back to Staff)
  orche errand --main --goal TEXT [--context TEXT]            hand a new request to Main
  orche errand UNIT --answer TEXT                             the owner's answer to the errand's question
  orche errand UNIT --main [--context TEXT]                   hand an errand, or the owner's answer to Main's
                                                              question, to Main
  orche errand UNIT --stop                                    stop or drop it (Staff; Main for a running one)

Run from a project's Staff pane (ORCHE_SESSION=staff-<p>); the project's Main may stop a running errand.
`--json` prints {unit, mode, state}; `--dry-run` writes nothing. Exit 0 done, 1 usage (bad arguments, an
unknown unit, not this project's errand), 2 refused: one stderr line `orche: errand refused: <why>`.
"""
import json
import os
import sys

from . import cli, errand, registry, roster

EXIT_OK, EXIT_USER, EXIT_REFUSED = 0, 1, 2


def register(subparsers):
    p = subparsers.add_parser("errand", help="Staff: run a small task in the background, or hand work to Main")
    p.add_argument("unit", nargs="?", help="the errand's unit id (for --answer, --main on an errand, --stop)")
    p.add_argument("--goal", help="what the owner wants, in one line")
    p.add_argument("--context", default="", help="the owner's own words first, then what you know")
    p.add_argument("--timebox", help=f"how long the errand may run (default {errand.TIMEBOX_DEFAULT}, "
                                     f"at most {errand.TIMEBOX_MAX_S // 60}m)")
    p.add_argument("--answer", help="the owner's answer to the errand's question")
    p.add_argument("--main", action="store_true", help="hand it to Main")
    p.add_argument("--stop", action="store_true", help="stop or drop the errand")
    p.set_defaults(func=cmd_errand)


def _mode(args):
    """(mode, error): start | main | answer | stop, or a usage error."""
    if args.stop:
        if not args.unit or args.goal or args.answer or args.main or args.timebox:
            return None, "--stop takes the errand's UNIT and nothing else"
        return "stop", None
    if args.answer is not None:
        if not args.unit or args.goal or args.main or args.timebox:
            return None, "--answer takes the errand's UNIT and the owner's answer"
        return "answer", None
    if args.main:
        if bool(args.unit) == bool(args.goal) or args.timebox:
            return None, "--main takes either a new request (--goal) or an errand's UNIT"
        return "main", None
    if args.unit or not args.goal:
        return None, "start an errand with --goal (UNIT goes with --answer, --main or --stop)"
    return "start", None


def _actor(r):
    """The session running the command: ORCHE_SESSION, else the session the cwd belongs to."""
    return roster.env_session() or r.identify(os.getcwd()).session


def cmd_errand(args):
    mode, err = _mode(args)
    if err:
        return cli.fail(EXIT_USER, err)
    reg = registry.load()
    r = roster.Roster(reg)
    session = roster.env_session() if mode != "stop" else _actor(r)
    try:
        if mode == "start":
            unit = errand.start(reg, session, args.goal, args.context, args.timebox, dry_run=args.dry_run)
            data = {"state": "running"}
            line = (f"errand {unit} started (up to {args.timebox or errand.TIMEBOX_DEFAULT}); "
                    "the result comes back here")
        elif mode == "answer":
            unit = args.unit
            rev = errand.answer(reg, session, unit, args.answer, dry_run=args.dry_run)
            data = {"state": "running", "rev": rev}
            line = f"errand {unit} rev {rev} started with the owner's answer; the result comes back here"
        elif mode == "main":
            unit = errand.hand_over(reg, session, unit=args.unit, goal=args.goal, context=args.context,
                                    dry_run=args.dry_run)
            data, line = {"state": "handed"}, f"handed to Main: {unit}; Main's answer comes back here"
        else:
            unit = args.unit
            state = errand.stop(reg, session, unit, dry_run=args.dry_run)
            data = {"state": state}
            line = {"stopped": f"errand {unit} stopped", "dropped": f"errand {unit} dropped",
                    "asked-main": f"asked Main to stop {unit} and its units"}[state]
    except errand.Refused as exc:
        print(f"orche: errand refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ValueError as exc:
        return cli.fail(EXIT_USER, str(exc))
    data = {"unit": unit, "mode": mode, **data}
    if args.dry_run:
        data["dry_run"] = True
        if mode == "start" or (mode == "main" and not args.unit):     # a new unit: name its brief
            path = os.path.join(str(errand.member(reg, session).home), ".orche", "units", unit, "BRIEF.md")
            data["brief"] = path
            line = f"[dry-run] would write {path} and " + ("start the errand" if mode == "start" else "hand it to Main")
        else:
            line = f"[dry-run] {line}"
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(line)
    return EXIT_OK
