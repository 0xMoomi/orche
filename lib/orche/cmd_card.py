"""`orche card` — a project's card (PROJECT_CARD.md).

- `orche card <project> [--path]`: print the card, or its path. Exit 0, 2 unknown project or no card.
- `orche card refresh <project> [--background]`: refresh the card with the Liaison's sealed,
  read-only headless worker (lib/orche/liaison.py). A git gate runs first: when the base commit
  under `## Last refreshed` is an ancestor of HEAD and no commit outside PROJECT_CARD.md and .orche/
  landed since, the card is fresh and nothing runs. Otherwise the worker returns JSON sections and
  the CLI writes them into the checkout's PROJECT_CARD.md (never through the `cards/<project>.md`
  symlink) when there was no base or a user-visible change landed; else nothing is written.
  `--background` starts the refresh detached and returns at once (log:
  ORCHE_HOME/logs/liaison/<project>.log); `--dry-run` prints the worker's argv and runs nothing.
  Exit 0 fresh / unchanged / refreshed / started, 1 the refresh failed (with `--json` stdout also
  gets `{"project", "status": "failed", "reason"}`) or a usage error, 2 unknown project, 130 Ctrl-C.
"""
import json
import shlex
import sys
from pathlib import Path

from . import cli, registry

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_INTERRUPTED = 0, 1, 2, 130
USAGE = "orche card <project> [--path] | orche card refresh <project> [--background]"


def register(subparsers):
    p = subparsers.add_parser("card", help="print a project's card, or refresh it", usage=USAGE)
    p.add_argument("words", nargs="+", metavar="[refresh] <project>", help="project key or alias")
    p.add_argument("--path", action="store_true", help="print the card's path instead of its text")
    p.add_argument("--background", action="store_true",
                   help="start the refresh detached and return at once; output goes to "
                        "ORCHE_HOME/logs/liaison/<project>.log")
    p.set_defaults(func=cmd_card)


def cmd_card(args):
    words = args.words
    if len(words) == 2 and words[0] == "refresh":
        return cmd_refresh(args, words[1])
    if len(words) != 1 or args.background:
        return cli.fail(EXIT_USER, f"usage: {USAGE}")
    return cmd_print(args, words[0])


def cmd_print(args, name):
    found = registry.project(registry.load(), name)
    if found is None:
        print(f"orche: unknown project: {name}", file=sys.stderr)
        return EXIT_NOT_FOUND
    key, proj = found
    path = Path(proj["card"]).expanduser()
    if args.path:
        print(json.dumps({"project": key, "path": str(path)}) if args.json else path)
        return EXIT_OK
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        print(f"orche: card not found: {path}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.json:
        print(json.dumps({"project": key, "path": str(path), "text": text}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
    return EXIT_OK


def cmd_refresh(args, name):
    if args.path:
        return cli.fail(EXIT_USER, f"usage: {USAGE}")
    from . import config, liaison, paths, procs
    reg, cfg = registry.load(), config.load()
    try:
        target = liaison.resolve(reg, name)
    except liaison.LiaisonError as exc:
        return cli.fail(exc.code, str(exc))
    key = target[0]
    if args.background and not args.dry_run:
        log = paths.orche_home() / "logs" / "liaison" / f"{key}.log"
        procs.spawn_orche(["card", "refresh", key], log)
        cli.out(args, {"project": key, "status": "started", "log": str(log)},
                f"card refresh started in the background (log: {log})")
        return EXIT_OK
    if args.dry_run:
        try:
            plan = liaison.refresh_plan(target, cfg)
        except liaison.LiaisonError as exc:
            return cli.fail(exc.code, str(exc))
        if plan.commits == 0:
            rec = liaison.record(plan, "fresh")
            cli.out(args, rec, liaison.line(rec))
            return EXIT_OK
        argv = plan.argv
        cli.out(args, {"project": key, "cwd": str(plan.root), "head": plan.head, "base": plan.base,
                       "commits": plan.commits, "timeout_s": plan.call.timeout, "argv": argv, "stdin": plan.stdin},
                [shlex.join(argv)])
        base = plan.base[:7] if plan.base else "none"
        cli.dry(f"cwd {plan.root}; stdin: card refresh request (base {base}, {plan.commits or 0} commits); "
                f"timeout {int(plan.call.timeout)}s; writes nothing")
        return EXIT_OK
    try:
        rec = liaison.refresh(target, cfg)
    except KeyboardInterrupt:       # claude's group was killed on the way out (headless.run)
        return cli.fail(EXIT_INTERRUPTED, f"card refresh {key} interrupted")
    except liaison.LiaisonError as exc:
        if args.json:
            print(json.dumps({"project": key, "status": "failed", "reason": str(exc)}, indent=2, ensure_ascii=False))
        return cli.fail(exc.code, f"card refresh {key} failed: {exc}")
    cli.out(args, rec, [liaison.line(rec)] + [f"note: {n}" for n in rec["notes"] or []])
    return EXIT_OK
