"""`orche dev add|remove` — developer access to secrets via personal IAM identities.

`add` grants roles/secretmanager.secretAccessor on the env's GCP project and records the
developer (and an optional age public key for `orche secret share`) in shares.json.
`remove` drops the binding and keeps the share history. Developers get dev/preview only, so the
single-GCP-project layout (prod and dev in one project) is refused (exit 3).
"""
import json
import re
import sys

from . import registry
from .secrets import SecretError, backends_gcp, parse_envs, share, store

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3
EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def register(subparsers):
    p = subparsers.add_parser("dev", help="grant/revoke developer access to project secrets")
    sub = p.add_subparsers(dest="dev_cmd", metavar="<action>")
    p.set_defaults(func=lambda a: (p.print_help(sys.stderr), EXIT_USER)[1])
    sp = sub.add_parser("add", help="grant secretAccessor on the env's GCP project")
    sp.add_argument("email")
    sp.add_argument("--project", required=True)
    sp.add_argument("--env", default="dev")
    sp.add_argument("--age-pubkey", help="age recipient for `orche secret share`")
    sp.set_defaults(func=_guard(cmd_add))
    sp = sub.add_parser("remove", help="remove the IAM binding (share history is kept)")
    sp.add_argument("email")
    sp.set_defaults(func=_guard(cmd_remove))


def _guard(func):
    def wrapped(args):
        try:
            return func(args)
        except SecretError as exc:
            print(f"orche dev: {exc}", file=sys.stderr)
            return exc.code
    return wrapped


def _emit(args, data, lines):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("\n".join(lines))


def cmd_add(args):
    if not EMAIL_RX.match(args.email):
        raise SecretError(f"not an email: {args.email!r}")
    env = parse_envs(args.env)[0]
    if env == "prod":
        raise SecretError("developers get dev/preview only; prod is owner + deploy accounts", code=EXIT_BLOCKED)
    reg = registry.load()
    key = store.resolve_project(reg, args.project)
    cfg = store.project_cfg(reg, key)
    ex = store.Exec(args.dry_run)
    lines, gcp_project = [], None
    if cfg.get("backend") == "gcp" and cfg.get("gcp_project"):
        if len({v for v in cfg["gcp_project"].values() if v}) == 1:
            raise SecretError("prod and dev share one GCP project; per-secret IAM is not implemented — "
                              "split projects or grant manually", code=EXIT_BLOCKED)
        gcp_project, _ = backends_gcp.locate(cfg, "X", env)
        backends_gcp.iam(ex, gcp_project, args.email, add=True)
    else:
        lines.append(f"no GCP project configured for {key}; IAM binding skipped")
    rec = share.record_dev(args.email, key, env, gcp_project, args.age_pubkey, dry_run=args.dry_run)
    if not args.dry_run and gcp_project:
        store.audit("iam-add", key, "-", env, "gcp", None, args.email)
    lines = [f"would run: {ln}" for ln in ex.planned] + lines
    lines.append(f"{'would add' if args.dry_run else 'added'} {args.email} to {key} ({env})")
    _emit(args, {"email": args.email, **rec, "dry_run": args.dry_run, "planned": ex.planned}, lines)
    return EXIT_OK


def cmd_remove(args):
    data = share.load()
    rec = data["devs"].get(args.email)
    if rec is None:
        print(f"orche dev: unknown developer {args.email}", file=sys.stderr)
        return EXIT_NOT_FOUND
    ex = store.Exec(args.dry_run)
    if rec.get("gcp_project"):
        backends_gcp.iam(ex, rec["gcp_project"], args.email, add=False)
        if not args.dry_run:
            store.audit("iam-remove", rec.get("project"), "-", rec.get("env"), "gcp", None, args.email)
    share.mark_dev_removed(args.email, dry_run=args.dry_run)
    lines = [f"would run: {ln}" for ln in ex.planned]
    lines.append(f"{'would remove' if args.dry_run else 'removed'} {args.email} (share history kept)")
    _emit(args, {"email": args.email, "dry_run": args.dry_run, "planned": ex.planned}, lines)
    return EXIT_OK
