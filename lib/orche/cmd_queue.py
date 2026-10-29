"""`orche queue` — the merge queue: status, add, drop, pause, resume, approve.

  orche queue [<p>]                               status
  orche queue add <unit> [--first] [--accept WHY] queue the unit's open PR at its worktree's HEAD
                                                  (a team repository: Main only, on the owner's "push";
                                                  the branch is published first when it has no open PR at HEAD)
  orche queue add --pr N --project <p>            queue a PR by number
      [--surface user]                            the change alters what a user sees or does: it lands only
                                                  on the check script's live-ui-verified
      [--safe-because FACT]                       a high-risk or large change: the one fact it is safe because
                                                  of; a test in the PR must exercise it
  orche queue drop <p> <id|#N> [--reason R]
  orche queue pause <p> [--reason R] · resume <p>
  orche queue approve <p> (#N ...|--all)          land: owner projects, Main only
  orche queue run <p>                             the integrator (the driver starts it detached)
"""
from . import brief, cli, config, donegate, github, publish, queue, registry, roster, units, vendors
from .queue import facts, repo
from .review import build as review_build

ACTIONS = ("add", "drop", "pause", "resume", "approve", "run", "status")


def register(subparsers):
    p = subparsers.add_parser("queue", help="the merge queue: status, add, drop, pause, resume, approve",
                              description=__doc__.split("\n", 1)[1])
    p.add_argument("words", nargs="*", metavar="[action] args", help=f"one of {', '.join(ACTIONS)}, or a project")
    p.add_argument("--first", action="store_true", help="add: put it at the front (after a testing entry)")
    p.add_argument("--accept", metavar="WHY", help="add: accept a held/capped pre-review or a [skip ci] head")
    p.add_argument("--surface", choices=("user", "internal"),
                   help="add: user when the change alters what a user sees or does (it then lands only on the "
                        "check script's live-ui-verified); default: the head review's, else internal")
    p.add_argument("--safe-because", metavar="FACT",
                   help="add: the one fact a high-risk or large change is safe because of (required there); "
                        "a test in the PR must exercise it")
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
    except queue.Refused as exc:
        return cli.fail(exc.code, str(exc))
    except github.GhError as exc:
        return cli.fail(cli.EXIT_BLOCKED, str(exc))


# ---- helpers ---------------------------------------------------------------------------------

def _project(reg, key):
    found = registry.project(reg, key) if key else None
    if found is None:
        raise queue.Refused(cli.EXIT_NOT_FOUND, f"unknown project: {key}" if key else "name a project")
    return found[0]


def caller(reg, project, *, main_only=False, target=None):
    """The calling session's Member if it is the project's Main (or, unless main_only, Build or the head
    of `target`, roster.Roster.head)."""
    r = roster.Roster(reg)
    m = r.member(roster.env_session())
    ok = m is not None and m.project == project and (
        m.kind == "main" or (not main_only and m.kind == "div" and (
            m.name == "build" or (target is not None and r.head(target) == m))))
    return m if ok else None


def _pr_checkout(reg, project, pr, head, base):
    """The queue's clone (ORCHE_HOME/queue/<p>/checkout) holding PR #pr's head for the §4.3 checks: a PR
    queued by number has its head on origin, not in the main checkout, so it is fetched there (with the
    base) when missing. Refuses with the reason when the clone cannot be made or the fetch fails."""
    from .queue import integrator
    try:
        where = integrator.checkout(project, reg)
    except integrator.Stop as exc:
        raise queue.Refused(cli.EXIT_BLOCKED, f"PR #{pr}: no queue checkout: {exc}") from None
    try:
        github.fetch_pr_head(where, pr, head, base)
    except github.GhError as exc:
        raise queue.Refused(cli.EXIT_BLOCKED, str(exc)) from None
    return where


def _team_publish(reg, target, where, unit, b, base) -> list:
    """R-TEAM-2, the owner's "push" in a team repository: publish the unit's branch exactly as its done would
    have (publish.member_done), then start the head review (review.build.after_publish). The one open PR, as
    github.pr_for_branch lists it; a gap or nothing to publish refuses (exit 3)."""
    report = b.folder / "REPORT.md"
    out = publish.member_done(reg, target, str(where), target.project, unit, b,
                              str(report) if report.is_file() else "", b.fields.get("GOAL") or unit)
    if isinstance(out, publish.Gap):
        raise queue.Refused(cli.EXIT_BLOCKED, out.text + (f"\nOWNER: {out.owner}" if out.owner else ""))
    if isinstance(out, publish.Unchanged):
        raise queue.Refused(cli.EXIT_BLOCKED, f"{unit}: {out.note()}")
    review_build.after_publish(target.project, out.pr, unit)
    return [{"number": out.pr, "headRefOid": out.head, "baseRefName": base}]


# ---- actions ---------------------------------------------------------------------------------

def cmd_add(args, words):
    reg, cfg = registry.load(), config.load()
    by = roster.env_session() or "auto"
    if args.pr is not None:
        if words:
            raise queue.Refused(cli.EXIT_USER, "add --pr N --project <p> takes no unit")
        project = _project(reg, args.project)
        me = caller(reg, project)
        if not me:
            raise queue.Refused(cli.EXIT_BLOCKED, "only Build or Main may queue")
        root = units.project_root(reg, project)
        base = publish.base(root, cfg, project=project)
        if not base:
            raise queue.Refused(cli.EXIT_BLOCKED, publish.no_base(root))
        try:
            view = github.view(root, args.pr)
        except github.GhError:
            raise queue.Refused(cli.EXIT_NOT_FOUND, f"unknown PR #{args.pr}") from None
        if view.get("state") not in (None, "OPEN") or view.get("baseRefName") != base:
            raise queue.Refused(cli.EXIT_BLOCKED, f"no open PR #{args.pr} against {base}")
        pr, head, branch = int(view["number"]), view["headRefOid"], view.get("headRefName", "")
        where = _pr_checkout(reg, project, pr, head, base)
        unit, writer, b = None, "", None
    else:
        if len(words) != 1:
            raise queue.Refused(cli.EXIT_USER, "usage: orche queue add <unit> [--first] [--accept WHY]")
        unit = words[0]
        err = brief.unit_id_error(unit)
        if err:
            raise queue.Refused(cli.EXIT_USER, err)
        b = brief.find(reg, unit)
        if b is None or b.rev == 0:
            raise queue.Refused(cli.EXIT_NOT_FOUND, f"unknown unit {unit!r}")
        project = b.project
        target = roster.Roster(reg).member(b.to)
        me = caller(reg, project, target=target)
        if not me:
            raise queue.Refused(cli.EXIT_BLOCKED, "only Build, Main or the unit's head may queue")
        if not donegate.gated(target):
            raise queue.Refused(cli.EXIT_BLOCKED, f"{unit}: target {b.to or '?'} is not a feature agent or Build")
        where = target.worktree
        if where is None or not where.is_dir():
            raise queue.Refused(cli.EXIT_NOT_FOUND, f"{unit}: worktree of {b.to} is missing")
        head, branch = repo.head(where)
        if not head:
            raise queue.Refused(cli.EXIT_NOT_FOUND, f"{unit}: cannot read HEAD of {where}")
        root = units.project_root(reg, project)
        base = publish.base(root, cfg, where, project)
        if not base:
            raise queue.Refused(cli.EXIT_BLOCKED, publish.no_base(root or where))
        team = registry.team(reg, project)
        if team and me.kind != "main":
            raise queue.Refused(cli.EXIT_BLOCKED, f"{project} is a team repository: only Main queues, on the owner's "
                                                  f'word ("push {unit}"); tell Main: ready: {unit}')
        prs = [p for p in github.pr_for_branch(where, branch) if p.get("baseRefName") == base]
        if team and not args.dry_run and not (len(prs) == 1 and prs[0].get("headRefOid") == head):
            prs = _team_publish(reg, target, where, unit, b, base)
        if len(prs) != 1:
            raise queue.Refused(cli.EXIT_BLOCKED, f"no open PR for {branch}")
        pr = int(prs[0]["number"])
        if prs[0].get("headRefOid") != head:
            raise queue.Refused(cli.EXIT_BLOCKED, f"PR #{pr}'s head is {str(prs[0].get('headRefOid'))[:7]}, the "
                                            f"worktree's is {head[:7]}: report done again to publish")
        writer = b.to
    surface = args.surface or review_build.record(project, pr, head).get("surface") or "internal"
    fact = " ".join(str(args.safe_because or "").split())
    res = queue.admit(project, pr, head, where=where, base=base, branch=branch, unit=unit, b=b, writer=writer,
                      accept=args.accept, first=args.first, by=by, surface=surface, safe_because=fact,
                      dry_run=args.dry_run, reg=reg, cfg=cfg)
    if args.dry_run:
        f = facts.gather(where, project=project, pr=pr, head=head, base=base, tier=b.tier if b else "standard",
                         scope=b.fields.get("SCOPE", "") if b else "",
                         docs_paths=queue.DOCS_PATHS, entry_id=res["entry"]["id"])
        cli.out(args, {"entry": res["entry"], "facts": dict(f)}, facts.lines(f))
        return cli.EXIT_OK
    if res["status"] == "already":
        cli.out(args, res, f"already queued, position {res['position']}")
        return cli.EXIT_OK
    # the pane's PASS is its head review: the record (surface, the fact) and a build row
    review_build.pane_record(project, pr, head, reviewer=f"{by} (pane)", model=vendors.of(reg, me), writer=writer,
                             surface=surface, safe_because=fact, accept=args.accept)
    started, owner_ok = res.pop("started"), res["entry"]["owner_ok"]
    line = (f"queued PR #{pr} ({branch} @ {head[:7]}) — position {res['position']} of {res['total']}; "
            f"integrator {'running' if started else 'idle'}")
    if not owner_ok:
        line += " (waits for the owner's word: land: owner)"
    cli.out(args, res, line)
    return cli.EXIT_OK


def cmd_status(args, words):
    reg = registry.load()
    if len(words) > 1:
        raise queue.Refused(cli.EXIT_USER, "usage: orche queue [<project>]")
    if words:
        keys = [_project(reg, words[0])]
    else:
        m = roster.Roster(reg).member(roster.env_session())
        keys = [m.project] if m else queue.projects()
    data, lines, cfg = [], [], config.load()
    for key in keys:
        s = queue.summary(key)
        data.append({k: v for k, v in s.items() if k != "last"})
        lost_after = queue.settings(cfg, key).get("lost_after_s")
        p50 = s["gate_p50"]
        gate = "gate —" if p50 is None else f"gate p50 {round(p50 / 60)}m (≈ {int(3600 // max(p50, 1))}/h)"
        state = s["state"]
        if state == "paused":
            state = f"paused ({(s['paused'] or {}).get('reason') or 'no reason'})"
        elif state == "closed":
            state = f"closed ({queue.closed_text(key, s['closed'])})"
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
        raise queue.Refused(cli.EXIT_USER, usage)
    project = _project(reg, words[0])
    m = caller(reg, project, main_only=main_only)
    if m is None:
        raise queue.Refused(cli.EXIT_BLOCKED, "only Main approves landing in a land: owner project; report it to Main"
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
        raise queue.Refused(cli.EXIT_NOT_FOUND, f"no entry {words[1]} in the {project} queue")
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
        raise queue.Refused(cli.EXIT_USER, usage)
    if queue.land_mode(cfg, project, reg) != "owner":
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
        e = queue.next_entry(queue.summary(project))
        cli.out(args, {"next": e}, f"next: PR #{e['pr']} @ {e['head'][:7]}" if e else "nothing to test")
        return cli.EXIT_OK
    from .queue import integrator
    res = integrator.loop(project)
    cli.out(args, res, res["lines"])
    return res["code"]
