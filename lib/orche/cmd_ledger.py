"""`orche ledger record|check|summary|export|grade` — verification ledger CLI (logic in ledger.py)."""
import json
import os
import sys
from pathlib import Path

from . import ledger, registry, roster, shell, units

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3


class _Exit(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def register(subparsers):
    p = subparsers.add_parser("ledger", help="verification ledger (record/check/summary/export/grade)")
    sub = p.add_subparsers(dest="ledger_cmd", metavar="<action>", parser_class=type(p))
    p.set_defaults(func=lambda args: (p.print_help(sys.stderr), EXIT_USER)[1])

    r = sub.add_parser("record", help="append a verdict row")
    r.add_argument("--proj")
    r.add_argument("--pr", required=True, help="PR number or 'none'")
    r.add_argument("--sha", required=True)
    r.add_argument("--verdict", required=True, choices=ledger.VERDICTS)
    r.add_argument("--evidence", required=True, help="path or URL of the verification output")
    r.add_argument("--verifier", required=True, help="session or script that verified")
    r.add_argument("--producer", default=None, help="session that wrote the code (default $ORCHE_SESSION)")
    r.set_defaults(func=_wrap(cmd_record))

    c = sub.add_parser("check", help="is a sha verified?")
    c.add_argument("--sha", help="default: git rev-parse HEAD in cwd")
    c.add_argument("--pr")
    c.add_argument("--min", default="type-check-only", choices=ledger.ORDER)
    c.add_argument("--proj")
    c.set_defaults(func=_wrap(cmd_check))

    s = sub.add_parser("summary", help="verdict counts, unverified feature heads, last 10 rows")
    s.add_argument("--proj")
    s.set_defaults(func=_wrap(cmd_summary))

    e = sub.add_parser("export", help="write <repo>/docs/LEDGER.tsv from the canonical ledger")
    e.add_argument("--repo", help="default: git toplevel of cwd")
    e.add_argument("--proj")
    e.set_defaults(func=_wrap(cmd_export))

    g = sub.add_parser("grade", help="prepare a separate-context grader package (max 3 per unit)")
    g.add_argument("--unit", required=True)
    g.add_argument("--rubric", required=True)
    g.add_argument("--diff", required=True, metavar="BASE..HEAD")
    g.add_argument("--proj", help="run git in this project's checkout instead of cwd")
    g.set_defaults(func=_wrap(cmd_grade))


def _wrap(handler):
    def run(args):
        try:
            return handler(args)
        except _Exit as exc:
            print(f"orche: {exc}", file=sys.stderr)
            return exc.code
    return run


def _emit(args, data, lines):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for line in lines:
            print(line)


def _project(args, reg=None):
    reg = reg if reg is not None else registry.load()
    if args.proj:
        found = registry.project(reg, args.proj)
        if found is None:
            raise _Exit(EXIT_NOT_FOUND, f"unknown project: {args.proj}")
        return found[0], reg
    key = roster.Roster(reg).place(os.getcwd()).project
    if key is None:
        raise _Exit(EXIT_USER, "cannot resolve the project from cwd; pass --proj")
    return key, reg


def _git(argv, cwd=None):
    res = shell.git(cwd or ".", *argv, timeout=20)
    if not res.ok:
        raise _Exit(EXIT_USER, f"git {' '.join(argv)} failed: {res.err.strip() or res.code}")
    return res.out


def cmd_record(args):
    proj, _ = _project(args)
    producer = args.producer if args.producer is not None else roster.env_session()
    try:
        row = ledger.make_row(args.pr, args.sha, args.verdict, args.evidence, args.verifier, producer)
    except ledger.WriterIsJudge as exc:
        raise _Exit(EXIT_BLOCKED, f"refused: {exc}") from None
    except ledger.LedgerError as exc:
        raise _Exit(EXIT_USER, str(exc)) from None
    if not args.dry_run:
        ledger.append(proj, row)
    _emit(args, {"project": proj, "path": str(ledger.path(proj)), "dry_run": bool(args.dry_run), "row": row},
          [ledger.format_row(row)])
    return EXIT_OK


def cmd_check(args):
    proj, _ = _project(args)
    sha = args.sha or _git(["rev-parse", "HEAD"]).strip()
    if not ledger.SHA_RE.fullmatch(sha or ""):
        raise _Exit(EXIT_USER, f"sha must be 7-40 hex characters, got {sha!r}")
    pr = args.pr.lower() if args.pr else None
    try:
        status, row, note = ledger.check(ledger.read(proj), sha, pr, args.min)
    except ledger.Ambiguous as exc:
        _emit(args, {"status": "AMBIGUOUS", "sha": sha, "candidates": exc.candidates}, [str(exc)])
        return EXIT_NOT_FOUND
    line = status if row is None else f"{status} {row['verdict']}"
    _emit(args, {"status": status, "project": proj, "sha": sha, "pr": pr, "min": args.min,
                 "row": row, "note": note}, [line] + ([f"  ({note})"] if note else []))
    return EXIT_OK if status == "VERIFIED" else EXIT_NOT_FOUND


def _feat_heads(reg, proj):
    """[(branch, sha)] for every division's worker branches (any naming), resolved in that division's
    worktree, or None when git fails."""
    heads = []
    for div in (reg["projects"][proj].get("divisions") or {}).values():
        where = units.worktree_path((div or {}).get("worktree", "")) or None
        if where and not Path(where).is_dir():
            where = None
        for feat in (div or {}).get("features") or []:
            branch = feat.get("branch")
            if not branch:
                continue
            res = shell.git(where or ".", "rev-parse", "--verify", "-q", branch, timeout=20)
            if not res.ok:
                return None
            heads.append((branch, res.out.strip()))
    return heads


def cmd_summary(args):
    proj, reg = _project(args)
    rows = ledger.read(proj)
    counts = ledger.counts(rows)
    heads = _feat_heads(reg, proj)
    unverified = None if heads is None else [
        {"branch": b, "sha": s} for b, s in heads
        if not (ledger.row_for_sha(proj, s) or {}).get("verdict") in ledger.ORDER]
    last = rows[-10:]
    lines = [f"ledger {proj}: {len(rows)} rows ({ledger.path(proj)})"]
    lines += [f"  {v:<20} {n}" for v, n in counts.items()]
    if unverified is None:
        lines.append("unverified feature heads: (git rev-parse failed)")
    else:
        lines.append(f"unverified feature heads: {len(unverified)}")
        lines += [f"  {u['branch']} {u['sha'][:12]}" for u in unverified]
    lines.append("last rows:")
    lines += [f"  {ledger.format_row(r)}" for r in last] or ["  (none)"]
    _emit(args, {"project": proj, "rows": len(rows), "counts": counts,
                 "unverified_heads": unverified, "last": last}, lines)
    return EXIT_OK


def cmd_export(args):
    repo = Path(args.repo).expanduser() if args.repo else Path(_git(["rev-parse", "--show-toplevel"]).strip())
    if not repo.is_dir():
        raise _Exit(EXIT_NOT_FOUND, f"repo not found: {repo}")
    reg = registry.load()
    if not args.proj:
        r = roster.Roster(reg)
        key = r.place(repo).project or r.place(os.getcwd()).project
        if key is None:
            raise _Exit(EXIT_USER, "cannot resolve the project; pass --proj")
        args.proj = key
    proj, _ = _project(args, reg)
    target = repo / "docs" / "LEDGER.tsv"
    count = len(ledger.read(proj))
    if not args.dry_run:
        target, count = ledger.export(proj, repo)
    _emit(args, {"project": proj, "path": str(target), "rows": count, "dry_run": bool(args.dry_run)},
          [f"{count} rows -> {target}" + (" (dry run)" if args.dry_run else "")])
    return EXIT_OK


def cmd_grade(args):
    try:
        base, head = ledger.parse_range(args.diff)
        n, state = ledger.next_grader_iteration(args.unit)
    except ledger.GraderLimit as exc:
        raise _Exit(EXIT_BLOCKED, f"{exc} for {args.unit} ({ledger.MAX_GRADER_ITERATIONS}/"
                                  f"{ledger.MAX_GRADER_ITERATIONS}); mark verifier-failed and "
                                  "send an urgent pointer to Main") from None
    except ledger.LedgerError as exc:
        raise _Exit(EXIT_USER, str(exc)) from None
    try:
        rubric = Path(args.rubric).expanduser().read_text(encoding="utf-8")
    except OSError:
        raise _Exit(EXIT_NOT_FOUND, f"rubric not found: {args.rubric}") from None
    cwd = None
    if args.proj:
        proj, reg = _project(args)
        cwd = units.project_root(reg, proj)
    rng = f"{base}..{head}"
    stat = _git(["diff", "--stat", rng, "--"], cwd)
    files = [f for f in _git(["diff", "--name-only", rng, "--"], cwd).splitlines() if f.strip()]
    package = ledger.grader_dir() / f"{args.unit}-{n}.md"
    if not args.dry_run:
        package = ledger.save_grader_iteration(state, n, rng, ledger.grader_package(
            args.unit, n, rng, rubric, stat, files))
    _emit(args, {"unit": args.unit, "iteration": n, "max": ledger.MAX_GRADER_ITERATIONS,
                 "package": str(package), "files": files, "dry_run": bool(args.dry_run)},
          [f"iteration {n}/{ledger.MAX_GRADER_ITERATIONS}", str(package)])
    return EXIT_OK
