"""The cross-vendor pre-review (design §3.1, §3.3, §3.7; R-REVIEW-1, 2, 4, 5, 6, 8, 9; rulings R3, R11,
R12, R32).

Inside a gated unit's run, after its verified done was published (job.py calls in_run): when the
branch differs from the base, a reviewer of another vendor reads the change read-only (review.ask)
and answers PASS or RETURN. RETURN → the writer reworks in the same run (Appendix A.8 appended to
the brief; a mechanical unit at the standard rung), ORCHE publishes again when HEAD moved (a
rebut-only rework goes straight on), and the next round runs. At most `review.rounds` (2) RETURNs
per unit revision; then the done goes to the Build pane with the open findings. No AMEND: G1/G3
never count review rounds (R12). A STOP between stages files `info` "stopped by Main"; G4 is
checked before every paid call (this run's calls included).

Reviewer (choose): the first model of `review.order` whose vendor differs from the writer's model's,
is not held (§8.5: flow/usage.py) and is available (Codex: vendors.available; Claude always). A held
vendor is skipped with a note (`fallback: same vendor (codex held: usage limit until 15:00)`); every
candidate held → the round is `held` (no row; the run records the hold, stage review). None: `required` → the first
model of the order that is not the writer's, noted `fallback: same vendor (codex not installed)`
(R3: Opus ↔ Sonnet); `optional` → a `skipped` row with that note; `off` → no review. A reviewer
without a parsable verdict (timeout, error, no VERDICT line) hands over to the next model of the
order once; still nothing → verdict `error` (the pane). A reviewer that changed files → `error`.

Records: `.orche/units/<unit>/REVIEW.pre.r<k>.md` (a header naming the revision, the findings, the
masked answers; the revision's RETURNs are counted from these across runs and reports), one
`ledger/<p>.review.tsv` row per round (ledger.record_review; a pass row for (PR, HEAD) is reused,
so a head is never reviewed twice), every call an attempt of the run (Ctx.record: `kind: review`,
its cost in the run and a history line) and an events line `prereview unit= reviewer= verdict=
round=`.

Pane units (`orche report done`, pane_round): one round, synchronously, within PANE_BUDGET_S;
RETURN refuses the done (exit 3) with the findings; once the revision has `review.rounds` RETURNs
the next report files the done with the open findings.
"""
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .. import config, ledger, log, procs, publish, shell, templates, util, vendors
from ..flow import usage as holds
from . import CHANGED, ask, fence, masked, one_line

MODES = ("required", "optional", "off")
STOPPED = "stopped by Main"
PANE_BUDGET_S = 540      # the pane's round inside the agent's 600000 ms Bash call (publish took the rest)
MIN_CALL_S = 30          # a hand-over call with less time left is not started
REPORT_CHARS = 2000
ANSWER_CHARS = 1500      # a rework's answer quoted to the next round
OPEN_LINES = 3
TIMEOUT_S = 480         # one reviewer call
EFFORT = "medium"
MAX_DIFF_LINES = 3000   # the package's diff, then it says so
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
_WHY = {"codex not installed": "codex is not installed"}
_HEADER = re.compile(r"^# pre-review r(\d+) — (\S+) rev (\d+) ")


def mode_of(cfg) -> str:
    mode = str(cfg["review"]["cross_vendor"] or "required").lower()
    return mode if mode in MODES else "required"


def rounds_of(cfg) -> int:
    try:
        return max(1, int(cfg["review"]["rounds"] or 2))
    except (TypeError, ValueError):
        return 2


# ---- who reviews ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    model: str | None    # None: skipped (optional mode, no second vendor), or held
    why: str             # "" or why no second vendor reviews ("codex not installed" | "codex does not start" |
                         # "codex held: usage limit until 15:00")
    rest: tuple          # the models after it in the order a failed reviewer hands over to (once)
    held: str = ""       # the vendor whose usage hold left no reviewer (model None)

    @property
    def note(self) -> str:
        return f"fallback: same vendor ({self.why})" if self.why else ""


def choose(cfg, writer_model, avail=None) -> Choice:
    """The reviewer of a head written by `writer_model` (R-REVIEW-2, R3; §8.5: held vendors skipped)."""
    order = [str(m) for m in cfg["review"]["order"] if m]
    writer_vendor = vendors.vendor_of(writer_model)
    state, held = {"avail": avail}, {}

    def hold_of(model):
        vendor = vendors.vendor_of(model)
        if vendor not in held:
            held[vendor] = holds.active(vendor)
        return held[vendor]

    def up(model) -> bool:
        if hold_of(model) is not None:
            return False
        if vendors.vendor_of(model) != vendors.CODEX:
            return True
        if state["avail"] is None:
            state["avail"] = vendors.available(cfg)
        return state["avail"].ok

    def after(model) -> tuple:
        tail = order[order.index(model) + 1:] if model in order else []
        return tuple(m for m in tail if m != writer_model and up(m))

    cross = next((m for m in order if vendors.vendor_of(m) != writer_vendor and up(m)), None)
    if cross is not None:
        return Choice(cross, "", after(cross))
    avail = state["avail"]
    cross_held = next((v for v, h in held.items() if h is not None and v != writer_vendor), None)
    if cross_held is not None:
        why = f"{cross_held} held: usage limit until {holds.hhmm(holds.until_of(held[cross_held]))}"
    else:
        why = "codex not installed" if avail is None or avail.state == "absent" else "codex does not start"
    if mode_of(cfg) == "optional":
        return Choice(None, why, ())
    same = next((m for m in order if m != writer_model and up(m)), None)
    if same is None:
        same = SONNET if writer_model == OPUS else OPUS
        blocked = next((v for v, h in held.items() if h is not None), None)
        if blocked is not None or hold_of(same) is not None:
            return Choice(None, why, (), blocked or vendors.vendor_of(same))
    return Choice(same, why if vendors.vendor_of(same) == writer_vendor else "", after(same))


# ---- the package -----------------------------------------------------------------------------------

def has_diff(worktree, ref) -> bool:
    """Whether `<ref>...HEAD` changes anything (git's error counts as a change: review rather than skip)."""
    return shell.git(worktree, "diff", "--quiet", f"{ref}...HEAD").code != 0


def _template(name) -> str:
    return (templates.TEMPLATES_ROOT / "review" / name).read_text(encoding="utf-8")


def _clip(text, limit) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n… (truncated)"


def package(ctx, *, pr, head, k, rounds, reviewer, why, report, earlier) -> str:
    """Appendix A.6 rendered: every value masked, the untrusted ones fenced as data."""
    ref = publish.base_ref(ctx.worktree, ctx.base)
    limit = MAX_DIFF_LINES
    stat = shell.git(ctx.worktree, "diff", "--stat=1000", f"{ref}...HEAD").out or ""     # every path, in full
    lines = (shell.git(ctx.worktree, "diff", f"{ref}...HEAD").out or "").splitlines()
    base7 = (shell.git(ctx.worktree, "rev-parse", "--short=7", ref, timeout=20).out or ref).strip()[:7]
    fields = ctx.fields or {}
    values = {
        "UNIT": ctx.unit, "PR": pr, "HEAD7": str(head)[:7], "ROUND": k, "ROUNDS": rounds, "REVIEWER": reviewer,
        "FALLBACK_NOTE": f" (a different model of the writer's vendor: {_WHY.get(why, why)})" if why else "",
        "WRITER_MODEL": ctx.writer_model, "GOAL": one_line(fields.get("GOAL")), "SCOPE": one_line(fields.get("SCOPE")),
        "ACCEPT": fence(fields.get("ACCEPT"), "accept"), "VERIFY": fence(fields.get("VERIFY"), "verify"),
        "REPORT": fence(_clip(report, REPORT_CHARS), "report"),
        "EARLIER": fence(earlier, "earlier") if earlier else "(none)",
        "BASE7": base7, "STAT": fence(stat, "stat"),
        "TRUNCATED": (f" (the first {limit} of {len(lines)} lines; the rest is not shown: every changed file is "
                      "in the stat above, open one with Read)") if len(lines) > limit else "",
        "DIFF": fence("\n".join(lines[:limit]), "diff"),
    }
    return templates.render(_template("pre.md"), **{name: masked(v) for name, v in values.items()})


def rework_prompt(rnd, rounds) -> str:
    """Appendix A.8 for a REWORK attempt (appended to the brief)."""
    return "\n\n" + templates.render(_template("rework.md"), ROUND=rnd.k, ROUNDS=rounds, REVIEWER=rnd.model,
                                     HEAD7=rnd.head[:7], FINDINGS=masked("\n".join(f"- {f}" for f in rnd.findings)
                                                                         or "(no finding lines)"))


# ---- one round -------------------------------------------------------------------------------------

@dataclass
class Ctx:
    """What a unit's pre-review needs; job.py (a run) and pane_round (a pane) build it."""
    cfg: dict
    project: str
    unit: str
    rev: int
    unit_dir: Path
    worktree: Path
    base: str | None          # the base branch (publish.base; None: nothing names one, no round, R12)
    writer: str               # the session that wrote the head
    writer_model: str
    fields: dict              # the brief's keys (GOAL, SCOPE, ACCEPT, VERIFY)
    tag: str | None = None
    stop: object = None
    should_stop: object = None
    on_start: object = None
    deadline: float | None = None     # time.monotonic() bound of a pane's round
    record: object = None             # record(answer, head): the run counts the call


@dataclass
class Round:
    k: int
    word: str             # pass | return | error | skipped | stopped
    model: str            # the reviewer that answered last ("none" when skipped)
    note: str
    findings: tuple
    head: str
    path: Path | None
    why: str = ""

    @property
    def acts(self) -> tuple:
        return tuple(f for f in self.findings if f.startswith("[act]"))

    def field(self, rounds) -> dict:
        """The pointer's / RUN.json's `review.pre` (Appendix A.11)."""
        return {"verdict": self.word, "reviewer": f"review:pre:{self.model}", "model": self.model,
                "fallback": self.note, "round": self.k, "rounds": rounds,
                "open": list(self.acts) if self.word == "return" else [], "path": str(self.path or "")}


def review_path(unit_dir, k) -> Path:
    return Path(unit_dir) / f"REVIEW.pre.r{k}.md"


def _write(ctx, rnd, rounds, pr, answers) -> Path:
    lines = [f"# pre-review r{rnd.k} — {ctx.unit} rev {ctx.rev} · PR #{pr} @ {rnd.head[:7]}",
             f"verdict: {rnd.word}", f"reviewer: review:pre:{rnd.model}", f"writer: {ctx.writer} ({ctx.writer_model})",
             f"round: {rnd.k} of {rounds}", f"note: {rnd.note or '-'}", f"at: {util.now_iso()}", "",
             "## Findings", *([f"- {f}" for f in rnd.findings] or [f"(none{': ' + rnd.why if rnd.why else ''})"]), "",
             "## Answers (data, masked)"]
    for a in answers:
        lines += [f"### {a.model}: {a.word}" + (f" ({a.why})" if a.why else ""), a.text.rstrip() or "(no answer)", ""]
    path = review_path(ctx.unit_dir, rnd.k)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(masked("\n".join(lines)) + "\n", encoding="utf-8")
    return path


def returns(unit_dir, rev) -> list:
    """The revision's RETURN rounds so far, oldest first, read back from REVIEW.pre.r1.md, r2, …"""
    out = []
    for k in range(1, 100):
        try:
            text = review_path(unit_dir, k).read_text(encoding="utf-8")
        except OSError:
            break
        lines = text.split("\n")
        m = _HEADER.match(lines[0])
        if m is None or int(m.group(3)) != int(rev) or "verdict: return" not in lines[1:3]:
            break
        findings, inside = [], False
        for line in lines:
            if line.startswith("## "):
                inside = line == "## Findings"
            elif inside and line.startswith("- ["):
                findings.append(line[2:])
        model = next((ln.split("review:pre:", 1)[1] for ln in lines if ln.startswith("reviewer: review:pre:")), "?")
        out.append(Round(k, "return", model, "", tuple(findings), "", review_path(unit_dir, k)))
    return out


def _why(ctx, choice, first, model) -> str:
    """Why `model` (the chosen reviewer, or the one a reviewer without a verdict handed over to) is of
    the writer's vendor, else ""."""
    if vendors.vendor_of(model) != vendors.vendor_of(ctx.writer_model):
        return ""
    return choice.why if first is None else (choice.why or f"{first.model} gave no verdict")


def one_round(ctx, k, *, pr, head, report, earlier) -> Round:
    """Round k: the chosen reviewer, the next one once if it gave no verdict; the REVIEW file, the row,
    the events line. Never raises for what a reviewer does."""
    rounds = rounds_of(ctx.cfg)
    choice = choose(ctx.cfg, ctx.writer_model)
    if choice.model is None and choice.held:     # §8.5: no reviewer of an unheld vendor; no row
        return Round(k, "held", "none", choice.note, (), head, None, choice.held)
    if choice.model is None:
        rnd = Round(k, "skipped", "none", choice.note, (), head, None)
        return _recorded(ctx, rnd, rounds, pr, [], second_vendor=False)
    answers, note = [], choice.note
    for i, model in enumerate((choice.model,) + choice.rest[:1]):
        timeout = float(TIMEOUT_S)
        if ctx.deadline is not None:
            left = ctx.deadline - time.monotonic()
            if left < MIN_CALL_S:
                break
            timeout = min(timeout, left)
        why = _why(ctx, choice, answers[0] if answers else None, model)
        note = f"fallback: same vendor ({why})" if why else ""
        text = package(ctx, pr=pr, head=head, k=k, rounds=rounds, reviewer=model, why=why, report=report,
                       earlier=earlier)
        a = ask(model, text, cwd=ctx.worktree, cfg=ctx.cfg, timeout=timeout, effort=EFFORT,
                tag=ctx.tag, stop=ctx.stop, should_stop=ctx.should_stop, on_start=ctx.on_start)
        answers.append(a)
        if ctx.record is not None:
            ctx.record(a, head)
        if a.verdict is not None or a.stopped or a.why == CHANGED:
            break
    last = answers[-1] if answers else None
    if last is None:
        return _recorded(ctx, Round(k, "error", choice.model, note, (), head, None, "no time left for the review"),
                         rounds, pr, answers, second_vendor=not choice.why)
    if last.stopped:
        return Round(k, "stopped", last.model, note, (), head, None, "stopped")
    why = "; ".join(f"{a.model}: {a.why}" for a in answers if a.why)
    if last.verdict is None:
        rnd = Round(k, "error", last.model, one_line(f"{note + '; ' if note else ''}{why}", 240), (), head, None, why)
    else:
        rnd = Round(k, last.verdict.verdict, last.model, note, last.verdict.findings, head, None)
    return _recorded(ctx, rnd, rounds, pr, answers, second_vendor=not choice.why)


def _recorded(ctx, rnd, rounds, pr, answers, *, second_vendor) -> Round:
    rnd.path = _write(ctx, rnd, rounds, pr, answers)
    try:
        ledger.record_review(ctx.project, "pre", pr, rnd.head, rnd.word, str(rnd.path), f"review:pre:{rnd.model}",
                             rnd.model, writer=ctx.writer, round=rnd.k, note=rnd.note, writer_model=ctx.writer_model,
                             mode=mode_of(ctx.cfg), second_vendor=second_vendor)
    except (ledger.LedgerError, OSError) as exc:
        log.log("hooks", f"pre-review: {ctx.unit}: review row not written: {exc}")
    log.log("events", f"prereview unit={ctx.unit} reviewer={rnd.model} verdict={rnd.word} round={rnd.k}")
    return rnd


def _reused(ctx, pr, head):
    row = ledger.review_for(ctx.project, pr, head, "pre")
    if row is None or row.get("verdict") != "pass":
        return None
    k = int(row.get("round") or 1) if str(row.get("round") or "").isdigit() else 1
    return Round(k, "pass", row.get("model") or "?", row.get("note") or "", (), head,
                 Path(row["evidence"]) if row.get("evidence") else None, "reused")


# ---- inside the run ---------------------------------------------------------------------------------

@dataclass
class Done:
    """What the run files: the published done before the stage, the outcome after it."""
    status: str
    summary: str
    owner: object
    pr: dict | None
    result: str
    head: str
    field: dict | None = None
    stopped: bool = False
    held: str = ""            # §8.5: the vendor whose hold stopped the review stage (no reviewer left)


def pr_line(pr) -> str:
    return f"PR: #{pr['number']} {pr.get('url', '')} head {pr.get('head', '')}"


def _passed(done, rnd, rounds) -> Done:
    said = (f"pre-review skipped: {rnd.note}" if rnd.word == "skipped"
            else f"pre-review PASS by {rnd.model} (round {rnd.k} of {rounds}{', reused' if rnd.why == 'reused' else ''})")
    return replace(done, summary=f"{done.summary}\n{said}".strip(),
                   result=f"{str(done.result or '').rstrip()}\n{said}: {rnd.path or '-'}\n",
                   field={"pre": rnd.field(rounds)})


def exception(done, rnd, rounds, reason=None) -> Done:
    """Appendix A.11 "an exception": the done goes to the Build pane (waking) with the open findings."""
    acts = rnd.acts if rnd.word == "return" else ()
    first = reason or (acts[0] if acts else rnd.why or rnd.word)
    lines = [f"PR #{done.pr['number']} @{str(done.head)[:7]} needs you: pre-review {rnd.word} ({rnd.model}, "
             f"round {rnd.k} of {rounds}), head review not run — {one_line(first, 160)}",
             *[one_line(f, 160) for f in acts[:OPEN_LINES]]]
    said = "\n".join(lines)
    return replace(done, status="done", summary=said,
                   result=f"{str(done.result or '').rstrip()}\n\n{said}\nReview: {rnd.path or '-'}\n",
                   field={"pre": rnd.field(rounds)})


def earlier_text(rounds_done) -> str:
    parts = []
    for rnd, answer in rounds_done:
        parts.append(f"Round {rnd.k}: {rnd.word.upper()} by {rnd.model}\n" + "\n".join(f"- {f}" for f in rnd.findings))
        if answer:
            parts.append(f"The writer's answer to round {rnd.k}:\n{_clip(answer, ANSWER_CHARS)}")
    return "\n\n".join(parts)


def in_run(ctx, done, *, rework, publish_again, stopped, capped, resume=None) -> Done:
    """PRE-REVIEW round k → PASS | RETURN, k < rounds → REWORK → publish (HEAD moved) → round k+1 |
    RETURN at the cap or error → the Build pane (design §3.1). Every reviewer held → `held` (§8.5).
    `resume` "rework": this run resumes one whose REWORK hit a usage limit at this HEAD (§8.5): the
    revision's last RETURN is reworked first, without a new round.

    `done` is the published done; rework(text) → (status, summary, owner, head, result, stopped) of a
    REWORK attempt; publish_again(summary, owner, head, result) → (status, summary, owner, pr, result);
    stopped() → a STOP or SIGTERM between stages; capped() → G4's text before a paid call, else None."""
    if mode_of(ctx.cfg) == "off" or done.pr is None:
        return done
    rounds = rounds_of(ctx.cfg)
    history = [(r, "") for r in returns(ctx.unit_dir, ctx.rev)]
    again = history.pop()[0] if resume == "rework" and history and history[-1][0].k < rounds else None
    k = len(history) + 1
    while True:
        if not ctx.base or not has_diff(ctx.worktree, publish.base_ref(ctx.worktree, ctx.base)):
            return done
        if again is not None:     # the resumed rework of the last RETURN (round k)
            rnd, again = replace(again, head=done.head), None
        else:
            reused = _reused(ctx, done.pr["number"], done.head)
            if reused is not None:
                return _passed(done, reused, rounds)
            if k > rounds:        # this revision already had its RETURNs (a re-run): the pane decides
                return exception(done, history[-1][0], rounds)
            if stopped():
                return replace(done, status="info", summary=STOPPED, stopped=True)
            hit = capped()
            if hit:
                return exception(done, Round(k, "skipped", "none", "", (), done.head, None), rounds,
                                 f"spend cap: review skipped ({hit})")
            rnd = one_round(ctx, k, pr=done.pr["number"], head=done.head, report=done.result,
                            earlier=earlier_text(history))
            if rnd.word == "stopped":
                return replace(done, status="info", summary=STOPPED, stopped=True)
            if rnd.word == "held":
                return replace(done, held=rnd.why)
            if rnd.word in ("pass", "skipped"):
                return _passed(done, rnd, rounds)
            if rnd.word == "error" or k >= rounds:
                return exception(done, rnd, rounds)
        if stopped():
            return replace(done, status="info", summary=STOPPED, stopped=True, field={"pre": rnd.field(rounds)})
        hit = capped()
        if hit:
            return exception(done, rnd, rounds, f"spend cap: rework skipped ({hit})")
        status, summary, owner, head, result, was_stopped = rework(rework_prompt(rnd, rounds))
        field = {"pre": rnd.field(rounds)}
        if was_stopped:
            return replace(done, status="info", summary=STOPPED, head=head, stopped=True, field=field)
        if status != "done":
            return Done(status, summary, owner, done.pr, result, head, field)
        history.append((rnd, masked(result)))
        if head != done.head:
            status, summary, owner, pr, result = publish_again(summary, owner, head, result)
            if pr is None:        # a publish gap: info "unverified: …"
                return Done(status, summary, owner, done.pr, result, head, field)
            done = Done(status, summary, owner, pr, result, head)
        else:                     # rebuttals only: the same head goes to the next round
            done = Done("done", f"{summary}\n{pr_line(done.pr)}".strip(), owner, done.pr,
                        f"{str(result or '').rstrip()}\n\n{pr_line(done.pr)}\n", head)
        k += 1


# ---- a pane unit's `orche report done` ----------------------------------------------------------------

@dataclass
class Pane:
    refused: bool          # RETURN: exit 3, nothing filed
    text: str              # what `orche report` prints on a refusal
    summary: str           # the pointer's summary otherwise
    field: dict | None     # the pointer's `review`


def pane_round(reg, member, worktree, project, b, report_text, summary, published, note=None) -> Pane:
    """R32 / R-REVIEW-9: one synchronous round for a pane member's verified, published done.
    `note(text)` is called once right before a round actually runs (cmd_report's quiet `verifying` note)."""
    from ..runs import history
    cfg = config.load()
    root = (reg.get("projects", {}).get(project) or {}).get("path") or worktree
    rev = b.rev if b is not None else 1
    to = member.session

    def record(a, head):
        try:
            history.attempt(project, rev, unit=b.unit, to=to, model=a.model, effort=getattr(a.call, "effort", None), res=a.res, started=a.started,
                            finished=a.finished, status=a.word, kind="review", cfg=cfg, stop_reason=a.why or None)
        except OSError as exc:
            log.log("hooks", f"pre-review: {b.unit}: history line not written: {exc}")

    ctx = Ctx(cfg=cfg, project=project, unit=b.unit, rev=rev, unit_dir=b.folder, worktree=Path(worktree),
              base=publish.base(root, cfg, worktree), writer=to, writer_model=vendors.of(reg, member), fields=dict(b.fields),
              tag=procs.run_id(f"{b.unit}-review", time.time_ns()), deadline=time.monotonic() + PANE_BUDGET_S,
              record=record)
    pr = published.record()
    done = Done("done", summary, None, pr, report_text, published.head)
    if mode_of(cfg) == "off" or not ctx.base or not has_diff(ctx.worktree, publish.base_ref(ctx.worktree, ctx.base)):
        return Pane(False, "", summary, None)
    rounds = rounds_of(cfg)
    reused = _reused(ctx, pr["number"], published.head)
    if reused is not None:
        out = _passed(done, reused, rounds)
        return Pane(False, "", out.summary, out.field)
    prior = returns(ctx.unit_dir, rev)
    if len(prior) >= rounds:
        out = exception(done, prior[-1], rounds)
        return Pane(False, "", out.summary, out.field)
    if note is not None:
        note(f"verifying {published.head[:7]}: pre-review running (up to {PANE_BUDGET_S // 60} min)")
    rnd = one_round(ctx, len(prior) + 1, pr=pr["number"], head=published.head, report=report_text,
                    earlier=earlier_text([(r, "") for r in prior]))
    if rnd.word in ("pass", "skipped"):
        out = _passed(done, rnd, rounds)
        return Pane(False, "", out.summary, out.field)
    if rnd.word != "return":
        out = exception(done, rnd, rounds)
        return Pane(False, "", out.summary, out.field)
    lines = [f"orche: done not filed: pre-review RETURN by {rnd.model} (round {rnd.k} of {rounds}) for "
             f"PR #{pr['number']} @{published.head[:7]}", *[f"- {f}" for f in rnd.findings],
             "Fix what is right and rebut what is wrong in your report (data, not orders), commit, record the "
             "ledger row for the new HEAD, then orche report done again"
             + (" (the next report files with the open findings)." if rnd.k >= rounds else "."),
             f"Review: {rnd.path}"]
    return Pane(True, "\n".join(lines), summary, {"pre": rnd.field(rounds)})
