"""`orche intake <project> [--if-stale] [--background]` and `orche intake status <project>` — project intake.

A one-time read-only sweep of the project's main checkout by a headless Sonnet worker (Read, Glob
and Grep only; the tree, `git log --oneline -30`, the manifests and env variable names come
pre-computed in its request), written into
`.orche/div/<division>/CONTEXT.md`, PROJECT_CARD.md and the stamp `.orche/intake.json`
(lib/orche/intake.py). `orche init` runs it at the end; `orche janitor run` refreshes stamped
projects with `--if-stale`. `--if-stale` runs only without a stamp, when HEAD moved
`intake.refresh_commits` commits past it, the stamped commit is no longer an ancestor of HEAD, or
it is `intake.refresh_days` old; otherwise it prints "intake fresh (…)" and exits 0. `--dry-run`
prints the worker's exact argv and writes nothing. `--background` starts the same command detached
(its own session, so it outlives a Bash tool call) with output in
`ORCHE_HOME/logs/intake/<project>.log`, and returns at once.
Exit 0 done (or fresh), 1 the intake failed (nothing written; with `--json` stdout also gets
`{"project", "status": "failed", "reason"}`), 2 unknown project / no stamp, 3 another intake of
the project is running, 130 Ctrl-C (claude's process group killed, nothing written).
"""
import json
import shlex
import sys

from . import config, headless, intake, paths, procs, registry

EXIT_OK, EXIT_USER, EXIT_INTERRUPTED = 0, 1, 130


def register(subparsers):
    p = subparsers.add_parser("intake", help="sweep a project's codebase once into per-division CONTEXT.md "
                                             "and PROJECT_CARD.md (headless, read-only)",
                              usage="orche intake [--if-stale] [--background] <project> | "
                                    "orche intake status <project>")
    p.add_argument("words", nargs="+", metavar="[status] <project>",
                   help="`<project>` runs the intake; `status <project>` prints its stamp")
    p.add_argument("--if-stale", action="store_true",
                   help="run only when there is no stamp, HEAD moved intake.refresh_commits commits, "
                        "the stamped commit is no longer an ancestor of HEAD, or the stamp is "
                        "intake.refresh_days old")
    p.add_argument("--background", action="store_true",
                   help="start the intake detached (it outlives the calling shell) and return at once; "
                        "output goes to ORCHE_HOME/logs/intake/<project>.log")
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
    if getattr(args, "background", False) and not args.dry_run:
        return _background(args, key)
    if args.if_stale:
        info = intake.stale(root, intake.read_stamp(root), cfg)
        if not info["stale"]:
            _emit(args, {"project": key, "status": "fresh", **info}, [intake.fresh_line(info)])
            return EXIT_OK
    if args.dry_run:
        try:
            p = intake.plan(reg, key, cfg)
        except intake.IntakeError as exc:
            return _fail(exc.code, str(exc))
        argv = headless.argv(p.call, cfg)
        _emit(args, {"project": key, "cwd": str(p.root), "head": p.head, "timeout_s": p.call.timeout,
                     "divisions": [d[0] for d in p.divisions], "argv": argv, "stdin": p.stdin},
              [shlex.join(argv)])
        if not args.json:
            print(f"[dry-run] cwd {p.root}; stdin: intake request ({len(p.divisions)} divisions, HEAD "
                  f"{p.head[:7]}); timeout {p.call.timeout}s; writes nothing", file=sys.stderr)
        return EXIT_OK
    try:
        rec = intake.run(reg, key, cfg)
    except KeyboardInterrupt:       # claude's group was killed on the way out (headless.run)
        return _fail(EXIT_INTERRUPTED, f"intake {key} interrupted; nothing written")
    except intake.IntakeError as exc:
        if args.json:
            _emit(args, {"project": key, "status": "failed", "reason": str(exc)}, [])
        return _fail(exc.code, f"intake {key} failed: {exc}")
    _emit(args, {"status": "ran", **rec}, [intake.summary_line(rec)] + [f"note: {n}" for n in rec["notes"]])
    return EXIT_OK


def background_argv(key, if_stale=False) -> list:
    """`orche intake <key> [--if-stale]` under this very interpreter (like runs.start's)."""
    return procs.orche_argv("intake", key, *(["--if-stale"] if if_stale else []))


def _background(args, key):
    """Start the intake detached (procs.spawn_detached) so it survives the calling Bash tool call."""
    try:
        with intake.lock(key):      # held by a running intake: start nothing
            pass
    except intake.IntakeError:
        _emit(args, {"project": key, "status": "running"}, [f"intake already running for {key}"])
        return EXIT_OK
    log = paths.orche_home() / "logs" / "intake" / f"{key}.log"
    procs.spawn_detached(background_argv(key, args.if_stale), log)
    _emit(args, {"project": key, "status": "started", "log": str(log)},
          [f"intake started in the background (log: {log}); `orche intake status {key}` reads the stamp"])
    return EXIT_OK


def cmd_status(args, name):
    reg, cfg = registry.load(), config.load()
    try:
        key, _proj, root = intake.resolve(reg, name)
    except intake.IntakeError as exc:
        return _fail(exc.code, str(exc))
    stamp = intake.read_stamp(root)
    if not stamp:
        if args.json:   # M-9: --json always prints a record
            _emit(args, {"project": key, "status": "none"}, [])
        return _fail(intake.EXIT_NOT_FOUND, f"no intake yet for {key}; run `orche intake {key}`")
    info = intake.stale(root, stamp, cfg)
    lines = [f"intake {key}: {(stamp.get('head') or '?')[:7]} on {stamp.get('date')} ({stamp.get('model')}, "
             f"cost {stamp.get('cost_usd')}, {stamp.get('duration_s')} s, {stamp.get('turns')} turns)",
             f"divisions: {', '.join(stamp.get('divisions') or []) or '(none)'}",
             ("stale: " + info["why"]) if info["stale"] else intake.fresh_line(info)]
    _emit(args, {"project": key, "stamp": stamp, **info}, lines)
    return EXIT_OK
