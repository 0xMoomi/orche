"""`orche queue` — the merge queue (design §4): status, add, drop, pause, resume, approve.

  orche queue [<p>]                               status
  orche queue add <unit> [--first] [--accept WHY] queue the unit's open PR at its worktree's HEAD
  orche queue add --pr N --project <p>            queue a PR by number
  orche queue drop <p> <id|#N> [--reason R]
  orche queue pause <p> [--reason R] · resume <p>
  orche queue approve <p> (#N ...|--all)          land: owner projects, Main only
  orche queue run <p>                             the integrator (the driver starts it detached)
"""
import re
import shlex

from . import brief, cli, config, inbox, ledger, publish, queue, registry, roster, shell, units, util
from .queue import facts, repo, store

ACTIONS = ("add", "drop", "pause", "resume", "approve", "run", "status")


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def register(subparsers):
    p = subparsers.add_parser("queue", help="the merge queue: status, add, drop, pause, resume, approve",
                              description=__doc__.split("\n", 1)[1])
    p.add_argument("words", nargs="*", metavar="[action] args", help=f"one of {', '.join(ACTIONS)}, or a project")
    p.add_argument("--first", action="store_true", help="add: put it at the front (after a testing entry)")
    p.add_argument("--accept", metavar="WHY", help="add: accept a held/capped pre-review or a [skip ci] head")
    p.add_argument("--pr", type=int, help="add: the PR number (with --project)")
    p.add_argument("--project", help="add --pr: the project")
    p.add_argument("--reason", help="drop/pause: why")
    p.add_argument("--all", action="store_true", help="approve: every waiting entry")
    p.set_defaults(func=run)


def run(args):
    words = list(args.words)
    action = words.pop(0) if words and words[0] in ACTIONS else "status"
    try:
        return globals()[f"cmd_{action}"](args, words)
    except Refused as exc:
        return cli.fail(exc.code, str(exc))
    except repo.GhError as exc:
        return cli.fail(cli.EXIT_BLOCKED, str(exc))


# ---- helpers ---------------------------------------------------------------------------------

def _project(reg, key):
    found = registry.project(reg, key) if key else None
    if found is None:
        raise Refused(cli.EXIT_NOT_FOUND, f"unknown project: {key}" if key else "name a project")
    return found[0]


def _caller(reg, project, *, main_only=False):
    """The calling session's Member if it is the project's Main (or Build unless main_only)."""
    m = roster.Roster(reg).member(roster.env_session())
    ok = m is not None and m.project == project and (
        m.kind == "main" or (not main_only and m.kind == "div" and m.name == "build"))
    return m if ok else None


def _verify_cmds(text) -> list:
    return [c.strip() for c in re.findall(r"`([^`]+)`", str(text or "")) if c.strip()]


def _has_gate(where, head, verify) -> bool:
    if repo.tree_mode(where, head, "scripts/verify/ci.sh") == "100755":
        return True
    for cmd in verify:
        try:
            first = shlex.split(cmd)[0]
        except (ValueError, IndexError):
            continue
        if repo.tree_mode(where, head, first.removeprefix("./")):
            return True
    return False


def _review_check(cfg, project, pr, head, unit, accept):
    """§4.3 step 6; returns the accepted reason or None."""
    rv = cfg.get("review") or {}
    mode, rounds = rv.get("cross_vendor", "required"), int(rv.get("rounds", 2))
    if mode == "off":
        return None
    h7 = head[:7]
    row = ledger.review_for(project, pr, head, "pre")
    if row is None:
        if mode == "optional":
            return None
        raise Refused(cli.EXIT_BLOCKED, f"no pre-review for {h7}: `orche review --pr {pr}`")
    verdict = row["verdict"]
    if verdict == "pass" or (mode == "optional" and verdict == "skipped"):
        return None
    try:
        rnd = int(row.get("round") or 1)
    except ValueError:
        rnd = 1
    capped = verdict in ("error", "held", "skipped") or (verdict == "return" and rnd >= rounds)
    if capped and accept:
        return accept
    if capped:
        raise Refused(cli.EXIT_BLOCKED, f"pre-review of {h7} is {verdict} (round {rnd} of {rounds}): "
                                        f're-review, or queue with --accept "<why>"')
    raise Refused(cli.EXIT_BLOCKED, f"pre-review of {h7} returned it (round {rnd} of {rounds}): fix and review again")


def _checks(args, cfg, project, where, *, pr, head, base, unit, verify):
    """§4.3 steps 5-8 (ledger, review, gate, [skip ci]); returns the accepted reasons."""
    h7, accepted = head[:7], []
    row = ledger.row_for_sha(project, head)
    if not row or row.get("verdict") not in ledger.ORDER:
        raise Refused(cli.EXIT_BLOCKED, f"no ledger row for {h7}")
    why = _review_check(cfg, project, pr, head, unit, args.accept)
    if why:
        accepted.append(why)
    if not _has_gate(where, head, verify):
        raise Refused(cli.EXIT_BLOCKED, "no gate: add scripts/verify/ci.sh (Appendix A.13) or a committed VERIFY script")
    docs = queue.settings(cfg, project)["docs_paths"]
    if repo.SKIP_CI.search(repo.message(where, head)):
        base_ref = f"origin/{base}" if repo.git(where, "rev-parse", "--verify", "-q", f"origin/{base}") else base
        code = [p for p, _a, _d in repo.changed(where, base_ref, head) if not facts.is_docs(p, docs)]
        if code and not args.accept:
            raise Refused(cli.EXIT_BLOCKED, f"head {h7} carries [skip ci] on a code change (STANDING 4): "
                                            "add a commit without it")
        if code:
            accepted.append(args.accept)
    return accepted


def _pr_checkout(reg, project, pr, head, base):
    """The queue's clone (ORCHE_HOME/queue/<p>/checkout) holding PR #pr's head for the §4.3 checks: a PR
    queued by number has its head on origin, not in the main checkout, so it is fetched there (with the
    base) when missing. Refuses with the reason when the clone cannot be made or the fetch fails."""
    from .queue import integrator
    try:
        where = integrator.checkout(project, reg)
    except integrator.Stop as exc:
        raise Refused(cli.EXIT_BLOCKED, f"PR #{pr}: no queue checkout: {exc}") from None
    if repo.git(where, "cat-file", "-e", f"{head}^{{commit}}") is not None:
        return where
    # never FETCH_HEAD: the integrator may be reading its own fetch in this clone right now
    res = shell.run(["git", "-C", str(where), "fetch", "--no-tags", "--no-write-fetch-head", "origin", head,
                     f"+refs/heads/{base}:refs/remotes/origin/{base}"], timeout=integrator.FETCH_S)
    if not res.ok or repo.git(where, "cat-file", "-e", f"{head}^{{commit}}") is None:
        why = next((ln.strip() for ln in (res.err or res.out).splitlines() if ln.strip()), f"exit {res.code}")
        raise Refused(cli.EXIT_BLOCKED, f"cannot fetch PR #{pr}'s head {head[:7]} from origin: {why[:160]}")
    return where


def _ask_owner(reg, project, entry, frm):
    main = roster.Roster(reg).main(project)
    if main is None:
        return
    name = entry.get("unit") or entry["branch"]
    text = (f"PR #{entry['pr']} ({name}) passed both reviews and waits for the owner's word to land "
            f"(land: owner): orche queue approve {project} #{entry['pr']}")
    inbox.post(inbox.make(entry.get("unit") or f"pr{entry['pr']}", frm, main.session, "question",
                          summary=text, via="queue"))


# ---- actions ---------------------------------------------------------------------------------

def cmd_add(args, words):
    reg, cfg = registry.load(), config.load()
    session = roster.env_session()
    if args.pr is not None:
        if words:
            raise Refused(cli.EXIT_USER, "add --pr N --project <p> takes no unit")
        project = _project(reg, args.project)
        if not _caller(reg, project):
            raise Refused(cli.EXIT_BLOCKED, "only Build or Main may queue")
        root = units.project_root(reg, project)
        base = publish.base(root, cfg)
        if not base:
            raise Refused(cli.EXIT_BLOCKED, publish.no_base(root))
        try:
            view = repo.pr_view(root, args.pr)
        except repo.GhError:
            raise Refused(cli.EXIT_NOT_FOUND, f"unknown PR #{args.pr}") from None
        if view.get("state") not in (None, "OPEN") or view.get("baseRefName") != base:
            raise Refused(cli.EXIT_BLOCKED, f"no open PR #{args.pr} against {base}")
        pr, head, branch = int(view["number"]), view["headRefOid"], view.get("headRefName", "")
        where = _pr_checkout(reg, project, pr, head, base)
        unit, writer, parent, rev, verify = None, "", "", 0, []
    else:
        if len(words) != 1:
            raise Refused(cli.EXIT_USER, "usage: orche queue add <unit> [--first] [--accept WHY]")
        unit = words[0]
        err = brief.unit_id_error(unit)
        if err:
            raise Refused(cli.EXIT_USER, err)
        b = brief.find(reg, unit)
        if b is None or b.rev == 0:
            raise Refused(cli.EXIT_NOT_FOUND, f"unknown unit {unit!r}")
        project = b.project
        if not _caller(reg, project):
            raise Refused(cli.EXIT_BLOCKED, "only Build or Main may queue")
        target = roster.Roster(reg).member(b.to)
        if target is None or not (target.kind == "feat" or (target.kind == "div" and target.name == "build")):
            raise Refused(cli.EXIT_BLOCKED, f"{unit}: target {b.to or '?'} is not a feature agent or Build")
        where = target.worktree
        if where is None or not where.is_dir():
            raise Refused(cli.EXIT_NOT_FOUND, f"{unit}: worktree of {b.to} is missing")
        head, branch = repo.head(where)
        if not head:
            raise Refused(cli.EXIT_NOT_FOUND, f"{unit}: cannot read HEAD of {where}")
        root = units.project_root(reg, project)
        base = publish.base(root, cfg, where)
        if not base:
            raise Refused(cli.EXIT_BLOCKED, publish.no_base(root or where))
        prs = [p for p in repo.open_prs(where, branch) if p.get("baseRefName") == base]
        if len(prs) != 1:
            raise Refused(cli.EXIT_BLOCKED, f"no open PR for {branch}")
        pr = int(prs[0]["number"])
        if prs[0].get("headRefOid") != head:
            raise Refused(cli.EXIT_BLOCKED, f"PR #{pr}'s head is {str(prs[0].get('headRefOid'))[:7]}, the "
                                            f"worktree's is {head[:7]}: report done again to publish")
        writer, parent, rev, verify = b.to, b.parent, b.rev, _verify_cmds(b.fields.get("VERIFY"))
    accepted = _checks(args, cfg, project, where, pr=pr, head=head, base=base, unit=unit, verify=verify)
    owner = queue.land_mode(cfg, project) == "owner"
    main = roster.Roster(reg).main(project)
    owner_ok = (not owner) or queue.approved_in_log(project, pr, main.session if main else None)
    by = session or "auto"
    entry = {"id": queue.entry_id(pr, head), "pr": pr, "head": head, "branch": branch, "unit": unit,
             "writer": writer, "parent": parent, "rev": rev, "verify": verify,
             "approved_by": f"{by} (review)", "accept": "; ".join(accepted) or None, "first": bool(args.first),
             "owner_ok": owner_ok, "at": None, "state": "waiting", "restarts": 0, "drop": False, "test": None}
    if args.dry_run:
        tier = brief.find(reg, unit).tier if unit else "standard"
        scope = brief.find(reg, unit).fields.get("SCOPE", "") if unit else ""
        f = facts.gather(where, project=project, pr=pr, head=head, base=base, tier=tier, scope=scope,
                         docs_paths=queue.settings(cfg, project)["docs_paths"], entry_id=entry["id"])
        cli.out(args, {"entry": entry, "facts": {k: v for k, v in f.items()}}, facts.lines(f))
        return cli.EXIT_OK
    entry["at"] = util.now_iso()
    res = queue.add(project, entry, first=args.first, by=by, base=base)
    if res["status"] == "already":
        cli.out(args, res, f"already queued, position {res['position']}")
        return cli.EXIT_OK
    if not owner_ok:
        _ask_owner(reg, project, entry, by)
    started = queue.start(project) or queue.running(project)
    line = (f"queued PR #{pr} ({branch} @ {head[:7]}) — position {res['position']} of {res['total']}; "
            f"integrator {'running' if started else 'idle'}")
    if not owner_ok:
        line += " (waits for the owner's word: land: owner)"
    cli.out(args, res, line)
    return cli.EXIT_OK


def cmd_status(args, words):
    reg = registry.load()
    if len(words) > 1:
        raise Refused(cli.EXIT_USER, "usage: orche queue [<project>]")
    if words:
        keys = [_project(reg, words[0])]
    else:
        m = roster.Roster(reg).member(roster.env_session())
        keys = [m.project] if m else store.projects()
    data, lines, cfg = [], [], config.load()
    for key in keys:
        s = queue.summary(key)
        data.append(s)
        lost_after = queue.settings(cfg, key).get("lost_after_s")
        p50 = s["gate_p50"]
        gate = "gate —" if p50 is None else f"gate p50 {round(p50 / 60)}m (≈ {int(3600 // max(p50, 1))}/h)"
        state = s["state"]
        if state == "paused":
            state = f"paused ({(s['paused'] or {}).get('reason') or 'no reason'})"
        elif state == "closed":
            state = f"closed ({queue.reopen(key, s['closed'])})"
        await_ = f" ({s['await']} await the owner)" if s["await"] else ""
        lines.append(f"queue {key}: {s['waiting']} waiting{await_} · {s['testing']} testing · "
                     f"{s['merged_today']} merged today · {s['returned_today']} returned today · {state} · {gate}")
        for i, e in enumerate(s["entries"], 1):
            name = (e.get("branch") or "").rsplit("/", 1)[-1] or "-"
            if queue.landed(e):
                what = "merged on GitHub, its proof pending (the next pass checks it)"
            elif e.get("state") == "testing":
                what = f"testing (lost after {lost_after}s)"     # a dead integrator's: at the next heartbeat
            else:
                what = f"approved {queue.age(e.get('at'))} ago by {e.get('approved_by')}"
            if not e.get("owner_ok"):
                what += " · awaits the owner"
            lines.append(f"  {i}  PR #{e['pr']} {name} {e['head'][:7]} — {what}")
    if not lines:
        lines = ["no queues yet"]
    cli.out(args, data, lines)
    return cli.EXIT_OK


def _gate(words, usage, reg, *, main_only=False, n=1):
    if len(words) < n:
        raise Refused(cli.EXIT_USER, usage)
    project = _project(reg, words[0])
    m = _caller(reg, project, main_only=main_only)
    if m is None:
        raise Refused(cli.EXIT_BLOCKED, "only Main approves landing in a land: owner project; report it to Main"
                      if main_only else "only Build or Main may change the queue")
    return project, m.session


def cmd_drop(args, words):
    reg = registry.load()
    project, by = _gate(words, "usage: orche queue drop <project> <id|#N> [--reason R]", reg, n=2)
    if args.dry_run:
        cli.dry(f"queue drop {project} {words[1]}")
        return cli.EXIT_OK
    e = queue.drop(project, words[1], reason=args.reason, by=by)
    if e is None:
        raise Refused(cli.EXIT_NOT_FOUND, f"no entry {words[1]} in the {project} queue")
    cli.out(args, e, f"dropped PR #{e['pr']} ({e['id']}) from the {project} queue")
    return cli.EXIT_OK


def cmd_pause(args, words):
    reg = registry.load()
    project, by = _gate(words, "usage: orche queue pause <project> [--reason R]", reg)
    if args.dry_run:
        cli.dry(f"queue pause {project}")
        return cli.EXIT_OK
    q = queue.pause(project, by=by, reason=args.reason)
    cli.out(args, q, f"queue {project} paused" + (f": {args.reason}" if args.reason else ""))
    return cli.EXIT_OK


def cmd_resume(args, words):
    reg = registry.load()
    project, by = _gate(words, "usage: orche queue resume <project>", reg)
    if args.dry_run:
        cli.dry(f"queue resume {project}")
        return cli.EXIT_OK
    q = queue.resume(project, by=by)
    queue.start(project)
    cli.out(args, q, f"queue {project} open")
    return cli.EXIT_OK


def cmd_approve(args, words):
    reg, cfg = registry.load(), config.load()
    usage = "usage: orche queue approve <project> (#N ...|--all)"
    project, by = _gate(words, usage, reg, main_only=True)
    refs = None if args.all else words[1:]
    if refs == []:
        raise Refused(cli.EXIT_USER, usage)
    if queue.land_mode(cfg, project) != "owner":
        cli.out(args, {"approved": []}, f"{project} lands automatically (land: auto): nothing to approve")
        return cli.EXIT_OK
    if args.dry_run:
        cli.dry(f"queue approve {project} {'--all' if refs is None else ' '.join(refs)}")
        return cli.EXIT_OK
    done = queue.approve(project, refs, by=by)
    if not done:
        cli.out(args, {"approved": []}, "nothing to approve")
        return cli.EXIT_OK
    queue.start(project)
    cli.out(args, {"approved": done}, f"approved PR {', '.join(f'#{n}' for n in done)} ({project}, land: owner)"
                                      " — the queue tests and lands them in order")
    return cli.EXIT_OK


def cmd_run(args, words):
    """`orche queue run <p>`: the integrator in the foreground (the driver spawns it detached)."""
    project = _project(registry.load(), words[0] if words else None)
    if args.dry_run:
        e = queue.next_entry(store.read(project))
        cli.out(args, {"next": e}, f"next: PR #{e['pr']} @ {e['head'][:7]}" if e else "nothing to test")
        return cli.EXIT_OK
    from .queue import integrator
    res = integrator.loop(project)
    cli.out(args, res, res["lines"])
    return res["code"]
