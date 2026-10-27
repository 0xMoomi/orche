"""Mistake classes for `orche hr audit` (principle 10, SPEC §5.10): what went wrong twice in a project's
last `days` days, and where to enforce it.

Sources, all durable records:
  head-review returns   ORCHE_HOME/ledger/<p>.review.tsv rows `kind build`, `verdict return`; each
                        checklist line `- N: why` of the row's evidence file, but 6-8 (overlap, risk,
                        size route a PR, they are not mistakes); the unit is the file's `unit:` line
  queue returns         ORCHE_HOME/queue/<p>/log.jsonl `returned` events: reason (red, conflict,
                        timeout) and the failed step
  reverts               `git log origin/<base>` commits whose subject starts with `Revert` or whose body
                        says `This reverts commit`; a `#N` naming a landed PR (queue log) gives the unit

A finding's class key (class_key): its first line, lowercased, without paths, numbers and quotes, the
first 6 words. A class is "seen twice" when units that differ share its key; at most MAX_CLASSES,
most frequent first. Each gets the highest enforcement level its words allow, in this order:
a structural change (type, lint rule, build step), a test, a hook or gate, a standing order (docs last).

The Enforcement table: `apply` keeps one section `## Enforcement (proposed by the audit)` in the
project's STANDING.md, a table `| class | seen | proposed level | status |` (new rows `proposed`; an
existing row keeps its status, its count and level follow the audit). It is the rules map SPEC §5.10
names. The numbered standing orders are never touched; `hr correct` adds its lines above the section.
"""
import datetime as _dt
import re
from pathlib import Path

from .. import ledger, publish, shell, templates, units, util
from ..queue import store
from .audit import AUDIT_DAYS

MAX_CLASSES = 8
KEY_WORDS = 6
ROUTING = (6, 7, 8)     # head-review checklist lines that route a PR (overlap, risk, size)
HEADING = "## Enforcement (proposed by the audit)"
TABLE_HEAD = ("| class | seen | proposed level | status |", "|---|---|---|---|")
LEVELS = (   # (level, words that point at it, why): the first level whose words the key holds wins
    ("structural change", {"type", "types", "typed", "lint", "import", "imports", "unused", "format", "compile",
                           "build", "syntax", "undefined", "null", "schema"},
     "the mistake is mechanical, so a type, a lint rule or a build step can make it impossible to write"),
    ("test", {"test", "tests", "regression", "coverage", "assert", "red", "broken", "fails", "failing", "accept",
              "verify", "revert", "reverted"},
     "the mistake shows as behaviour, so a test that fails on it stops every repeat before review"),
    ("hook or gate", {"scope", "skip", "ci", "commit", "push", "branch", "secret", "env", "conflict",
                      "conflicts", "stale", "overlap", "timeout", "ledger"},
     "the mistake is a process step, so a hook or gate can refuse it the moment it happens"),
    ("standing order", set(),
     "no tool can see this mistake, so a standing order is the place left (docs come last)"),
)
_FINDING = re.compile(r"^- (\d+): (.+)$")
_UNIT = re.compile(r"^unit: (\S+)\s*$", re.M)
_QUOTES = re.compile(r"[\"'`‘’“”]")
_PATHISH = re.compile(r"[/\\]|^\.|\.[a-z0-9]{1,5}$")
_PR = re.compile(r"#(\d+)\b")


def class_key(text) -> str:
    """The class of one finding: its first line, lowercased, paths, numbers and quotes dropped, 6 words."""
    lines = str(text or "").strip().splitlines()
    t = _QUOTES.sub(" ", lines[0].lower() if lines else "")
    words = [w for w in (w.strip(",;:()[]{}<>!?") for w in t.split()) if w and not _PATHISH.search(w)]
    t = re.sub(r"[^a-z\s-]", " ", re.sub(r"\d+", " ", " ".join(words)))
    return " ".join([w for w in (w.strip("-") for w in t.split()) if w][:KEY_WORDS])


def level_of(key) -> tuple:
    """(level, why) for a class key: the first of LEVELS whose words the key holds."""
    words = set(key.split())
    return next((name, why) for name, hits, why in LEVELS if words & hits or not hits)


def _when(text):
    return units.parse_iso(str(text or "").replace("Z", "+00:00"))


def review_returns(key, since) -> list:
    """[{source, unit, at, text}] of the head review's RETURN findings since `since`."""
    out = []
    for row in ledger.read_reviews(key):
        at = _when(row.get("ts"))
        if row.get("kind") != "build" or row.get("verdict") != "return" or at is None or at < since:
            continue
        try:
            body = Path(row.get("evidence") or "").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        m = _UNIT.search(body)
        unit = m.group(1) if m else f"pr{row.get('pr')}"
        for line in body.split("\n## ", 1)[0].splitlines():
            f = _FINDING.match(line)
            if f and int(f.group(1)) not in ROUTING:
                out.append({"source": "review", "unit": unit, "at": at, "text": f.group(2)})
    return out


def queue_returns(key, since) -> list:
    """[{source, unit, at, text}] of the merge queue's returns since `since`."""
    out = []
    for ln in store.read_log(key):
        at = _when(ln.get("at"))
        if ln.get("event") != "returned" or at is None or at < since:
            continue
        what = " ".join(str(x) for x in ("queue return", ln.get("reason"), ln.get("step")) if x)
        out.append({"source": "queue", "unit": ln.get("unit") or f"pr{ln.get('pr')}", "at": at, "text": what})
    return out


def _landed(key) -> dict:
    """{pr number: unit} of the PRs the queue merged."""
    return {str(ln.get("pr")): ln.get("unit") or f"pr{ln.get('pr')}" for ln in store.read_log(key)
            if ln.get("event") == "merged" and ln.get("pr") is not None}


def reverts(key, root, since) -> list:
    """[{source, unit, at, text}] of the revert commits on origin/<base> since `since` (no fetch)."""
    base = publish.base(root, project=key) if root and (Path(root) / ".git").exists() else None
    if not base:
        return []
    res = shell.git(root, "log", f"origin/{base}", f"--since={since.isoformat()}",
                    "--format=%H%x1f%cI%x1f%s%x1f%b%x1e", timeout=30)
    if not res.ok:
        return []
    landed, out = _landed(key), []
    for rec in res.out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 4:
            continue
        sha, at, subject, body = parts[0], _when(parts[1]), parts[2], parts[3]
        if not (subject.startswith("Revert") or "This reverts commit" in body) or at is None:
            continue
        prs = [n for n in _PR.findall(f"{subject}\n{body}") if n in landed]
        reason = next((ln.strip() for ln in body.splitlines()
                       if ln.strip() and not ln.strip().startswith("This reverts commit")), "")
        out.append({"source": "revert", "unit": landed[prs[0]] if prs else sha[:7], "at": at,
                    "text": reason or "revert of a landed pr"})
    return out


def events(key, proj, now, days=AUDIT_DAYS) -> list:
    since = now - _dt.timedelta(days=days)
    return review_returns(key, since) + queue_returns(key, since) + reverts(key, proj.get("path"), since)


def classes(key, proj, now, days=AUDIT_DAYS) -> list:
    """The mistake classes seen twice (by units that differ), most frequent first, at most MAX_CLASSES:
    [{key, count, units, dates, sources, level, why, draft}]."""
    groups = {}
    for ev in events(key, proj, now, days):
        k = class_key(ev["text"])
        if k:
            groups.setdefault(k, []).append(ev)
    out = []
    for k, evs in groups.items():
        who = sorted({e["unit"] for e in evs})
        if len(who) < 2:
            continue
        level, why = level_of(k)
        out.append({"key": k, "count": len(evs), "units": who,
                    "dates": sorted({e["at"].astimezone().date().isoformat() for e in evs}),
                    "sources": sorted({e["source"] for e in evs}), "level": level, "why": why,
                    "draft": f'Before you report done, check that the change does not repeat "{k}" '
                             f"(seen {len(evs)} times in {days} days)."})
    out.sort(key=lambda c: (-c["count"], c["key"]))
    return out[:MAX_CLASSES]


# ---- the Enforcement table -------------------------------------------------------------------------

def _path(proj) -> Path:
    return Path(proj["path"]) / proj["standing"]


def _rows(lines, start) -> tuple:
    """(end index of the table under the heading at `start`, {class: [class, seen, level, status]})."""
    rows, i = {}, start + 1
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("|")):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        if lines[i].startswith("|") and len(cells) == 4 and cells[0] not in ("class", "---"):
            rows[cells[0]] = cells
        i += 1
    return i, rows


def table(proj) -> dict:
    """{class: [class, seen, level, status]} of the project's Enforcement table ({} without one)."""
    try:
        lines = _path(proj).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return _rows(lines, lines.index(HEADING))[1] if HEADING in lines else {}


def apply(key, proj, found, *, dry_run=False) -> dict:
    """Write `found` (classes()) into the Enforcement table of `key`'s STANDING.md: {new, updated, path}."""
    path = _path(proj)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        tmpl = templates.TEMPLATES_ROOT / "project" / ".orche" / "STANDING.md"
        text = templates.render(tmpl.read_text(encoding="utf-8"), PROJECT=key)
    lines = text.splitlines()
    if HEADING in lines:
        start = lines.index(HEADING)
        end, rows = _rows(lines, start)
    else:
        start = end = len(lines)
        rows = {}
    new = [c["key"] for c in found if c["key"] not in rows]
    updated = [c["key"] for c in found if c["key"] in rows]
    for c in found:
        status = rows[c["key"]][3] if c["key"] in rows else "proposed"
        rows[c["key"]] = [c["key"], str(c["count"]), c["level"], status]
    section = [HEADING, "", *TABLE_HEAD, *("| " + " | ".join(r) + " |" for r in rows.values()), ""]
    before = lines[:start]
    if before and before[-1].strip():
        before.append("")
    out = "\n".join(before + section + lines[end:]).rstrip("\n") + "\n"
    if not dry_run and (new or updated) and out != text:
        util.atomic_write(path, out)
    return {"new": new, "updated": updated, "path": str(path)}
