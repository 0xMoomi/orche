"""The headless head review (design §3.5, Appendix A.7): after a pre-review PASS (or skip), a fresh,
read-only session with Build's persona and model at the standard rung reads the facts and the diff
against the eight-line checklist. Both PASS → the runner queues the PR (R5) and the done is quiet;
RETURN, disagreement, high risk, size or error → a waking pointer to Build with both verdicts.

The reviewer runs exactly as the pre-review's (review.call_for: Claude sealed with Read, Glob and Grep;
Codex `-s read-only`, no writable root, no network) in a detached worktree at the PR head, and `git status
--porcelain` is compared before and after: a change is the verdict `error` (review.CHANGED). Every
value of the package is masked and the untrusted ones fenced (review.fence); the evidence file and
every pointer are masked. The answer's first non-blank line is a lone `VERDICT: PASS` or `VERDICT:
RETURN`, else `error`; checklist lines (`- N: why`) after a PASS make it a RETURN. A `SURFACE: user` line says
the change alters what a user sees or does (R-REVIEW-12): the record keeps `surface: user|internal` (internal
without one) and the queue lands a user-visible change only on the check script's live-ui-verified.

Spend and holds (§7, §8.5): G4 before the call (over a cap → a `skipped` row, Build decides); the
reviewer's vendor held → no call and no row, one quiet `held: … (stage build-review)` note, and the
heartbeat starts it once the hold is over; a usage limit hit during the call → the same. Every call
is a history line (`kind: build-review`, its vendor, model and cost).

  build_review(project, pr, unit=None)   decide one (pr, head); idempotent per head
  after_publish(project, pr, unit)       the run's publish hook: True when a head review decides
  pending(project)                       runs whose RUN.json says `review.build: pending`, no build row yet
  may_start(project, item)               the heartbeat's check before start(): not held, < MAX_STARTS (no gh)
  for_pr(project, pr, unit=None)         `orche review --pr N` (R33): the pre-review round, then this
  record(project, pr, sha)               the head review record's own fields at (pr, head): surface, safe_because
  pane_record(project, pr, sha, ...)     the pane's PASS (`orche queue add`): its record and a build pass row
"""
import contextlib
import os
import re
import shutil
import time
from pathlib import Path

from .. import (brief, config, github, inbox, ladder, ledger, log, paths, procs, publish, queue, registry, roster,
                shell, templates, units, util, vendors)
from ..flow import usage as holds
from ..queue import facts as _facts_mod, repo
from . import RISK, Verdict, ask, call_for, fence, masked, one_line, pre

TEMPLATE = templates.TEMPLATES_ROOT / "review" / "build.md"
MAX_TURNS = 12
MAX_STARTS = 3           # heartbeat starts of one (pr, head) before Build is told
BUILD_TIMEOUT_S = 240    # one head-review call
PRE_OK = ("pass", "skipped")
STAGE = "build-review"
_VERDICT = re.compile(r"VERDICT:\s*(PASS|RETURN)", re.IGNORECASE)
_FINDING = re.compile(r"^\s*-\s*([1-8])\s*:\s*(.+?)\s*$")
_SURFACE = re.compile(r"^\s*SURFACE\s*:\s*(user|internal)\s*$", re.IGNORECASE | re.MULTILINE)
_FIELD = re.compile(r"^(surface|safe_because): (.+)$")
sleep, clock = time.sleep, time.monotonic   # test seams


def _folder(project) -> Path:
    return paths.orche_home() / "reviews" / project


def _lock(project, pr, sha, stale_s):
    """O_EXCL lock per (pr, head); a lock older than `stale_s` is taken over. Path or None."""
    p = _folder(project) / f"pr{pr}-{sha[:7]}.lock"
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
    return github.view(where, pr)


def _gather(where, **kw) -> dict:
    f = _facts_mod.gather(where, **kw)
    f["paths"] = [c[0] for c in repo.changed(where, publish.base_ref(where, kw["base"]), kw["head"])]
    return f


def _diff(where, base, sha, limit) -> str:
    text = repo.git(where, "diff", f"{publish.base_ref(where, base)}...{sha}") or ""
    lines = text.splitlines()
    if len(lines) > limit:
        lines = lines[:limit] + [f"… diff cut at {limit} lines"]
    return "\n".join(lines)


# ---- the parts --------------------------------------------------------------------------------

def _members(reg, project, branch, unit):
    """(reviewer Member or None — the writer's head, roster.Roster.head: a worker's division, else Build —,
    writer Member or None, Brief or None)."""
    r = roster.Roster(reg)
    b = brief.find(reg, unit, project=project) if unit else None
    writer = r.member(b.to) if b is not None and b.to else None
    if writer is None and branch:
        same = [m for s in r.sessions() if (m := r.member(s)) and m.project == project and m.branch == branch]
        same.sort(key=lambda m: m.kind != "feat")
        writer = same[0] if same else None
    return r.head(writer, project), writer, b


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


def checklist(text, res, cfg):
    """The head review's grammar (review.ask): a usage limit is the verdict `held` (§8.5), then a failed
    call has none, then parse()'s lone VERDICT line and its `- N: why` checklist lines."""
    if holds.is_limit(res.limit_text, cfg):
        return Verdict("held", ()), ""
    if res.is_error or res.failure:
        return None, res.failure or res.error or "the review call failed"
    word, found = parse(text)
    return (Verdict(word, tuple(found)), "") if word else (None, "no lone VERDICT line first in the answer")


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


def surface_of(text) -> str:
    """R-REVIEW-12: `user` when the answer has a `SURFACE: user` line (the change alters what a user sees or
    does), else `internal`."""
    found = {m.lower() for m in _SURFACE.findall(str(text or ""))}
    return "user" if "user" in found else "internal"


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
              "BUILD_SESSION": build_session, "PANE_LINES": cfg_rv["pane_lines"],
              "FACTS": fence("\n".join(_facts_mod.lines(f)), "facts"), "GOAL": one_line(fields.get("GOAL") or "-"),
              "TIER": f.get("tier"), "ACCEPT": fence(fields.get("ACCEPT") or "-", "accept"),
              "VERIFY": fence(fields.get("VERIFY") or "-", "verify"), "PRE": fence(pre_text, "pre-review"),
              "BASE": f["base"], "DIFF": fence(diff, "diff")}
    return templates.render(TEMPLATE.read_text(encoding="utf-8"), **{k: masked(v) for k, v in values.items()})


def make_call(reg, cfg, head, cwd, tag, timeout):
    """Build's model at the standard rung, read-only exactly as the pre-review's reviewer (§3.3,
    review.call_for); a Claude call gets Build's persona appended (Codex: prepended to the prompt)."""
    rung = ladder.rung(cfg, vendors.of(reg, head) or "opus", "standard")
    persona = (head.worktree / ".claude" / "agents" / f"{head.session}.md") if head.worktree else None
    return call_for(rung.model, cwd=cwd, timeout=timeout, effort=rung.effort, tag=tag, max_turns=MAX_TURNS,
                    append_file=persona if persona is not None and persona.is_file() else None)


def reviewer_vendor(reg, cfg, head) -> str:
    return vendors.vendor_of(ladder.rung(cfg, vendors.of(reg, head) or "opus", "standard").model)


def _persona_text(head) -> str:
    try:
        p = head.worktree / ".claude" / "agents" / f"{head.session}.md"
        return p.read_text(encoding="utf-8") + "\n\n" if p.is_file() else ""
    except (OSError, TypeError, AttributeError):
        return ""


def _notify(project, pr, sha, unit, head, text, *, quiet, to=None):
    ptr = inbox.make(unit or f"pr{pr}", "review", to or head.session, "info", head_sha=sha,
                     urgent=not quiet, summary=masked(text), via="review", quiet=True if quiet else None)
    inbox.post(ptr)


def _first_reason(findings, error):
    if error:
        return error
    return f"{findings[0][0]}: {findings[0][1]}" if findings else "-"


def _event(project, pr, verdict, queued):
    log.log("events", f"buildreview project={project} pr={pr} verdict={verdict} queued={'yes' if queued else 'no'}")


def _held(project, pr, sha, unit, head, vendor, hold) -> dict:
    """§8.5: no row (pending() keeps it; the heartbeat starts it once the hold is over), one quiet note."""
    _notify(project, pr, sha, unit, head, f"{holds.held_text(hold, vendor)} (stage {STAGE})", quiet=True)
    _event(project, pr, "held", False)
    return {"status": "held", "head": sha, "vendor": vendor}


# ---- the review -------------------------------------------------------------------------------

def _not_run(project, pr, sha, unit, head, why, quiet=False) -> dict:
    """An exception before the review: Build hears `why` (waking unless `quiet`); the result says it was told."""
    _notify(project, pr, sha, unit, head, f"PR #{pr} needs you: {why}", quiet=quiet)
    _event(project, pr, "error", False)
    return {"status": "error", "verdict": "error", "head": sha, "error": why}


def _ended(project, pr, sha, state, reg) -> None:
    """G8a: the heartbeat's pending markers of PR `pr` (pending(), may_start()) end quietly once the review
    it started reads the PR: all of them when it is not open, else those at another head that the
    heartbeat already claimed (a fresh push's marker may meet GitHub's lagging view of the head)."""
    starts, ended = _starts(project), False
    for x in pending(project, reg):
        key = _key(pr, x["sha"])
        if x["pr"] != pr:
            continue
        if state != "OPEN":
            starts.setdefault(key, {})["end"] = f"PR {state.lower()}"
        elif not _same(x["sha"], sha) and (starts.get(key) or {}).get("n"):
            starts[key]["end"] = "head moved"
        else:
            continue
        ended = True
    if ended:
        util.write_json(_starts_path(project), starts)


def build_review(project, pr, unit=None, *, wait_s=0, reg=None, cfg=None) -> dict:
    """Decide PR `pr` of `project` at its current head. {"status": off|no-head|error|already|busy|
    waiting|skipped|held|queued|returned|pane, ...}; every exception path but off/already/busy/skipped/
    waiting posts a waking pointer to Build (held: a quiet one)."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    rv = cfg["review"]
    if not rv["build"]:
        return {"status": "off"}
    root = units.project_root(reg, project)
    try:
        view = _pr(root, pr)
    except github.GhError as exc:
        view, gh_error = None, str(exc)
    head, writer, b = _members(reg, project, (view or {}).get("headRefName", ""), unit)
    if head is None:
        log.log("hooks", f"buildreview: {project}: no Build division; PR #{pr} not reviewed")
        return {"status": "no-head"}
    if view is None:
        # quiet: a gh outage must not wake Build once per start; MAX_STARTS gives up with one that wakes
        return _not_run(project, pr, "", unit, head, f"head review could not read the PR — {gh_error}", quiet=True)
    sha, state = view["headRefOid"], str(view.get("state") or "OPEN").upper()
    _ended(project, pr, sha, state, reg)
    if state != "OPEN":       # G8a: the heartbeat asks gh nothing; a closed or merged PR ends here, quietly
        return {"status": "closed", "head": sha, "state": view.get("state")}
    base = publish.base(root, cfg, project=project)
    if not base:
        return _not_run(project, pr, sha, unit, head, f"head review not run — {publish.no_base(root)}")
    h7 = sha[:7]
    if ledger.review_for(project, pr, sha, "build") is not None:
        return {"status": "already", "head": sha}
    timeout = float(BUILD_TIMEOUT_S)
    lock = _lock(project, pr, sha, 2 * timeout + 60)
    if lock is None:
        return {"status": "busy", "head": sha}
    try:      # always the PR's head in a detached worktree: never the writer's live one (no lock held there)
        with _checkout(project, pr, sha, root) as where:
            if where is None:
                return _not_run(project, pr, sha, unit, head, f"head review not run — head {h7} is not in "
                                                                f"{root}: git fetch origin, then orche review --pr {pr}")
            return _decide(reg, cfg, rv, project, pr, unit, view, sha, base, h7, root, where, head, writer, b,
                           timeout, wait_s)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _decide(reg, cfg, rv, project, pr, unit, view, sha, base, h7, root, where, head, writer, b, timeout, wait_s):
    mode = rv["cross_vendor"]
    pre_row = ledger.review_for(project, pr, sha, "pre")
    deadline = clock() + wait_s
    while pre_row is None and mode != "off" and clock() < deadline:
        sleep(min(10, max(0.0, deadline - clock())))
        pre_row = ledger.review_for(project, pr, sha, "pre")
    if pre_row is None and mode == "required":
        return {"status": "waiting", "head": sha}
    pre_v = pre_row["verdict"] if pre_row else "off"
    if pre_row is not None and pre_v not in PRE_OK:
        return {"status": "skipped", "head": sha, "pre": pre_v}
    tier = b.tier if b is not None else "standard"
    scope = b.fields.get("SCOPE", "") if b is not None else ""
    pre_model = pre_row.get("model") if pre_row else "-"
    pre_round = pre_row.get("round") if pre_row else "-"
    who = unit or f"pr{pr}"
    evidence = record_path(project, pr, sha)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    error, model, answer, findings, surface = None, "facts", "", [], "internal"
    try:
        f = _gather(where, project=project, pr=pr, head=sha, base=base, tier=tier, scope=scope,
                    docs_paths=queue.DOCS_PATHS, entry_id=queue.entry_id(pr, sha))
        findings = deterministic(f, int(rv["pane_lines"]))
    except Exception as exc:   # noqa: BLE001 — never silent: Build hears of it
        f, error = None, f"facts: {exc}"
    verdict = "return" if findings else ("error" if error else None)
    if verdict is None:
        try:
            from ..runs import history
            call = make_call(reg, cfg, head, where, procs.run_id(f"review-pr{pr}", time.time_ns()), timeout)
            vendor = vendors.vendor_of(call.model)
            hold = holds.active(vendor)
            if hold is not None:      # §8.5: no review of a held vendor starts
                holds.join(vendor, project, who, STAGE)
                return _held(project, pr, sha, unit, head, vendor, hold)
            hit = history.spend_hit(project, unit, cfg, tag=f"buildreview: {project}")
            if hit:                   # G4: Build decides
                verdict, error = "skipped", f"spend cap: head review not run ({hit})"
            else:
                model = call.model
                prompt = _prompt(f, b, pre_row, _diff(where, base, sha, pre.MAX_DIFF_LINES),
                                 head.session, unit, rv)
                if vendor == vendors.CODEX:     # a Claude call gets Build's persona appended instead
                    prompt = masked(_persona_text(head)) + prompt
                a = ask(model, prompt, checklist, cwd=where, cfg=cfg, call=call)
                res, answer = a.res, a.text
                if a.verdict is None:
                    verdict, error = "error", a.why
                else:
                    verdict, findings, surface = a.verdict.verdict, list(a.verdict.findings), surface_of(answer)
                try:
                    history.attempt(project, b.rev if b is not None else 1, unit=who, to=head.session, model=model,
                                    effort=getattr(call, "effort", None), res=res, started=a.started, finished=a.finished, status=verdict, kind=STAGE,
                                    cfg=cfg)
                except Exception as exc:   # noqa: BLE001 — the history line never decides the review
                    log.log("hooks", f"buildreview: {project} PR #{pr}: history line not written: {exc}")
                if verdict == "held":
                    hold = holds.hit(vendor, res.limit_text, cfg, project=project, unit=who, stage=STAGE)
                    if hold is not None:
                        return _held(project, pr, sha, unit, head, vendor, hold)
                    verdict, error = "error", one_line(res.failure or "a usage limit", 200)
        except Exception as exc:   # noqa: BLE001
            verdict, error = "error", masked(f"{type(exc).__name__}: {exc}")
    body = [f"# head review · PR #{pr} @ {h7}", "", f"verdict: {verdict}", f"model: {model}",
            f"reviewer: {head.name}", f"unit: {who}", f"pre-review: {pre_v} ({pre_model}, round {pre_round})",
            f"surface: {surface}", ""]
    body += [f"- {n}: {why}" for n, why in findings]
    if error:
        body.append(f"error: {error}")
    if f is not None:
        body += ["", "## Facts", *_facts_mod.lines(f)]
    if answer:
        body += ["", "## Answer (data, masked)", answer]
    util.atomic_write(evidence, masked("\n".join(body)) + "\n")
    try:
        ledger.record_review(project, "build", pr, sha, verdict, str(evidence), f"{head.session} (review)",
                             model, writer=writer.session if writer else "unknown", note=one_line(error or "", 200))
    except ledger.LedgerError as exc:
        log.log("hooks", f"buildreview: {project} PR #{pr}: ledger row refused: {exc}")
    return _act(project, pr, unit, view, sha, base, h7, where, head, writer, b, verdict, findings, error, pre_v,
                pre_model, pre_round, reg, cfg, surface)


def _act(project, pr, unit, view, sha, base, h7, where, head, writer, b, verdict, findings, error, pre_v,
         pre_model, pre_round, reg, cfg, surface):
    if verdict == "pass":     # R5: queued as the runner, through the queue's one admission
        try:
            res = queue.admit(project, int(view["number"]), sha, where=where, base=base,
                              branch=view.get("headRefName", ""), unit=unit, b=b, writer=writer.session if writer else "",
                              by="runner", approved_by=f"{head.session} (review)", surface=surface, reg=reg, cfg=cfg)
        except queue.Refused as exc:
            error, findings = f"the queue refused it: {exc}", []
        else:
            text = (f"auto-queued PR #{pr} @{h7}: pre-review {pre_v.upper()} ({pre_model}, round {pre_round}), "
                    f"head review PASS — position {res.get('position')} of {res.get('total')}")
            _notify(project, pr, sha, unit, head, text, quiet=True)
            _event(project, pr, verdict, True)
            return {"status": "queued", "head": sha, "position": res.get("position"), "total": res.get("total")}
    text = (f"PR #{pr} @{h7} needs you: pre-review {pre_v.upper()} ({pre_model}), head review {verdict.upper()} — "
            f"{_first_reason(findings, error)}")
    extra = [f"- {n}: {why}" for n, why in (findings[:3] if error else findings[1:4])]
    _notify(project, pr, sha, unit, head, "\n".join([text, *extra]), quiet=False)
    _event(project, pr, verdict, False)
    if verdict == "error":
        status = "error"
    elif verdict == "return" and not any(n in (7, 8) for n, _ in findings):
        status = "returned"
    else:                     # high risk or size, a spend-cap skip, a PASS the queue refused
        status = "pane"
    return {"status": status, "head": sha, "verdict": verdict, "findings": findings, "error": error}


# ---- the head review record (R-REVIEW-10, R-REVIEW-12) -------------------------------------------

def record_path(project, pr, sha, who="build") -> Path:
    """The head review record of (pr, head): `pr<N>-<h7>-build.md` from the headless review, `pr<N>-<h7>-pane.md`
    from the pane's PASS."""
    return _folder(project) / f"pr{pr}-{str(sha)[:7]}-{who}.md"


def record(project, pr, sha) -> dict:
    """The head review record's own fields at (pr, head) (`surface`: user or internal; `safe_because`, the pane's
    blast-radius fact): the headless record's, overlaid by the pane's; {} when neither exists. Only the lines above
    a record's first `## ` section count: the facts and the reviewer's answer below it are data."""
    out = {}
    for who in ("build", "pane"):
        try:
            lines = record_path(project, pr, sha, who).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("## "):
                break
            m = _FIELD.match(line)
            if m:
                out[m.group(1)] = m.group(2).strip()
    return out


def pane_record(project, pr, sha, *, reviewer, model, writer, surface, safe_because="", accept=None) -> Path:
    """The pane's PASS (`orche queue add`): its record `pr<N>-<h7>-pane.md` and a `build` row with verdict pass,
    so the head is decided once (R-REVIEW-10). A refused row is logged; the record stays."""
    path = record_path(project, pr, sha, "pane")
    lines = [f"# head review · PR #{pr} @ {str(sha)[:7]} (pane)", "", "verdict: pass", f"reviewer: {reviewer}",
             f"surface: {surface}"]
    if safe_because:
        lines.append(f"safe_because: {one_line(safe_because)}")
    if accept:
        lines.append(f"accept: {one_line(accept)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    util.atomic_write(path, masked("\n".join(lines)) + "\n")
    try:
        ledger.record_review(project, "build", pr, sha, "pass", str(path), reviewer, model or "pane",
                             writer=writer or "unknown")
    except ledger.LedgerError as exc:
        log.log("hooks", f"buildreview: {project} PR #{pr}: pane row refused: {exc}")
    return path


# ---- Build's own PRs (design §3.8, R33) ---------------------------------------------------------

@contextlib.contextmanager
def _checkout(project, pr, sha, root):
    """The PR's head to review: a temporary detached worktree of the project's checkout (the head fetched
    when missing, github.fetch_pr_head; one per process, removed after), never the writer's live worktree.
    None when the head is not there."""
    tmp = _folder(project) / f"wt-pr{pr}-{sha[:7]}-{os.getpid()}"
    try:
        github.fetch_pr_head(root, pr, sha)
    except github.GhError:
        pass                         # the worktree cannot be made: None
    shutil.rmtree(tmp, ignore_errors=True)
    shell.git(root, "worktree", "prune")
    made = shell.git(root, "worktree", "add", "--detach", str(tmp), sha, timeout=120).ok
    try:
        yield tmp if made else None
    finally:
        if made:
            shell.git(root, "worktree", "remove", "--force", str(tmp), timeout=120)
        shutil.rmtree(tmp, ignore_errors=True)


def _pre_round(reg, cfg, project, pr, unit, view, root) -> dict | None:
    """One pre-review round of the PR's head (pre.one_round: a reviewer of another vendor than the
    writer's model, row kind pre; a deep head's panel). None on PASS or skipped, else the result. With
    review.cross_vendor off, only a deep head is reviewed (R-REVIEW-13): None for any other."""
    from ..runs import history
    sha = view["headRefOid"]
    head, writer, b = _members(reg, project, view.get("headRefName", ""), unit)
    writer = writer or head
    if writer is None:
        return {"status": "no-head"}
    rev = b.rev if b is not None else 1
    unit_id = b.unit if b is not None else f"pr{pr}"
    unit_dir = b.folder if b is not None else _folder(project) / f"pr{pr}"

    def record(a, at):
        history.attempt(project, rev, unit=unit_id, to=writer.session, model=a.model,
                        effort=getattr(a.call, "effort", None), res=a.res, started=a.started,
                        finished=a.finished, status=a.word, kind="review", cfg=cfg, stop_reason=a.why or None)

    off = pre.mode_of(cfg) == "off"      # the head review reports a missing base or head itself
    base = publish.base(root, cfg, project=project)
    if not base:
        return None if off else {"status": "pre-error", "head": sha, "error": publish.no_base(root)}
    with _checkout(project, pr, sha, root) as wt:
        if wt is None:
            return None if off else {"status": "pre-error", "head": sha, "error": f"head {sha[:7]} is not in "
                                     f"{root}: git fetch origin, then orche review --pr {pr} again"}
        ctx = pre.Ctx(cfg=cfg, project=project, unit=unit_id, rev=rev, unit_dir=unit_dir, worktree=wt,
                      base=base, writer=writer.session,
                      writer_model=vendors.of(reg, writer), fields=dict(b.fields) if b is not None else {},
                      tag=procs.run_id(f"review-pr{pr}", time.time_ns()), record=record)
        if off and not pre.deep_of(ctx):
            return None
        hit = history.spend_hit(project, b.unit if b is not None else None, cfg, tag=f"buildreview: {project}")
        if hit:                   # G4 before the paid pre-review call
            return {"status": "pre-error", "head": sha, "error": f"spend cap: pre-review not run ({hit})"}
        prior = pre.returns(unit_dir, rev)
        report = ""
        if b is not None and (b.folder / "REPORT.md").is_file():
            report = (b.folder / "REPORT.md").read_text(encoding="utf-8", errors="replace")
        rnd = pre.one_round(ctx, len(prior) + 1, pr=pr, head=sha, report=report,
                            earlier=pre.earlier_text([(r, "") for r in prior]))
    if rnd.word in PRE_OK:
        return None
    return {"status": f"pre-{rnd.word}", "head": sha, "model": rnd.model, "round": rnd.k,
            "findings": list(rnd.findings), "error": rnd.why, "path": str(rnd.path or "")}


def for_pr(project, pr, unit=None, *, tell=None, reg=None, cfg=None) -> dict:
    """`orche review --pr N` (design §3.8, R33): the PR's pre-review at its head unless a pass, skipped
    or return row is there already (one round; row kind pre), then on PASS or skipped the head review
    (build_review), so `orche queue add --pr N` finds its pre row. A pre-review RETURN or error is the
    result; `tell` (the `--background` caller) hears it as a waking `review` pointer."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    root = units.project_root(reg, project)
    try:
        view = _pr(root, pr)
    except github.GhError as exc:     # nobody was told: the caller prints it
        return {"status": "error", "error": f"gh cannot read it ({masked(str(exc))}); nothing was reviewed"}
    sha = view.get("headRefOid") or ""
    if str(view.get("state") or "OPEN").upper() != "OPEN" or not sha:
        return {"status": "closed", "head": sha, "state": view.get("state")}
    row = ledger.review_for(project, pr, sha, "pre")
    if row is None or row.get("verdict") == "error":     # off: _pre_round reviews a deep head only
        out = _pre_round(reg, cfg, project, pr, unit, view, root)
        if out is not None:
            if tell and out["status"] != "no-head":
                lines = [f"PR #{pr} @{sha[:7]}: pre-review {out['status'][4:].upper()} by {out.get('model')} "
                         f"(round {out.get('round')}) — {one_line(out.get('error') or 'fix and push, then orche review --pr ' + str(pr), 160)}",
                         *[one_line(x, 160) for x in out.get("findings", [])[:3]]]
                head = _members(reg, project, view.get("headRefName", ""), unit)[0]
                if head is not None:
                    _notify(project, pr, sha, unit, head, "\n".join(lines), quiet=False, to=tell)
            return out
    return build_review(project, pr, unit, reg=reg, cfg=cfg)


# ---- hooks --------------------------------------------------------------------------------------

def after_publish(project, pr, unit) -> bool:
    """The run's publish hook. True when a head review decides this PR — started now (`orche review
    --pr N --wait`, detached) or left to the heartbeat (inside a sandbox, or the spawn failed: RUN.json
    says `review.build: pending`, which pending() finds) — so the run files its done quiet (Appendix
    A.11: one done; the review's pointer wakes only on an exception). False when none will
    (review.build off, no Build division). Never raises."""
    try:
        if not config.load()["review"]["build"] or _members(registry.load(), project, "", unit)[0] is None:
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
    argv = ["review", "--pr", str(pr), "--project", project, "--wait"]
    if unit:
        argv += ["--unit", unit]
    procs.spawn_orche(argv, paths.orche_home() / "logs" / "review" / f"{project}-pr{pr}.log")


def running(project, pr, sha) -> bool:
    """A head review of (pr, head) holds its lock now: the lock exists and is not yet stale."""
    stale_s = 2 * float(BUILD_TIMEOUT_S) + 60
    try:
        return time.time() - (_folder(project) / f"pr{pr}-{sha[:7]}.lock").stat().st_mtime < stale_s
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
    the unit's BRIEF.md) filed a `done` with `review.build: pending` — the run started its head review
    or left it to the heartbeat; pane units never do (§3.7: the pane is there) — with no build row at
    that head and not given up (may_start). [] with review.build off. Reads the review rows once."""
    if not config.load()["review"]["build"]:
        return []
    reg = reg if reg is not None else registry.load()
    root = units.project_root(reg, project)
    if not root:
        return []
    reviewed = {}
    for row in ledger.read_reviews(project):
        if row.get("kind") == "build":
            reviewed.setdefault(str(row.get("pr")), []).append(row.get("sha") or "")
    from .. import runs
    starts, out = _starts(project), []
    for unit, rec, _ns in runs.last_runs(root, fresh=True):
        if rec.get("status") != "done":
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
        out.append({"pr": int(number), "sha": sha, "unit": unit, "finished": str(rec.get("finished") or "")})
    out.sort(key=lambda x: x["finished"])
    return out


def may_start(project, item, reg=None, cfg=None) -> bool:
    """The heartbeat's check before start(): True → start `item` now (the start is counted). False →
    not now: the reviewer's vendor is held (not counted), or it was started MAX_STARTS times
    without a verdict (given up with one waking pointer to Build). No gh call (the heartbeat's watchdog):
    the review it starts ends the marker of a closed PR or a moved head quietly (_ended)."""
    reg = reg if reg is not None else registry.load()
    cfg = cfg if cfg is not None else config.load()
    head = _members(reg, project, "", item.get("unit"))[0]
    if head is None or holds.active(reviewer_vendor(reg, cfg, head)) is not None:
        return False
    pr, sha, unit = item["pr"], item["sha"], item.get("unit")
    starts = _starts(project)
    entry, n = dict(starts.get(_key(pr, sha)) or {}), int((starts.get(_key(pr, sha)) or {}).get("n") or 0)
    if n >= MAX_STARTS:
        entry["end"] = "gave up"
        row = ledger.review_for(project, pr, sha, "pre") or {}
        _notify(project, pr, sha, unit, head,
                f"PR #{pr} @{sha[:7]} needs you: pre-review {str(row.get('verdict') or 'off').upper()} "
                f"({row.get('model') or '-'}), head review not run — started {n} times without a verdict "
                f"(log: logs/review/{project}-pr{pr}.log)", quiet=False)
        _event(project, pr, "gave-up", False)
    else:
        entry.update(n=n + 1, at=util.now_iso())
    starts[_key(pr, sha)] = entry
    util.write_json(_starts_path(project), starts)
    return "end" not in entry


HEARTBEAT_CHECKS = 3       # may_start() checks (no gh call) per project a heartbeat pass


def heartbeat(reg, keys, dry_run, deadline) -> list:
    """The heartbeat's class `review` (reconcile): per project, its oldest pending Build review (pending())
    that is not running and that may_start() lets start -> start(project, pr, unit), detached: the pass
    never runs a review inline. At most one start per project a pass and HEARTBEAT_CHECKS may_start()
    checks, none once time.monotonic() reaches `deadline`, none for a project the owner paused. Rows as
    owner.heartbeat's."""
    from ..flow import guard
    rows = []
    for key in sorted(reg.get("projects", {})):
        if (keys is not None and key not in keys) or guard.paused(key) is not None:
            continue
        if time.monotonic() >= deadline:
            break
        try:
            items = list(pending(key, reg) or [])
            todo = [x for x in items if not running(key, x["pr"], x["sha"])]
        except Exception as exc:  # noqa: BLE001 - one project's reviews never stop the pass
            log.log("hooks", f"reconcile: review pending {key}: {type(exc).__name__}: {exc}")
            continue
        for item in todo[:HEARTBEAT_CHECKS]:
            pr, failed = item["pr"], None
            if not dry_run:
                try:
                    if not may_start(key, item, reg):
                        continue
                    start(key, pr, item.get("unit"))
                except Exception as exc:  # noqa: BLE001 - it is pending again next pass
                    failed = type(exc).__name__
                    log.log("hooks", f"reconcile: build review {key} #{pr}: {failed}: {exc}")
            rows.append({"action": "build-review", "class": "review", "project": key, "target": f"pr-{pr}",
                         "rev": None, "attempt": 1, "cap": 1, "failed": failed,
                         "line": f"{'would start' if dry_run else 'start'} the head review of {key} PR #{pr} "
                                 f"({len(items)} pending)"})
            break
    return rows
