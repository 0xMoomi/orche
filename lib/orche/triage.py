"""Triage gate for a Main pane's prompts: does this need orchestration, decided before Main spends a turn.

Runs inside the UserPromptSubmit hook (hooks/report_inject.py) of a Main session only, in stages:

1. tags (first token, case-insensitive, stripped before anything reads the prompt):
   `[main]` -> Main; `[direct]`, `[build]`, `[research]`, `[rnd]`, `[gtm]`, `[ops]` -> straight to
   that division; `[drain]` and a prompt starting with `/` or `!` -> bypass.
2. bypasses (no model): too short, a question (ends with `?`), a reply to a question Main just asked
   (its last text, trailing fenced blocks and STATUS lines stripped, ends with `?`).
3. structure (no model): a candidate names a concrete target (a path, a file name, a quoted
   identifier) and is not a question; intent is the classifier's, in any language.
4. classifier (candidates only): Jev (`jev.JevClient`, 1 s) or `claude -p` Haiku, the blank
   headless call (`headless.py`: a fresh temp dir outside $HOME, no tools, no inherited identity),
   killed at `triage.max_latency_s`; direct only when route == direct, confidence >=
   triage.min_confidence and tier != hard.
5. action: a brief relayed through the same path as a Main dispatch (dispatch.relay_detail,
   `by=triage`), then the hook blocks the prompt so Main spends no turn (Main's activity is touched,
   as on any Main prompt). GOAL is the classifier's goal, the prompt (<= GOAL_CHARS) or a marked
   excerpt; CONTEXT then quotes the whole prompt.

Everything is fail-open: an error, a timeout or a low confidence leaves the prompt to Main. Once the
runner started, the prompt is always blocked: recording the decision is best-effort. Every
decision writes one `events` line and one masked row to ORCHE_HOME/triage/samples.jsonl (the prompt
itself only with `triage.keep_prompts`). Off unless `triage.enabled`; a non-Main session never triages.
"""
import hashlib
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, config, headless, log, mask, paths, registry, units, util

DIVISION_TAGS = registry.DIVISIONS                   # rnd research build gtm ops
ROUTES = ("main", "direct")
BACKENDS = ("auto", "jev", "claude-haiku")
HAIKU_MODEL = "claude-haiku-5-5"
MIN_CHARS = 12
TAIL_BYTES = (64 * 1024, 512 * 1024)                 # transcript tail read for "did Main ask?"
MAX_SCOPE = 20

CONTEXT = "routed by triage from a Main prompt; the prompt is the whole instruction"
CONTEXT_QUOTED = "routed by triage from a Main prompt; the prompt, quoted below, is the whole instruction"
GOAL_CHARS = 300                                     # a prompt up to this long is the GOAL verbatim
GOAL_EXCERPT = 200                                   # else GOAL is this long an excerpt of it, plus:
GOAL_POINTER = "excerpt; the full prompt is quoted in CONTEXT"
REASON_GOAL = 60                                     # GOAL characters shown in the block reason
TIMEBOX = "30m"
FORBID = "push, amend, --no-verify, files outside SCOPE"
REPORT = "standard"
DEFAULT_ACCEPT = "the change the prompt asks for is made in the SCOPE files and nothing else changed"
COMMIT_ACCEPT = ("`git add` only the files you changed, by name (never `-A`, `-u`, `.` or a glob); exactly one "
                 "commit with a conventional message")
NO_SCOPE = "."
SCOPE_PATH = re.compile(r"[A-Za-z0-9_./-]+")         # a SCOPE entry: path characters only
# A classifier SCOPE glob: path characters plus `*` and `?` (`**` spans directories); the
# units.scope_match grammar. An entry with `[` or `]` is neither a glob nor a plain path: dropped.
# A glob never reaches VERIFY (refused); the commit clause tells the worker never to `git add` one.
SCOPE_GLOB = re.compile(r"(?:[A-Za-z0-9_./-]|\*|\?)+")
GLOB_CHARS = "*?["
# What the classifier may say: a one-line VERIFY a mechanical worker can run (MECH_TOOLS in runs/job.py).
VERIFY_OK = re.compile(r"^(?:ls|test|git\s+(?:status|diff))(?:\s|$)")
VERIFY_SHELL = re.compile(r"[;&|<>`$\\\n\r*?\[\]]")   # shell syntax, and globs (SCOPE-only)
# `git status`/`git diff` options a VERIFY may use; any other option (`--output`, `--ext-diff`,
# `--textconv`, `-c`, …) is refused, since some write files or run programs.
GIT_VERIFY_OPTS = ("--short", "--stat", "--name-only", "--name-status", "--cached", "--porcelain", "--")
MAX_GOAL, MAX_ACCEPT, MAX_VERIFY = 500, 300, 200

_TAG = re.compile(r"^\s*\[([A-Za-z]+)\](?=\s|$)\s*")      # the whole first token: `[build]s` is no tag
# A question: the text ends with `?` or `？` (emphasis or a closing bracket may follow). One rule for
# every language: what a prompt means is the classifier's call.
_QUESTION_END = re.compile(r"[?？]\s*[*_`)\]]*\s*$")
_EXT = (r"(?:md|mdx|py|ts|tsx|js|jsx|mjs|cjs|json|ya?ml|toml|txt|sh|css|scss|html|go|rs|java|kt|swift|rb|"
        r"php|c|h|cpp|hpp|sql|ini|cfg|csv|env\.example|lock)")
# ASCII classes on purpose: a word of another script glued to a file name ("README.mdを") must not hide it.
_PATH = re.compile(r"(?<![A-Za-z0-9_/.-])(?:\.{0,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9]{1,8}"
                   r"(?![A-Za-z0-9_/])")
_FILE = re.compile(r"(?<![A-Za-z0-9_/.-])[A-Za-z0-9_-][A-Za-z0-9_.-]*\." + _EXT + r"(?![A-Za-z0-9_/])",
                   re.IGNORECASE)
HEURISTIC_CHARS = 4000                               # stage 3 reads at most this much of a prompt
_QUOTED = re.compile(r"[`'\"“‘]([A-Za-z_][\w.:/-]{1,80})[`'\"”’]")
_STATUS_LINE = re.compile(r"^\W*STATUS:", re.IGNORECASE)
_BARE_FENCE = re.compile(r"^(?:`{3,}|~{3,})$")
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# One description per division, shared by the Haiku prompt and the Jev division question.
DIVISION_HINTS = {"build": "code and doc changes", "research": "reading and reporting",
                  "rnd": "experiments and prototypes", "gtm": "marketing copy",
                  "ops": "operations and configuration"}
ROUTE_HINTS = {"direct": "one worker division can do it now with no planning",
               "main": "needs planning, design, judgement, a conversation, several steps across areas, "
                       "or is risky or irreversible"}
TIER_HINTS = {"mechanical": "a small, fully specified edit", "standard": "a contained change needing some care",
              "hard": "design or root-cause work"}

SYSTEM_PROMPT = """You are the triage classifier of a software project's orchestrator. Decide whether a
user's request can go straight to ONE worker division with no planning (route "direct"), or needs the
main orchestrator session (route "main": planning, design, judgement, a conversation, several steps
across areas, unclear intent, anything risky or irreversible).
Divisions: {divisions}. """ + "; ".join(f"{k} = {v}" for k, v in DIVISION_HINTS.items()) + """.
Tier: """ + "; ".join(f"{k} = {v}" for k, v in TIER_HINTS.items()) + """.
Reply with ONE JSON object and nothing else, no prose, no code fence:
{{"route": "main"|"direct", "division": one of the divisions, "tier": "mechanical"|"standard"|"hard",
"confidence": a number 0..1 for the route, "goal": one sentence, "accept": how to tell it is done,
"verify": one read-only command starting with ls, test, git status or git diff that checks it (no wildcards),
"scope": [relative file paths it touches, or globs such as src/**/*.ts]}}"""


@dataclass
class Decision:
    decision: str                   # main | direct | bypass | tag
    stage: int
    prompt: str = ""                # tag stripped
    reason: str = ""
    division: str = ""
    tier: str = ""
    confidence: object = None
    backend: str = "none"
    goal: str = ""
    accept: str = ""
    verify: str = ""
    scope: list = field(default_factory=list)
    model_s: float = 0.0            # seconds spent in the classifier call
    cost_usd: object = None
    unit: str = ""


@dataclass
class Outcome:
    block: object = None            # hook JSON that blocks the prompt, or None
    note: str = ""                  # one line injected for Main
    model_s: float = 0.0


# ---- config ----------------------------------------------------------------

def settings(cfg=None) -> dict:
    cfg = cfg if cfg is not None else config.load()
    t = cfg.get("triage") or {}
    return t if isinstance(t, dict) else {}


def _num(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def min_confidence(cfg) -> float:
    return float(_num(settings(cfg).get("min_confidence"), config.DEFAULTS["triage"]["min_confidence"]))


def max_latency(cfg) -> float:
    return float(_num(settings(cfg).get("max_latency_s"), config.DEFAULTS["triage"]["max_latency_s"]))


def backend(cfg):
    """(name, jev client or None): `auto` is Jev when enabled and configured, else claude-haiku.
    An explicit `jev` uses the configured client even with jev.enabled false; None when unusable."""
    from . import jev
    want = settings(cfg).get("backend") or "auto"
    if want == "claude-haiku":
        return "claude-haiku", None
    if want == "jev":
        return "jev", jev.client(require_enabled=False)
    return ("jev", c) if (c := jev.client()) is not None else ("claude-haiku", None)


# ---- stage 1: tags ---------------------------------------------------------

def parse_tag(prompt: str):
    """(tag or None, rest). A tag is a known bracketed first token; `/…` and `!…` read as `bypass`."""
    text = prompt or ""
    m = _TAG.match(text)
    if m:
        tag = m.group(1).lower()
        if tag in ("main", "direct", "drain") or tag in DIVISION_TAGS:
            return tag, text[m.end():].strip()
    if text.lstrip().startswith(("/", "!")):
        return "bypass", text.strip()
    return None, text.strip()


# ---- stage 2: bypasses -----------------------------------------------------

def is_question(prompt: str) -> bool:
    return bool(_QUESTION_END.search(prompt))


def _tail_lines(path, size):
    with open(path, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        start = max(0, end - size)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return (lines[1:] if start > 0 else lines), start > 0


def last_assistant_text(path) -> str:
    """Text of the transcript's last assistant entry carrying text, read from the file's tail only."""
    if not path:
        return ""
    try:
        for size in TAIL_BYTES:
            lines, cut = _tail_lines(path, size)
            for line in reversed(lines):
                if '"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip():
                    return content
                texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
                if any(t.strip() for t in texts):
                    return "\n".join(texts)
            if not cut:
                break
    except (OSError, TypeError):
        return ""
    return ""


def _closing_block_start(lines):
    """The index of the fence that opens the block the last line closes, or None (util.fence_spans:
    ``` or ~~~, a closer of the opener's character and at least its length; inside a dispatch/amend/
    stop block only a closer indented no deeper than its opener, so a nested snippet stays inside)."""
    last = len(lines) - 1
    return next((start for start, end, _info in util.fence_spans(lines) if end == last), None)


def strip_trailer(text: str) -> str:
    """`text` without its trailing fenced blocks (a fenced block Main left in its reply), STATUS
    lines and blank lines."""
    lines = (text or "").rstrip().splitlines()
    while lines:
        last = lines[-1].strip()
        if not last or _STATUS_LINE.match(last):
            lines.pop()
        elif _BARE_FENCE.match(last):
            opening = _closing_block_start(lines)
            if opening is None:
                break
            del lines[opening:]
        else:
            break
    return "\n".join(lines).rstrip()


def main_asked(path) -> bool:
    """Main's last assistant message, its trailer stripped, ends with a question."""
    return is_question(strip_trailer(last_assistant_text(path)))


def bypass_reason(prompt: str, transcript_path=None):
    """Why this untagged prompt skips triage, or None."""
    if len(prompt.strip()) < MIN_CHARS:
        return "short"
    if is_question(prompt):
        return "question"
    if main_asked(transcript_path):
        return "reply-to-main"
    return None


# ---- stage 3: structure ----------------------------------------------------

def find_paths(prompt: str) -> list:
    """Path-like tokens and file names, in order, de-duplicated."""
    prompt = prompt[:HEURISTIC_CHARS]
    hits = [(m.start(), m.group(0)) for m in _PATH.finditer(prompt)]
    hits += [(m.start(), m.group(0)) for m in _FILE.finditer(prompt)]
    out = []
    for _pos, token in sorted(hits):
        token = token.rstrip(".,;:")
        if token and token not in out and not any(token != o and o.endswith("/" + token) for o in out):
            out.append(token)
    return out[:MAX_SCOPE]


def has_target(prompt: str) -> bool:
    return bool(find_paths(prompt) or _QUOTED.search(prompt[:HEURISTIC_CHARS]))


def is_candidate(prompt: str) -> bool:
    """A prompt naming a target that is not a question, in any language."""
    return has_target(prompt) and not is_question(prompt)


# ---- stage 4: classifier ---------------------------------------------------

def is_glob(entry: str) -> bool:
    return any(c in entry for c in GLOB_CHARS)


def _glob_base(entry: str) -> str:
    """The literal directories before a glob's first wildcard segment (`src/**/*.ts` -> `src`)."""
    parts = entry.split("/")
    return "/".join(itertools.takewhile(lambda s: not is_glob(s), parts[:-1]))


def _clean_scope(value, root=None, globs=False) -> list:
    """Plain relative paths (SCOPE_PATH: no spaces, no shell characters, no leading `-`), no `..`
    segment, inside `root` when given, `./` dropped; anything else dropped. With `globs` (the
    classifier's scope only) an entry may also be a glob (SCOPE_GLOB: `*`, `?`, `**`, nothing else;
    `[`/`]` drop the entry), relative, without `..`, whose literal base directory is inside `root`;
    it is matched like a feature scope (units.scope_match) and never handed to a command."""
    out = []
    for p in value if isinstance(value, list) else []:
        if not isinstance(p, str):
            continue
        p = p.strip()
        while p.startswith("./"):
            p = p[2:]
        glob = is_glob(p)
        shape = SCOPE_GLOB.fullmatch(p) if glob and globs else (not glob and SCOPE_PATH.fullmatch(p))
        inside = Path(root) / (_glob_base(p) if glob else p) if root is not None else None
        if (p and len(p) <= 200 and shape and not p.startswith(("/", "-"))
                and ".." not in p.split("/") and p not in out
                and (root is None or units.is_within(inside, root))):
            out.append(p)
    return out[:MAX_SCOPE]


def verify_ok(command) -> bool:
    """A one-line VERIFY a mechanical worker can run: `ls`, `test`, `git status` or `git diff`, no shell
    syntax; `git` takes only GIT_VERIFY_OPTS and plain paths."""
    if not (isinstance(command, str) and 0 < len(command) <= MAX_VERIFY and not VERIFY_SHELL.search(command)
            and VERIFY_OK.match(command)):
        return False
    words = command.split()
    if words[0] != "git":
        return True
    return all(w in GIT_VERIFY_OPTS or (SCOPE_PATH.fullmatch(w) and not w.startswith("-")) for w in words[2:])


def normalize(data, divisions):
    """A typed decision dict, or None when `data` is not one."""
    if not isinstance(data, dict):
        return None
    route, tier, conf = data.get("route"), data.get("tier"), data.get("confidence")
    if route not in ROUTES or tier not in brief.TIERS:
        return None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        return None
    division = data.get("division")
    verify = str(data.get("verify") or "").strip()
    return {"route": route, "division": division if division in divisions else "", "tier": tier,
            "confidence": float(conf), "goal": str(data.get("goal") or "").strip()[:MAX_GOAL],
            "accept": " ".join(str(data.get("accept") or "").split())[:MAX_ACCEPT],
            "verify": verify if verify_ok(verify) else "", "scope": _clean_scope(data.get("scope"), globs=True)}


def parse_json_object(text):
    """The first JSON object in a model's reply: bare, fenced, or embedded in prose; None if none."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    candidates = [text]
    m = _FENCED_JSON.search(text)
    if m:
        candidates.append(m.group(1))
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    for c in candidates:
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def classify_haiku(prompt, divisions, cfg, timeout):
    """(decision or None, cost_usd). The blank headless call (headless.py) on the masked prompt,
    killed at `timeout`; a launch failure, a timeout or a non-zero exit is one log line and None."""
    call = headless.Call("blank", HAIKU_MODEL, 1, timeout,
                         system=SYSTEM_PROMPT.format(divisions=", ".join(divisions)))
    res = headless.run(call, "Request:\n" + mask.redact(prompt)[0], cfg)
    if res.error or res.code:
        log.log("hooks", f"triage: claude-haiku {res.error or f'exit {res.code}'}")
        return None, None
    if res.is_error:
        return None, res.cost_usd
    return normalize(parse_json_object(res.text), divisions), res.cost_usd


def classify_jev(client, prompt, divisions, deadline):
    from . import jev
    qs = [jev.choice("route", "Can this request go straight to one worker division with no planning "
                              "(direct), or does it need the main orchestrator session (main)?", ROUTE_HINTS),
          jev.choice("division", "Which division should do it?",
                     {d: DIVISION_HINTS.get(d, d) for d in divisions}),
          jev.choice("tier", "How hard is it?", TIER_HINTS)]
    a = client.ask(prompt, qs, deadline=deadline)
    if not a or "route" not in a or "tier" not in a:
        return None
    # Gate on the route answer alone: Jev's probability for the route it chose, else its confidence.
    route = a["route"]
    confidence = route.get("probabilities", {}).get(route["value"], route["confidence"])
    data = {"route": route["value"], "division": (a.get("division") or {}).get("value"),
            "tier": a["tier"]["value"], "confidence": confidence}
    return normalize(data, divisions)


def classify(prompt, divisions, cfg, deadline=None):
    """(decision or None, backend name, seconds, cost_usd). Never raises."""
    started = time.monotonic()
    name = "none"
    try:
        name, client = backend(cfg)
        limit = max_latency(cfg)
        if name == "jev":
            if client is None:              # unconfigured: no classifier call was made
                return None, "none", 0.0, None
            return (classify_jev(client, prompt, divisions, deadline or time.monotonic() + limit), name,
                    time.monotonic() - started, None)
        result, cost = classify_haiku(prompt, divisions, cfg, limit)
        return result, name, time.monotonic() - started, cost
    except Exception as exc:  # noqa: BLE001 - fail-open
        log.log("hooks", f"triage: classifier: {type(exc).__name__}: {exc}")
        return None, name, time.monotonic() - started, None


def direct_ok(result, min_conf) -> bool:
    """The decision rule: direct only when route == direct, confidence >= min_conf, tier != hard."""
    return bool(result and result["route"] == "direct" and result["confidence"] >= min_conf
                and result["tier"] != "hard" and result["division"])


# ---- stages 1-4 -------------------------------------------------------------

def _fill(d: Decision, result) -> Decision:
    if result:
        d.goal, d.accept, d.verify = result["goal"], result["accept"], result["verify"]
        d.scope = result["scope"]
    return d


def decide(prompt, divisions, cfg, transcript_path=None, classifier=None) -> Decision:
    """Stages 1-4 for one prompt of an enabled Main session; `divisions` the project's (with a session).
    `classifier(prompt, divisions, cfg)` -> (decision|None, backend, seconds, cost); default `classify`."""
    classifier = classifier or classify
    tag, rest = parse_tag(prompt)
    if tag in ("drain", "bypass"):
        return Decision("bypass", 1, rest, reason=tag)
    if tag == "main":
        return Decision("tag", 1, rest, reason="main")
    if tag is not None:                                   # [direct] or a division tag
        if not rest:
            return Decision("bypass", 1, rest, reason="empty")
        d = Decision("direct", 1, rest, reason=f"tag {tag}", tier="standard", confidence=1.0)
        fixed = tag if tag != "direct" else ""
        if fixed and has_target(rest):
            d.division = fixed
            return d
        result, d.backend, d.model_s, d.cost_usd = classifier(rest, divisions or [fixed], cfg)
        if result and result["tier"] == "mechanical":
            d.tier = "mechanical"
        d.division = fixed or (result or {}).get("division") or ("build" if "build" in divisions else
                                                                  (divisions[0] if divisions else "build"))
        return _fill(d, result)
    why = bypass_reason(rest, transcript_path)
    if why:
        return Decision("bypass", 2, rest, reason=why)
    if not is_candidate(rest):
        return Decision("main", 3, rest, reason="not a candidate")
    result, name, secs, cost = classifier(rest, divisions, cfg)
    d = Decision("main", 4, rest, backend=name, model_s=secs, cost_usd=cost)
    if result is None:
        d.reason = "classifier unavailable"
        return d
    d.confidence, d.tier, d.division = result["confidence"], result["tier"], result["division"]
    if direct_ok(result, min_confidence(cfg)):
        d.decision, d.reason = "direct", "classifier"
        return _fill(d, result)
    d.reason = "classifier said main"
    return d


# ---- stage 5: action -------------------------------------------------------

def target_root(reg, proj, division):
    """The worktree SCOPE paths must stay in: the division's, else the project's main checkout."""
    entry = reg.get("projects", {}).get(proj) or {}
    wt = units.worktree_path(((entry.get("divisions") or {}).get(division) or {}).get("worktree") or "")
    return wt or entry.get("path") or None


def excerpt(text: str, limit: int) -> str:
    """`text` on one line (whitespace collapsed), cut to at most `limit` characters at a word
    boundary when one is near, with `…` when cut."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit * 2 // 3:
        cut = cut[:space]
    return cut.rstrip() + "…"


def goal_and_context(d: Decision):
    """(GOAL, CONTEXT). GOAL is the classifier's goal, else the prompt itself when it has at most
    GOAL_CHARS characters, else a GOAL_EXCERPT-character excerpt marked as one. Whenever GOAL is not
    the prompt verbatim, CONTEXT quotes the whole prompt (`> ` lines), so nothing is lost."""
    if d.goal:
        goal = d.goal
    elif len(d.prompt) <= GOAL_CHARS:
        return d.prompt, CONTEXT
    else:
        goal = f"{excerpt(d.prompt, GOAL_EXCERPT)} ({GOAL_POINTER})"
    quoted = "\n".join("> " + line if line.strip() else ">" for line in d.prompt.splitlines())
    return goal, f"{CONTEXT_QUOTED}\n{quoted}"


def brief_fields(d: Decision, root=None) -> dict:
    """The brief. GOAL/CONTEXT: goal_and_context. SCOPE: the classifier's paths and globs, else the
    prompt's paths, each cleaned (_clean_scope: relative, inside `root`); else `.`. VERIFY: the
    classifier's when verify_ok, else `ls -l <scope files>` (plain paths only, never a glob) when that
    is verify_ok too, else `git status --short`."""
    scope = _clean_scope(d.scope, root, globs=True) or _clean_scope(find_paths(d.prompt), root)
    files = [p for p in scope if not is_glob(p)]
    fallback = "ls -l " + " ".join(files) if files else ""
    if not verify_ok(fallback):
        fallback = "git status --short"
    goal, context = goal_and_context(d)
    return {"TO": d.division, "GOAL": goal, "SCOPE": " ".join(scope) or NO_SCOPE,
            "CONTEXT": context, "ACCEPT": f"{d.accept or DEFAULT_ACCEPT}; {COMMIT_ACCEPT}",
            "VERIFY": d.verify if verify_ok(d.verify) else fallback,
            "TIMEBOX": TIMEBOX, "FORBID": FORBID, "REPORT": REPORT, "TIER": d.tier or "standard"}


def block_reason(to, unit, state, tier, goal="") -> str:
    """What the user sees instead of Main's turn: where it went, the GOAL's first REASON_GOAL
    characters (masked; the prompt itself is gone from Main's context) and how to cancel."""
    what = f': "{excerpt(mask.redact(goal)[0], REASON_GOAL)}"' if goal.strip() else ""
    return (f"[orche] routed to {to} as {unit} ({state}, TIER {tier}){what}. Results arrive in this inbox; "
            f"prefix the prompt with [main] to talk to Main instead (to cancel: [main] STOP {unit}).")


def act(d: Decision, r, me, deadline=None) -> Outcome:
    """Relay decision `d` as a brief to its division, exactly like a Main dispatch, and block the
    prompt only when the runner started. `r` is the hook's Roster, `me` the Main's roster.Member
    (the sender, so `me.project` picks the division worktree), `deadline` the hook's monotonic
    deadline passed on to `dispatch.relay_detail`. Any refusal or error returns a note instead."""
    from . import dispatch
    try:
        fields = brief_fields(d, target_root(r.reg, me.project, d.division))
        [res] = dispatch.relay_detail(r, [brief.Block("brief", fields)], me, deadline, by="triage")
    except Exception as exc:  # noqa: BLE001 - Refusal or anything else: the prompt goes to Main
        reason = " ".join(str(exc).replace(brief.REFUSE, "").split()) or type(exc).__name__
        return Outcome(note=f"[orche] triage could not route this prompt: {reason}")
    d.unit = res["unit"]
    if res["state"] != "runner-started":
        return Outcome(note=f"[orche] triage could not route this prompt: {res['unit']} was filed for "
                            f"{res['to']} but the runner did not start ({res['state']})")
    return Outcome(block={"decision": "block",
                          "reason": block_reason(res["to"], res["unit"], res["state"], fields["TIER"],
                                                 fields["GOAL"])})


# ---- record ----------------------------------------------------------------

def samples_path():
    return paths.orche_home() / "triage" / "samples.jsonl"


def record(me, d: Decision, ms: int, cfg) -> None:
    conf = "-" if d.confidence is None else f"{float(d.confidence):.2f}"
    log.log("events", f"triage session={me} decision={d.decision} stage={d.stage} conf={conf} "
                      f"backend={d.backend} ms={ms} unit={d.unit or '-'}")
    row = {"at": util.now_iso(), "session": me,
           "prompt_sha": hashlib.sha256(d.prompt.encode("utf-8")).hexdigest()[:16], "chars": len(d.prompt),
           "decision": d.decision, "stage": d.stage, "reason": d.reason, "confidence": d.confidence,
           "backend": d.backend, "ms": ms, "model_ms": int(d.model_s * 1000), "division": d.division,
           "tier": d.tier, "unit": d.unit, "cost_usd": d.cost_usd}
    if settings(cfg).get("keep_prompts"):
        row["prompt"] = mask.redact(d.prompt)[0]
    try:
        path = samples_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.log("hooks", f"triage: cannot append sample: {exc}")


def read_samples() -> list:
    out = []
    try:
        with open(samples_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


# ---- hook entry ------------------------------------------------------------

def project_divisions(reg, proj) -> list:
    entry = reg.get("projects", {}).get(proj) or {}
    divs = entry.get("divisions") or {}
    return [d for d in registry.divisions_of(entry) if (divs.get(d) or {}).get("session")]


def handle(payload, started, r, who) -> Outcome:
    """The UserPromptSubmit triage of a Main pane (`r`: roster.Roster, `who`: its Identity for this
    session). Never raises; Outcome() means "go to Main as is"."""
    t0 = time.monotonic()
    try:
        cfg = config.load()
        me = who.member
        if not settings(cfg).get("enabled") or me is None or me.kind != "main":
            return Outcome()
        if cfg.get("delivery", "runner") != "runner":
            return Outcome()                    # triage routes through the runner only
        from . import inbox
        if inbox.NUDGE_RE.match(payload.get("prompt") or ""):
            return Outcome()                    # a wake line is ORCHE's own, not the user's: no sample
        d = decide(payload.get("prompt") or "", project_divisions(r.reg, me.project), cfg,
                   payload.get("transcript_path"))
        out = Outcome(model_s=d.model_s)
        if d.decision == "direct":
            from . import orca
            base = started if started is not None else t0
            out = act(d, r, me, orca.hook_deadline(base + d.model_s))
            out.model_s = d.model_s
            if out.block is None:
                d.decision, d.stage, d.reason = "main", 5, out.note
            else:
                # The blocked prompt skips inbox.handle_prompt, which touches Main's activity on every
                # other prompt: touch it here so `orche depts` and the status line do not show Main idle.
                units.touch_activity(me.session)
    except Exception as exc:  # noqa: BLE001 - fail-open
        log.log("hooks", f"triage: {type(exc).__name__}: {exc}")
        return Outcome()
    try:                                        # best-effort: never changes the decision made above
        record(me.session, d, int((time.monotonic() - t0) * 1000), cfg)
    except Exception as exc:  # noqa: BLE001
        log.log("hooks", f"triage: cannot record the decision: {type(exc).__name__}: {exc}")
    return out
