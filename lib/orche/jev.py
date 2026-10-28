"""Jev gate client: typed decisions (Choice / Score / Noul) for routing and urgency, never code.

Wire shapes (SPEC §7.1), checked 2026-10-18 against docs.typesafe.ai/api (direct) and
vercel.com/docs/ai-gateway/sdks-and-apis/typesafe (gateway). They are encoded only here, in the
`WIRE` constants and `_wire_question` / `_parse` below:

- direct: `POST https://api.typesafe.ai/v1/systemone`, model pinned `jev-1.13.0` (config `jev.model`;
  versioned ids are accepted besides the `jev-latest` alias).
- Vercel AI Gateway (`base_url` contains `ai-gateway`, i.e. `https://ai-gateway.vercel.sh/typesafe`):
  same path, body and answers ("This API implements the TypeSafe request and response shapes"),
  model `typesafe-ai/<jev.model>` = `typesafe-ai/jev`.
- header `Authorization: Bearer <key>`; body `{"model", "state": "<masked text>", "questions": {id: q}}`
  with q = `{"type": "noul", "instructions"}`, `{"type": "choice", "instructions", "criteria":
  {option: description or null}}` (descriptions masked like the instructions) or `{"type": "score", "instructions", "criteria": [level labels, low to high]}`.
- response `{"model", "answers": {id: answer}, "usage"}`: noul `{"type", "noul": 0-1}` (no
  confidence: ORCHE uses max(p, 1 - p)); choice `{"type", "choice", "probabilities", "confidence"}`
  (ORCHE keeps `probabilities` for the asked options, as floats in [0, 1]);
  score `{"type", "score": 0-based level, "legend", "probabilities", "confidence"}` (ORCHE reports
  `min + score`, so a 1-5 question still answers 1-5).
- limits (documented in tokens: 64k per request, 32k per question) are enforced here in chars, which
  is stricter; `state` is truncated with a `[truncated]` marker.

Adoption rules (SPEC §7.3): every string sent has code/diffs/blobs replaced (`strip_code`) and is
passed WHOLE through `mask.redact` before any cut; 1 s wall-clock timeout bounded by the caller's deadline;
fail-open — any error returns None and writes one latency line to `logs/jev.log`
(`ms=<int> q=<n> ok|timeout|error:<Type>`), never the payload. Nothing here raises.
The key comes from `jevkey.resolve`: the env var first, then the keychain (macOS) or
ORCHE_HOME/secrets/jev.key. It is sent only to an https `base_url`, as an unredirected header,
and a 3xx is an error (never followed). A Choice answer naming an option that was not asked is dropped.
"""
import json
import math
import re
import threading
import time
import urllib.error
import urllib.request

from . import config, jevkey, log, mask

GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/typesafe"
DIRECT_BASE_URL = "https://api.typesafe.ai"
KINDS = {"gateway": (GATEWAY_BASE_URL, "jev"), "direct": (DIRECT_BASE_URL, "jev-1.13.0")}
PROBE_TIMEOUT_S = 10.0  # a first call pays the TLS handshake; the 1 s hook budget does not apply
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
# Score rubrics (TypeSafe Score `criteria`, low to high); answers come back as 1-5.
URGENCY_LEVELS = ("1: can wait for days", "2: this week", "3: today", "4: within the hour", "5: right now")
OTHER = "other"

# Test seam: an `(request, timeout) -> bytes | file-like` callable used by the module-level
# helpers (`classify_pointers`, ...). None means `urllib.request.urlopen`.
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


def _wire_question(q: dict) -> dict:
    """The masked, length-limited wire form of a question built by `choice` / `score` / `noul`."""
    out = {"type": q["type"], "instructions": _cut(_redact(q.get("question", "")), WIRE["max_question_chars"])}
    if q["type"] == "choice":
        descriptions = q["descriptions"]
        out["criteria"] = {_redact(o): (_cut(_redact(descriptions[o]), WIRE["max_question_chars"])
                                        if descriptions.get(o) else None) for o in q["options"]}
    elif q["type"] == "score":
        out["criteria"] = [_redact(label) for label in q["labels"]]
    return out


def choice(qid, question, options):
    """`options`: a list of names, or a dict {name: description} (descriptions go to `criteria`)."""
    descriptions = dict(options) if isinstance(options, dict) else {}
    return {"type": "choice", "id": qid, "question": question, "options": list(options),
            "descriptions": descriptions}


def score(qid, question, lo=1, hi=5, labels=None):
    """A score over levels lo..hi; `labels` (one per level, low to high) default to the numbers."""
    labels = list(labels) if labels else [str(i) for i in range(lo, hi + 1)]
    if len(labels) != hi - lo + 1:
        raise ValueError("one label per level")
    return {"type": "score", "id": qid, "question": question, "min": lo, "max": hi, "labels": labels}


def noul(qid, question):
    return {"type": "noul", "id": qid, "question": question}


def _is_timeout(exc) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), TimeoutError)


def secure_url(base_url) -> bool:
    """Only https endpoints get the key (config `jev.base_url`)."""
    return str(base_url or "").lower().startswith("https://")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, never followed: urllib would resend the request headers (the key) to the
    redirect target. The HTTPError carries only the status; `ask` masks what it reports."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect refused ({code})", headers, fp)


def build_opener(*handlers):
    """The urllib opener used for Jev calls: redirects refused (`handlers` are for tests)."""
    return urllib.request.build_opener(*handlers, _NoRedirect)


_URLLIB = build_opener()


def _default_opener(request, timeout):
    return _URLLIB.open(request, timeout=timeout)  # https only (`secure_url`), redirects refused


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


def _number(x, lo=None, hi=None):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        return None
    return float(x)


def _probabilities(q, a):
    """A Choice answer's `probabilities` for the asked options, as floats in [0, 1] (others dropped)."""
    probs = a.get("probabilities")
    if not isinstance(probs, dict):
        return {}
    out = {}
    for o in q["options"]:
        p = _number(probs.get(o), 0.0, 1.0)
        if p is not None:
            out[o] = p
    return out


def _answer(q, a):
    """(value, confidence, probabilities dict or None) of one wire answer to question `q`, or None."""
    if not isinstance(a, dict) or a.get("type") != q["type"]:
        return None
    if q["type"] == "noul":
        p = _number(a.get("noul"), 0.0, 1.0)
        return None if p is None else (p, max(p, 1.0 - p), None)
    c = _number(a.get("confidence"), 0.0, 1.0)
    if c is None:
        return None
    if q["type"] == "choice":
        if a.get("choice") not in q["options"]:
            return None  # asked options only
        return a["choice"], c, _probabilities(q, a)
    s = _number(a.get("score"), 0.0, float(q["max"] - q["min"]))
    return None if s is None else (q["min"] + s, c, None)


def _parse(body: bytes, questions):
    """{id: {"value", "confidence", "band"}} for answers to asked questions (a Choice entry also has
    "probabilities" when the answer carried usable ones); ValueError if malformed."""
    data = json.loads(body.decode("utf-8"))
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("no answers map")
    out = {}
    for q in questions:
        got = _answer(q, answers.get(q.get("id")))
        if got is not None:
            out[q["id"]] = {"value": got[0], "confidence": got[1], "band": band(got[1])}
            if got[2]:
                out[q["id"]]["probabilities"] = got[2]
    if not out:
        raise ValueError("no usable answers")
    return out


def _error_message(raw) -> str:
    """The `message` of a JSON error body (TypeSafe and the gateway use that field), else ""."""
    try:
        data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError):
        return ""
    msg = data.get("message") if isinstance(data, dict) else None
    if not msg and isinstance(data, dict) and isinstance(data.get("error"), dict):
        msg = data["error"].get("message")
    return " ".join(str(msg).split())[:200] if msg else ""


class JevClient:
    def __init__(self, base_url, api_key_env=DEFAULT_KEY_ENV, model=DEFAULT_MODEL,
                 timeout_s=DEFAULT_TIMEOUT_S, opener=None, api_key=None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key_env = api_key_env or DEFAULT_KEY_ENV
        self.model = model or DEFAULT_MODEL
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.opener = opener or _default_opener
        self._key = api_key
        self.key_source = "given" if api_key else None
        self.last_error = ""  # masked detail of the last failed call (HTTP error message), for probes
        self.outcome = ""     # the last call's log outcome: ok | timeout | error:<Type>|HTTP<status>

    @property
    def gateway(self) -> bool:
        return WIRE["gateway_marker"] in self.base_url

    @property
    def kind(self) -> str:
        return "gateway" if self.gateway else "direct"

    def key(self):
        """The API key (env, then the key store), resolved once per client."""
        if self._key is None:
            self._key, self.key_source = jevkey.resolve(self.api_key_env)
        return self._key

    @property
    def url(self) -> str:
        return self.base_url + WIRE["path"]

    @property
    def wire_model(self) -> str:
        return (WIRE["gateway_model_prefix"] + self.model) if self.gateway else self.model

    def configured(self) -> bool:
        return secure_url(self.base_url) and bool(self.key())

    def body(self, state, questions) -> dict:
        """The masked, size-limited request body (public for tests and dry runs)."""
        qs = {str(q["id"]): _wire_question(q) for q in questions}
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
        self.last_error = ""
        key = None
        try:
            if self.base_url and not secure_url(self.base_url):
                outcome = "error:NotHttps"  # checked before the key is even resolved
                return None
            key = self.key() if self.base_url else None
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
            req = urllib.request.Request(self.url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            req.add_unredirected_header("Authorization", f"Bearer {key}")
            box = {}

            def work():
                try:
                    box["resp"] = _read(self.opener(req, timeout))
                except urllib.error.HTTPError as exc:  # urlopen raises on 4xx/5xx; keep status and body
                    box["resp"] = _read(exc)
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
                self.last_error = jevkey.scrub(_error_message(raw), key)
                return None
            return _parse(raw, questions)
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            outcome = "timeout" if _is_timeout(exc) else f"error:{type(exc).__name__}"
            return None
        finally:
            self.outcome = outcome
            ms = int((time.monotonic() - started) * 1000)
            log.log("jev", f"ms={ms} q={n} {outcome}")


# ---- configured client -----------------------------------------------------

def client_from(cfg, opener=None):
    """JevClient for a `jev` config section (no enabled/key checks)."""
    return JevClient(cfg.get("base_url", ""), cfg.get("api_key_env") or DEFAULT_KEY_ENV,
                     cfg.get("model") or DEFAULT_MODEL, cfg.get("timeout_s") or DEFAULT_TIMEOUT_S,
                     opener=opener or OPENER)


def client(require_enabled=True):
    """JevClient from config, or None when disabled (if required), no base_url, or no key
    (env var, keychain or key file)."""
    try:
        cfg = config.load().get("jev") or {}
        if require_enabled and not cfg.get("enabled"):
            return None
        c = client_from(cfg)
        return c if c.configured() else None
    except Exception as exc:  # noqa: BLE001
        log.log("jev", f"ms=0 q=0 error:{type(exc).__name__}")
        return None


PROBE_STATE = "ORCHE setup check: a short connection test with no user data."


def probe(c):
    """One tiny Choice question through `c` with PROBE_TIMEOUT_S: {"ok", "ms", "kind", "error"}.

    `error` is masked (key replaced, then `mask.redact`) and names the HTTP status or exception type.
    """
    c.timeout_s = max(c.timeout_s, PROBE_TIMEOUT_S)
    started = time.monotonic()
    answers = c.ask(PROBE_STATE, [choice("probe", "Is this a connection test?", ["yes", "no"])])
    ms = int((time.monotonic() - started) * 1000)
    if answers and "probe" in answers:
        return {"ok": True, "ms": ms, "kind": c.kind, "error": ""}
    outcome = getattr(c, "outcome", "error")
    if outcome in ("ok", "error:ValueError", "error:JSONDecodeError"):  # 200 but no usable answer
        outcome = "error:unexpected response shape"
    elif outcome == "error:NotConfigured":
        outcome = "error:no base_url or no API key"
    elif outcome == "error:NotHttps":
        outcome = "error:jev.base_url must be https"
    detail = outcome.removeprefix("error:") + (f": {c.last_error}" if c.last_error else "")
    return {"ok": False, "ms": ms, "kind": c.kind, "error": jevkey.scrub(detail, c._key)}


def ask(state, questions, deadline=None):
    c = client()
    if c is None:
        return None
    return c.ask(state, questions, deadline=deadline)


def prob(answer):
    """Noul answer -> probability in [0, 1], or None."""
    if not isinstance(answer, dict):
        return None
    return _number(answer.get("value"), 0.0, 1.0)


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
          score("urgency", "How urgent is this request?", 1, 5, URGENCY_LEVELS)]
    if units:
        qs.insert(0, choice("target", "Which unit should handle this request?", list(units) + [OTHER]))
    return qs


# ---- public helpers --------------------------------------------------------

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
        a = ask(state, questions, deadline)
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
