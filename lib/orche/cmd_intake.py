"""`orche intake <project> [--if-stale]` and `orche intake status <project>` — project intake.

A one-time read-only sweep of the project's main checkout by a headless Sonnet worker (Read, Glob
and Grep only; the tree, `git log --oneline -30`, the manifests and env variable names come
pre-computed in its request), written into
`.orche/div/<division>/CONTEXT.md`, PROJECT_CARD.md and the stamp `.orche/intake.json`
(lib/orche/intake.py). `orche init` runs it at the end; `orche janitor run` refreshes stamped
projects with `--if-stale`. `--if-stale` runs only without a stamp, when HEAD moved
`intake.refresh_commits` commits past it or it is `intake.refresh_days` old; otherwise it prints
"intake fresh (…)" and exits 0. `--dry-run` prints the worker's exact argv and writes nothing.
Exit 0 done (or fresh), 1 the intake failed (nothing written; with `--json` stdout also gets
`{"project", "status": "failed", "reason"}`), 2 unknown project / no stamp, 3 another intake of
the project is running, 130 Ctrl-C (claude's process group killed, nothing written).
"""
import json
import shlex
import sys

from . import config, intake, registry, runner

EXIT_OK, EXIT_USER, EXIT_INTERRUPTED = 0, 1, 130


def register(subparsers):
    p = subparsers.add_parser("intake", help="sweep a project's codebase once into per-division CONTEXT.md "
                                             "and PROJECT_CARD.md (headless, read-only)",
                              usage="orche intake [--if-stale] <project> | orche intake status <project>")
    p.add_argument("words", nargs="+", metavar="[status] <project>",
                   help="`<project>` runs the intake; `status <project>` prints its stamp")
    p.add_argument("--if-stale", action="store_true",
                   help="run only when there is no stamp, HEAD moved intake.refresh_commits commits "
                        "or the stamp is intake.refresh_days old")
    p.set_defaults(func=cmd_intake)


def _fail(code, msg):
    print(f"orche: {msg}", file=sys.stderr)
    return code


def _emit(args, data, lines):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)


def cmd_intake(args):
    words = args.words
    if len(words) == 2 and words[0] == "status":
        return cmd_status(args, words[1])
    if len(words) != 1:
        return _fail(EXIT_USER, "usage: orche intake <project> [--if-stale] | orche intake status <project>")
    reg, cfg = registry.load(), config.load()
    try:
        key, _proj, root = intake.resolve(reg, words[0])
    except intake.IntakeError as exc:
        return _fail(exc.code, str(exc))
    if args.if_stale:
        info = intake.stale(root, intake.read_stamp(root), cfg)
        if not info["stale"]:
            _emit(args, {"project": key, "status": "fresh", **info}, [intake.fresh_line(info)])
            return EXIT_OK
    if args.dry_run:
        try:
            p = intake.plan(reg, key, cfg, runner.claude_flags(cfg, write=False))
        except intake.IntakeError as exc:
            return _fail(exc.code, str(exc))
        _emit(args, {"project": key, "cwd": str(p.root), "head": p.head, "timeout_s": p.timeout,
                     "divisions": [d[0] for d in p.divisions], "argv": p.argv, "stdin": p.stdin},
              [shlex.join(p.argv)])
        if not args.json:
            print(f"[dry-run] cwd {p.root}; stdin: intake request ({len(p.divisions)} divisions, HEAD "
                  f"{p.head[:7]}); timeout {p.timeout}s; writes nothing", file=sys.stderr)
        return EXIT_OK
    try:
        rec = intake.run(reg, key, cfg)
    except KeyboardInterrupt:       # claude's group was killed on the way out (runner.execute)
        return _fail(EXIT_INTERRUPTED, f"intake {key} interrupted; nothing written")
    except intake.IntakeError as exc:
        if args.json:
            _emit(args, {"project": key, "status": "failed", "reason": str(exc)}, [])
        return _fail(exc.code, f"intake {key} failed: {exc}")
    _emit(args, {"status": "ran", **rec}, [intake.summary_line(rec)] + [f"note: {n}" for n in rec["notes"]])
    return EXIT_OK


def cmd_status(args, name):
    reg, cfg = registry.load(), config.load()
    try:
        key, _proj, root = intake.resolve(reg, name)
    except intake.IntakeError as exc:
        return _fail(exc.code, str(exc))
    stamp = intake.read_stamp(root)
    if not stamp:
        return _fail(intake.EXIT_NOT_FOUND, f"no intake yet for {key}; run `orche intake {key}`")
    info = intake.stale(root, stamp, cfg)
    lines = [f"intake {key}: {(stamp.get('head') or '?')[:7]} on {stamp.get('date')} ({stamp.get('model')}, "
             f"cost {stamp.get('cost_usd')}, {stamp.get('duration_s')} s, {stamp.get('turns')} turns)",
             f"divisions: {', '.join(stamp.get('divisions') or []) or '(none)'}",
             ("stale: " + info["why"]) if info["stale"] else intake.fresh_line(info)]
    _emit(args, {"project": key, "stamp": stamp, **info}, lines)
    return EXIT_OK
