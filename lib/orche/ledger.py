"""Verification ledger: parsing, validation and queries (SPEC §5.1).

Canonical ledger: ORCHE_HOME/ledger/<proj>.tsv, one file per project, columns
`pr sha verdict evidence verifier ts` (header written on first record). Rows are
append-only and file order is chronological, so the newest row (last in the file)
wins for a key. `<repo>/docs/LEDGER.tsv` is only an export of this file.

The done gate (donegate.py) uses `row_for_sha` and `satisfies`.
"""
import datetime as _dt
import re
from pathlib import Path

from . import paths, util

COLUMNS = ("pr", "sha", "verdict", "evidence", "verifier", "ts")
HEADER = "\t".join(COLUMNS)
ORDER = ("type-check-only", "unit-test-verified", "live-ui-verified")   # weakest first
FAILURES = ("verifier-blocked", "verifier-failed")
VERDICTS = tuple(reversed(ORDER)) + FAILURES
SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
PR_RE = re.compile(r"[0-9]+|none")
_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " "})


class LedgerError(ValueError):
    """Invalid input for a ledger row."""


class WriterIsJudge(LedgerError):
    """The verifier is the session that produced the code."""


class Ambiguous(LookupError):
    """A short sha matches rows for more than one commit."""

    def __init__(self, sha, candidates):
        super().__init__(f"AMBIGUOUS {sha}: matches {', '.join(candidates)}")
        self.sha = sha
        self.candidates = list(candidates)


def path(proj: str) -> Path:
    return paths.orche_home() / "ledger" / f"{proj}.tsv"


def cell(value) -> str:
    """Single-line TSV cell (tabs, CR and LF become spaces)."""
    return str(value).translate(_BREAKS).strip()


def now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def satisfies(verdict, minimum) -> bool:
    """True if `verdict` is at least `minimum`; verifier-blocked/-failed never satisfy."""
    if verdict not in ORDER or minimum not in ORDER:
        return False
    return ORDER.index(verdict) >= ORDER.index(minimum)


def parse(text: str) -> list:
    """Rows (dicts keyed by COLUMNS) from TSV text; a header line is optional."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split("\t")]
    cols, body = (header, lines[1:]) if "sha" in header else (list(COLUMNS), lines)
    rows = []
    for line in body:
        row = dict(zip(cols, (c.strip() for c in line.split("\t"))))
        rows.append({c: row.get(c, "") for c in COLUMNS})
    return rows


def read(proj: str) -> list:
    """All rows of a project's canonical ledger, in file (chronological) order."""
    try:
        return parse(path(proj).read_text(encoding="utf-8"))
    except OSError:
        return []


def make_row(pr, sha, verdict, evidence, verifier, producer=None, ts=None) -> dict:
    """Validated row dict. Raises LedgerError (WriterIsJudge if verifier == producer)."""
    pr, sha, verdict = cell(pr).lower(), cell(sha), cell(verdict)
    evidence, verifier = cell(evidence), cell(verifier)
    if not PR_RE.fullmatch(pr):
        raise LedgerError(f"pr must be a number or 'none', got {pr!r}")
    if not SHA_RE.fullmatch(sha):
        raise LedgerError(f"sha must be 7-40 hex characters, got {sha!r}")
    if verdict not in VERDICTS:
        raise LedgerError(f"verdict must be one of {'|'.join(VERDICTS)}, got {verdict!r}")
    if not evidence:
        raise LedgerError("evidence must not be empty")
    if not verifier:
        raise LedgerError("verifier must not be empty")
    if producer and cell(producer) == verifier:
        raise WriterIsJudge(f"verifier {verifier} produced the code (writer != judge)")
    return {"pr": pr, "sha": sha, "verdict": verdict, "evidence": evidence,
            "verifier": verifier, "ts": ts or now_utc()}


def format_row(row: dict) -> str:
    return "\t".join(cell(row.get(c, "")) for c in COLUMNS)


def render(rows) -> str:
    return HEADER + "\n" + "".join(format_row(r) + "\n" for r in rows)


def append(proj: str, row: dict) -> Path:
    """Append one row (one O_APPEND write; header created atomically first). Returns the ledger path."""
    target = path(proj)
    util.append_row(target, HEADER, format_row(row))
    return target


def _same_commit(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def row_for_sha(proj, sha):
    """Newest row whose sha matches `sha` (either may be abbreviated, >= 7 chars), else None.

    Used by the done gate, which always passes a full HEAD sha.
    """
    if not sha:
        return None
    for row in reversed(read(proj)):
        if _same_commit(row["sha"], sha):
            return row
    return None


def lookup(rows, sha, pr=None):
    """(row, note) for `sha` (optionally restricted to `pr`); newest row wins.

    row is None when unverified; note explains a voided row. Raises Ambiguous when a
    short sha matches rows for commits that are not prefixes of one another.
    A new head sha on a PR voids prior rows for that PR (R-LEDGER-1).
    """
    pool = [r for r in rows if pr is None or r["pr"] == str(pr).lower()]
    hits = [r for r in pool if _same_commit(r["sha"], sha)]
    if not hits:
        return None, ""
    shas = sorted({r["sha"].lower() for r in hits})
    for i, a in enumerate(shas):
        for b in shas[i + 1:]:
            if not _same_commit(a, b):
                raise Ambiguous(sha, shas)
    row = hits[-1]
    if pr is not None and pr != "none" and not _same_commit(pool[-1]["sha"], row["sha"]):
        return None, f"superseded by newer head {pool[-1]['sha'][:12]} on PR {pr}"
    return row, ""


def check(rows, sha, pr=None, minimum="type-check-only"):
    """(status, row, note): status is VERIFIED | NOT-VERIFIED | BELOW-MIN. May raise Ambiguous."""
    row, note = lookup(rows, sha, pr)
    if row is None:
        return "NOT-VERIFIED", None, note
    if satisfies(row["verdict"], minimum):
        return "VERIFIED", row, ""
    return "BELOW-MIN", row, ""


def latest_by_key(rows) -> list:
    """Newest row per (pr, sha), in order of each key's latest appearance."""
    latest = {}
    for row in rows:
        key = (row["pr"], row["sha"].lower())
        latest.pop(key, None)
        latest[key] = row
    return list(latest.values())


def counts(rows) -> dict:
    """Count of verdicts over the newest row per (pr, sha)."""
    out = {v: 0 for v in VERDICTS}
    for row in latest_by_key(rows):
        out[row["verdict"]] = out.get(row["verdict"], 0) + 1
    return out


def sorted_by_ts(rows) -> list:
    """Rows sorted by ts (stable; unparseable timestamps sort first)."""
    epoch = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)

    def key(row):
        try:
            value = _dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except ValueError:
            return epoch
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)

    return sorted(rows, key=key)


def export(proj: str, repo) -> tuple:
    """Write <repo>/docs/LEDGER.tsv from the canonical file, sorted by ts. Returns (path, count)."""
    rows = sorted_by_ts(read(proj))
    target = Path(repo) / "docs" / "LEDGER.tsv"
    util.atomic_write(target, render(rows))
    return target, len(rows)


# ---- grader bookkeeping (SPEC §5.2, R-GRADE-1) ---------------------------

MAX_GRADER_ITERATIONS = 3
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class GraderLimit(RuntimeError):
    """max grader iterations reached."""


def grader_dir() -> Path:
    return paths.orche_home() / "grader"


def grader_state(unit: str) -> dict:
    state = util.read_json(grader_dir() / f"{unit}.json", default=None) or {}
    return {"unit": unit, "iterations": int(state.get("iterations", 0)), "history": state.get("history", [])}


def parse_range(text: str):
    """`<base>..<head>` -> (base, head). Raises LedgerError."""
    base, sep, head = str(text).partition("..")
    if not sep or not base or not head or head.startswith(".") or "\n" in text \
            or base.startswith("-") or head.startswith("-"):
        raise LedgerError(f"--diff must be <base>..<head>, got {text!r}")
    return base, head


def grader_package(unit, n, rng, rubric_text, stat, files) -> str:
    listing = "".join(f"- {f}\n" for f in files) or "- (no files changed)\n"
    return (f"# Grader package: {unit} (iteration {n}/{MAX_GRADER_ITERATIONS})\n\n"
            f"Range: `{rng}`  \nPrepared: {now_utc()}\n\n"
            "Grade in a fresh session that has not seen the writer's conversation. Check each rubric\n"
            "criterion against the diff, then record the verdict with `orche ledger record`.\n\n"
            f"## Rubric\n\n{rubric_text.rstrip()}\n\n"
            f"## Diff stat\n\n```\n{stat.rstrip()}\n```\n\n"
            f"## Files\n\n{listing}")


def next_grader_iteration(unit: str):
    """(n, state) for the next iteration. Raises GraderLimit at MAX_GRADER_ITERATIONS."""
    if not UNIT_RE.fullmatch(unit or ""):
        raise LedgerError(f"invalid unit name {unit!r}")
    state = grader_state(unit)
    if state["iterations"] >= MAX_GRADER_ITERATIONS:
        raise GraderLimit("max grader iterations reached")
    return state["iterations"] + 1, state


def save_grader_iteration(state: dict, n: int, rng: str, text: str) -> Path:
    unit = state["unit"]
    package = grader_dir() / f"{unit}-{n}.md"
    util.atomic_write(package, text)
    state = {**state, "iterations": n,
             "history": [*state["history"], {"n": n, "range": rng, "package": str(package), "at": now_utc()}]}
    util.write_json(grader_dir() / f"{unit}.json", state)
    return package


# ---- the review table (design §6.1) -----------------------------------------

REVIEW_COLUMNS = ("pr", "sha", "kind", "verdict", "reviewer", "model", "writer", "round", "note",
                  "evidence", "ts")
REVIEW_HEADER = "\t".join(REVIEW_COLUMNS)
REVIEW_KINDS = ("pre", "build")
REVIEW_VERDICTS = ("pass", "return", "error", "skipped", "held")
REVIEW_REQUIRED = ("pr", "sha", "kind", "verdict", "reviewer", "model", "writer", "round", "evidence")


def review_path(proj: str) -> Path:
    return paths.orche_home() / "ledger" / f"{proj}.review.tsv"


def make_review_row(pr, sha, kind, verdict, reviewer, model, writer, round=1, note="", evidence="",
                    ts=None, *, writer_model=None, mode="required", second_vendor=True) -> dict:
    """A validated review row. Refuses an empty field (note excepted), reviewer == writer, and, for a
    `pre` row in required mode while a second vendor is available, a reviewer model of the writer's
    vendor (`writer_model`) whose note does not carry a `fallback` reason; a panel's model is its two
    joined by `+` (R-REVIEW-13), of the writer's vendor only when both are. Raises LedgerError."""
    from . import vendors
    row = {"pr": cell(pr).lower(), "sha": cell(sha), "kind": cell(kind), "verdict": cell(verdict),
           "reviewer": cell(reviewer), "model": cell(model), "writer": cell(writer), "round": cell(round),
           "note": cell(note), "evidence": cell(evidence), "ts": ts or now_utc()}
    empty = [c for c in REVIEW_REQUIRED if not row[c]]
    if empty:
        raise LedgerError(f"review row: empty {', '.join(empty)}")
    if not PR_RE.fullmatch(row["pr"]):
        raise LedgerError(f"pr must be a number or 'none', got {row['pr']!r}")
    if not SHA_RE.fullmatch(row["sha"]):
        raise LedgerError(f"sha must be 7-40 hex characters, got {row['sha']!r}")
    if row["kind"] not in REVIEW_KINDS:
        raise LedgerError(f"kind must be one of {'|'.join(REVIEW_KINDS)}, got {row['kind']!r}")
    if row["verdict"] not in REVIEW_VERDICTS:
        raise LedgerError(f"verdict must be one of {'|'.join(REVIEW_VERDICTS)}, got {row['verdict']!r}")
    if row["reviewer"] == row["writer"]:
        raise WriterIsJudge(f"reviewer {row['reviewer']} wrote the code (writer != judge)")
    if (row["kind"] == "pre" and mode == "required" and second_vendor and writer_model
            and all(vendors.vendor_of(m) == vendors.vendor_of(writer_model) for m in row["model"].split("+"))
            and "fallback" not in row["note"]):
        raise LedgerError(f"pre-review by {row['model']} is the writer's vendor "
                          f"({vendors.vendor_of(writer_model)}): use the other vendor or note `fallback: <why>`")
    return row


def append_review(proj: str, row: dict) -> Path:
    """Append one review row (header created on first write). Returns the table's path."""
    target = review_path(proj)
    util.append_row(target, REVIEW_HEADER, "\t".join(cell(row.get(c, "")) for c in REVIEW_COLUMNS))
    return target


def read_reviews(proj: str) -> list:
    try:
        text = review_path(proj).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = [line for line in text.split("\n") if line.strip()]
    if lines and lines[0].split("\t")[:2] == ["pr", "sha"]:
        lines = lines[1:]
    return [dict(zip(REVIEW_COLUMNS, (c.strip() for c in line.split("\t")))) for line in lines]


def review_for(proj, pr, sha, kind):
    """The newest review row of `kind` for (pr, sha) (sha prefixes match), else None."""
    for row in reversed(read_reviews(proj)):
        if row.get("kind") == kind and row.get("pr") == str(pr).lower() and _same_commit(row.get("sha", ""), sha):
            return row
    return None


def record_review(proj, kind, pr, head, verdict, findings, reviewer, model, *, writer, round=1, note="",
                  writer_model=None, mode="required", second_vendor=True) -> dict:
    """make_review_row + append_review; `findings` (the review's report path) is the evidence."""
    row = make_review_row(pr, head, kind, verdict, reviewer, model, writer, round, note, findings,
                          writer_model=writer_model, mode=mode, second_vendor=second_vendor)
    append_review(proj, row)
    return row
