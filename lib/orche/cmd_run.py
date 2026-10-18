"""`orche run <unit>` — run one dispatched unit headless (`claude -p`) in its target's worktree.

The Stop hook starts it detached for every new brief (and AMEND) to a division or feature (config
`delivery: runner`); run it by hand to re-run a unit. `--dry-run` prints the exact `claude` argv and
writes nothing; `--json` prints the RUN record. Exit 0 when the run finished (done, unverified,
blocked, stopped by Main — also before start), 1 when it failed (crash, timeout, `STATUS: failed`,
a runner error, SIGTERM), 2 for a missing brief/worktree or a retired target, 3 when the project's
run slots (or the target's one) stay taken for `runner.queue_wait_s` ("runner busy"), 4 when the
unit is already running (that run files its own result), 5 when RUN.json, REPORT.md or the pointer
cannot be written (ENOSPC, EACCES, …): the error goes to stderr (the run log) and the run's lock
stays, so reconcile files `failed` once the filesystem takes writes again. Each run first deletes
`logs/runs/*.log` older than `runner.log_keep_days` (14).

Every outcome past resolving the unit leaves an inbox pointer from the target (busy/duplicate:
`info`; a runner error or SIGTERM: `failed`; STOP: `info` "stopped by Main" / "… before start") and,
except for a duplicate, a RUN.json, so a spawned run never ends silently. A run whose `orche run`
dies without filing (SIGKILL, OOM, power loss) is filed later by runs.reconcile(): `failed`
"runner died: …" at Main's next prompt or the next `orche` command. A target that routes to no
recipient gets no pointer (logged; RUN.json `pointer_filed: false`).

A run started inside another run's worker drops the ORCHE_RUN_ID it inherited (re-exec), so the
other run's end-of-run sweep never kills it; its own claude gets its own tag (runner.env_for).
"""
import json
import os
import shlex
import sys
import traceback
from pathlib import Path

from . import config, procs, registry, runner, runs

EXIT_OK, EXIT_USER, EXIT_BLOCKED, EXIT_DUPLICATE, EXIT_UNFILED = 0, 1, 3, 4, 5


def register(subparsers):
    p = subparsers.add_parser("run", help="run one unit headless with claude -p (the runner; manual re-run)")
    p.add_argument("unit", help="unit id <proj>-<slug>-<yyyymmdd>-<n>")
    p.set_defaults(func=cmd_run)


def _unrun(reg, job, model, status, summary, error, stop_reason, code, record=True):
    print(f"orche: {error}", file=sys.stderr)
    runner.file_unrun(reg, job, model, status, summary, error, stop_reason, record)
    return code


def _runner_error(reg, job, model, exc):
    traceback.print_exc(file=sys.stderr)
    error = f"runner error: {type(exc).__name__}: {exc}"
    return _unrun(reg, job, model, "failed", f"run failed: {error}", error, "error", EXIT_USER)


SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "orche.py"


def _drop_inherited_tag():
    """A run started inside another run's worker (its Bash runs `orche run <other>`) inherits that
    run's ORCHE_RUN_ID, and the other run's end-of-run sweep would kill it. `ps -E` and
    /proc/<pid>/environ show the environment the process was exec'd with, which unsetenv does not
    change, so a process started as the CLI re-execs itself without the tag; in-process (tests) the
    variable is only removed from os.environ (env_for never passes it on either way)."""
    if procs.ENV not in os.environ:
        return
    env = {k: v for k, v in os.environ.items() if k != procs.ENV}
    try:
        as_cli = bool(sys.argv) and Path(sys.argv[0]).resolve() == SCRIPT
    except OSError:
        as_cli = False
    if as_cli:
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)
        except OSError as exc:
            print(f"orche: cannot re-exec without the inherited {procs.ENV}: {exc}", file=sys.stderr)
    os.environ.pop(procs.ENV, None)


def cmd_run(args):
    _drop_inherited_tag()
    reg = registry.load()
    cfg = config.load()
    try:
        job = runner.resolve(reg, args.unit)
    except runner.RunError as exc:
        print(f"orche: {exc}", file=sys.stderr)
        if exc.job is not None and not args.dry_run:
            runner.file_unrun(reg, exc.job, None, "failed", f"run failed: {exc}", str(exc), "error")
        return exc.code
    try:
        argv = runner.build_argv(job, cfg, runner.claude_flags(cfg, write=not args.dry_run))
    except Exception as exc:  # noqa: BLE001 - bad config / unreadable persona: still a pointer
        if args.dry_run:
            print(f"orche: runner error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_USER
        try:
            model = runner.model_for(job.tier, cfg)
        except Exception:  # noqa: BLE001
            model = None
        return _runner_error(reg, job, model, exc)
    model = argv[argv.index("--model") + 1]
    if args.dry_run:
        timeout = job.timebox_s + runner.GRACE_S
        if args.json:
            print(json.dumps({"unit": job.unit, "to": job.to, "cwd": str(job.worktree), "tier": job.tier,
                              "timeout_s": timeout, "argv": argv}, indent=2, ensure_ascii=False))
        else:
            print(shlex.join(argv))
            print(f"[dry-run] cwd {job.worktree}; ORCHE_SESSION={job.to}; stdin {job.brief} + runner footer; "
                  f"timeout {timeout}s", file=sys.stderr)
        return EXIT_OK
    runs.prune_logs((cfg.get("runner") or {}).get("log_keep_days"))
    try:
        record = runner.run(reg, job, argv, cfg)
    except runner.FilingError as exc:
        # Nothing more can be written; the lock stays and, once this process is gone, reconcile
        # files `failed` "runner died" (the next `orche` command or Main's next prompt).
        print(f"orche: cannot file the result of {job.unit}: {exc}; the run's lock stays for reconcile",
              file=sys.stderr)
        return EXIT_UNFILED
    except runs.Busy as exc:
        return _unrun(reg, job, model, "info", f"{exc}, retry with `orche run {job.unit}`", str(exc), "busy",
                      EXIT_BLOCKED)
    except runs.Duplicate as exc:
        # The live run files its own result (its snapshot ignores this `via: runner` pointer) and
        # writes its own RUN.json; Main, who asked, still gets an answer.
        return _unrun(reg, job, model, "info", f"not started: {exc}; that run files its own result",
                      str(exc), "duplicate", EXIT_DUPLICATE, record=False)
    except Exception as exc:  # noqa: BLE001 - never end a spawned run without a pointer
        return _runner_error(reg, job, model, exc)
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
    else:
        print(f"ran {job.unit} -> {record['status']} (model {record['model']}, cost {record['cost_usd']}, "
              f"turns {record['num_turns']})")
    return EXIT_USER if record["status"] == "failed" else EXIT_OK
