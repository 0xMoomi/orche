"""Briefs: the ```dispatch block grammar, unit ids, the unit folder and BRIEF.md revisions.
Callers route (roster) and deliver (dispatch); this module never does either.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import log, mask, shell, units, util

if TYPE_CHECKING:
    from . import roster

KEYS = ("TO", "GOAL", "SCOPE", "CONTEXT", "ACCEPT", "VERIFY", "TIMEBOX", "FORBID", "REPORT", "TIER", "ROLLBACK")
TIERS = ("mechanical", "standard", "hard")
REFUSE = "[orche] dispatch refused: "
UNITS = ".orche/units"          # a unit folder is <project root>/UNITS/<unit>

_REQUIRED = tuple(k for k in KEYS if k != "ROLLBACK")
_UNIT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*-\d{8}-\d+$")
_AMEND = re.compile(r"AMEND\s+(\S+)\s+rev\s+(\d+)")
_STOP = re.compile(r"STOP\s+(\S+)(?:\s+(.*))?")
_SUPERSEDES = re.compile(r"^SUPERSEDES\s+(.*)$")
_OPENER = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*dispatch[ \t]*$")
_REV = re.compile(r"^rev:\s*(\d+)\s*$", re.MULTILINE)
_MARK = re.compile(r"^## Brief\b.*$", re.MULTILINE)
_ORDER = re.compile(r"-(\d{8})-(\d+)$")
_MAIN_CHECKOUT = " (main checkout)"


class Refusal(Exception):
    """A dispatch block, or a dispatch routing check, that goes back to its author (Stop hook exit 2).
    str(exc) is the full message."""


def unit_id_error(unit):
    """None for a valid unit id, else "bad unit id '<u>' (want <proj>-<slug>-<yyyymmdd>-<n>)"."""
    if isinstance(unit, str) and _UNIT_ID.match(unit):
        return None
    return f"bad unit id {unit!r} (want <proj>-<slug>-<yyyymmdd>-<n>)"


def _refuse_unknown(names, allowed):
    bad = [k for k in dict.fromkeys(names) if k not in allowed]
    if bad:
        raise Refusal(f"{REFUSE}unknown keys {', '.join(bad)}. Allowed: {' '.join(allowed)}.")


def _missing(keys):
    return Refusal(f"{REFUSE}missing {', '.join(keys)}. Fill every field or drop the block.")


@dataclass(frozen=True)
class Block:
    """One parsed dispatch block. Construction validates it (Refusal), so every Block is relayable."""
    kind: str                       # "brief" | "amend" | "stop"
    fields: dict = field(default_factory=dict)   # KEY -> value; continuation lines joined with "\n"
    unit: str = ""                  # amend/stop: the unit id
    rev: int = 0                    # amend: the N the author wrote (advisory)
    note: str = ""                  # amend: the SUPERSEDES text; stop: the reason

    def __post_init__(self):
        if self.kind not in ("brief", "amend", "stop"):
            raise ValueError(f"unknown block kind {self.kind!r}")
        _refuse_unknown(self.fields, ("TO",) if self.kind == "stop" else KEYS)
        tier = str(self.fields.get("TIER", "")).strip()
        if tier and tier not in TIERS:
            raise Refusal(f"{REFUSE}TIER must be one of {'|'.join(TIERS)}, got {tier!r}.")
        if self.kind == "brief":
            missing = [k for k in _REQUIRED if not str(self.fields.get(k, "")).strip()]
            if missing:
                raise _missing(missing)
        else:
            err = unit_id_error(self.unit)
            if err:
                raise Refusal(f"{REFUSE}{err}.")


@dataclass(frozen=True)
class Scan:
    bodies: tuple[str, ...]         # bodies of the closed dispatch blocks, in order
    unrelayed: str | None           # when bodies is empty and dispatch openers exist: the S1 message


def _is_comment(line):
    return line.lstrip().startswith("#")


def _codec(lines, supersedes=False):
    """The §5.3 line rules over `lines` (the verb line excluded for AMEND/STOP).
    Returns (every key name in text order, {known KEY: value} with the last value winning, the last
    SUPERSEDES text; "" unless `supersedes`)."""
    names, fields, note, cur = [], {}, "", None
    for raw in lines:
        if not raw.strip():
            continue
        if raw.startswith("  "):
            if cur is not None:     # G1: content even when it starts with "#"
                piece = raw[2:].rstrip()
                fields[cur] = f"{fields[cur]}\n{piece}" if fields[cur] else piece
            continue
        if _is_comment(raw):
            continue
        if supersedes:
            m = _SUPERSEDES.match(raw)
            if m:
                note = m.group(1).strip()
                continue
        key, _, value = raw.strip().partition(" ")
        names.append(key)
        cur = key if key in KEYS else None
        if cur:
            fields[key] = value.strip()
    return names, fields, note


def parse(body: str) -> Block:
    """One block body -> a valid Block (§5.3). Raises Refusal (§5.4); the first fault wins:
    empty, verb syntax, unknown keys, duplicate keys, then the Block's own checks."""
    lines = (body or "").splitlines()
    i = next((i for i, ln in enumerate(lines) if ln.strip() and not _is_comment(ln)), None)
    if i is None:
        raise _missing(_REQUIRED)
    verb, rest = lines[i].strip(), lines[:i] + lines[i + 1:]
    unit, rev, reason = "", 0, ""
    if verb.startswith("AMEND"):
        m = _AMEND.fullmatch(verb)
        if not m:
            raise Refusal(f"{REFUSE}AMEND needs 'AMEND <unit> rev <N>'.")
        kind, unit, rev, allowed = "amend", m.group(1), int(m.group(2)), KEYS
    elif verb.startswith("STOP"):
        m = _STOP.fullmatch(verb)
        if not m:
            raise Refusal(f"{REFUSE}STOP needs 'STOP <unit>'.")
        kind, unit, allowed, reason = "stop", m.group(1), ("TO",), (m.group(2) or "").strip()
    else:
        kind, allowed, rest = "brief", KEYS, lines
    names, fields, note = _codec(rest, supersedes=kind == "amend")
    _refuse_unknown(names, allowed)
    dups = [k for k in dict.fromkeys(names) if names.count(k) > 1]
    if dups:
        raise Refusal(f"{REFUSE}duplicate keys {', '.join(dups)}. Write each key once; continue a value on "
                      "lines indented by two spaces.")
    return Block(kind, fields, unit, rev, reason if kind == "stop" else note)


def _not_relayed(lines, spans):
    """[(line, reason)] (1-based) for each dispatch opener in a reply that yields no relayed block."""
    out = []
    for i, line in enumerate(lines):
        m = _OPENER.match(line)
        if not m:
            continue
        outer = next(((s, e) for s, e, _info in spans if s < i and (e is None or e > i)), None)
        if outer is not None:
            reason = (f"it is inside the fenced block opened at line {outer[0] + 1}"
                      + (", which is never closed" if outer[1] is None else
                         f", which swallows it (that fence pairs with the closer at line {outer[1] + 1})"))
        else:
            indent, char = len(m.group(1).expandtabs()), m.group(2)[0]
            deeper = next((j for j in range(i + 1, len(lines))
                           if lines[j].strip().strip(char) == "" and lines[j].strip().startswith(char * 3)
                           and len(lines[j]) - len(lines[j].lstrip()) > indent), None)
            reason = ("its closing fence at line %d is indented deeper than its opener" % (deeper + 1)
                      if deeper is not None else "it is never closed")
        out.append((i + 1, reason))
    return out


def scan(text: str) -> Scan:
    """The reply's dispatch blocks, with one util.fence_spans pass. Never raises."""
    lines = (text or "").splitlines()
    spans = util.fence_spans(lines)
    bodies = tuple("\n".join(lines[s + 1:e]).rstrip("\n") for s, e, info in spans
                   if e is not None and info == "dispatch")
    if bodies:
        return Scan(bodies, None)
    found = _not_relayed(lines, spans)
    if not found:
        return Scan((), None)
    where = "; ".join(f"the block at line {n}: {reason}" for n, reason in found)
    return Scan((), f"[orche] dispatch block not relayed: {where}. Nothing in this reply was relayed; re-send "
                    "the dispatch block outside any fence (close the earlier fence / outdent the closer).")


# ---- BRIEF.md: the unit folder, revisions, writer and reader -------------

@dataclass(frozen=True)
class Brief:
    """A unit's current BRIEF.md as read back. rev 0: the folder exists, BRIEF.md is missing or unreadable."""
    unit: str
    project: str                    # the registry key whose root holds the folder
    root: Path                      # that project root
    rev: int
    fields: dict                    # the `## Brief` section's KEY -> value ({} without the mark)
    text: str = field(repr=False)

    @property
    def folder(self) -> Path:
        return Path(self.root) / UNITS / self.unit

    @property
    def path(self) -> Path:
        return self.folder / "BRIEF.md"

    @property
    def to(self) -> str:
        return str(self.fields.get("TO", "")).strip()

    @property
    def tier(self) -> str:
        tier = str(self.fields.get("TIER", "")).strip().lower()
        return tier if tier in TIERS else "standard"


def _read(unit, project, root, text) -> Brief:
    """The Brief for BRIEF.md `text` (None: missing or unreadable -> rev 0)."""
    if text is None:
        return Brief(unit, project, Path(root), 0, {}, "")
    m = _REV.search(text)
    marks = list(_MARK.finditer(text))
    fields = _codec(text[marks[-1].end():].splitlines())[1] if marks else {}
    return Brief(unit, project, Path(root), int(m.group(1)) if m else 1, fields, text)


def _text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def find(reg, unit, *, project=None) -> Brief | None:
    """The unit's Brief, or None for a bad id or when no registered project root holds its folder.
    Without `project`, the longest project key whose slug prefixes the id wins; `project` restricts
    the search to that root."""
    if unit_id_error(unit):
        return None
    if project is not None:
        keys = [project]
    else:
        keys = sorted((k for k in reg.get("projects", {}) if unit.startswith(util.slugify(k) + "-")),
                      key=len, reverse=True)
    for key in keys:
        root = units.project_root(reg, key)
        if root is not None and (root / UNITS / unit).is_dir():
            return _read(unit, key, root, _text(root / UNITS / unit / "BRIEF.md"))
    return None


def _order(unit):
    m = _ORDER.search(unit)
    return (m.group(1), int(m.group(2)), unit) if m else ("", 0, unit)


def newest(reg, project, session) -> Brief | None:
    """The project's readable brief addressed to `session` with the newest BRIEF.md mtime, or None.
    A tie goes to the later unit id: its date, then its counter `n` compared as a number (so -10 beats
    -9), then the id text. Never raises."""
    try:
        root = units.project_root(reg, project)
        if root is None or not (root / UNITS).is_dir():
            return None
        best, best_key = None, None
        for folder in (root / UNITS).iterdir():
            if unit_id_error(folder.name) or not folder.is_dir():
                continue
            b = _read(folder.name, project, root, _text(folder / "BRIEF.md"))
            if b.rev == 0 or b.to != session:
                continue
            try:
                key = (b.path.stat().st_mtime_ns, _order(b.unit))
            except OSError:
                continue
            if best_key is None or key > best_key:
                best, best_key = b, key
        return best
    except Exception:   # the contract is "never raises": a malformed registry entry fails in project_root
        return None


def _allocate(root, proj, goal) -> str:
    """`<proj>-<slug>-<yyyymmdd>-<n>`: slug = the first 4 (redacted) GOAL words, n = next free for the
    project today. The folder is created here, exclusively, so concurrent relays never share an id."""
    date = f"{units.now().date():%Y%m%d}"
    goal = mask.redact(goal)[0]         # the id lands in paths, pointers, nudges and events.log
    slug = util.slugify(" ".join(goal.split()[:4])) or "unit"
    base = Path(root) / UNITS
    base.mkdir(parents=True, exist_ok=True)
    pat = re.compile(rf"^{re.escape(util.slugify(proj))}-.+-{date}-(\d+)$")
    n = 1 + max([int(m.group(1)) for m in (pat.match(p.name) for p in base.iterdir()) if m] or [0])
    while True:
        unit = f"{util.slugify(proj)}-{util.slugify(slug)}-{date}-{n}"
        try:
            (base / unit).mkdir()
            return unit
        except FileExistsError:
            n += 1


def _where(target, timeout):
    """(worktree, branch, root) for the header. A unit with no recorded worktree is shown as the project
    root marked `(main checkout)`, never `.`; `timeout` bounds the main checkout's `git branch` call
    (at or below 0 it is skipped and the branch is `-`)."""
    root = str(target.home)
    owner = target.owns_worktree
    if owner and target.worktree is not None:
        return str(target.worktree), target.branch or "-", root
    recorded = target.branch if owner else ""
    res = (shell.run(["git", "-C", root, "branch", "--show-current"], timeout=timeout)
           if root and timeout > 0 and not recorded else None)
    branch = recorded or (res.out.strip() if res is not None and res.ok else "") or "-"
    return root + (_MAIN_CHECKOUT if owner else ""), branch, root


def _render(unit, rev, supersedes, note, to, frm, at, standing, fields) -> str:
    worktree, branch, root = at
    lines = [f"rev: {rev}"]
    if supersedes:
        lines.append(f"supersedes: rev {supersedes}")
        if note:
            lines.append(f"supersedes-note: {note}")
    lines += [f"unit: {unit}",
              f"to: {to}   from: {frm}",
              f"worktree: {worktree}   branch: {branch}",
              f"project root: {root}   (brief + REPORT.md live under project root/{UNITS}/{unit}/)",
              "Work only inside `worktree`. SCOPE paths are relative to it."]
    lines += ["", standing.rstrip(), "", f"persona: .claude/agents/{to}.md", "", f"## Brief {unit}"]
    for key in KEYS:
        if key in fields:
            first, *more = str(fields[key]).split("\n")
            lines.append(f"{key:<9} {first}".rstrip())
            lines += [f"  {m}" for m in more]
    return "\n".join(lines) + "\n"


def write(block: Block, target: "roster.Member", sender: str, standing: str, *, prior: Brief | None = None,
          timeout: float = 0.5) -> Brief:
    """Write the next revision of a brief for `target` (a roster.Member) from `sender` (a session):
    BRIEF.rev<N>.md, then BRIEF.md. A "brief" block allocates a new unit folder (rev 1); an "amend"
    block needs `prior` (find()) and merges its fields over prior.fields. TO is always target.session.
    Raises ValueError for a STOP block, an AMEND without prior, a brief with prior, or a target without
    a project root."""
    if block.kind == "stop":
        raise ValueError("a STOP block writes no brief")
    if block.kind == "amend" and prior is None:
        raise ValueError(f"AMEND {block.unit} needs the prior Brief")
    if block.kind == "brief" and prior is not None:
        raise ValueError("a new brief takes no prior Brief")
    if target.home is None:
        raise ValueError(f"{target.session} has no project root")
    if block.kind == "amend":
        unit, project, root = prior.unit, prior.project, Path(prior.root)
        rev, supersedes = prior.rev + 1, prior.rev
        if block.rev != rev:
            log.log("hooks", f"dispatch: AMEND {unit} said rev {block.rev}; using {rev}")
        fields = {**prior.fields, **block.fields, "TO": target.session}
    else:
        project, root = target.project, Path(target.home)
        unit = _allocate(root, project, block.fields["GOAL"])
        rev, supersedes = 1, 0
        fields = {**block.fields, "TO": target.session}
    text = _render(unit, rev, supersedes, block.note if block.kind == "amend" else "", target.session, sender,
                   _where(target, timeout), standing, fields)
    folder = root / UNITS / unit
    util.atomic_write(folder / f"BRIEF.rev{rev}.md", text)
    util.atomic_write(folder / "BRIEF.md", text)
    return _read(unit, project, root, text)
