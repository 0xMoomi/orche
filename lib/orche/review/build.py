"""The headless Build review (design §3.5, Appendix A.7): after a pre-review PASS (or skip), a fresh,
read-only session with Build's persona and model at the standard rung reads the facts and the diff
against the eight-line checklist. Both PASS → the runner queues the PR (R5) and the done is quiet;
RETURN, disagreement, high risk, size or error → a waking pointer to the Build pane with both verdicts.

The reviewer runs exactly as the pre-review's (review.call_for: Claude sealed with Read, Glob and Grep;
Codex `-s read-only`, no writable root, no network) in a detached worktree at the PR head, and `git status
--porcelain` is compared before and after: a change is the verdict `error` (review.CHANGED). Every
value of the package is masked and the untrusted ones fenced (review.fence); the evidence file and
every pointer are masked. The answer's first non-blank line is a lone `VERDICT: PASS` or `VERDICT:
RETURN`, else `error`; checklist lines (`- N: why`) after a PASS make it a RETURN.

Spend and holds (§7, §8.5): G4 before the call (over a cap → a `skipped` row, the pane decides); the
reviewer's vendor held → no call and no row, one quiet `held: … (stage build-review)` note, and the
heartbeat starts it once the hold is over; a usage limit hit during the call → the same. Every call
is a history line (`kind: build-review`, its vendor, model and cost).

  build_review(project, pr, unit=None)   decide one (pr, head); idempotent per head
  after_publish(project, pr, unit)       the run's publish hook: True when a Build review decides
  pending(project)                       runs whose RUN.json says `review.build: pending`, no build row yet
  claim(project, item)                   the heartbeat's check before start(): not held, < max_starts (no gh)
  for_pr(project, pr, unit=None)         `orche review --pr N` (R33): the pre-review round, then this
"""
import argparse
import contextlib
import os
import re
import shutil
import sys
import time
from pathlib import Path

from .. import (brief, config, inbox, ladder, ledger, log, paths, procs, publish, queue, registry, roster,
                shell, templates, units, util, vendors)
from ..flow import usage as holds
from ..queue import facts as _facts_mod, repo
from . import CHANGED, call_for, fence, masked, one_line, pre, tree

TEMPLATE = templates.TEMPLATES_ROOT / "review" / "build.md"
MAX_TURNS = 12
MAX_STARTS = 3           # review.max_starts: heartbeat starts of one (pr, head) before Build is told
PRE_OK = ("pass", "skipped")
STAGE = "build-review"
RISK = re.compile(r"(^|/)(payments?|billing|auth|oauth|migrations?|deploy|wallets?|keys?|secrets?)(/|\.|_|-|$)",
                  re.IGNORECASE)
_VERDICT = re.compile(r"VERDICT:\s*(PASS|RETURN)", re.IGNORECASE)
_FINDING = re.compile(r"^\s*-\s*([1-8])\s*:\s*(.+?)\s*$")
sleep, clock = time.sleep, time.monotonic   # test seams


def _settings(cfg) -> dict:
    rv = dict(config.DEFAULTS.get("review") or {})
    rv.update(cfg.get("review") or {})
    return rv


def _max_starts(rv) -> int:
    try:
        return max(1, int(rv.get("max_starts") or MAX_STARTS))
    except (TypeError, ValueError):
        return MAX_STARTS


def _folder(project) -> Path:
    return paths.orche_home() / "reviews" / project


def _lock(project, pr, head, stale_s):
    """O_EXCL lock per (pr, head); a lock older than `stale_s` is taken over. Path or None."""
    p = _folder(project) / f"pr{pr}-{head[:7]}.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return p
        except FileExistsError:
            try:
                if time.time() - p.stat().st_mtime < stale_s:
                    return None
                p.unlink()
            except FileNotFoundError:
                pass
    return None


def _same(a, b) -> bool:
    """Two commit ids name the same commit (either may be abbreviated, >= 7 characters)."""
    a, b = str(a or "").lower(), str(b or "").lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


# ---- seams (tests replace these) ---------------------------------------------------------------

def _pr(where, pr) -> dict:
    return repo.pr_view(where, pr)


def _gather(where, **kw) -> dict:
    f = _facts_mod.gather(where, **kw)
    base_ref = f"origin/{kw['base']}" if repo.git(where, "rev-parse", "--verify", "-q", f"origin/{kw['base']}") else kw["base"]
    f["paths"] = [c[0] for c in repo.changed(where, base_ref, kw["head"])]
    return f


def _diff(where, base, head, limit) -> str:
    base_ref = f"origin/{base}" if repo.git(where, "rev-parse", "--verify", "-q", f"origin/{base}") else base
    text = repo.git(where, "diff", f"{base_ref}...{head}") or ""
    lines = text.splitlines()
    if len(lines) > limit:
        lines = lines[:limit] + [f"… diff cut at {limit} lines"]
    return "\n".join(lines)


def _call(call, prompt, cfg):
    from .. import codex, headless
    if isinstance(call, codex.Call):
        return codex.run(call, prompt, cfg)
    return headless.run(call, prompt, cfg)


def _tree(cwd):
    return tree(cwd)


# ---- the parts --------------------------------------------------------------------------------

def _build_member(reg, project):
    div = (((reg.get("projects") or {}).get(project) or {}).get("divisions") or {}).get("build") or {}
    return roster.Roster(reg).member(div.get("session")) if div.get("session") else None


def _members(reg, project, branch, unit):
    """(build Member or None, writer Member or None, Brief or None)."""
    r = roster.Roster(reg)
    build = _build_member(reg, project)
    b = brief.find(reg, unit, project=project) if unit else None
    writer = r.member(b.to) if b is not None and b.to else None
    if writer is None and branch:
        same = [m for s in r.sessions() if (m := r.member(s)) and m.project == project and m.branch == branch]
        same.sort(key=lambda m: m.kind != "feat")
        writer = same[0] if same else None
    return build, writer, b


def deterministic(f, pane_lines) -> list:
    """Checklist findings the facts decide alone: [(n, why)]."""
    out = []
    if f.get("skip_ci"):
        out.append((4, f"[skip ci] on a code change: {', '.join(f['skip_ci'])}; add a commit without it"))
    if f.get("scope_outside"):
        out.append((3, f"outside the scope: {', '.join(f['scope_outside'][:5])}"))
    for o in f.get("overlap") or []:
        out.append((6, f"wait for PR #{o['pr']} ({', '.join(o['paths'][:3])})"))
    risky = [p for p in f.get("paths") or [] if RISK.search(p)]
    if risky:
        out.append((7, f"pane review: high risk ({', '.join(risky[:3])})"))
    if f.get("added", 0) + f.get("deleted", 0) > pane_lines or f.get("tier") == "hard":
        out.append((8, f"pane review: size (+{f.get('added', 0)} −{f.get('deleted', 0)}, tier {f.get('tier')})"))
    return out


def parse(text):
    """('pass'|'return', [(n, why)]), or (None, []) when the first non-blank line is not a lone
    `VERDICT: PASS` / `VERDICT: RETURN`. Checklist lines after a PASS make it a RETURN."""
    lines = str(text or "").splitlines()
    i = next((n for n, ln in enumerate(lines) if ln.strip()), None)
    m = _VERDICT.fullmatch(lines[i].strip()) if i is not None else None
    if m is None:
        return None, []
    found = [(int(f.group(1)), f.group(2)) for f in map(_FINDING.match, lines[i + 1:]) if f]
    if m.group(1).upper() == "PASS" and not found:
        return "pass", []
    return "return", found or [(0, "returned without a checklist line")]


def _prompt(f, b, pre_row, diff, build_session, unit, cfg_rv) -> str:
    """Appendix A.7: every value masked; the facts, the brief's ACCEPT/VERIFY, the pre-review and the
    diff fenced as data (a fence longer than any backtick run inside, so nothing closes it)."""
    fields = b.fields if b is not None else {}
    pre_text = "none"
    if pre_row:
        pre_text = (f"{pre_row.get('verdict')} by {pre_row.get('model')} (round {pre_row.get('round')}): "
                    f"{pre_row.get('evidence')}")
        try:
            ev = Path(pre_row.get("evidence") or "")
            if ev.is_file():
                pre_text += "\n\n" + ev.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            pass
    values = {"UNIT": unit or f"pr{f['pr']}", "PR": f["pr"], "HEAD7": str(f["head"])[:7],
              "BUILD_SESSION": build_session, "PANE_LINES": cfg_rv.get("pane_lines", 400),
              "FACTS": fence("\n".join(_facts_mod.lines(f)), "facts"), "GOAL": one_line(fields.get("GOAL") or "-"),
              "TIER": f.get("tier"), "ACCEPT": fence(fields.get("ACCEPT") or "-", "accept"),
              "VERIFY": fence(fields.get("VERIFY") or "-", "verify"), "PRE": fence(pre_text, "pre-review"),
              "BASE": f["base"], "DIFF": fence(diff, "diff")}
    return templates.render(TEMPLATE.read_text(encoding="utf-8"), **{k: masked(v) for k, v in values.items()})


def make_call(reg, cfg, build, cwd, tag, timeout):
    """Build's model at the standard rung, read-only exactly as the pre-review's reviewer (§3.3,
    review.call_for); a Claude call gets Build's persona appended (Codex: prepended to the prompt)."""
    rung = ladder.rung(cfg, vendors.of(reg, build) or "opus", "standard")
    persona = (build.worktree / ".claude" / "agents" / f"{build.session}.md") if build.worktree else None
    return call_for(rung.model, cwd=cwd, timeout=timeout, effort=rung.effort, tag=tag, max_turns=MAX_TURNS,
                    append_file=persona if persona is not None and persona.is_file() else None)


def reviewer_vendor(reg, cfg, build) -> str:
    return vendors.vendor_of(ladder.rung(cfg, vendors.of(reg, build) or "opus", "standard").model)


def _persona_text(build) -> str:
    try:
        p = build.worktree / ".claude" / "agents" / f"{build.session}.md"
        return p.read_text(encoding="utf-8") + "\n\n" if p.is_file() else ""
    except (OSError, TypeError, AttributeError):
        return ""


def _notify(project, pr, head, unit, build, text, *, quiet, to=None):
    ptr = inbox.make(unit or f"pr{pr}", "review", to or build.session, "info", head_sha=head,
                     urgent=not quiet, summary=masked(text), via="review", quiet=True if quiet else None)
    inbox.post(ptr)


def _first_reason(findings, error):
    if error:
        return error
    return f"{findings[0][0]}: {findings[0][1]}" if findings else "-"


def _enqueue(reg, cfg, project, where, view, head, base, unit, writer, b, build):
    """Queue (pr, head) as the runner (R5); returns the queue.add result. Raises cmd_queue.Refused."""
    from .. import cmd_queue
    pr = int(view["number"])
    verify = cmd_queue._verify_cmds(b.fields.get("VERIFY")) if b is not None else []
    accepted = cmd_queue._checks(argparse.Namespace(accept=None), cfg, project, where, pr=pr, head=head,
                                 base=base, unit=unit, verify=verify)
    main = roster.Roster(reg).main(project)
    owner_ok = queue.land_mode(cfg, project) != "owner" or queue.approved_in_log(
        project, pr, main.session if main else None)
    entry = {"id": queue.entry_id(pr, head), "pr": pr, "head": head, "branch": view.get("headRefName", ""),
             "unit": unit, "writer": writer.session if writer else "", "parent": b.parent if b else "",
             "rev": b.rev if b else 0, "verify": verify, "approved_by": f"{build.session} (review)",
             "accept": "; ".join(accepted) or None, "first": False, "owner_ok": owner_ok,
             "at": util.now_iso(), "state": "waiting", "restarts": 0, "drop": False, "test": None}
    res = queue.add(project, entry, first=False, by="runner", base=base)
    if res.get("status") != "already":
        if not owner_ok:
            cmd_queue._ask_owner(reg, project, entry, "runner")
        queue.start(project) or queue.running(project)
    return res


def _event(project, pr, verdict, queued):
    log.log("events", f"buildreview project={project} pr={pr} verdict={verdict} queued={'yes' if queued else 'no'}")


def _spend_hit(project, unit, cfg):
    """G4 before the paid call: the unit's and the project's headless spend inside W (flow.guard), the
    Build reviews' history lines included. Fails open, as guard.safe."""
    from ..flow import guard
    from ..runs import history
    f = cfg.get("flow") or {}
    if f.get("enabled") is False:
        return None
    try:
        w = guard.window(project)
        if unit:
            cap, spent = guard.unit_cap(f), history.spend(project, w, unit)
            if spent >= cap:
                return f"{unit} spent ${spent:.2f}, cap ${cap:g} a unit"
        cap, spent = float(f.get("max_project_cost_usd") or 30), history.spend(project, w)
        if spent >= cap:
            return f"{project} spent ${spent:.2f}, cap ${cap:g}"
    except Exception as exc:  # noqa: BLE001 - the guard fails open
        log.log("hooks", f"buildreview: {project}: spend check skipped ({type(exc).__name__}: {exc})")
    return None


def _history(project, unit, rev, build, call, res, started, finished, word, cfg) -> None:
    """The call's history line (`kind: build-review`): G4 and `orche metrics` count it."""
    from ..flow import guard
    from ..runs import history
    cost = res.cost_usd
    if cost is None and res.code != 127:      # it ran, its cost is unknown: §7.3, as a worker's
        cost = history.reserve(started, finished, guard.unit_cap(cfg.get("flow") or {}))
    history.append(project, {"unit": unit, "to": build.session, "model": call.model,
                             "vendor": vendors.vendor_of(call.model), "effort": getattr(call, "effort", None),
                             "status": word, "stop_reason": None, "cost_usd": cost, "num_turns": res.turns,
                             "started": started, "finished": finished, "kind": STAGE}, rev)


def _held(project, pr, head, unit, build, vendor, hold) -> dict:
    """§8.5: no row (pending() keeps it; the heartbeat starts it once the hold is over), one quiet note."""
    _notify(project, pr, head, unit, build, f"{holds.held_text(hold, vendor)} (stage {STAGE})", quiet=True)
    _event(project, pr, "held", False)
    return {"status": "held", "head": head, "vendor": vendor}


# ---- the review -------------------------------------------------------------------------------

def _not_run(project, pr, head, unit, build, why, quiet=False) -> dict:
    """An exception before the review: Build hears `why` (waking unless `quiet`); the result says it was told."""
    _notify(project, pr, head, unit, build, f"PR #{pr} needs you: {why}", quiet=quiet)
    _event(project, pr, "error", False)
    return {"status": "error", "verdict": "error", "head": head, "error": why}


def _ended(project, pr, head, state, reg) -> None:
    """G8a: the heartbeat's pending markers of PR `pr` (pending(), claim()) end quietly once the review
    it started reads the PR: all of them when it is not open, else those at another head that the
    heartbeat already claimed (a fresh push's marker may meet GitHub's lagging view of the head)."""
    starts, ended = _starts(project), False
    for x in pending(project, reg):
        key = _key(pr, x["sha"])
        if x["pr"] != pr:
            continue
        if state != "OPEN":
            starts.setdefault(key, {})["end"] = f"PR {state.lower()}"
        elif not _same(x["sha"], head) and (starts.get(key) or {}).get("n"):
            starts[key]["end"] = "head moved"
        else:
            continue
        ended = True
    if ended:
        util.write_json(_starts_path(project), starts)


def build_review(project, pr, unit=None, *, wait_s=0, reg=None, cfg=None) -> dict:
    """Decide PR `pr` of `project` at its current head. {"status": off|no-build|error|already|busy|
    waiting|skipped|held|queued|returned|pane, ...}; every exception path but off/already/busy/skipped/
    waiting posts a waking pointer to Build (held: a quiet one)."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    rv = _settings(cfg)
    if not rv.get("build", True):
        return {"status": "off"}
    root = units.project_root(reg, project)
    try:
        view = _pr(root, pr)
    except repo.GhError as exc:
        view, gh_error = None, str(exc)
    build, writer, b = _members(reg, project, (view or {}).get("headRefName", ""), unit)
    if build is None:
        log.log("hooks", f"buildreview: {project}: no Build division; PR #{pr} not reviewed")
        return {"status": "no-build"}
    if view is None:
        # quiet: a gh outage must not wake Build once per start; review.max_starts gives up with one that wakes
        return _not_run(project, pr, "", unit, build, f"Build review could not read the PR — {gh_error}", quiet=True)
    head, state = view["headRefOid"], str(view.get("state") or "OPEN").upper()
    _ended(project, pr, head, state, reg)
    if state != "OPEN":       # G8a: the heartbeat asks gh nothing; a closed or merged PR ends here, quietly
        return {"status": "closed", "head": head, "state": view.get("state")}
    base = publish.base(root, cfg)
    if not base:
        return _not_run(project, pr, head, unit, build, f"Build review not run — {publish.no_base(root)}")
    h7 = head[:7]
    if ledger.review_for(project, pr, head, "build") is not None:
        return {"status": "already", "head": head}
    timeout = float(rv.get("build_timeout_s", 240))
    lock = _lock(project, pr, head, 2 * timeout + 60)
    if lock is None:
        return {"status": "busy", "head": head}
    try:      # always the PR's head in a detached worktree: never the writer's live one (no lock held there)
        with _checkout(project, pr, head, root, view.get("headRefName", "")) as where:
            if where is None:
                return _not_run(project, pr, head, unit, build, f"Build review not run — head {h7} is not in "
                                                                f"{root}: git fetch origin, then orche review --pr {pr}")
            return _decide(reg, cfg, rv, project, pr, unit, view, head, base, h7, root, where, build, writer, b,
                           timeout, wait_s)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _decide(reg, cfg, rv, project, pr, unit, view, head, base, h7, root, where, build, writer, b, timeout, wait_s):
    mode = rv.get("cross_vendor", "required")
    pre_row = ledger.review_for(project, pr, head, "pre")
    deadline = clock() + wait_s
    while pre_row is None and mode != "off" and clock() < deadline:
        sleep(min(10, max(0.0, deadline - clock())))
        pre_row = ledger.review_for(project, pr, head, "pre")
    if pre_row is None and mode == "required":
        return {"status": "waiting", "head": head}
    pre_v = pre_row["verdict"] if pre_row else "off"
    if pre_row is not None and pre_v not in PRE_OK:
        return {"status": "skipped", "head": head, "pre": pre_v}
    tier = b.tier if b is not None else "standard"
    scope = b.fields.get("SCOPE", "") if b is not None else ""
    pre_model = pre_row.get("model") if pre_row else "-"
    pre_round = pre_row.get("round") if pre_row else "-"
    who = unit or f"pr{pr}"
    folder = _folder(project)
    folder.mkdir(parents=True, exist_ok=True)
    evidence = folder / f"pr{pr}-{h7}-build.md"
    error, model, answer, findings = None, "facts", "", []
    try:
        f = _gather(where, project=project, pr=pr, head=head, base=base, tier=tier, scope=scope,
                    docs_paths=queue.settings(cfg, project)["docs_paths"], entry_id=queue.entry_id(pr, head))
        findings = deterministic(f, int(rv.get("pane_lines", 400)))
    except Exception as exc:   # noqa: BLE001 — never silent: Build hears of it
        f, error = None, f"facts: {exc}"
    verdict = "return" if findings else ("error" if error else None)
    if verdict is None:
        try:
            call = make_call(reg, cfg, build, where, procs.run_id(f"review-pr{pr}", time.time_ns()), timeout)
            vendor = vendors.vendor_of(call.model)
            hold = holds.active(vendor)
            if hold is not None:      # §8.5: no review of a held vendor starts
                holds.join(vendor, project, who, STAGE)
                return _held(project, pr, head, unit, build, vendor, hold)
            hit = _spend_hit(project, unit, cfg)
            if hit:                   # G4: the pane decides
                verdict, error = "skipped", f"spend cap: Build review not run ({hit})"
            else:
                model = call.model
                prompt = _prompt(f, b, pre_row, _diff(where, base, head, int(rv.get("max_diff_lines", 3000))),
                                 build.session, unit, rv)
                if vendor == vendors.CODEX:
                    prompt = masked(_persona_text(build)) + prompt
                before, started = _tree(where), util.now_iso()
                res = _call(call, prompt, cfg)
                finished, after = util.now_iso(), _tree(where)
                answer = masked(res.text or "")
                limited = holds.is_limit(res.limit_text, cfg)
                if before is not None and after is not None and before != after:
                    verdict, error = "error", CHANGED
                elif limited:
                    verdict = "held"
                elif res.is_error or res.failure:
                    verdict, error = "error", one_line(res.failure or res.error or "the review call failed", 200)
                else:
                    verdict, findings = parse(answer)
                    if verdict is None:
                        verdict, error = "error", "no lone VERDICT line first in the answer"
                try:
                    _history(project, who, b.rev if b is not None else 1, build, call, res, started, finished,
                             verdict, cfg)
                except Exception as exc:   # noqa: BLE001 — the history line never decides the review
                    log.log("hooks", f"buildreview: {project} PR #{pr}: history line not written: {exc}")
                if verdict == "held":
                    hold = holds.hit(vendor, res.limit_text, cfg, project=project, unit=who, stage=STAGE)
                    if hold is not None:
                        return _held(project, pr, head, unit, build, vendor, hold)
                    verdict, error = "error", one_line(res.failure or "a usage limit", 200)
        except Exception as exc:   # noqa: BLE001
            verdict, error = "error", masked(f"{type(exc).__name__}: {exc}")
    body = [f"# Build review · PR #{pr} @ {h7}", "", f"verdict: {verdict}", f"model: {model}",
            f"pre-review: {pre_v} ({pre_model}, round {pre_round})", ""]
    body += [f"- {n}: {why}" for n, why in findings]
    if error:
        body.append(f"error: {error}")
    if f is not None:
        body += ["", "## Facts", *_facts_mod.lines(f)]
    if answer:
        body += ["", "## Answer (data, masked)", answer]
    util.atomic_write(evidence, masked("\n".join(body)) + "\n")
    try:
        ledger.record_review(project, "build", pr, head, verdict, str(evidence), f"{build.session} (review)",
                             model, writer=writer.session if writer else "unknown", note=one_line(error or "", 200))
    except ledger.LedgerError as exc:
        log.log("hooks", f"buildreview: {project} PR #{pr}: ledger row refused: {exc}")
    return _act(reg, cfg, project, pr, unit, view, head, base, h7, where, build, writer, b, verdict,
                findings, error, pre_v, pre_model, pre_round)


def _act(reg, cfg, project, pr, unit, view, head, base, h7, where, build, writer, b, verdict, findings,
         error, pre_v, pre_model, pre_round):
    from .. import cmd_queue
    if verdict == "pass":
        try:
            res = _enqueue(reg, cfg, project, where, view, head, base, unit, writer, b, build)
        except cmd_queue.Refused as exc:
            error, findings = f"the queue refused it: {exc}", []
        else:
            text = (f"auto-queued PR #{pr} @{h7}: pre-review {pre_v.upper()} ({pre_model}, round {pre_round}), "
                    f"Build review PASS — position {res.get('position')} of {res.get('total')}")
            _notify(project, pr, head, unit, build, text, quiet=True)
            _event(project, pr, verdict, True)
            return {"status": "queued", "head": head, "position": res.get("position"), "total": res.get("total")}
    text = (f"PR #{pr} @{h7} needs you: pre-review {pre_v.upper()} ({pre_model}), Build review {verdict.upper()} — "
            f"{_first_reason(findings, error)}")
    extra = [f"- {n}: {why}" for n, why in (findings[:3] if error else findings[1:4])]
    _notify(project, pr, head, unit, build, "\n".join([text, *extra]), quiet=False)
    _event(project, pr, verdict, False)
    if verdict == "error":
        status = "error"
    elif verdict == "return" and not any(n in (7, 8) for n, _ in findings):
        status = "returned"
    else:                     # high risk or size, a spend-cap skip, a PASS the queue refused
        status = "pane"
    return {"status": status, "head": head, "verdict": verdict, "findings": findings, "error": error}


# ---- Build's own PRs (design §3.8, R33) ---------------------------------------------------------

@contextlib.contextmanager
def _checkout(project, pr, head, root, branch=""):
    """The PR's head to review: a temporary detached worktree of the project's checkout (the head fetched
    by its branch, else GitHub's `pull/<N>/head`; one per process, removed after), never the writer's live
    worktree. None when the head is not there."""
    tmp = _folder(project) / f"wt-pr{pr}-{head[:7]}-{os.getpid()}"

    def git(*args):
        return shell.run(["git", "-C", str(root), *args], timeout=120)

    for ref in ([f"refs/heads/{branch}"] if branch else []) + [f"pull/{pr}/head"]:
        if git("cat-file", "-e", f"{head}^{{commit}}").ok:
            break
        git("fetch", "-q", "origin", ref)
    shutil.rmtree(tmp, ignore_errors=True)
    git("worktree", "prune")
    made = git("worktree", "add", "--detach", str(tmp), head).ok
    try:
        yield tmp if made else None
    finally:
        if made:
            git("worktree", "remove", "--force", str(tmp))
        shutil.rmtree(tmp, ignore_errors=True)


def _pre_round(reg, cfg, project, pr, unit, view, root) -> dict | None:
    """One pre-review round of the PR's head (pre.one_round: a reviewer of another vendor than the
    writer's model, row kind pre). None on PASS or skipped, else the result."""
    from ..runs import history
    head = view["headRefOid"]
    build, writer, b = _members(reg, project, view.get("headRefName", ""), unit)
    writer = writer or build
    if writer is None:
        return {"status": "no-build"}
    rev = b.rev if b is not None else 1
    unit_id = b.unit if b is not None else f"pr{pr}"
    unit_dir = b.folder if b is not None else _folder(project) / f"pr{pr}"

    def record(a, at):
        history.append(project, {"unit": unit_id, "to": writer.session, "model": a.model,
                                 "vendor": vendors.vendor_of(a.model), "effort": getattr(a.call, "effort", None),
                                 "status": a.word, "stop_reason": a.why or None, "cost_usd": a.res.cost_usd,
                                 "num_turns": a.res.turns, "started": a.started, "finished": a.finished,
                                 "kind": "review"}, rev)

    base = publish.base(root, cfg)
    if not base:
        return {"status": "pre-error", "head": head, "error": publish.no_base(root)}
    hit = _spend_hit(project, b.unit if b is not None else None, cfg)
    if hit:                   # G4 before the paid pre-review call
        return {"status": "pre-error", "head": head, "error": f"spend cap: pre-review not run ({hit})"}
    with _checkout(project, pr, head, root, view.get("headRefName", "")) as wt:
        if wt is None:
            return {"status": "pre-error", "head": head,
                    "error": f"head {head[:7]} is not in {root}: git fetch origin, then orche review --pr {pr} again"}
        ctx = pre.Ctx(cfg=cfg, project=project, unit=unit_id, rev=rev, unit_dir=unit_dir, worktree=wt,
                      base=base, writer=writer.session,
                      writer_model=vendors.of(reg, writer), fields=dict(b.fields) if b is not None else {},
                      tag=procs.run_id(f"review-pr{pr}", time.time_ns()), record=record)
        prior = pre.returns(unit_dir, rev)
        report = ""
        if b is not None and (b.folder / "REPORT.md").is_file():
            report = (b.folder / "REPORT.md").read_text(encoding="utf-8", errors="replace")
        rnd = pre.one_round(ctx, len(prior) + 1, pr=pr, head=head, report=report,
                            earlier=pre.earlier_text([(r, "") for r in prior]))
    if rnd.word in PRE_OK:
        return None
    return {"status": f"pre-{rnd.word}", "head": head, "model": rnd.model, "round": rnd.k,
            "findings": list(rnd.findings), "error": rnd.why, "path": str(rnd.path or "")}


def for_pr(project, pr, unit=None, *, tell=None, reg=None, cfg=None) -> dict:
    """`orche review --pr N` (design §3.8, R33): the PR's pre-review at its head unless a pass, skipped
    or return row is there already (one round; row kind pre), then on PASS or skipped the Build review
    (build_review), so `orche queue add --pr N` finds its pre row. A pre-review RETURN or error is the
    result; `tell` (the `--background` caller) hears it as a waking `review` pointer."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    root = units.project_root(reg, project)
    try:
        view = _pr(root, pr)
    except repo.GhError as exc:     # nobody was told: the caller prints it
        return {"status": "error", "error": f"gh cannot read it ({masked(str(exc))}); nothing was reviewed"}
    head = view.get("headRefOid") or ""
    if str(view.get("state") or "OPEN").upper() != "OPEN" or not head:
        return {"status": "closed", "head": head, "state": view.get("state")}
    row = ledger.review_for(project, pr, head, "pre")
    if pre.mode_of(cfg) != "off" and (row is None or row.get("verdict") == "error"):
        out = _pre_round(reg, cfg, project, pr, unit, view, root)
        if out is not None:
            if tell and out["status"] != "no-build":
                lines = [f"PR #{pr} @{head[:7]}: pre-review {out['status'][4:].upper()} by {out.get('model')} "
                         f"(round {out.get('round')}) — {one_line(out.get('error') or 'fix and push, then orche review --pr ' + str(pr), 160)}",
                         *[one_line(x, 160) for x in out.get("findings", [])[:3]]]
                build = _build_member(reg, project)
                if build is not None:
                    _notify(project, pr, head, unit, build, "\n".join(lines), quiet=False, to=tell)
            return out
    return build_review(project, pr, unit, reg=reg, cfg=cfg)


# ---- hooks --------------------------------------------------------------------------------------

def after_publish(project, pr, unit) -> bool:
    """The run's publish hook. True when a Build review decides this PR — started now (`orche review
    --pr N --wait`, detached) or left to the heartbeat (inside a sandbox, or the spawn failed: RUN.json
    says `review.build: pending`, which pending() finds) — so the run files its done quiet (Appendix
    A.11: one done; the review's pointer wakes only on an exception). False when none will
    (review.build off, no Build division). Never raises."""
    try:
        if not _settings(config.load()).get("build", True) or _build_member(registry.load(), project) is None:
            return False
    except Exception as exc:   # noqa: BLE001 — the publish already happened; never fail the run
        _quiet_log(f"buildreview: {project} PR #{pr}: not started: {exc}")
        return False
    try:
        from .. import runs
        if runs.in_sandbox():
            log.log("hooks", f"buildreview: {project} PR #{pr}: inside a sandbox; the heartbeat starts it")
        else:
            start(project, pr, unit)
    except Exception as exc:   # noqa: BLE001 — pending: the heartbeat starts it
        _quiet_log(f"buildreview: {project} PR #{pr}: not started now ({exc}); the heartbeat starts it")
    return True


def _quiet_log(line) -> None:
    try:
        log.log("hooks", line)
    except Exception:   # noqa: BLE001
        pass


def start(project, pr, unit=None) -> None:
    """Start `orche review --pr N --wait` detached (the publish hook and the heartbeat)."""
    argv = [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), "review", "--pr", str(pr),
            "--project", project, "--wait"]
    if unit:
        argv += ["--unit", unit]
    procs.spawn_detached(argv, paths.orche_home() / "logs" / "review" / f"{project}-pr{pr}.log")


def running(project, pr, head) -> bool:
    """A Build review of (pr, head) holds its lock now: the lock exists and is not yet stale."""
    stale_s = 2 * float(_settings(config.load()).get("build_timeout_s", 240)) + 60
    try:
        return time.time() - (_folder(project) / f"pr{pr}-{head[:7]}.lock").stat().st_mtime < stale_s
    except OSError:
        return False


# ---- the heartbeat's pending reviews ------------------------------------------------------------

def _starts_path(project) -> Path:
    return _folder(project) / "starts.json"


def _starts(project) -> dict:
    try:
        data = util.read_json(_starts_path(project), default={})
    except (OSError, util.JsonError):
        return {}
    return data if isinstance(data, dict) else {}


def _key(pr, sha) -> str:
    return f"pr{pr}-{str(sha)[:7]}"


def pending(project, reg=None) -> list:
    """[{pr, sha, unit, finished}], oldest first: the project's headless runs whose RUN.json (as new as
    the unit's BRIEF.md) filed a `done` with `review.build: pending` — the run started its Build review
    or left it to the heartbeat; pane units never do (§3.7: the pane is there) — with no build row at
    that head and not given up (claim). [] with review.build off. Reads the review rows once."""
    if not _settings(config.load()).get("build", True):
        return []
    reg = reg if reg is not None else registry.load()
    root = units.project_root(reg, project)
    if not root:
        return []
    reviewed = {}
    for row in ledger.read_reviews(project):
        if row.get("kind") == "build":
            reviewed.setdefault(str(row.get("pr")), []).append(row.get("sha") or "")
    starts, out = _starts(project), []
    for path in sorted((Path(root) / brief.UNITS).glob("*/RUN.json")):
        try:
            fresh = path.stat().st_mtime >= (path.parent / "BRIEF.md").stat().st_mtime
            rec = util.read_json(path, default=None) if fresh else None
        except (OSError, util.JsonError):
            continue
        if not isinstance(rec, dict) or rec.get("status") != "done":
            continue
        rv, pr = rec.get("review"), rec.get("pr")
        mark = rv.get("build") if isinstance(rv, dict) else None
        if not isinstance(mark, dict) or mark.get("verdict") != "pending" or not isinstance(pr, dict):
            continue
        number, sha = pr.get("number"), str(pr.get("head") or rec.get("head_sha") or "")
        if not number or len(sha) < 7 or any(_same(s, sha) for s in reviewed.get(str(number), ())):
            continue
        if (starts.get(_key(number, sha)) or {}).get("end"):
            continue
        out.append({"pr": int(number), "sha": sha, "unit": path.parent.name, "finished": str(rec.get("finished") or "")})
    out.sort(key=lambda x: x["finished"])
    return out


def claim(project, item, reg=None, cfg=None) -> bool:
    """The heartbeat's check before start(): True → start `item` now (the start is counted). False →
    not now: the reviewer's vendor is held (not counted), or it was started `review.max_starts` times
    without a verdict (given up with one waking pointer to Build). No gh call (the heartbeat's watchdog):
    the review it starts ends the marker of a closed PR or a moved head quietly (_ended)."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    build = _build_member(reg, project)
    if build is None or holds.active(reviewer_vendor(reg, cfg, build)) is not None:
        return False
    pr, sha, unit = item["pr"], item["sha"], item.get("unit")
    starts = _starts(project)
    entry, n = dict(starts.get(_key(pr, sha)) or {}), int((starts.get(_key(pr, sha)) or {}).get("n") or 0)
    if n >= _max_starts(_settings(cfg)):
        entry["end"] = "gave up"
        row = ledger.review_for(project, pr, sha, "pre") or {}
        _notify(project, pr, sha, unit, build,
                f"PR #{pr} @{sha[:7]} needs you: pre-review {str(row.get('verdict') or 'off').upper()} "
                f"({row.get('model') or '-'}), Build review not run — started {n} times without a verdict "
                f"(log: logs/review/{project}-pr{pr}.log)", quiet=False)
        _event(project, pr, "gave-up", False)
    else:
        entry.update(n=n + 1, at=util.now_iso())
    starts[_key(pr, sha)] = entry
    util.write_json(_starts_path(project), starts)
    return "end" not in entry
