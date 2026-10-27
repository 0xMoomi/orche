"""`orche secret …` — Keymaster: agents get secret NAMES, values go human -> backend -> child env.

request/wait/fill/add/ls/missing/rotate/run/render/share/revoke/redact-transcripts.
No subcommand ever prints a value; `--dry-run` prints backend argv with `<value>`.
"""
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from . import cli, log, registry, roster, units, util
from .secrets import ENVS, SecretError, parse_envs, share, store
from .secrets import dialog

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3
POLL_SECONDS = 1.0
RENDER_WARNING = "warning: {path} holds secret values; agents cannot read it (env-read gate). Do not commit it."


def register(subparsers):
    p = subparsers.add_parser("secret", help="Keymaster: request, store and inject secrets by name")
    sub = p.add_subparsers(dest="secret_cmd", metavar="<action>")
    p.set_defaults(func=lambda a: (p.print_help(sys.stderr), EXIT_USER)[1])

    def add(name, func, help_text, project=True):
        sp = sub.add_parser(name, help=help_text)
        if project:
            sp.add_argument("--project", help="registry project (default: from cwd)")
        sp.set_defaults(func=_guard(func))
        return sp

    sp = add("request", cmd_request, "ask the human for a secret (queue + notification + dialog)")
    sp.add_argument("name")
    sp.add_argument("--env", required=True, help="comma list of dev|preview|prod")
    sp.add_argument("--why", required=True)
    sp.add_argument("--how", default="", help="URL where the human gets the value")
    sp.add_argument("--no-dialog", action="store_true", help="queue only; do not open the dialog")

    sp = add("wait", cmd_wait, "wait until a requested secret is filled (READY / TIMEOUT)")
    sp.add_argument("name")
    sp.add_argument("--timeout", type=float, default=600)

    add("fill", cmd_fill, "run the dialog for every pending request (foreground)")

    sp = add("add", cmd_add, "store a value typed at a hidden prompt")
    sp.add_argument("name")
    sp.add_argument("--env", required=True)

    sp = add("ls", cmd_ls, "list known secret names")
    sp.add_argument("--env")

    sp = add("missing", cmd_missing, "names in dev or .env.example that ENV lacks")
    sp.add_argument("env", choices=ENVS)
    sp.add_argument("--repo", help="directory holding .env.example (default: nearest from cwd)")

    sp = add("rotate", cmd_rotate, "replace a value via the dialog; disables old GCP versions")
    sp.add_argument("name")
    sp.add_argument("--env", help="comma list (default: every env the name is stored in)")
    sp.add_argument("--no-dialog", action="store_true")

    sp = add("run", cmd_run, "run CMD with secrets injected into its environment only")
    sp.add_argument("--env", default="dev", choices=ENVS)
    sp.add_argument("--names", help="comma list added to the .env.example names")
    sp.add_argument("--repo", help="directory holding .env.example (default: nearest from cwd)")
    sp.add_argument("cmd", nargs="...", help="-- CMD [ARGS...]")

    sp = add("render", cmd_render, "write a dotenv file for frameworks that insist on one")
    sp.add_argument("--env", default="dev", choices=ENVS)
    sp.add_argument("--out", default=".env.local", help="a .env* file name (the env-read gate covers it)")
    sp.add_argument("--names")
    sp.add_argument("--repo")
    sp.add_argument("--force", action="store_true",
                    help="replace an existing --out (the old file is kept as .env.bak-<ts>.local, 0600)")

    sp = add("share", cmd_share, "encrypt an env's values for a developer with age")
    sp.add_argument("--to", required=True)
    sp.add_argument("--env", default="dev", choices=ENVS)
    sp.add_argument("--names", help="comma list (default: every name stored for the env)")
    sp.add_argument("--out", help="output file (default: .env.<env>.age)")

    sp = add("revoke", cmd_revoke, "rotate every secret a developer received", project=False)
    sp.add_argument("who")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--no-dialog", action="store_true")

    sp = add("redact-transcripts", cmd_redact, "back up then redact secrets in transcripts", project=False)
    sp.add_argument("--root", default=str(Path("~/.claude/projects")))


def _guard(func):
    def wrapped(args):
        try:
            return func(args)
        except SecretError as exc:
            print(f"orche secret: {exc}", file=sys.stderr)
            return exc.code
        except util.JsonError as exc:
            print(f"orche secret: {exc}", file=sys.stderr)
            return EXIT_USER
    return wrapped


def _context(args):
    reg = registry.load()
    key = store.resolve_project(reg, getattr(args, "project", None))
    return reg, key, store.project_cfg(reg, key)


def _names_arg(text) -> list:
    return [store.check_name(n.strip()) for n in str(text or "").split(",") if n.strip()]


def _example_names(repo) -> list:
    path = store.find_env_example(repo) if repo is None else Path(repo) / ".env.example"
    return store.env_example_names(path) if path and path.is_file() else []


def _plan_lines(ex):
    return [f"would run: {line}" for line in ex.planned]


# ---------------------------------------------------------------- request / wait / fill


def cmd_request(args):
    _, key, _ = _context(args)
    name, envs = store.check_name(args.name), parse_envs(args.env)
    ex = store.Exec(args.dry_run)
    rid, req, created = store.new_request(key, name, envs, args.why, args.how, roster.actor(),
                                          dry_run=args.dry_run)
    store.notify(ex, f"{name} needed for {key} ({','.join(req['envs'])}): {req['why']}", key)
    spawned = False
    if not args.dry_run:
        store.audit("request", key, name, ",".join(req["envs"]), "-", req["requested_by"], rid)
        if not args.no_dialog:
            spawned = store.spawn_dialog() is not None
    data = {"id": rid, "created": created, "request": req, "dialog": spawned, "dry_run": args.dry_run,
            "planned": ex.planned}
    lines = (_plan_lines(ex) + [f"would request {rid}"]) if args.dry_run else [f"requested {rid}"]
    cli.out(args, data, lines)
    return EXIT_OK


def cmd_wait(args):
    reg = registry.load()
    key = store.resolve_project(reg, args.project) if args.project else None
    name = store.check_name(args.name)
    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        reqs = store.list_requests(project=key, name=name)
        if not reqs:
            print(f"orche secret: no request for {name}", file=sys.stderr)
            return EXIT_NOT_FOUND
        if reqs[-1][1].get("status") == "filled":
            cli.out(args, {"name": name, "id": reqs[-1][0], "status": "READY"}, ["READY"])
            return EXIT_OK
        if time.monotonic() >= deadline:
            cli.out(args, {"name": name, "id": reqs[-1][0], "status": "TIMEOUT"}, ["TIMEOUT"])
            return EXIT_NOT_FOUND
        time.sleep(POLL_SECONDS)


def _run_dialog(args, project=None, only=None):
    ex = store.Exec(args.dry_run)
    with store.keymaster_env():
        summary = dialog.run_loop(ex=ex, project=project, only=only)
    return ex, summary


def cmd_fill(args):
    project = None
    if args.project:
        project = store.resolve_project(registry.load(), args.project)
    ex, summary = _run_dialog(args, project)
    lines = _plan_lines(ex) + [f"{outcome}: {' '.join(ids)}" for outcome, ids in sorted(summary.items())]
    cli.out(args, {"summary": summary, "dry_run": args.dry_run, "planned": ex.planned}, lines or ["nothing pending"])
    return EXIT_OK if not summary.get("failed") else EXIT_USER


# ---------------------------------------------------------------- add / ls / missing / rotate


def cmd_add(args):
    _, key, cfg = _context(args)
    name, envs = store.check_name(args.name), parse_envs(args.env)
    ex = store.Exec(args.dry_run)
    if args.dry_run:
        value = store.VALUE_PLACEHOLDER
    else:
        value = (store.GETPASS(f"{name} for {key} ({','.join(envs)}), hidden: ") or "").strip()
        ok, reason = store.validate_format(name, value)
        if not ok:
            raise SecretError(f"not stored: {reason}")
    stored = {env: store.put(ex, cfg, name, env, value) for env in envs}
    value = None
    lines = _plan_lines(ex) if args.dry_run else \
        [f"stored {name} ({env}) -> {', '.join(b)}" for env, b in stored.items()]
    cli.out(args, {"name": name, "stored": stored, "dry_run": args.dry_run, "planned": ex.planned}, lines)
    return EXIT_OK


def cmd_ls(args):
    _, key, _ = _context(args)
    env = parse_envs(args.env)[0] if args.env else None
    names = store.known_names(key, env)
    cli.out(args, {"project": key, "env": env, "names": names},
         [f"{n}\t{','.join(e)}" for n, e in names.items()] or ["no secrets"])
    return EXIT_OK


def cmd_missing(args):
    _, key, _ = _context(args)
    reference = set(store.known_names(key, "dev")) | set(_example_names(args.repo))
    missing = sorted(reference - set(store.known_names(key, args.env)))
    cli.out(args, {"project": key, "env": args.env, "missing": missing}, missing)
    return EXIT_OK


def rotate_requests(args, key, items, why):
    """Enqueue rotate requests [(name, envs)]; returns [(id, request)]."""
    out = []
    for name, envs in items:
        rid, req, _ = store.new_request(key, name, envs, why, "", roster.actor(), mode="rotate",
                                        dry_run=args.dry_run)
        if not args.dry_run:
            store.audit("rotate-request", key, name, ",".join(envs), "-", None, rid)
        out.append((rid, req))
    return out


def cmd_rotate(args):
    _, key, _ = _context(args)
    name = store.check_name(args.name)
    envs = parse_envs(args.env) if args.env else store.known_names(key).get(name)
    if not envs:
        raise SecretError(f"{name} is not stored for {key}; pass --env", code=EXIT_NOT_FOUND)
    reqs = rotate_requests(args, key, [(name, envs)], "rotate")
    ex, summary = _rotate(args, key, reqs)
    ids = [rid for rid, _ in reqs]
    lines = _plan_lines(ex) + [f"rotate requested {rid}" for rid in ids]
    lines += [f"{outcome}: {' '.join(v)}" for outcome, v in sorted(summary.items())]
    cli.out(args, {"requests": ids, "summary": summary, "dry_run": args.dry_run, "planned": ex.planned}, lines)
    return EXIT_USER if summary.get("failed") else EXIT_OK


def _rotate(args, key, reqs):
    """Plan (dry-run), run the dialog for `reqs`, or leave them queued (--no-dialog). -> (ex, summary)."""
    summary, ex = {}, store.Exec(args.dry_run)
    if args.dry_run:
        reg = registry.load()
        for rid, req in reqs:
            dialog.handle(rid, req, None, ex, reg)
    elif reqs and not args.no_dialog:
        ex, summary = _run_dialog(args, key, {rid for rid, _ in reqs})
    return ex, summary


# ---------------------------------------------------------------- run / render


def _resolve_values(args, cfg, ex):
    names = _example_names(args.repo)
    names += [n for n in _names_arg(args.names) if n not in names]
    if not names:
        raise SecretError("no names: add keys to .env.example or pass --names A,B")
    values, missing = {}, []
    with store.keymaster_env():
        for name in names:
            try:
                values[name] = store.get(ex, cfg, name, args.env)
            except SecretError:
                missing.append(name)
    if missing:
        raise SecretError(f"missing for {cfg['project']} ({args.env}): {', '.join(missing)}; "
                          f"request them with `orche secret request`", code=EXIT_NOT_FOUND)
    return names, values


def cmd_run(args):
    cmd = list(args.cmd)
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        raise SecretError("usage: orche secret run [--env dev] -- CMD [ARGS...]")
    _, key, cfg = _context(args)
    ex = store.Exec(args.dry_run)
    names, values = _resolve_values(args, cfg, ex)
    if args.dry_run:
        cli.out(args, {"names": names, "cmd": cmd, "planned": ex.planned, "dry_run": True},
             _plan_lines(ex) + [f"would exec: {shlex.join(cmd)} with {','.join(names)}"])
        return EXIT_OK
    env = dict(os.environ)
    env.pop("ORCHE_KEYMASTER", None)  # the child is not the keymaster
    env.update(values)
    values = None
    store.audit("run", key, ",".join(names), args.env, cfg.get("backend"), None, cmd[0])
    log.log("keymaster", f"run {key}/{args.env} names={','.join(names)} cmd={cmd[0]}")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        store.EXEC(cmd[0], cmd, env)
    except OSError as exc:
        raise SecretError(f"cannot exec {cmd[0]}: {exc.strerror}", code=EXIT_NOT_FOUND) from None
    return EXIT_OK


RENDER_NAME = re.compile(r"^\.env(\..+)?$", re.IGNORECASE)


def render_name_ok(out: Path) -> bool:
    """`.env` / `.env.<x>` (case-insensitive, not .env.example) for both the path and its target,
    so the env-read gate covers every file a render writes."""
    names = {out.name, Path(os.path.realpath(out)).name}
    return all(RENDER_NAME.match(n) and n.lower() != ".env.example" for n in names)


BACKUP_GLOB = ".env.bak-*.local"   # matched by the common `.env*.local` ignore and the env-read gate


def _backup(out: Path) -> Path:
    """Byte-exact 0600 copy at `<dir>/.env.bak-<ts>.local`; the pattern goes into info/exclude."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    bak, n = out.parent / f".env.bak-{stamp}.local", 1
    while bak.exists() or bak.is_symlink():
        n += 1
        bak = out.parent / f".env.bak-{stamp}-{n}.local"
    fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh, open(out, "rb") as src:
        shutil.copyfileobj(src, fh)
        os.fchmod(fh.fileno(), 0o600)
    from .hr import onboarding
    onboarding.exclude_lines(out.parent, (BACKUP_GLOB,))
    return bak


def cmd_render(args):
    out = Path(args.out)
    if store.is_env_example(out):
        raise SecretError("--out must not be .env.example (names only, committed)", code=EXIT_BLOCKED)
    if not render_name_ok(out):
        raise SecretError(f"--out must be a .env or .env.<name> file (got {out.name!r}); "
                          "other names escape the env-read gate", code=EXIT_BLOCKED)
    exists = out.exists() or out.is_symlink()
    if exists and not args.force:
        raise SecretError(f"{out} exists; refusing to overwrite (pass --force to replace it; "
                          f"the old file is kept as a 0600 {BACKUP_GLOB})", code=EXIT_BLOCKED)
    _, key, cfg = _context(args)
    ex = store.Exec(args.dry_run)
    names, values = _resolve_values(args, cfg, ex)
    if args.dry_run:
        backup = [f"would back up {out} to {out.parent / '.env.bak-<ts>.local'}"] if exists else []
        cli.out(args, {"out": str(out), "names": names, "planned": ex.planned, "dry_run": True},
             _plan_lines(ex) + backup + [f"would write {out} with {','.join(names)}"])
        return EXIT_OK
    if exists:
        bak = _backup(out)
        print(f"backed up {out} to {bak}", file=sys.stderr)
    util.atomic_write(out, store.dotenv_text(values), mode=0o600)
    values = None
    store.audit("render", key, ",".join(names), args.env, cfg.get("backend"), None, str(out))
    cli.out(args, {"out": str(out), "names": names}, [f"wrote {out} ({len(names)} names)"])
    print(RENDER_WARNING.format(path=out), file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------- share / revoke / redact


def cmd_share(args):
    _, key, cfg = _context(args)
    names = _names_arg(args.names) or list(store.known_names(key, args.env))
    out = Path(args.out or f".env.{args.env}.age")
    ex = store.Exec(args.dry_run)
    entry = share.share(ex, cfg, args.to, args.env, names, out)
    lines = _plan_lines(ex) if args.dry_run else [f"shared {','.join(names)} ({args.env}) with {entry['to']} -> {out}"]
    cli.out(args, {**entry, "dry_run": args.dry_run, "planned": ex.planned}, lines)
    return EXIT_OK


def _rotated_since(project, name, envs, since) -> bool:
    """A rotate request for `name` covering every env in `envs` was filled at or after `since`."""
    start = units.parse_iso(since)
    for _, req in store.list_requests(status="filled", project=project, name=name):
        at = units.parse_iso(req.get("filled_at"))
        if (req.get("mode") == "rotate" and start and at and at >= start
                and set(envs) <= set(req.get("envs") or [])):
            return True
    return False


def cmd_revoke(args):
    email, items = share.received(args.who)
    if not items:
        print(f"orche secret: nothing to revoke for {args.who}", file=sys.stderr)
        return EXIT_NOT_FOUND
    print(f"{email} received: " + ", ".join(f"{p}/{n} ({','.join(e)})" for p, n, e, _ in items), file=sys.stderr)
    if not args.yes and not args.dry_run:
        try:
            answer = store.CONFIRM(f"rotate these {len(items)} secrets? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return EXIT_USER
    by_project = {}
    for project, name, envs, since in items:
        by_project.setdefault(project, []).append((name, envs, since))
    lines, planned, ids, rotated_all, pending_all, failed = [], [], [], [], [], False
    for project, triples in by_project.items():
        rotated = [n for n, envs, since in triples if since and _rotated_since(project, n, envs, since)]
        todo = [(n, envs) for n, envs, _ in triples if n not in rotated]
        reqs = rotate_requests(args, project, todo, f"revoke {email}") if todo else []
        ex, summary = _rotate(args, project, reqs)
        failed = failed or bool(summary.get("failed"))
        planned += ex.planned
        ids += [rid for rid, _ in reqs]
        pending = []
        for rid, req in reqs:
            filled = not args.dry_run and (store.get_request(rid) or {}).get("status") == "filled"
            (rotated if filled else pending).append(req["name"])
        if not args.dry_run:
            share.record_revoke(email, project, rotated, pending)
        rotated_all += [f"{project}/{n}" for n in rotated]
        pending_all += [f"{project}/{n}" for n in pending]
    lines = [f"would run: {ln}" for ln in planned]
    lines += [f"rotate requested {rid}" for rid in ids]
    lines.append(f"rotated: {', '.join(rotated_all) or '-'}")
    lines.append(f"pending: {', '.join(pending_all) or '-'}")
    if pending_all and not args.dry_run:
        lines.append(f"pending names stay shared; run `orche secret fill`, then `orche secret revoke {args.who}` again")
    cli.out(args, {"to": email, "requests": ids, "rotated": rotated_all, "pending": pending_all,
                "dry_run": args.dry_run, "planned": planned}, lines)
    return EXIT_USER if failed else EXIT_OK


def cmd_redact(args):
    ex = store.Exec(args.dry_run)
    report = share.redact_transcripts(ex, args.root)
    lines = _plan_lines(ex) + [f"{'would redact' if args.dry_run else 'redacted'} {report['changed']}/"
                               f"{report['files']} files"]
    lines += [f"  {name}: {n}" for name, n in sorted(report["counts"].items())]
    if report["errors"]:
        lines.append(f"  errors (file left unchanged, see keymaster log): {report['errors']}")
    if report["unparsed"]:
        lines.append(f"  unparsed (left unchanged): {report['unparsed']} lines")
    if not args.dry_run:
        lines.append(f"backup: {report['backup']} (mode 0600, holds the unredacted transcripts; "
                     f"kept indefinitely until you delete it)")
    cli.out(args, {**report, "dry_run": args.dry_run, "planned": ex.planned}, lines)
    return EXIT_USER if report["errors"] else EXIT_OK
