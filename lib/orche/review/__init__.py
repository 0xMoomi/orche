"""Reviews of a published PR before it enters the merge queue (design 0.9.0 §3.4-§3.5), and the
model reviews of a unit's change (design §3.3, §3.5, §6; R-REVIEW-3, R-REVIEW-4): the read-only reviewer
call, fenced and masked package values, the verdict grammar and the tree check. The pre-review
(pre.py) builds on these; so can the headless Build review.

A reviewer reads, never writes. Claude runs sealed (`--restricted --tools Read,Glob,Grep`, no Bash at
all: `git diff --output=<path>` or `--no-index` would write or read any file; intake's deny rules, no
session persistence); the package carries the diff (up to review.max_diff_lines, then the full file
list and a note). Codex runs `-s read-only` (codex.REVIEW_SANDBOX: no writable root, no network).
Both get headless.base_env (no ORCHE_SESSION, no Jev key; Codex no CLAUDE*/ANTHROPIC* name). ask()
compares `git status --porcelain` before and after the call: a change is the terminal error "the
reviewer changed files". The headless Build review (build.py) uses the same call and check.

Verdict: the first non-blank line `VERDICT: PASS|RETURN`; finding lines `- [act] …`,
`- [consider] …`, `- [noted] …`; any `[act]` line makes it RETURN whatever the first line says
(structure over prose). No parsable first line → no verdict (the caller tries the next reviewer once).
"""
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import codex, headless, intake, mask, shell, util, vendors

MAX_TURNS = 40
TOOLS = intake.TOOLS          # Read, Glob, Grep: no Bash (R10)
DENIED = intake.DENIED        # Bash, every writing and web tool, the secret reads
CHANGED = "the reviewer changed files"
_VERDICT = re.compile(r"^[\s>*_`#]*VERDICT\s*:\s*[*_`]*\s*(PASS|RETURN)\b", re.IGNORECASE)
_FINDING = re.compile(r"^\s*[-*•]\s*\[(act|consider|noted)\]\s*(.*\S)", re.IGNORECASE)
_TICKS = re.compile(r"`+")


@dataclass(frozen=True)
class Verdict:
    verdict: str        # "pass" | "return"
    findings: tuple     # "[act] …", "[consider] …", "[noted] …" (masked), in order

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
    res = shell.run(["git", "-C", str(cwd), "status", "--porcelain"], timeout=30)
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


def ask(model, package, *, cwd, cfg, timeout, effort, tag=None, stop=None, should_stop=None, on_start=None) -> Answer:
    """Run reviewer `model` once on `package` (stdin) in `cwd`; never raises for what the reviewer does."""
    call = call_for(model, cwd=cwd, timeout=timeout, effort=effort, tag=tag)
    before = tree(cwd)
    started = util.now_iso()
    try:
        res = (codex.run if isinstance(call, codex.Call) else headless.run)(
            call, package, cfg, stop=stop, should_stop=should_stop, on_start=on_start)
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
    elif res.error is not None:
        why = res.failure or res.error
    else:
        verdict = parse(text)
        if verdict is None:
            why = res.failure or ("no VERDICT line" if text.strip() else "an empty answer")
    return Answer(model, call, res, started, finished, verdict, one_line(why, 200), text)
