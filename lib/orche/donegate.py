"""The done gate (SPEC §14.11): what a message claims, and whether a completion claim is proven.

One claim grammar for every reader. Three callers, each with its own SPEC rule set: the Stop hook
(`may_stop`: may this session stop?), `orche report` (`for_report`: may this pointer say done?) and
the runner (`after_run`: did this run prove its claim?). Only ledger-gated members (the Build
division, feature agents) need proof. Missing proof is returned in `Proof.missing`, never raised.
"""
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import ledger, shell, transcript, units

DONE_WORDS = ("done", "release-ready")            # completion claims; release-ready is filed as done (R-INBOX-1)
_MINIMUM = {"done": "type-check-only", "release-ready": "unit-test-verified"}
_PREFIX = "[orche] completion blocked: missing "
_SUFFIX = ". Record the verdict / update STATUS.md, then report again."
_EMPH = r"(?:\*\*|__|[*_`])*"                     # emphasis markers around STATUS and the word
_WORDS = r"(?:release-ready|done|blocked|failed|stop)(?![^\W_]|-)"    # never run on: done-ish, doneness
_LINE = re.compile(
    r"(?:(?![^\W_]).|\d+[.)])*?"                  # before STATUS: no letter or digit, except a list number
    r"STATUS" + _EMPH + r"[ \t]*:[ \t]*" + _EMPH + r"[ \t]*"
    r"(" + _WORDS + r")"                           # the status word
    r"(?!" + _EMPH + r"[ \t]*\|[ \t]*" + _EMPH + _WORDS + r")",      # `done|blocked|failed`: a template echo
    re.IGNORECASE)


@dataclass(frozen=True)
class Claim:
    """The message's STATUS line: `word` lower-case, `line` stripped, `index` in splitlines()."""
    word: str
    line: str
    index: int


@dataclass(frozen=True)
class Proof:
    """The evidence read for a claim: HEAD (None: unreadable), its ledger row, and what is missing."""
    head: str | None
    row: dict | None
    missing: tuple = ()

    @property
    def message(self) -> str:
        return _PREFIX + "; ".join(self.missing) + _SUFFIX if self.missing else ""


def claim(text) -> Claim | None:
    """The last line of `text` that states a status (SPEC §14.11), or None."""
    found = None
    for i, line in enumerate(str(text or "").splitlines()):
        m = _LINE.match(line)
        if m:
            found = (i, line.strip(), m.group(1).lower())
    if found is None:
        return None
    i, line, word = found
    return Claim(word, line, i)


def gated(m) -> bool:
    """Ledger-gated members: the Build division and feature agents."""
    return m is not None and (m.kind == "feat" or (m.kind == "div" and m.name == "build"))


def _root(m, cwd) -> Path:
    """Where a member's claim is checked: its recorded worktree, else the cwd (realpath)."""
    return Path(os.path.realpath((m.worktree if m is not None else None) or cwd))


def _ledger_gaps(project, head, row, minimum, root) -> list:
    if head is None:
        return [f"LEDGER row (cannot read git HEAD of {root})"]
    if row is None:
        return [f"LEDGER row for HEAD {head[:12]} in ORCHE_HOME/ledger/{project}.tsv (verdict >= {minimum})"]
    if not ledger.satisfies(row.get("verdict", ""), minimum):
        return [f"LEDGER row for HEAD {head[:12]} has verdict {row.get('verdict')!r}; need >= {minimum}"]
    return []


def _status_md(m, root, transcript_path) -> list:
    rel = Path(".orche") / m.kind / m.name / "STATUS.md"
    path = root / rel
    if not path.is_file():
        return [f"{rel} (missing)"]
    started = transcript.first_timestamp(transcript_path) if transcript_path else None
    if started is not None and path.stat().st_mtime < started.timestamp():
        return [f"{rel} update in this session (last written before the session started)"]
    return []


def _stop_proof(c, m, cwd, transcript_path, deadline) -> Proof:
    if c is None or c.word not in DONE_WORDS or not gated(m):
        return Proof(None, None)
    root = _root(m, cwd)
    left = (deadline - time.monotonic()) if deadline is not None else 2
    head = shell.git_head(root, timeout=min(2, max(left, 0.2)))
    row = ledger.row_for_sha(m.project, head) if head else None
    missing = _ledger_gaps(m.project, head, row, _MINIMUM[c.word], root)
    if not missing and c.word == "release-ready" and row.get("verifier") == m.session:
        missing.append(f"independent verifier for HEAD {head[:12]} (LEDGER row verifier is "
                       f"{row.get('verifier')}; writer != judge)")
    return Proof(head, row, tuple(missing + _status_md(m, root, transcript_path)))


def _night(root, session) -> tuple:
    """(ok, message) of `<root>/.orche/night/<session>.predicate.sh`; missing or not executable -> ok."""
    path = Path(root) / ".orche" / "night" / f"{session}.predicate.sh"
    if not path.is_file() or not os.access(path, os.X_OK):
        return True, ""
    res = shell.run([str(path)], timeout=10)
    if res.ok:
        return True, ""
    return False, res.out.strip() or f"[orche] night predicate {path.name} not satisfied (exit {res.code})."


def may_stop(text, r, who, cwd, transcript_path=None, deadline=None, stop_hook_active=False) -> tuple:
    """(exit code, stderr message) for the Stop hook: 0 lets the session stop, 2 makes it continue.

    Under stop_hook_active nothing is enforced (no loop). Else a completion claim from a gated member
    (`who.member`, the identity) needs a ledger row for HEAD of its worktree (release-ready: >=
    unit-test-verified from a verifier other than the claimant) and STATUS.md written this session;
    then the session's night predicate, if any (R-NIGHT-4), must pass.
    """
    if stop_hook_active:
        return 0, ""
    p = _stop_proof(claim(text), who.member, cwd, transcript_path, deadline)
    if p.missing:
        return 2, p.message
    if not who.session:
        return 0, ""
    unit, proj = who.place.unit, who.place.project
    root = (unit.worktree if unit else None) or (units.project_root(r.reg, proj) if proj else None) or cwd
    ok, message = _night(root, who.session)
    return (0, "") if ok else (2, message)


def for_report(member, cwd, status) -> Proof:
    """`orche report`: HEAD of the member's worktree (else the cwd) and its ledger row; `done` from a
    gated member is missing a row >= type-check-only unless the ledger has one."""
    root = _root(member, cwd)
    head = shell.git_head(root)
    row = ledger.row_for_sha(member.project, head) if (member is not None and head) else None
    if status != "done" or not gated(member):
        return Proof(head, row)
    return Proof(head, row, tuple(_ledger_gaps(member.project, head, row, _MINIMUM["done"], root)))


def _commit_gap(worktree, head_before, head):
    if not head or head == head_before:
        return "no commit"
    if not head_before:
        return "history rewritten (HEAD before the run unknown)"
    res = shell.git(worktree, "merge-base", "--is-ancestor", head_before, head, timeout=30)
    return None if res.ok else f"history rewritten ({head_before[:7]} is not an ancestor of HEAD)"


def _tree_gap(worktree, session):
    res = shell.git(worktree, "status", "--porcelain", "--untracked-files=all", timeout=30)
    if not res.ok:
        return "uncommitted changes"
    foreign = [ln for ln in res.out.splitlines()
               if ln.strip() and not (ln.startswith("??") and units.orche_owned(units.porcelain_path(ln), session))]
    return "uncommitted changes" if foreign else None


def _row_gap(word, head, row, session):
    if row is None:
        return f"no ledger row for {head[:7]}"
    if not ledger.satisfies(row.get("verdict", ""), _MINIMUM[word]):
        return f"ledger verdict {row.get('verdict')} for {head[:7]}"
    if word == "release-ready" and row.get("verifier") == session:
        return f"ledger verifier {row.get('verifier')} is the claimant for {head[:7]}"
    return None


def after_run(member, word, worktree, head_before, head) -> Proof:
    """The runner (R-DISPATCH-9): a done or release-ready claim from a gated target needs, in this
    order, a new commit, the pre-run HEAD as its ancestor, a clean tree (ORCHE's own untracked files
    aside) and the ledger row; `missing` holds the first gap only."""
    # Keyed by the target identity's project (Roster), not the brief's job.project: the row is the target's.
    row = ledger.row_for_sha(member.project, head) if (member is not None and head) else None
    if word not in DONE_WORDS or not gated(member):
        return Proof(head, row)
    why = (_commit_gap(worktree, head_before, head) or _tree_gap(worktree, member.session)
           or _row_gap(word, head, row, member.session))
    return Proof(head, row, (why,) if why else ())
