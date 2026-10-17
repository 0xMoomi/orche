"""`orche metrics [--days N] [--root PATH] [--prices FILE]` — token-economy metrics from transcripts."""
import json
import sys

from . import metrics

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
    result = metrics.compute(args.root, days=args.days, prices=prices)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("\n".join(metrics.render(result)))
    return EXIT_OK
