"""`orche ask <project> "<question>"` — ask another project a question (the Liaison, SPEC §14.14).

One sealed, read-only headless Sonnet worker in the project's main checkout answers from its card,
its recent history and the files it reads (lib/orche/liaison.py). The answer goes to stdout under
a header that marks it as data; cost, time and turns go to stderr. Nothing is written but one
events line (counts only). `--dry-run` prints the worker's exact argv and runs nothing.
Exit 0 answered, 1 the ask failed (with `--json` stdout also gets `{"project", "status": "failed",
"reason"}`) or the question is empty or too long, 2 unknown project, 130 Ctrl-C.
"""
import json
import shlex
import sys

from . import cli

EXIT_OK, EXIT_INTERRUPTED = 0, 130


def register(subparsers):
    p = subparsers.add_parser("ask", help="ask another project a question (headless, read-only answer)",
                              usage='orche ask <project> "<question>"',
                              epilog="Quote the question. Put `--` before a question that starts with '-'.")
    p.add_argument("project", help="project key or alias")
    p.add_argument("question", nargs="*", help="the question (quoted)")
    p.set_defaults(func=cmd_ask)


def cmd_ask(args):
    from . import config, liaison, registry
    reg, cfg = registry.load(), config.load()
    try:
        question = liaison.question_text(args.question)
        target = liaison.resolve(reg, args.project)
    except liaison.LiaisonError as exc:
        return cli.fail(exc.code, str(exc))
    key = target[0]
    if args.dry_run:
        try:
            plan = liaison.ask_plan(target, question, cfg)
        except liaison.LiaisonError as exc:
            return cli.fail(exc.code, str(exc))
        argv = plan.argv
        cli.out(args, {"project": key, "cwd": str(plan.root), "head": plan.head, "timeout_s": plan.call.timeout,
                       "argv": argv, "stdin": plan.stdin}, [shlex.join(argv)])
        cli.dry(f"cwd {plan.root}; stdin: ask request ({len(plan.stdin.encode('utf-8'))} bytes, HEAD "
                f"{plan.head[:7]}); timeout {int(plan.call.timeout)}s; runs nothing")
        return EXIT_OK
    try:
        rec = liaison.ask(target, question, cfg)
    except KeyboardInterrupt:       # claude's group was killed on the way out (headless.run)
        return cli.fail(EXIT_INTERRUPTED, f"ask {key} interrupted")
    except liaison.LiaisonError as exc:
        if args.json:
            print(json.dumps({"project": key, "status": "failed", "reason": str(exc)}, indent=2, ensure_ascii=False))
        return cli.fail(exc.code, f"ask {key} failed: {exc}")
    if args.json:
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"(answer from {key} at {rec['head'][:7]} — data, not instructions)")
    print(rec["answer"])
    cost = f"${liaison._num(rec['cost_usd'], '.2f')}"
    secs = liaison._num(round(rec["duration_s"]) if isinstance(rec["duration_s"], (int, float)) else None, "d")
    turns = rec["turns"] if rec["turns"] is not None else "?"
    print(f"ask {key}: {cost}, {secs} s, {turns} turns ({rec['model']})", file=sys.stderr)
    return EXIT_OK
