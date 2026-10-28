"""`orche review --pr N`: the cross-check of an open PR on request (R-REVIEW-1, R33).

  orche review --pr N [--project <p>] [--unit <id>] [--background]

The request: Main runs it on the owner's words, Build on a change it doubts. One pre-review round of the
PR's head (the two-vendor panel when both vendors are up, else a single reviewer of the other vendor; a
ledger row of kind pre, so `orche queue add --pr N` finds it), then on PASS the headless head review;
both PASS queue the PR. A pre-review RETURN prints its findings and exits 3 (fix, push, review again);
anything else wakes the Build pane. `--background` starts the same detached and tells the caller its
pre-review verdict as a waking `review` pointer. Nothing in ORCHE starts a cross-check by itself: after
a publish the run and the heartbeat start the head review alone (internally, `--head`).
"""
import argparse

from . import cli, config, log, paths, procs, registry, roster
from .review import build as _build


def register(subparsers):
    p = subparsers.add_parser("review", help="cross-check an open PR on request (two vendors), then its head review",
                              description=__doc__.split("\n", 1)[1])
    p.add_argument("--pr", type=int, required=True, help="the PR number")
    p.add_argument("--project", help="the project (default: the calling session's)")
    p.add_argument("--unit", help="the unit whose brief the PR answers")
    p.add_argument("--background", action="store_true", help="start it detached and return")
    p.add_argument("--head", action="store_true", help=argparse.SUPPRESS)   # the head review alone (start())
    p.add_argument("--tell", help=argparse.SUPPRESS)                        # --background's caller
    p.set_defaults(func=run)


LINES = {"off": "head review is off (review.build: false)",
         "already": "PR #{pr} @{h7} was already reviewed",
         "busy": "PR #{pr} is under review now",
         "skipped": "PR #{pr}: pre-review {pre}; the pre-review path handles it",
         "held": "PR #{pr}: {vendor} usage limit: the head review runs after the hold (orche review --pr {pr} again)",
         "closed": "PR #{pr} is not open ({state})",
         "queued": "queued PR #{pr} — position {position} of {total}"}


def _line(project, pr, res) -> str:
    status = res["status"]
    if status == "no-head":
        return f"{project} has no Build division"
    if status.startswith("pre-"):
        found = "".join(f"\n- {f}" for f in res.get("findings") or [])
        why = f" — {res['error']}" if res.get("error") else ""
        return (f"PR #{pr} @{str(res.get('head', ''))[:7]}: pre-review {status[4:].upper()} by {res.get('model', '-')} "
                f"(round {res.get('round', '-')}){why}{found}\nFix what is right, push, then orche review --pr {pr} again"
                + (f"\nReview: {res['path']}" if res.get("path") else ""))
    if status in LINES:
        return LINES[status].format(pr=pr, h7=str(res.get("head", ""))[:7], pre=res.get("pre"),
                                    vendor=res.get("vendor"), state=res.get("state"),
                                    position=res.get("position"), total=res.get("total"))
    if not res.get("verdict"):       # nothing reviewed and nobody told: say what happened
        return f"PR #{pr}: {res.get('error') or status}"
    return f"PR #{pr}: head review {res['verdict']}; the head was told"


def run(args):
    reg = registry.load()
    key = args.project
    if not key:
        m = roster.Roster(reg).member(roster.env_session())
        key = m.project if m else None
    found = registry.project(reg, key) if key else None
    if found is None:
        return cli.fail(cli.EXIT_NOT_FOUND, f"unknown project: {key}" if key else "name a project: --project <p>")
    project = found[0]
    if args.background:
        caller = roster.env_session() or ""
        argv = ["review", "--pr", str(args.pr), "--project", project] + (["--unit", args.unit] if args.unit else []) + (["--tell", caller] if caller else [])
        procs.spawn_orche(argv, paths.orche_home() / "logs" / "review" / f"{project}-pr{args.pr}.log")
        cli.out(args, {"status": "started"}, f"reviews of PR #{args.pr} started")
        return cli.EXIT_OK
    cfg = config.load()
    try:
        if args.head:
            res = _build.build_review(project, args.pr, args.unit, reg=reg, cfg=cfg)
        else:
            res = _build.for_pr(project, args.pr, args.unit, tell=args.tell, reg=reg, cfg=cfg)
    except Exception as exc:   # noqa: BLE001 — a detached run's log is the only witness
        log.log("hooks", f"buildreview: {project} PR #{args.pr}: {type(exc).__name__}: {exc}")
        return cli.fail(cli.EXIT_BLOCKED, f"review failed: {exc}")
    cli.out(args, res, _line(project, args.pr, res))
    return cli.EXIT_BLOCKED if res["status"] in ("error", "pre-return", "pre-error", "pre-held") else cli.EXIT_OK
