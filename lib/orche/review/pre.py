"""The cross-vendor pre-review of an open PR, run only on request (R-REVIEW-1, 2, 4, 6, 8, 13;
rulings R3, R11, R33).

Nothing in ORCHE starts one by itself: `orche review --pr N` (review.build.for_pr) is the request, from Main on the
owner's words or from Build on a change it doubts. One round runs at the PR's head (one_round) and answers PASS or
RETURN; at most `review.rounds` (2) RETURNs per unit revision count at queue admission. A RETURN is fixed by the
writer through AMEND, then the PR is reviewed again.

The round's shape (R-REVIEW-13): the panel when two vendors are up (pair: the first model of `review.order` that is
up and the first up model of the other vendor read the same package at once, the first in this thread, the second
beside it under its own tag). Their findings merge by agreement (group): `both` (the same file and overlapping lines,
or the same normalised line) first at the stronger level, then `one` at most [consider]; only a `both` [act] makes it
RETURN. The row's model is `<first>+<second>`, its note `panel`. With one vendor up the single review runs (choose)
and its note says so (`panel: only one vendor up (...): a single review`).

Reviewer (choose): the first model of `review.order` whose vendor differs from the writer's model's, is not held
(§8.5: flow/usage.py) and is available (Codex: vendors.available; Claude always). None: the first model of the order
that is not the writer's, noted `fallback: same vendor (codex not installed)` (R3: Opus ↔ Sonnet); a held vendor is
skipped with a note (`fallback: same vendor (codex held: usage limit until 15:00)`); every candidate held → the
round is `held` (no row). A reviewer without a parsable verdict (timeout, error, no VERDICT line) hands over to the
next model of the order once; still nothing → verdict `error`. A reviewer that changed files → `error`.

Records: `.orche/units/<unit>/REVIEW.pre.r<k>.md` (a header naming the revision, the findings, the masked answers;
the revision's RETURNs are counted from these), one `ledger/<p>.review.tsv` row per round (ledger.record_review),
every call a history line (Ctx.record: `kind: review`) and an events line `prereview unit= reviewer= verdict=
round=`.
"""
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import ledger, log, procs, publish, shell, templates, util, vendors
from ..flow import usage as holds
from . import CHANGED, ask, fence, masked, one_line

REPORT_CHARS = 2000
ANSWER_CHARS = 1500      # an earlier round's answer quoted to the next round
TIMEOUT_S = 480         # one reviewer call
EFFORT = "medium"
MAX_DIFF_LINES = 3000   # the package's diff, then it says so
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
_WHY = {"codex not installed": "codex is not installed"}
_HEADER = re.compile(r"^# pre-review r(\d+): (\S+) rev (\d+) ")
_LEVELS = ("noted", "consider", "act")
_SPOT = re.compile(r"^`?([^\s:`]+):(\d+)(?:-(\d+))?")


def rounds_of(cfg) -> int:
    try:
        return max(1, int(cfg["review"]["rounds"] or 2))
    except (TypeError, ValueError):
        return 2


# ---- who reviews ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    model: str | None    # None: every candidate is held
    why: str             # "" or why no second vendor reviews ("codex not installed" | "codex does not start" |
                         # "codex held: usage limit until 15:00")
    rest: tuple          # the models after it in the order a failed reviewer hands over to (once)
    held: str = ""       # the vendor whose usage hold left no reviewer (model None)

    @property
    def note(self) -> str:
        return f"fallback: same vendor ({self.why})" if self.why else ""


def _probe(cfg, avail):
    """(up(model), hold_of(model), held, state): whether a model can review now (its vendor not held, §8.5; Codex
    installed and starting), each vendor's hold read once and Codex probed once, only when asked."""
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

    return up, hold_of, held, state


def _missing(held, state, vendor) -> str:
    """Why no model of a vendor other than `vendor` is up: that vendor's hold, else Codex's state."""
    other = next((v for v, h in held.items() if h is not None and v != vendor), None)
    if other is not None:
        return f"{other} held: usage limit until {holds.hhmm(holds.until_of(held[other]))}"
    avail = state["avail"]
    return "codex not installed" if avail is None or avail.state == "absent" else "codex does not start"


def choose(cfg, writer_model, avail=None) -> Choice:
    """The reviewer of a head written by `writer_model` (R-REVIEW-2, R3; §8.5: held vendors skipped): the other
    vendor's first model that is up, else a different model of the writer's vendor with the reason noted."""
    order = [str(m) for m in cfg["review"]["order"] if m]
    writer_vendor = vendors.vendor_of(writer_model)
    up, hold_of, held, state = _probe(cfg, avail)

    def after(model) -> tuple:
        tail = order[order.index(model) + 1:] if model in order else []
        return tuple(m for m in tail if m != writer_model and up(m))

    cross = next((m for m in order if vendors.vendor_of(m) != writer_vendor and up(m)), None)
    if cross is not None:
        return Choice(cross, "", after(cross))
    why = _missing(held, state, writer_vendor)
    same = next((m for m in order if m != writer_model and up(m)), None)
    if same is None:
        same = SONNET if writer_model == OPUS else OPUS
        blocked = next((v for v, h in held.items() if h is not None), None)
        if blocked is not None or hold_of(same) is not None:
            return Choice(None, why, (), blocked or vendors.vendor_of(same))
    return Choice(same, why if vendors.vendor_of(same) == writer_vendor else "", after(same))


def pair(cfg, avail=None) -> tuple:
    """R-REVIEW-13: (the panel's two models, ""): the first model of review.order that is up and the first up
    model of the other vendor; else ((), why only one vendor is up)."""
    order = [str(m) for m in cfg["review"]["order"] if m]
    up, _hold_of, held, state = _probe(cfg, avail)
    first = next((m for m in order if up(m)), None)
    if first is not None:
        second = next((m for m in order if vendors.vendor_of(m) != vendors.vendor_of(first) and up(m)), None)
        if second is not None:
            return (first, second), ""
    if len({vendors.vendor_of(m) for m in order}) < 2:
        return (), "review.order names one vendor"
    return (), _missing(held, state, vendors.vendor_of(first) if first else "")


# ---- the package -----------------------------------------------------------------------------------

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


# ---- one round -------------------------------------------------------------------------------------

@dataclass
class Ctx:
    """What a requested pre-review needs; review.build._pre_round builds it."""
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
    record: object = None             # record(answer, head): the call's history line


@dataclass
class Round:
    k: int
    word: str             # pass | return | error | held
    model: str            # the reviewer that answered last ("none" when held)
    note: str
    findings: tuple
    head: str
    path: Path | None
    why: str = ""


def review_path(unit_dir, k) -> Path:
    return Path(unit_dir) / f"REVIEW.pre.r{k}.md"


def _write(ctx, rnd, rounds, pr, answers) -> Path:
    lines = [f"# pre-review r{rnd.k}: {ctx.unit} rev {ctx.rev} · PR #{pr} @ {rnd.head[:7]}",
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
    """Round k: the panel (_panel) when two vendors are up, else the single review noting why not (the chosen
    reviewer, the next one once if it gave no verdict); the REVIEW file, the row, the events line. Never raises
    for what a reviewer does."""
    rounds = rounds_of(ctx.cfg)
    avail = vendors.available(ctx.cfg)
    models, missing = pair(ctx.cfg, avail)
    if models:
        return _panel(ctx, k, models, pr=pr, head=head, report=report, earlier=earlier, rounds=rounds)
    degraded = f"panel: only one vendor up ({missing}): a single review"

    def recorded(rnd, answers, second_vendor):
        rnd.note = "; ".join(x for x in (degraded, rnd.note) if x)
        return _recorded(ctx, rnd, rounds, pr, answers, second_vendor=second_vendor)

    choice = choose(ctx.cfg, ctx.writer_model, avail)
    if choice.model is None:     # §8.5: no reviewer of an unheld vendor; no row
        return Round(k, "held", "none", choice.note, (), head, None, choice.held)
    answers, note = [], choice.note
    for model in (choice.model,) + choice.rest[:1]:
        why = _why(ctx, choice, answers[0] if answers else None, model)
        note = f"fallback: same vendor ({why})" if why else ""
        text = package(ctx, pr=pr, head=head, k=k, rounds=rounds, reviewer=model, why=why, report=report,
                       earlier=earlier)
        a = ask(model, text, cwd=ctx.worktree, cfg=ctx.cfg, timeout=float(TIMEOUT_S), effort=EFFORT, tag=ctx.tag)
        answers.append(a)
        if ctx.record is not None:
            ctx.record(a, head)
        if a.verdict is not None or a.why == CHANGED:
            break
    last = answers[-1]
    why = "; ".join(f"{a.model}: {a.why}" for a in answers if a.why)
    if last.verdict is None:
        rnd = Round(k, "error", last.model, one_line(f"{note + '; ' if note else ''}{why}", 240), (), head, None, why)
    else:
        rnd = Round(k, last.verdict.verdict, last.model, note, last.verdict.findings, head, None)
    return recorded(rnd, answers, not choice.why)


# ---- the panel (R-REVIEW-13) ---------------------------------------------------------------------------

def _panel(ctx, k, models, *, pr, head, report, earlier, rounds) -> Round:
    """Both `models` read the same package at once: the first in this thread exactly as a single reviewer (the
    round's tag), the second in a thread beside it under its own tag, so neither's tag sweep reaches the other.
    Both calls are history lines; the findings merge by agreement (agreed). The first call raising (Ctrl-C) stops
    the second before it is joined."""
    timeout = float(TIMEOUT_S)
    texts = [package(ctx, pr=pr, head=head, k=k, rounds=rounds, reviewer=m, why="", report=report, earlier=earlier)
             for m in models]
    got, abort = {}, threading.Event()

    def second():
        try:
            got["answer"] = ask(models[1], texts[1], cwd=ctx.worktree, cfg=ctx.cfg, timeout=timeout, effort=EFFORT,
                                tag=procs.run_id(f"{ctx.unit}-panel", time.time_ns()),
                                should_stop=abort.is_set)
        except Exception as exc:  # noqa: BLE001 - a failed half is a missing answer, never a lost round
            got["why"] = one_line(f"{type(exc).__name__}: {exc}", 200)

    beside = threading.Thread(target=second, name=f"panel-{ctx.unit}", daemon=True)
    beside.start()
    try:
        first = ask(models[0], texts[0], cwd=ctx.worktree, cfg=ctx.cfg, timeout=timeout, effort=EFFORT, tag=ctx.tag)
    except BaseException:
        abort.set()
        raise
    finally:
        beside.join()
    answers = [a for a in (first, got.get("answer")) if a is not None]
    if ctx.record is not None:
        for a in answers:
            ctx.record(a, head)
    rnd = agreed(ctx, k, models, (first, got.get("answer")), "panel", head, got.get("why", ""))
    return _recorded(ctx, rnd, rounds, pr, answers, second_vendor=True)


def agreed(ctx, k, models, answers, note, head, lost="") -> Round:
    """The panel's round from its two answers: both verdicts → the findings grouped (group), RETURN only on a
    `both` [act]; one verdict → that reviewer's review alone, as a single one; a changed tree or no verdict at
    all → error. `lost`: why the second call left no answer."""
    model = "+".join(models)
    failed = [(m, a) for m, a in zip(models, answers) if a is None or a.verdict is None]
    why = "; ".join(f"{m}: {(a.why if a is not None else lost) or 'no answer'}" for m, a in failed)
    if len(failed) == 2 or any(a is not None and a.why == CHANGED for _m, a in failed):
        return Round(k, "error", model, one_line(f"{note}; {why}", 240), (), head, None, why)
    if failed:
        gone = failed[0][0]
        a = next(x for x in answers if x is not None and x.verdict is not None)
        same = vendors.vendor_of(a.model) == vendors.vendor_of(ctx.writer_model)
        tail = f"fallback: same vendor ({gone} gave no verdict)" if same else f"{gone} gave no verdict"
        return Round(k, a.verdict.verdict, a.model, one_line(f"{note}; {tail}", 240), a.verdict.findings, head, None)
    findings = group(answers[0].verdict.findings, answers[1].verdict.findings, models)
    return Round(k, "return" if any(f.startswith("[act]") for f in findings) else "pass", model, note, findings,
                 head, None)


def _split(finding) -> tuple:
    level, _, text = str(finding).partition("] ")
    return level.lstrip("["), text


def _spot(text):
    """(path, first line, last line) a finding starts with (`path:12` or `path:12-20`), else None."""
    m = _SPOT.match(text)
    if m is None:
        return None
    lo, hi = sorted((int(m.group(2)), int(m.group(3) or m.group(2))))
    return m.group(1).removeprefix("./"), lo, hi


def _same(x, y) -> bool:
    """Two reviewers name one finding: the same file and overlapping lines, or the same normalised text."""
    sx, sy = _spot(x), _spot(y)
    if sx and sy and sx[0] == sy[0] and sx[1] <= sy[2] and sy[1] <= sx[2]:
        return True
    return " ".join(re.findall(r"[a-z0-9]+", x.lower())) == " ".join(re.findall(r"[a-z0-9]+", y.lower()))


def group(first, second, models) -> tuple:
    """Two reviewers' findings by agreement: `[<level>] both: …` first (the stronger level, in the stronger one's
    words), then `[consider] one (<model>): …` (a [noted] stays noted)."""
    a, b = [_split(f) for f in first], [_split(f) for f in second]
    both, alone, used = [], [], set()
    for level, text in a:
        j = next((j for j, (_lv, other) in enumerate(b) if j not in used and _same(text, other)), None)
        if j is None:
            alone.append((models[0], level, text))
            continue
        used.add(j)
        level, text = max((level, text), b[j], key=lambda f: _LEVELS.index(f[0]) if f[0] in _LEVELS else 0)
        both.append(f"[{level}] both: {text}")
    alone += [(models[1], level, text) for j, (level, text) in enumerate(b) if j not in used]
    return tuple(both) + tuple(f"[{'noted' if level == 'noted' else 'consider'}] one ({m}): {text}"
                               for m, level, text in alone)


def _recorded(ctx, rnd, rounds, pr, answers, *, second_vendor) -> Round:
    rnd.path = _write(ctx, rnd, rounds, pr, answers)
    try:
        ledger.record_review(ctx.project, "pre", pr, rnd.head, rnd.word, str(rnd.path), f"review:pre:{rnd.model}",
                             rnd.model, writer=ctx.writer, round=rnd.k, note=rnd.note, writer_model=ctx.writer_model,
                             second_vendor=second_vendor)
    except (ledger.LedgerError, OSError) as exc:
        log.log("hooks", f"pre-review: {ctx.unit}: review row not written: {exc}")
    log.log("events", f"prereview unit={ctx.unit} reviewer={rnd.model} verdict={rnd.word} round={rnd.k}")
    return rnd


def earlier_text(rounds_done) -> str:
    parts = []
    for rnd, answer in rounds_done:
        parts.append(f"Round {rnd.k}: {rnd.word.upper()} by {rnd.model}\n" + "\n".join(f"- {f}" for f in rnd.findings))
        if answer:
            parts.append(f"The writer's answer to round {rnd.k}:\n{_clip(answer, ANSWER_CHARS)}")
    return "\n\n".join(parts)
