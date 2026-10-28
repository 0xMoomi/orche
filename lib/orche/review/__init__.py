"""Reviews of a published PR before it enters the merge queue, and the
model reviews of a unit's change (R-REVIEW-3, R-REVIEW-4): the read-only reviewer
call, fenced and masked package values, the verdict grammar and the tree check. The pre-review
(pre.py, run only on request) builds on these; so does the headless Build review.

A reviewer reads, never writes. Claude runs sealed (`--restricted --tools Read,Glob,Grep`, no Bash at
all: `git diff --output=<path>` or `--no-index` would write or read any file; intake's deny rules, no
session persistence); the package carries the diff (up to pre.MAX_DIFF_LINES, then the full file
list and a note). Codex runs `-s read-only` (codex.REVIEW_SANDBOX: no writable root, no network).
Both get headless.base_env (no ORCHE_SESSION, no Jev key; Codex no CLAUDE*/ANTHROPIC* name). ask()
compares `git status --porcelain` before and after the call: a change is the terminal error "the
reviewer changed files". The headless Build review (build.py) uses the same call and check.

Verdict: the first non-blank line `VERDICT: PASS|RETURN`; finding lines `- [act] …`,
`- [consider] …`, `- [noted] …`; any `[act]` line makes it RETURN whatever the first line says
(structure over prose). No parsable first line → no verdict (the caller tries the next reviewer once).

Depth (R-REVIEW-14): a change with a high-risk path (the head review's line 7, RISK) or more than
`review.deep_lines` added and removed lines against origin/<base> is deep, never in a mechanical unit (deep): its
admission names the one fact it is safe because of, which a test in the PR must exercise (proves).
"""
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import codex, headless, intake, mask, shell, util, vendors

MAX_TURNS = 40
RISK = re.compile(r"(^|/)(payments?|billing|auth|oauth|migrations?|deploy|wallets?|keys?|secrets?)(/|\.|_|-|$)",
                  re.IGNORECASE)        # the head review's line 7: payments, auth, migrations, deploy, wallets, keys
TESTS = re.compile(r"(^|/)(tests|__tests__)/|\.test\.|_test\.|\.spec\.")    # the test layouts a fact's proof is in
TOOLS = intake.TOOLS          # Read, Glob, Grep: no Bash (R10)
DENIED = intake.DENIED        # Bash, every writing and web tool, the secret reads
CHANGED = "the reviewer changed files"
_VERDICT = re.compile(r"^[\s>*_`#]*VERDICT\s*:\s*[*_`]*\s*(PASS|RETURN)\b", re.IGNORECASE)
_FINDING = re.compile(r"^\s*[-*•]\s*\[(act|consider|noted)\]\s*(.*\S)", re.IGNORECASE)
_TICKS = re.compile(r"`+")


@dataclass(frozen=True)
class Verdict:
    verdict: str        # "pass" | "return" (the head review's checklist: also "held", a usage limit)
    findings: tuple     # in order: "[act] …", "[consider] …", "[noted] …" (masked); the checklist's (n, why)

    @property
    def acts(self) -> tuple:
        return tuple(f for f in self.findings if f.startswith("[act]"))


def parse(text):
    """The Verdict of a reviewer's answer, or None when its first non-blank line is no VERDICT line."""
    lines = str(text or "").splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    m = _VERDICT.match(first)
    if m is None:
        return None
    findings = tuple(f"[{f.group(1).lower()}] {masked(f.group(2))}"
                     for f in (_FINDING.match(ln) for ln in lines[1:]) if f)
    word = m.group(1).lower()
    if any(f.startswith("[act]") for f in findings):
        word = "return"
    return Verdict(word, findings)


def masked(text) -> str:
    return mask.redact(str(text or ""))[0]


def deep(cfg, changed, tier="standard") -> str:
    """Why the change `changed` ([(path, added, removed)] against origin/<base>) is reviewed in depth: `high risk
    (<paths>)` or `size (+A -R)` over review.deep_lines; "" for an ordinary one and for a mechanical unit."""
    if str(tier or "").strip().lower() == "mechanical":
        return ""
    risky = [p for p, _a, _r in changed if RISK.search(p)]
    if risky:
        return f"high risk ({', '.join(risky[:3])})"
    added, removed = sum(c[1] for c in changed), sum(c[2] for c in changed)
    return f"size (+{added} -{removed})" if added + removed > int(cfg["review"]["deep_lines"]) else ""


def proves(changed) -> bool:
    """Whether the change adds lines to a test (tests/, __tests__/, *.test.*, *_test.*, *.spec.*): where the
    proof of a deep change's blast-radius fact lives (R-REVIEW-14)."""
    return any(added > 0 and TESTS.search(path) for path, added, _r in changed)


def fence(text, label="data") -> str:
    """Untrusted text as a fenced block (principle 7): masked, inside a backtick fence longer than any
    run of backticks in it, so nothing inside can close it."""
    body = masked(text).rstrip("\n") or "(empty)"
    n = max([3] + [len(run) + 1 for run in _TICKS.findall(body)])
    return f"{'`' * n}{label}\n{body}\n{'`' * n}"


def one_line(text, limit=300) -> str:
    flat = " ".join(masked(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def tree(cwd):
    """`git status --porcelain` of the worktree, or None when git cannot tell."""
    res = shell.git(cwd, "status", "--porcelain", timeout=30)
    return res.out if res.ok else None


def call_for(model, *, cwd, timeout, effort, tag=None, max_turns=MAX_TURNS, append_file=None):
    """The read-only call of a reviewer `model` (Claude sealed or Codex read-only); `append_file` (a
    persona) is appended to a Claude call's system prompt."""
    if vendors.vendor_of(model) == vendors.CODEX:
        return codex.Call(model, effort if effort in codex.EFFORTS else "medium", float(timeout), Path(cwd), (),
                          codex.out_path(tag or f"review@{time.time_ns()}"), sandbox=codex.REVIEW_SANDBOX, tag=tag)
    return headless.Call("sealed", model, max_turns, float(timeout), cwd=Path(cwd), effort=effort or None,
                         allowed=TOOLS, denied=DENIED, append_file=append_file, tag=tag)


@dataclass
class Answer:
    """One reviewer call and what it said."""
    model: str
    call: object
    res: object          # headless.Result
    started: str
    finished: str
    verdict: object      # Verdict or None
    why: str             # "" or why there is no verdict (stopped, timeout, error, CHANGED, no VERDICT line)
    text: str            # the masked answer

    @property
    def stopped(self) -> bool:
        return bool(self.res.killed)

    @property
    def word(self) -> str:
        return self.verdict.verdict if self.verdict is not None else ("stopped" if self.stopped else "error")


def findings(text, res, cfg):
    """The pre-review's grammar (ask): (Verdict, "") from parse(), else (None, why): the call failed or no
    VERDICT line came first."""
    if res.error is not None:
        return None, res.failure or res.error
    verdict = parse(text)
    return verdict, "" if verdict is not None else res.failure or ("no VERDICT line" if text.strip() else "an empty answer")


def ask(model, package, grammar=findings, *, cwd, cfg, timeout=None, effort=None, tag=None, should_stop=None,
        call=None) -> Answer:
    """Run reviewer `model` once on `package` (stdin) in `cwd` — its `call`, else call_for's — started by
    job.adapter: the one reviewer call. A call that was stopped or changed the tree has no verdict; otherwise `grammar(text,
    res, cfg)` reads it (findings: the pre-review's; build.checklist: the head review's). Never raises for
    what the reviewer does."""
    from ..runs import job
    call = call or call_for(model, cwd=cwd, timeout=timeout, effort=effort, tag=tag)
    before = tree(cwd)
    started = util.now_iso()
    try:
        res = job.adapter(call).run(call, package, cfg, should_stop=should_stop)
    except (OSError, ValueError) as exc:
        why = masked(f"could not start: {exc}")
        res = headless.Result(127, {}, why, why, False)
    finished = util.now_iso()
    text = masked(res.text)
    verdict, why = None, ""
    after = tree(cwd)
    if res.killed:
        why = "stopped"
    elif before is not None and after is not None and before != after:
        why = CHANGED
    else:
        verdict, why = grammar(text, res, cfg)
    return Answer(model, call, res, started, finished, verdict, one_line(why, 200), text)
