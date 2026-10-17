"""Jev gate client: typed decisions (Choice / Score / Noul) for routing and urgency, never code.

Wire shapes (SPEC §7.1) — VERIFY AGAINST docs.typesafe.ai BEFORE FIRST LIVE USE. They are encoded
only here, in the `WIRE` constants and `_question` / `_parse` below:

- model pinned `jev-1.13.0` (config `jev.model`).
- direct: `POST {base_url}/v1/systemone`, header `Authorization: Bearer <key>`,
  body `{"model": "<model>", "state": "<masked text>", "questions": [...]}`.
- Vercel AI Gateway (`base_url` contains `ai-gateway`): same path and body, model named
  `typesafe-ai/<model>`.
- questions: `{"type": "choice", "id", "question", "options": [...]}`,
  `{"type": "score", "id", "question", "min": 1, "max": 5}`, `{"type": "noul", "id", "question"}`.
- response: `{"answers": [{"id", "value", "confidence": 0.0-1.0}]}`. A Noul value is read as a
  probability: a number in [0, 1] as is, a boolean as `confidence` (true) / `1 - confidence` (false).
- limits: 64K chars per request, 32K per question; `state` is truncated with a `[truncated]` marker.

Adoption rules (SPEC §7.3): every string sent has code/diffs/blobs replaced (`strip_code`) and is
passed WHOLE through `mask.redact` before any cut; 1 s wall-clock timeout bounded by the caller's deadline;
fail-open — any error returns None and writes one latency line to `logs/jev.log`
(`ms=<int> q=<n> ok|timeout|error:<Type>`), never the payload. Nothing here raises.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import config, log, mask

WIRE = {
    "path": "/v1/systemone",
    "gateway_marker": "ai-gateway",
    "gateway_model_prefix": "typesafe-ai/",
    "max_request_chars": 64_000,
    "max_question_chars": 32_000,
    "truncated_marker": "[truncated]",
}
CODE_OMITTED = "[code omitted]"
BLOB_MIN_CHARS = 400
RESERVE_S = 0.2  # left of the caller's deadline for the rest of the hook
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_KEY_ENV = "JEV_API_KEY"
DEFAULT_TIMEOUT_S = 1.0
MIN_TIMEOUT_S = 0.05

ACT = 0.9
CONFIRM = 0.6

ROUTINE_KINDS = ("status", "resume-nudge", "confirm", "revision", "new request")
TIERS = ("mechanical", "standard", "hard")  # mirrors dispatch.TIERS
OTHER = "other"

# Test seam: an `(request, timeout) -> bytes | file-like` callable used by the module-level
# helpers (`classify_*`, `suggest_tier`, ...). None means `urllib.request.urlopen`.
OPENER = None


def band(confidence) -> str:
    """'act' (>= 0.9), 'confirm' (0.6-0.9) or 'defer' (< 0.6 or not a number)."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "defer"
    return "act" if c >= ACT else "confirm" if c >= CONFIRM else "defer"


# ``` or ~~~ fences (3+ chars); closed by a line starting with the same fence, else runs to the end.
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[^\n]*$|\Z)", re.DOTALL | re.MULTILINE)
_HUNK = re.compile(r"^@@ .* @@")
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]+")
_DIFF_META = ("diff --git ", "index ", "--- ", "+++ ", "new file mode", "deleted file mode",
              "old mode", "new mode", "similarity index", "rename from", "rename to", "Binary files")


def _in_diff(line: str) -> bool:
    return line.startswith(_DIFF_META) or bool(_HUNK.match(line)) or line[:1] in ("+", "-", " ", "\\")


def strip_code(text: str) -> str:
    """Replace fenced code, `diff --git` sections and unified-diff hunks with `[code omitted]`;
    drop lines longer than 400 chars that look like base64/hex blobs (SPEC §7.3.1: no code/diffs)."""
    text = _FENCE.sub(CODE_OMITTED, str(text))
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        starts = (line.startswith("diff --git ") or bool(_HUNK.match(line))
                  or (line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")))
        if starts:
            i += 1
            while i < len(lines):
                if lines[i] == "":
                    # Blank lines never end a hunk by themselves: it continues when the next
                    # non-empty line is diff-shaped; otherwise the blanks and that line are kept.
                    j = i
                    while j < len(lines) and lines[j] == "":
                        j += 1
                    if j == len(lines) or not _in_diff(lines[j]):
                        break
                    i = j
                    continue
                if not _in_diff(lines[i]):
                    break
                i += 1
            out.append(CODE_OMITTED)
            continue
        stripped = line.strip()
        if not (len(stripped) > BLOB_MIN_CHARS and _BLOB.fullmatch(stripped)):
            out.append(line)
        i += 1
    return "\n".join(out)


def _redact(text) -> str:
    """Code/diffs/blobs removed, then secrets masked — on the WHOLE text (cut only afterwards)."""
    return mask.redact(strip_code(text))[0]


def _cut(text: str, limit: int) -> str:
    marker = WIRE["truncated_marker"]
    if len(text) <= limit:
        return text
    return text[:max(0, limit - len(marker) - 1)] + "\n" + marker


def _question(q: dict) -> dict:
    """Masked, length-limited copy of a question dict."""
    out = {k: v for k, v in q.items() if k in ("type", "id", "min", "max")}
    out["question"] = _cut(_redact(q.get("question", "")), WIRE["max_question_chars"])
    if "options" in q:
        out["options"] = [_redact(o) for o in q["options"]]
    return out


def choice(qid, question, options):
    return {"type": "choice", "id": qid, "question": question, "options": list(options)}


def score(qid, question, lo=1, hi=5):
    return {"type": "score", "id": qid, "question": question, "min": lo, "max": hi}


def noul(qid, question):
    return {"type": "noul", "id": qid, "question": question}


def _is_timeout(exc) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), TimeoutError)


def _default_opener(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https endpoint from config


def _read(resp):
    """(status, bytes) from an opener result: raw bytes/str, or a file-like response."""
    if isinstance(resp, (bytes, bytearray)):
        return 200, bytes(resp)
    if isinstance(resp, str):
        return 200, resp.encode("utf-8")
    try:
        status = getattr(resp, "status", None)
        if status is None and hasattr(resp, "getcode"):
            status = resp.getcode()
        return int(status or 200), resp.read()
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()


def _parse(body: bytes, ids):
    """{id: {"value", "confidence", "band"}} for answers to asked questions; ValueError if malformed."""
    data = json.loads(body.decode("utf-8"))
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, list):
        raise ValueError("no answers list")
    out = {}
    for a in answers:
        if not isinstance(a, dict) or a.get("id") not in ids:
            continue
        c = a.get("confidence")
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0.0 <= c <= 1.0:
            continue
        out[a["id"]] = {"value": a.get("value"), "confidence": float(c), "band": band(c)}
    if not out:
        raise ValueError("no usable answers")
    return out


class JevClient:
    def __init__(self, base_url, api_key_env=DEFAULT_KEY_ENV, model=DEFAULT_MODEL,
                 timeout_s=DEFAULT_TIMEOUT_S, opener=None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key_env = api_key_env or DEFAULT_KEY_ENV
        self.model = model or DEFAULT_MODEL
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.opener = opener or _default_opener

    @property
    def gateway(self) -> bool:
        return WIRE["gateway_marker"] in self.base_url

    @property
    def url(self) -> str:
        return self.base_url + WIRE["path"]

    @property
    def wire_model(self) -> str:
        return (WIRE["gateway_model_prefix"] + self.model) if self.gateway else self.model

    def configured(self) -> bool:
        return bool(self.base_url) and bool(os.environ.get(self.api_key_env))

    def body(self, state, questions) -> dict:
        """The masked, size-limited request body (public for tests and dry runs)."""
        qs = [_question(q) for q in questions]
        head = {"model": self.wire_model, "state": "", "questions": qs}
        room = WIRE["max_request_chars"] - len(json.dumps(head, ensure_ascii=False))
        state = _redact(state or "")
        # json.dumps escapes some characters (and the marker's newline); shrink by the overflow.
        limit = room
        cut = _cut(state, max(0, limit))
        over = len(json.dumps(cut, ensure_ascii=False)) - 2 - room
        while over > 0 and limit > len(WIRE["truncated_marker"]) + 1:
            limit -= over
            cut = _cut(state, max(0, limit))
            over = len(json.dumps(cut, ensure_ascii=False)) - 2 - room
        if over > 0:
            cut = ""
        head["state"] = cut
        return head

    def ask(self, state, questions, deadline=None):
        """{question id: {"value", "confidence", "band"}} or None (fail-open, never raises).

        Wall-clock bounded: the opener runs in a daemon thread joined for at most
        min(timeout_s, deadline - now - RESERVE_S), computed AFTER masking; on expiry the thread
        is abandoned and the call counts as a timeout.
        """
        started = time.monotonic()
        n = len(questions or ())
        outcome = "ok"
        try:
            key = os.environ.get(self.api_key_env)
            if not self.base_url or not key:
                outcome = "error:NotConfigured"
                return None
            data = json.dumps(self.body(state, questions), ensure_ascii=False).encode("utf-8")
            timeout = self.timeout_s
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic() - RESERVE_S)
            if timeout <= MIN_TIMEOUT_S:
                outcome = "timeout"
                return None
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            box = {}

            def work():
                try:
                    box["resp"] = _read(self.opener(req, timeout))
                except BaseException as exc:  # noqa: BLE001 - handed to the caller thread
                    box["exc"] = exc

            worker = threading.Thread(target=work, name="jev-call", daemon=True)
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                outcome = "timeout"
                return None
            if "exc" in box:
                if not isinstance(box["exc"], Exception):  # KeyboardInterrupt/SystemExit in the worker
                    outcome = f"error:{type(box['exc']).__name__}"
                    return None
                raise box["exc"]
            status, raw = box["resp"]
            if status != 200:
                outcome = f"error:HTTP{status}"
                return None
            return _parse(raw, {q.get("id") for q in questions})
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            outcome = "timeout" if _is_timeout(exc) else f"error:{type(exc).__name__}"
            return None
        finally:
            ms = int((time.monotonic() - started) * 1000)
            log.log("jev", f"ms={ms} q={n} {outcome}")


# ---- configured client -----------------------------------------------------

def client(require_enabled=True):
    """JevClient from config, or None when disabled (if required), no base_url, or no key in env."""
    try:
        cfg = config.load().get("jev") or {}
        if require_enabled and not cfg.get("enabled"):
            return None
        c = JevClient(cfg.get("base_url", ""), cfg.get("api_key_env") or DEFAULT_KEY_ENV,
                      cfg.get("model") or DEFAULT_MODEL, cfg.get("timeout_s") or DEFAULT_TIMEOUT_S,
                      opener=OPENER)
        return c if c.configured() else None
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None


def _ask(state, questions, deadline=None):
    c = client()
    if c is None:
        return None
    return c.ask(state, questions, deadline=deadline)


def prob(answer):
    """Noul answer -> probability in [0, 1], or None."""
    if not isinstance(answer, dict):
        return None
    value = answer.get("value")
    if isinstance(value, bool):
        return answer["confidence"] if value else 1.0 - answer["confidence"]
    if isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
        return float(value)
    return None


def _min_conf(answers):
    return min(a["confidence"] for a in answers.values()) if answers else 0.0


# ---- placement questions (shared with `orche jev eval run`) ---------------

def drain_request(pointers):
    """(state, questions, pointers) for placement ① — the ONE builder shared by the drain hook
    (`classify_pointers`) and `orche jev eval run`. Question ids: `wake_now.<i>`, `user_now.<i>`."""
    ptrs = [_pointer(p) for p in pointers or ()]
    ptrs = [p for p in ptrs if p.get("unit")]
    lines, questions = [], []
    for i, p in enumerate(ptrs):
        lines.append(f"pointer {i}: unit {p.get('unit')} · from {p.get('from', '?')} · "
                     f"status {p.get('status', '?')} · summary {' '.join(strip_code(p.get('summary', '')).split())}")
        questions.append(noul(f"wake_now.{i}", f"Must the main session act on pointer {i} now?"))
        questions.append(noul(f"user_now.{i}", f"Must the user see pointer {i} now?"))
    return "\n".join(lines), questions, ptrs


def routine_questions():
    return [choice("routine_kind", "What kind of message is this?", ROUTINE_KINDS)]


def intake_questions(units=()):
    qs = [noul("needs_plan", "Does this request need a written plan before work starts?"),
          noul("irreversible", "Would carrying out this request be hard to reverse?"),
          score("urgency", "How urgent is this request?", 1, 5)]
    if units:
        qs.insert(0, choice("target", "Which unit should handle this request?", list(units) + [OTHER]))
    return qs


# ---- public helpers --------------------------------------------------------

def classify_prompt(text, units=(), *, deadline=None):
    """{"target","needs_plan","irreversible","urgency","routine_kind","confidence"} or None."""
    try:
        units = [str(u) for u in units or ()]
        a = _ask(text, intake_questions(units) + routine_questions(), deadline)
        if not a:
            return None
        urgency = (a.get("urgency") or {}).get("value")
        return {
            "target": (a.get("target") or {}).get("value"),
            "needs_plan": prob(a.get("needs_plan")),
            "irreversible": prob(a.get("irreversible")),
            "urgency": urgency if isinstance(urgency, (int, float)) and not isinstance(urgency, bool) else None,
            "routine_kind": (a.get("routine_kind") or {}).get("value"),
            "confidence": _min_conf(a),
        }
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None


def _pointer(p):
    if isinstance(p, (tuple, list)) and len(p) == 2:
        p = p[1]
    return p if isinstance(p, dict) else {}


def classify_pointers(pointers, *, deadline=None):
    """{unit: {"wake_now": p, "user_now": p}} for inbox pointers ((path, ptr) pairs or dicts), or None."""
    try:
        state, questions, ptrs = drain_request(pointers)
        if not ptrs:
            return None
        a = _ask(state, questions, deadline)
        if not a:
            return None
        out = {}
        for i, p in enumerate(ptrs):
            wake, user = prob(a.get(f"wake_now.{i}")), prob(a.get(f"user_now.{i}"))
            if wake is None and user is None:
                continue
            row = out.setdefault(str(p["unit"]), {"wake_now": 0.0, "user_now": 0.0})
            row["wake_now"] = max(row["wake_now"], wake or 0.0)
            row["user_now"] = max(row["user_now"], user or 0.0)
        return out or None
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None


def suggest_tier(brief_text, *, deadline=None):
    """{"tier": mechanical|standard|hard, "confidence"} or None."""
    try:
        a = _ask(brief_text, [score("complexity", "How complex is this task?", 1, 5),
                              noul("needs_design", "Does this task need design work or root-cause analysis?")],
                 deadline)
        if not a or "complexity" not in a:
            return None
        cx = a["complexity"]["value"]
        if isinstance(cx, bool) or not isinstance(cx, (int, float)):
            return None
        design = prob(a.get("needs_design")) or 0.0
        tier = "hard" if cx >= 4 or design >= 0.5 else "mechanical" if cx <= 2 else "standard"
        return {"tier": tier, "confidence": _min_conf(a)}
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None


def match_scope(request, features, *, deadline=None):
    """{"match": feature name|"other", "new_justified": p, "confidence"} or None."""
    try:
        names, lines = [], []
        for f in features or ():
            name = f.get("name") if isinstance(f, dict) else str(f)
            if not name:
                continue
            names.append(str(name))
            scope = ", ".join(f.get("scope") or []) if isinstance(f, dict) else ""
            lines.append(f"feature {name}: {scope}".rstrip(": "))
        state = f"request: {request}\n" + "\n".join(lines)
        a = _ask(state, [choice("match", "Which existing unit fits this request best?", names + [OTHER]),
                         noul("new_justified", "Is a new agent justified for this request?")], deadline)
        if not a or "match" not in a:
            return None
        return {"match": a["match"]["value"], "new_justified": prob(a.get("new_justified")),
                "confidence": _min_conf(a)}
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None


def card_needs_refresh(commit_messages, *, deadline=None):
    """Probability that these commits contain a user-visible change, or None."""
    try:
        state = "\n".join(str(m) for m in commit_messages or ())
        if not state.strip():
            return None
        a = _ask(state, [noul("user_visible", "Do these commits contain a user-visible change?")], deadline)
        return prob((a or {}).get("user_visible"))
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None
