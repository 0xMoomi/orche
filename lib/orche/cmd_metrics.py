"""`orche metrics [--days N] [--root PATH] [--prices FILE]` — token-economy metrics from transcripts,
a Triage section from ORCHE_HOME/triage/samples.jsonl, a Runner section summing every worker attempt in the
registered projects' run history, also by model and effort, a Rework block (re-runs by cause, killed runs, the
queue's re-tests and returns, the head review's routes, each Main's spend a day; metrics.rework_summary) and
the spend by role (Main, Staff, each division's pane, workers, review, intake, ask, triage; metrics.role_rows).
An unreadable source prints `n/a`, never 0 (metrics.py)."""
import json
import sys

from . import metrics, registry, runs, triage

EXIT_OK, EXIT_USER = 0, 1


def register(subparsers):
    p = subparsers.add_parser("metrics", help="responses, context, cost and wake-up metrics from transcripts")
    p.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    p.add_argument("--root", default=None, help="transcript root (default ~/.claude/projects)")
    p.add_argument("--prices", default=None, metavar="FILE", help="JSON price overrides per model")
    p.set_defaults(func=cmd_metrics)


def cmd_metrics(args):
    if args.days <= 0:
        print("orche: --days must be positive", file=sys.stderr)
        return EXIT_USER
    try:
        prices = metrics.load_prices(args.prices)
    except (OSError, ValueError) as exc:
        print(f"orche: cannot read prices: {exc}", file=sys.stderr)
        return EXIT_USER
    try:
        reg = registry.load()
    except registry.RegistryError:
        reg = None                      # the Runner section and the spend by role are n/a, not zero
    result = metrics.compute(args.root, days=args.days, prices=prices, classify=metrics.classifier(reg))
    result["headless"] = metrics.headless_spend(sorted(reg["projects"]), days=args.days) if reg is not None else None
    if reg is not None:
        runs.reconcile_cli(reg, args)   # a lost run's history line counts in the Runner section
    result["runner"] = (metrics.runner_summary(metrics.attempts(sorted(reg["projects"])), days=args.days)
                        if reg is not None else None)
    result["rework"] = (metrics.rework_summary(reg, days=args.days, named=result.get("named"))
                        if reg is not None else None)
    result["triage"] = metrics.triage_summary(triage.samples_path(), days=args.days)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("\n".join(metrics.render(result)))
    return EXIT_OK
