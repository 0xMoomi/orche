"""`orche run <unit>` — run one dispatched unit headless (`claude -p`) in its target's worktree.

The Stop hook starts it detached for every new brief (and AMEND) to a division or feature (config
`delivery: runner`); run it by hand to re-run a unit. `--dry-run` prints the exact `claude` argv and
writes nothing; `--json` prints the RUN record. Exit 0 when the run finished (done, unverified,
blocked, stopped by Main — also before start), 1 when it failed (crash, timeout, `STATUS: failed`,
a runner error, SIGTERM), 2 for a missing brief/worktree or a retired target, 3 when the project's
run slots (or the target's one) stay taken for `runner.queue_wait_s` ("runner busy") or a flow cap
holds the unit (G4 spend, G5 runs per unit: "not started: …; ask the owner", one quiet `info`), 4 when the
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
other run's end-of-run sweep never kills it; its own claude gets its own tag (headless.env).
"""
import json
import os
import shlex
import sys
from pathlib import Path

from . import config, procs, registry, runs

EXIT_OK, EXIT_USER = 0, 1


def register(subparsers):
    p = subparsers.add_parser("run", help="run one unit headless with claude -p (the runner; manual re-run)")
    p.add_argument("unit", help="unit id <proj>-<slug>-<yyyymmdd>-<n>")
    p.set_defaults(func=cmd_run)


SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "orche.py"


def _drop_inherited_tag():
    """A run started inside another run's worker (its Bash runs `orche run <other>`) inherits that
    run's ORCHE_RUN_ID, and the other run's end-of-run sweep would kill it. `ps -E` and
    /proc/<pid>/environ show the environment the process was exec'd with, which unsetenv does not
    change, so a process started as the CLI re-execs itself without the tag; in-process (tests) the
    variable is only removed from os.environ (headless.env never passes it on either way)."""
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
    if args.dry_run:
        return _dry_run(args, reg, cfg)
    out = runs.execute(reg, args.unit, cfg)
    for line in out.stderr:
        print(line, file=sys.stderr)
    if out.record is not None:
        rec = out.record
        if args.json:
            print(json.dumps(rec, indent=2, ensure_ascii=False))
        else:
            print(f"ran {args.unit} -> {rec['status']} (model {rec['model']}, cost {rec['cost_usd']}, "
                  f"turns {rec['num_turns']})")
    return out.code


def _dry_run(args, reg, cfg):
    try:
        p = runs.preview(reg, args.unit, cfg)
    except runs.RunError as exc:
        print(f"orche: {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # noqa: BLE001 - bad config / unreadable persona
        print(f"orche: runner error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_USER
    job, call = p.job, p.call
    if args.json:
        print(json.dumps({"unit": job.unit, "to": job.to, "cwd": str(job.worktree), "tier": job.tier,
                          "timeout_s": call.timeout, "argv": p.argv}, indent=2, ensure_ascii=False))
    else:
        print(shlex.join(p.argv))
        context = f" + project context {job.context}" if job.context is not None else ""
        env = " ".join(f"{k}={v}" for k, v in call.env.items())
        print(f"[dry-run] cwd {job.worktree}; {env}; stdin {job.brief}{context} + runner "
              f"footer; timeout {call.timeout}s", file=sys.stderr)
    return EXIT_OK
