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
_FROM = re.compile(r"^to: \S+\s+from: (\S+)\s*$", re.M)
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


_NOTE = "\0note"                 # _codec's cursor while plain text feeds the AMEND note / STOP reason
_DUP = "Write each key once; continue a value on the lines below it."
_FIX = "start each field with its exact KEY"


def _quote(text):
    """`text` as one quoted line of at most 60 chars, for refusals."""
    return '"%s"' % " ".join(str(text).split())[:60]


def _keylike(token):
    return token.upper() in KEYS or (len(token) > 1 and token.isupper() and token.replace("_", "").isalpha())


def _merged(n, text, key, kind):
    """G2: refuse a plain line that shows two blocks run together or a pasted code sample: a code
    fence anywhere, a valid STOP/AMEND verb line inside a brief or AMEND body, or a KEY line other
    than TO inside a STOP."""
    at = f"line {n} {_quote(text)}"
    if text.startswith(("```", "~~~")):
        raise Refusal(f"{REFUSE}{at} is a code fence inside a dispatch block; "
                      "code samples go indented by two spaces.")
    if kind != "stop":
        m = _STOP.fullmatch(text) or _AMEND.fullmatch(text)
        if m and unit_id_error(m.group(1)) is None:
            raise Refusal(f"{REFUSE}{at} starts another block; put it in its own dispatch block.")
    elif key in KEYS and key != "TO":
        raise Refusal(f"{REFUSE}{at} is a {key} line inside a STOP; a STOP takes only TO and the reason.")


def _codec(lines, kind="brief", skip=None):
    """The §5.3 line rules over `lines` after index `skip` (the AMEND/STOP verb line; None: all).
    A plain line whose first token is a key the kind allows (STOP: TO only) opens that field; any other
    plain line, and every line indented by two spaces (G1: even one starting with "#"), continues the
    current field. Before any key, AMEND text feeds the note, and a SUPERSEDES line restarts the note;
    every other plain line of a STOP is reason. Raises Refusal for the merged-block lines of
    _merged. Returns (names [(KEY, line no, text)] in text order,
    {KEY: value} with the last value winning, the note / reason lines joined by "\n", absorbed
    [(line no, text, KEY)] for plain lines that continued a field, and the first plain line of a brief
    before any key as (line no, text) or None)."""
    allowed = ("TO",) if kind == "stop" else KEYS
    names, fields, notes, absorbed, stray = [], {}, [], [], None
    cur = None if kind == "brief" else _NOTE

    def extend(piece):
        if cur == _NOTE:
            notes.append(piece)
        elif cur is not None:
            fields[cur] = f"{fields[cur]}\n{piece}" if fields[cur] else piece

    for n, raw in enumerate(lines, 1):
        if (skip is not None and n - 1 <= skip) or not raw.strip():   # up to the verb: blanks, comments
            continue
        if raw.startswith("  "):
            extend(raw[2:].rstrip())
            continue
        if _is_comment(raw):
            continue
        text = raw.strip()
        key, _, value = text.partition(" ")
        _merged(n, text, key, kind)
        m = _SUPERSEDES.match(text) if kind == "amend" else None
        if m:
            notes[:], cur = [m.group(1).strip()], _NOTE
        elif key in allowed:
            names.append((key, n, text))
            cur, fields[key] = key, value.strip()
        elif kind == "stop":
            cur = _NOTE
            notes.append(text)
        elif cur is None:
            stray = stray or (n, text)
        else:
            if cur != _NOTE:
                absorbed.append((n, text, cur))
            extend(text)
    return names, fields, "\n".join(notes), absorbed, stray


def parse(body: str) -> Block:
    """One block body -> a valid Block (§5.3). Raises Refusal (§5.4); the first fault wins:
    empty, verb syntax, a brief that opens with prose, duplicate keys, then the Block's own checks.
    Each refusal quotes the line at fault (60 chars) and says the fix."""
    lines = (body or "").splitlines()
    i = next((i for i, ln in enumerate(lines) if ln.strip() and not _is_comment(ln)), None)
    if i is None:
        raise _missing(_REQUIRED)
    verb, at = lines[i].strip(), f"line {i + 1} {_quote(lines[i])}"
    unit, rev, reason, skip = "", 0, "", i
    if verb.startswith("AMEND"):
        m = _AMEND.fullmatch(verb)
        if not m:
            raise Refusal(f"{REFUSE}AMEND needs 'AMEND <unit> rev <N>'; {at} does not match.")
        kind, unit, rev = "amend", m.group(1), int(m.group(2))
    elif verb.startswith("STOP"):
        m = _STOP.fullmatch(verb)
        if not m:
            raise Refusal(f"{REFUSE}STOP needs 'STOP <unit>'; {at} does not match.")
        kind, unit, reason = "stop", m.group(1), (m.group(2) or "").strip()
    else:
        kind, skip = "brief", None
    names, fields, note, absorbed, stray = _codec(lines, kind, skip)
    if stray:
        raise Refusal(f"{REFUSE}line {stray[0]} {_quote(stray[1])} is not a KEY line; a brief starts with TO.")
    keys = [k for k, _n, _t in names]
    dups = [k for k in dict.fromkeys(keys) if keys.count(k) > 1]
    if dups:
        n, text = next((n, t) for j, (k, n, t) in enumerate(names) if k in keys[:j])
        raise Refusal(f"{REFUSE}duplicate keys {', '.join(dups)}; line {n} {_quote(text)} repeats one. {_DUP}")
    if kind == "stop":
        note = "\n".join(p for p in (reason, note) if p)
    try:
        return Block(kind, fields, unit, rev, note)
    except Refusal as exc:
        msg = str(exc)
        if msg.startswith(f"{REFUSE}missing"):
            hint = next((a for a in absorbed if _keylike(a[1].split()[0])), None)
        elif msg.startswith(f"{REFUSE}TIER"):
            hint = next((a for a in absorbed if a[2] == "TIER"), None)
        else:
            hint = None
        if hint is None:
            raise
        raise Refusal(f"{msg} Line {hint[0]} {_quote(hint[1])} was read as part of {hint[2]}; {_FIX}.") from None


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
    def sender(self) -> str:
        """The header's `from:` session (who sent this revision), or "" when the header has none."""
        m = _FROM.search(self.text)
        return m.group(1) if m else ""

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
