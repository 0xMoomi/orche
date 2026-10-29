"""The owner's voice: Orca's own notification covers Main's closing line; ORCHE notifies
only what Orca cannot know (the ask's reminders, a parked guard refusal, notices Main never wrote).

Main's Stop hook (dispatch.handle_stop, also under stop_hook_active) reads the closing line — the last
non-empty line outside fenced blocks of the final message (parse; a quote, a bullet, backticks or bold
around the tag, a space before the colon and a full-width colon are tolerated, the tag stays upper
case) — and acts on its tag (closing): every tag stamps `flow/<p>/tagged` with the turn's start (the
prompt hook's turn record; the fallback reads it);
`OWNER: <the decision or step>` records the project's ask (`flow/<p>/ask.json`, replacing an open one);
`DONE: <the result>` closes an open ask and records the `done` notice; neither sends a notification:
Orca shows its own when Main's turn ends, so the first one counts as shown by Orca (_orca: events
`owner recorded … by=orca`) and the ask's reminders still run from the ask. `RUNNING: …` records nothing. The open ask's own text again, or the last DONE again with no ask since, is a repeat: nothing
is recorded or sent (the schedule runs on from the first). An untagged line logs `owner untagged`. A
unit's `OWNER:` line (the runner: the last one outside fences, job.owner_of; a pane unit: `orche report
--owner`) is the pointer field `owner` (step_of, step). A guard refusal the runner parks as a quiet
pointer records an ask itself (parked).

ask.json {text, at, kind: owner|secret|guard|fallback, source: main|runner|heartbeat[, unit]
[, closed_at | answered_at]}: text masked, one line, at most ASK_MAX characters; every write under
`flow/<p>/.notify.lock`, in sequence with a notification, never nested. Answered (answer): the owner
prompted Main after the ask (owner-seen; the prompt hook calls answer at once, the heartbeat again),
or every secret a `secret …` ask names is in — none pending, one filled since FILL_SLACK_S before the
ask (a fill during Main's own turn counts; secrets.dialog calls secret_filled).

Notifications (notify): at most one per project per NOTIFY_GAP_S; no sound inside flow.quiet_hours; a
failed show is a try (_note). notified.json {at, kind, n, ask_at, ask_last, shown, tried} under the
lock: the last one ORCHE showed (at, kind: the gap), the tries for the ask made at ask_at (n; the last
at ask_last), per notice kind the id of the last notice shown (by ORCHE or Orca), and a notice's failed
tries (its next waits for the next RENOTIFY_S slot). A notice (a DONE, a `stuck` escalation, a fallback
notice) is recorded in `flow/<p>/<kind>.json` {text, at, id} (one writer each) before it is tried: one
held back is the heartbeat's to send once.

The heartbeat (heartbeat, from `orche reconcile`; a dry run writes and sends nothing) per project:
marks an answered ask; tells Main about each unit a secret filled in the last day still resumes
(secret_filled: a dialog that died, a report filed after the fill, the last of several secrets); then,
unless the project is paused or notifications are off, at most one of: the 10-minute fallback
(fallback, R26), a held notice (dropped once the owner prompted Main after it, or after a day), the
ask's next notification (due: the first at once unless Orca showed it, then RENOTIFY_S after the ask:
30, 60, 120, 240 min; none inside quiet hours and one at the first pass after them, a slot passed unsent folded into the
next one shown; then none, the `owner:` line stays). Events carry ids, kinds and counts, never text.
"""
import contextlib
import datetime as _dt
import fcntl
import json
import os
import re
import time

from .. import config, log, notify as notice, paths, units, util
from . import guard

ASK, SENT, NOTIFY_LOCK, TAGGED, RESUMED = "ask.json", "notified.json", ".notify.lock", "tagged", "secret-filled"
NOTICES = ("done", "stuck", "fallback", "usage")     # one-off notices: flow/<p>/<kind>.json
USAGE_NOTICE = ("{label} usage limit reached (resets {hhmm}). ORCHE re-runs the {n} held units then. "   # Appendix A.15
                "To keep going now: run on an API key, or move some divisions to Codex.")
VENDOR_LABEL = {"claude": "Claude", "codex": "Codex"}
ASK_MAX, STEP_MAX, LINE_MAX = 160, 200, 100
NOTIFY_GAP_S = 300          # at most one notification per project per this many seconds
RENOTIFY_S = (1800, 3600, 7200, 14400)  # re-notifications this long after the ask; then none
FALLBACK_S = 600            # a report waiting on the owner this long with Main silent: the fallback
FILL_SLACK_S = 600          # a secret filled this long before its ask (during Main's turn) answers it
RESUME_SLACK_S = 60         # a report this long before its secret's request still asks for the secret
RECENT = _dt.timedelta(hours=24)        # the reports, filled requests and notices the heartbeat reads
ASKING, ENDED = ("blocked", "question"), ("failed",)   # fallback: an ask; a notice (so is a close)
LOCK_WAIT_S, LOCK_POLL_S = 2.0, 0.05
_TAG = re.compile(r"^(?:\*\*|__)?(RUNNING|OWNER|DONE)(?:\*\*|__)?\s*[:：](?:\*\*|__)?\s*(.*)$")
_SECRET = re.compile(r"^secret\s+[A-Z][A-Z0-9_]*")
_NAMES = re.compile(r"\bsecret\s+([A-Z][A-Z0-9_]*(?:\s*,\s*[A-Z][A-Z0-9_]*)*)")


# ---- parsing -------------------------------------------------------------------------------------

def _outside(text) -> list:
    """The lines of `text` outside fenced blocks (util.fence_spans with the dispatch grammar's nesting:
    a snippet fence indented inside ```dispatch stays inside; an unclosed fence runs to the end)."""
    lines = str(text or "").splitlines()
    inside = set()
    for start, end, _info in util.fence_spans(lines):
        inside.update(range(start, len(lines) if end is None else end + 1))
    return [line for i, line in enumerate(lines) if i not in inside]


def _tagged(line):
    """(tag, body) of a tagged line, or None when untagged or empty. Tolerated around the upper-case
    tag: a quote (`>`), a bullet (`-`, `*`, `+`), wrapping backticks, bold (`**`, `__`), a space before
    the colon, a full-width colon."""
    s = re.sub(r"^[-*+]\s+", "", re.sub(r"^(?:>\s*)+", "", line.strip()))
    if len(s) > 1 and s[0] == s[-1] == "`":
        s = s.strip("`").strip()
    m = _TAG.match(s)
    if m is None:
        return None
    body = m.group(2).strip()
    for mark in ("**", "__"):          # **OWNER: …** closes its bold at the end
        if body.endswith(mark) and body.count(mark) % 2:
            body = body[:-2].rstrip()
    return (m.group(1), body) if body else None


def parse(text):
    """(tag, body) of the closing line — the last non-empty line outside fences — or None."""
    for line in reversed(_outside(text)):
        if line.strip():
            return _tagged(line)
    return None


def one_line(text, limit) -> str:
    """Masked, one line, at most `limit` characters (… when cut)."""
    flat = notice.clean(text)
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def step(text):
    """The pointer field `owner`: masked, one line, at most STEP_MAX characters; None when empty."""
    return one_line(text, STEP_MAX) or None


def step_of(text):
    """A unit's owner step: its last `OWNER:` line outside fences (step), or None."""
    for line in reversed(_outside(text)):
        hit = _tagged(line)
        if hit is not None and hit[0] == "OWNER":
            return step(hit[1])
    return None


def kind_of(text) -> str:
    """`secret` for `secret <NAME>[, <NAME>]`, else `owner`."""
    return "secret" if _SECRET.match(text) else "owner"


def secret_names(text) -> list:
    """The names of the first `secret <NAME>[, <NAME>]` in `text`."""
    m = _NAMES.search(str(text or ""))
    return re.split(r"\s*,\s*", m.group(1)) if m else []


# ---- the ask -------------------------------------------------------------------------------------

def _path(project, name):
    return guard.folder(project) / name


def _iso(now) -> str:
    return now.astimezone().isoformat(timespec="seconds")


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _stamp(path, now) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (now.timestamp(), now.timestamp()))


def read_ask(project):
    """ask.json as a dict, or None (missing or unreadable)."""
    return util.read_dict(_path(project, ASK))


def open_ask(project):
    """The project's ask while it is neither closed nor answered, else None."""
    a = read_ask(project)
    return a if a is not None and not a.get("closed_at") and not a.get("answered_at") else None


def record(project, text, *, kind, source, unit=None, now=None) -> dict:
    """Write the project's ask (replacing any) under the notify lock — also when the lock stays busy:
    an ask is never lost — and its events line; returns it."""
    now = now or units.now()
    data = {"text": one_line(text, ASK_MAX), "at": _iso(now), "kind": kind, "source": source}
    if unit:
        data["unit"] = unit
    with _locked(_path(project, NOTIFY_LOCK)):
        util.write_json(_path(project, ASK), data)
    log.log("events", f"owner ask project={project} kind={kind} source={source}" + (f" unit={unit}" if unit else ""))
    _status_line(project)
    return data


def _status_line(project):
    from .. import statusline       # the status line's `waiting` counts the open ask
    statusline.touch(project)


def close(project, now=None) -> bool:
    """DONE: the open ask gets `closed_at` (under the notify lock). False when none was open."""
    with _locked(_path(project, NOTIFY_LOCK)):
        a = open_ask(project)
        if a is None:
            return False
        a["closed_at"] = _iso(now or units.now())
        util.write_json(_path(project, ASK), a)
    log.log("events", f"owner closed project={project}")
    _status_line(project)
    return True


def _filled_at(project, name):
    """When `name` was last filled in `project`; None while a request for it is pending or none was filled."""
    if not (paths.orche_home() / "secrets" / "requests").is_dir():    # store.requests_dir() creates it
        return None
    from ..secrets import store
    reqs = [r for _rid, r in store.list_requests(project=project, name=name)]
    if any(r.get("status") == "pending" for r in reqs):
        return None
    stamps = (units.parse_iso(r.get("filled_at")) for r in reqs if r.get("status") == "filled")
    return max((t for t in stamps if t is not None), default=None)


def _filled(project, name, since) -> bool:
    """`name` has no request pending in `project` and one filled at or after `since`."""
    t = _filled_at(project, name)
    return t is not None and t >= since


def answered_by(project, a):
    """How the ask `a` is answered: `prompt` (owner-seen newer than it), `secret` (every secret it
    names is in: _filled since FILL_SLACK_S before it), else None."""
    at = units.parse_iso(a.get("at"))
    if at is None:
        return None
    seen = _mtime(_path(project, "owner-seen"))
    if seen is not None and seen > at.timestamp() + 1:          # `at` has whole seconds
        return "prompt"
    names = secret_names(a.get("text")) if a.get("kind") == "secret" else []
    since = at - _dt.timedelta(seconds=FILL_SLACK_S)
    if names and all(_filled(project, name, since) for name in names):
        return "secret"
    return None


def answer(project, now=None):
    """Mark the open ask answered (answered_by) under the notify lock; how, or None (also when the lock
    stays busy: the next caller sees it again)."""
    if open_ask(project) is None:
        return None
    now = now or units.now()
    with _locked(_path(project, NOTIFY_LOCK)) as held:
        a = open_ask(project) if held else None
        how = answered_by(project, a) if a is not None else None
        if how is None:
            return None
        a["answered_at"] = _iso(now)
        util.write_json(_path(project, ASK), a)
    after = int((now - units.parse_iso(a["at"])).total_seconds() // 60)
    log.log("events", f"owner answered project={project} by={how} after={after}m")
    _status_line(project)
    return how


# ---- notifications -------------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(path):
    """Hold the flock on `path` for at most LOCK_WAIT_S of waiting; yields whether it is held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        end = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    yield False
                    return
                time.sleep(LOCK_POLL_S)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _held(project, kind, reason) -> bool:
    log.log("events", f"owner held project={project} kind={kind} reason={reason}")
    return False


def in_quiet_hours(cfg_all, now) -> bool:
    """flow.quiet_hours "HH:MM-HH:MM" (may cross midnight) holds `now`'s own wall time (the pass's clock,
    not the host's zone); False when unset or malformed."""
    raw = str(((cfg_all or {}).get("flow") or {}).get("quiet_hours") or "")
    try:
        a, b = (int(h) * 60 + int(m) for h, m in (x.strip().split(":") for x in raw.split("-")))
    except ValueError:
        return False
    t = now.hour * 60 + now.minute
    return a <= t < b if a <= b else (t >= a or t < b)


def _quiet(now) -> bool:
    """Quiet hours on the caller's clock: `now` as given (units.now() is local), never re-read or
    re-projected onto the host's zone."""
    return in_quiet_hours(config.load(), now)


def _sent(project) -> dict:
    return util.read_dict(_path(project, SENT)) or {}


def _shown(sent) -> dict:
    return sent["shown"] if isinstance(sent.get("shown"), dict) else {}


def _gap(sent, now) -> bool:
    """The project's last notification is younger than NOTIFY_GAP_S."""
    at = units.parse_iso(sent.get("at"))
    return at is not None and (now - at).total_seconds() < NOTIFY_GAP_S


def _tries(sent, kind, notice_id):
    """(n, last) of the failed tries of the notice `notice_id` of `kind` (notified.json `tried`)."""
    tried = sent.get("tried") if isinstance(sent.get("tried"), dict) else {}
    t = tried.get(kind)
    if not isinstance(t, dict) or t.get("id") != notice_id:
        return 0, None
    return int(t.get("n") or 0), units.parse_iso(t.get("last"))


def _note(project, sent, kind, now, ask_at, notice_id, outcome) -> int:
    """Under the lock: notified.json after one try, `shown`, `orca` (Orca showed it: _orca) or `failed`.
    Only one ORCHE showed moves the gap (`at`, `kind`); any try moves an ask's counters (n, ask_at, ask_last),
    so its schedule runs on; a notice shown (by either) is marked in `shown`, a failed one counted in `tried`
    {kind: {id, n, last}} (its next try waits for the next slot). Returns n (the ask's tries; a notice: 1)."""
    new, n = dict(sent), 1
    if outcome == "shown":
        new.update(at=_iso(now), kind=kind)
    if ask_at is not None:
        n = int(sent.get("n") or 0) + 1 if sent.get("ask_at") == ask_at else 1
        new.update(n=n, ask_at=ask_at, ask_last=_iso(now))
    if notice_id is not None and outcome == "failed":
        tried = sent.get("tried") if isinstance(sent.get("tried"), dict) else {}
        new["tried"] = {**tried, kind: {"id": notice_id, "n": _tries(sent, kind, notice_id)[0] + 1, "last": _iso(now)}}
    elif notice_id is not None:
        new["shown"] = {**_shown(sent), kind: notice_id}
    util.write_json(_path(project, SENT), new)
    return n


def _orca(project, kind, now, ask_at=None, notice_id=None) -> None:
    """Main's closing line: Orca shows its own notification when the turn ends, so ORCHE sends none and
    counts this first one as shown (_note `orca`: the ask's reminders run from the ask; the notice is
    never flushed). Under the notify lock, also when it stays busy (as record)."""
    with _locked(_path(project, NOTIFY_LOCK)):
        _note(project, _sent(project), kind, now, ask_at, notice_id, "orca")
    log.log("events", f"owner recorded project={project} kind={kind} by=orca")


def _show(project, sent, body, kind, now, ask_at=None, notice_id=None) -> bool:
    """Under the lock: show one notification, then write notified.json (_note: a failed show is a try too).
    False when osascript failed (or timed out)."""
    quiet = _quiet(now)
    ok = notice.send(f"ORCHE {project}", body, sound=not quiet, kind=kind, project=project)
    n = _note(project, sent, kind, now, ask_at, notice_id, "shown" if ok else "failed")
    if ok:
        log.log("events", f"owner notify project={project} kind={kind} n={n} quiet={int(quiet)}")
    return ok


def notify(project, body, kind, now=None, *, ask_at=None, notice_id=None) -> bool:
    """One owner notification for `project` (title `ORCHE <project>`). Held (`owner held`, the next pass
    tries again): notifications off, the lock busy, the project's last one younger than NOTIFY_GAP_S.
    osascript failing is a try (`owner held … reason=failed`; the next waits for its slot). notified.json:
    `ask_at` (an ask's) moves that ask's counters, `notice_id` (a notice's) marks it. True when shown."""
    now = now or units.now()
    if not notice.enabled():
        return _held(project, kind, "off")
    with _locked(_path(project, NOTIFY_LOCK)) as held:
        if not held:
            return _held(project, kind, "lock")
        sent = _sent(project)
        if _gap(sent, now):
            return _held(project, kind, "gap")
        if _show(project, sent, body, kind, now, ask_at, notice_id):
            return True
    return _held(project, kind, "failed")


def _notice(project, kind, body, now) -> dict:
    """Record a one-off notice of `kind` (NOTICES) in `flow/<p>/<kind>.json` {text, at, id}; returns it."""
    rec = {"text": notice.clean(body), "at": _iso(now), "id": os.urandom(6).hex()}
    util.write_json(_path(project, f"{kind}.json"), rec)
    return rec


def announce(project, kind, body, now=None) -> bool:
    """A one-off notice of `kind` (NOTICES): recorded (_notice) before it is tried, so one held back is
    the heartbeat's to send once (_flush). True when shown."""
    now = now or units.now()
    rec = _notice(project, kind, body, now)
    return notify(project, rec["text"], kind, now, notice_id=rec["id"])


def usage_notice(project, vendor, hhmm, n, now=None) -> bool:
    """The one owner notice of a usage hold, as `project`'s `usage` notice (announce:
    a held-back one is flushed by the heartbeat)."""
    text = USAGE_NOTICE.format(label=VENDOR_LABEL.get(vendor, vendor), hhmm=hhmm, n=n)
    return announce(project, "usage", text, now)


# ---- entry points --------------------------------------------------------------------------------

def _repeat_done(project, text) -> bool:
    """The same DONE as the last one, with no ask recorded after it."""
    last = util.read_dict(_path(project, "done.json")) or {}
    at = units.parse_iso(last.get("at"))
    if at is None or last.get("text") != notice.clean(text):
        return False
    asked = units.parse_iso((read_ask(project) or {}).get("at"))
    return asked is None or asked <= at


def closing(project, text, now=None, turn_at=None):
    """Act on Main's closing line for `project` (the Stop hook); returns the tag acted on, or None. A tag
    stamps `tagged` with the turn's start (`turn_at`, from the prompt hook's turn record; none known: no
    stamp): a report that landed during the turn is the next turn's, and only a tag covers it there."""
    now = now or units.now()
    hit = parse(text)
    if hit is None:
        log.log("events", f"owner untagged project={project}")
        return None
    tag, body = hit
    if turn_at is not None:
        _stamp(_path(project, TAGGED), min(turn_at, now))
    if tag == "OWNER":
        a = open_ask(project)
        if a is not None and a.get("text") == one_line(body, ASK_MAX):
            log.log("events", f"owner repeat project={project} tag=OWNER")
            return tag
        a = record(project, body, kind=kind_of(body), source="main", now=now)
        answer(project, now)                      # a secret filled during this very turn answers it
        _orca(project, a["kind"], now, ask_at=a["at"])
    elif tag == "DONE":
        done = f"DONE: {one_line(body, ASK_MAX)}"
        if _repeat_done(project, done):
            log.log("events", f"owner repeat project={project} tag=DONE")
            return tag
        close(project, now)
        _orca(project, "done", now, notice_id=_notice(project, "done", done, now)["id"])
    return tag


def parked(project, unit, text, now=None) -> None:
    """A guard refusal parked as a quiet pointer (the runner's refused start): it records the ask and
    notifies — no presence check, the pointer is not on Main's screen. Never raises."""
    try:
        now = now or units.now()
        a = record(project, text, kind="guard", source="runner", unit=unit, now=now)
        notify(project, f"OWNER: {a['text']}", "guard", now, ask_at=a["at"])
    except Exception as exc:  # noqa: BLE001 - the pointer is filed; the ask is best-effort
        log.log("hooks", f"owner: parked ask for {project}: {type(exc).__name__}")


def _pointers(project, since=None):
    """(time, pointer, path) of the reports in main-<project>'s inbox, pending or drained, from `since` on."""
    from .. import inbox
    try:
        folder = inbox.box(f"main-{project}")
    except ValueError:
        return
    for path in [*folder.glob("*.json"), *(folder / inbox.DRAINED).glob("*.json")]:
        if path.name.startswith(".") or (since is not None and (_mtime(path) or 0) < since.timestamp()):
            continue        # written before the window (a pointer's `at` precedes its write): never opened
        try:
            ptr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = inbox.pointer_time(path, ptr) if isinstance(ptr, dict) and ptr.get("unit") else None
        if t is not None and (since is None or t >= since):
            yield t, ptr, path


def _resumable(project, name, since) -> list:
    """(unit, rev, names, marker, filled) per unit Main must hear about now that `name` is in: the unit's
    reports to Main (pending or drained) from `since` on whose owner step names `name`, none of them told
    yet (`marker`: flow/<p>/secret-filled/<report>), and the newest one's step naming only secrets that
    are in (`names`; the last one filled at `filled`)."""
    folder, asking = _path(project, RESUMED), {}
    for t, ptr, path in _pointers(project, since):
        if name in secret_names(ptr.get("owner")):
            asking.setdefault(str(ptr["unit"]), []).append((t, ptr, path))
    out = []
    for unit, reports in sorted(asking.items()):
        if any((folder / path.stem).exists() for _t, _ptr, path in reports):
            continue
        _t, ptr, path = max(reports, key=lambda r: r[0])
        names = secret_names(ptr.get("owner"))
        stamps = [_filled_at(project, n) for n in names]
        if all(stamps):
            out.append((unit, int(ptr.get("brief_rev") or 1), names, folder / path.stem, max(stamps)))
    return out


def _tell(project, unit, rev, names, marker, filled) -> bool:
    """One urgent `info` from `orche` to Main: resume `unit`. The marker is created exclusively first (False:
    another caller told it) and removed again when the post fails (the heartbeat tries again)."""
    from .. import inbox
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    try:
        inbox.post(inbox.make(unit, "orche", f"main-{project}", "info", brief_rev=rev, urgent=True,
                              summary=f"owner step done: secret {', '.join(names)} filled at {units.hhmm(filled)} — "
                                      f"resume {unit} (AMEND or orche run {unit})"))
    except Exception as exc:  # noqa: BLE001 - the heartbeat tries again
        marker.unlink(missing_ok=True)
        log.log("hooks", f"owner: resume {unit} for {project}: {type(exc).__name__}")
        return False
    return True


def secret_filled(project, name, rid, now=None, filled_at=None) -> list:
    """A secret was filled (secrets.dialog after store.mark_filled; the heartbeat repeats it for a day: a
    dialog that died, a report filed after the fill, the last of several secrets): the open ask is answered
    when every secret it names is in; then, not while the project is paused, the lookup (_resumable, from
    the request's `at` less RESUME_SLACK_S) finds the units to resume — each gets one urgent `info` from
    `orche` to Main, once per asking report (its marker is written only now, after the lookup) — and such a
    fill is the owner's word: owner-seen moves to the fill (never back), so G1 lets the resume AMEND
    through. Returns the units told ([] when none yet). Never raises."""
    try:
        now = now or units.now()
        if not project:
            return []
        answer(project, now)
        if guard.paused(project) is not None:
            return []
        from ..secrets import store
        asked = units.parse_iso((store.get_request(rid) or {}).get("at"))
        if asked is None:
            return []
        ready = _resumable(project, name, asked - _dt.timedelta(seconds=RESUME_SLACK_S))
        if ready:
            seen, word = _path(project, "owner-seen"), units.parse_iso(filled_at) or now
            if (_mtime(seen) or 0) < word.timestamp():
                _stamp(seen, word)
        told = [r[0] for r in ready if _tell(project, *r)]
        for unit in told:
            log.log("events", f"owner secret-filled project={project} name={name} unit={unit}")
        return told
    except Exception as exc:  # noqa: BLE001 - the secret is stored; telling Main is best-effort
        log.log("hooks", f"owner: secret filled for {project}: {type(exc).__name__}")
        return []


# ---- the heartbeat -------------------------------------------------------------------------------

def due(a, sent):
    """(n, at): the notifications shown for the ask `a` (notified.json) and when its next one falls due
    — the first at the ask, then the first RENOTIFY_S slot after the last one shown (slots passed unsent
    fold into it); `at` None when none is left."""
    asked = units.parse_iso(a.get("at"))
    if asked is None:
        return 0, None
    n = int(sent.get("n") or 0) if sent.get("ask_at") == a.get("at") else 0
    return n, _slot(asked, n, units.parse_iso(sent.get("ask_last")))


def _slot(start, n, last):
    """When try n + 1 of what began at `start` falls due: the first at `start`, then the first RENOTIFY_S slot
    after `start` later than the last try (`last`; slots passed fold into it); None when none is left."""
    if not n:
        return start
    left = [t for t in (start + _dt.timedelta(seconds=s) for s in RENOTIFY_S) if t > (last or start)]
    return left[0] if left else None


def fallback(reg, project, now):
    """R26: the oldest report to Main, FALLBACK_S to a day old, that waits on the owner — the newest of
    its unit is blocked, question or failed or carries an owner step, and no brief revision answered
    it; or a lineage's close pointer — with no tagged Main turn, owner prompt, ask or fallback notice
    since it. A report its lineage batch held (`quiet: "batch"`, pending or drained) stays its unit's
    newest but is never a cause: the batch's close is. (pointer, text) or None."""
    from .. import brief
    newest, closes = {}, []
    for t, ptr, _file in _pointers(project, now - RECENT):
        unit = str(ptr["unit"])
        if ptr.get("lineage_closed"):
            closes.append((t, ptr))
        elif unit not in newest or t >= newest[unit][0]:
            newest[unit] = (t, ptr)
    marks = [_mtime(_path(project, TAGGED)), _mtime(_path(project, "owner-seen"))]
    for rec in (read_ask(project), util.read_dict(_path(project, "fallback.json"))):
        at = units.parse_iso((rec or {}).get("at"))
        marks.append(at.timestamp() if at else None)
    last = max((m for m in marks if m is not None), default=None)
    for t, ptr in sorted([*newest.values(), *closes], key=lambda x: x[0]):
        if (now - t).total_seconds() < FALLBACK_S or (last is not None and last >= t.timestamp()):
            continue
        if ptr.get("quiet") == "batch":
            continue
        step_ = ptr.get("owner")
        if not ptr.get("lineage_closed"):
            if ptr.get("status") not in ASKING + ENDED and not step_:
                continue
            b = brief.find(reg, str(ptr["unit"]), project=project)
            revised = _mtime(b.path) if b is not None else None
            if revised is not None and revised > t.timestamp():
                continue
        what = step_ or ("lineage closed" if ptr.get("lineage_closed") else ptr.get("status"))
        return ptr, f"{ptr['unit']}: {what}"
    return None


def _row(project, target, line, dry_run, action="notify", attempt=1, cap=1) -> dict:
    return {"action": action, "class": "owner", "project": project, "target": target, "rev": None,
            "attempt": attempt, "cap": cap, "line": ("would " if dry_run else "") + line, "failed": None}


def _resumes(p, now, dry_run) -> list:
    """Requests filled in the last day with a unit still to tell (secret_filled): a dialog that died, a
    report filed after the fill, the last of several secrets filled."""
    if not (paths.orche_home() / "secrets" / "requests").is_dir() or guard.paused(p) is not None:
        return []                                       # store.requests_dir() creates it: a dry run must not
    from ..secrets import store
    rows = []
    for rid, req in store.list_requests(status="filled", project=p):
        t, asked, name = units.parse_iso(req.get("filled_at")), units.parse_iso(req.get("at")), str(req.get("name") or "")
        if t is None or asked is None or now - t > RECENT:
            continue
        told = ([r[0] for r in _resumable(p, name, asked - _dt.timedelta(seconds=RESUME_SLACK_S))] if dry_run
                else secret_filled(p, name, rid, now, req.get("filled_at")))
        rows += [_row(p, unit, f"tell main-{p}: secret {name} filled — resume {unit}", dry_run, "resume")
                 for unit in told]
    return rows


def _fallback(p, cause, now, dry_run) -> dict:
    """A report that asks (blocked, question, an owner step) becomes the ask, re-notified until
    answered; a failed report or a close pointer is one fallback notice. The row is `held`, not `notify`,
    when the notification was not shown (the gap, the lock, osascript): a later pass sends it."""
    ptr, text = cause
    unit, step_ = str(ptr["unit"]), ptr.get("owner")
    if dry_run:
        shown = not _gap(_sent(p), now)
    elif step_ or ptr.get("status") in ASKING:
        a = record(p, text, kind="secret" if step_ and kind_of(step_) == "secret" else "fallback",
                   source="heartbeat", unit=unit, now=now)
        shown = notify(p, f"OWNER: {a['text']}", a["kind"], now, ask_at=a["at"])
    else:
        shown = announce(p, "fallback", text, now)
    what = f"{unit} waits in main-{p} (fallback: Main did not tag it"
    return (_row(p, unit, f"notify the owner: {what})", dry_run) if shown
            else _row(p, unit, f"hold the owner's notification: {what}; a later pass sends it)", dry_run, "held"))


def _flush(p, now, dry_run):
    """The oldest notice not yet shown whose try is due: dropped (marked shown, unsent) once the owner
    prompted Main after it or when a day old; else sent once the gap has passed. A failed try waits for the next RENOTIFY_S slot after the
    notice (at most 5 tries; the day's end drops it)."""
    with (contextlib.nullcontext(True) if dry_run else _locked(_path(p, NOTIFY_LOCK))) as ok:
        if not ok:
            return None
        sent, seen, waiting = _sent(p), _mtime(_path(p, "owner-seen")), []
        for kind in NOTICES:
            rec = util.read_dict(_path(p, f"{kind}.json")) or {}
            at = units.parse_iso(rec.get("at"))
            if at is None or _shown(sent).get(kind) == rec.get("id"):
                continue
            if now - at > RECENT or (seen is not None and seen > at.timestamp() + 1):
                if not dry_run:
                    sent = dict(sent, shown={**_shown(sent), kind: rec.get("id")})
                    util.write_json(_path(p, SENT), sent)
                    log.log("events", f"owner dropped project={p} kind={kind}")
                continue
            slot = _slot(at, *_tries(sent, kind, rec.get("id")))
            if slot is not None and slot <= now:
                waiting.append((at, kind, rec))
        if not waiting:
            return None
        at, kind, rec = min(waiting, key=lambda w: w[0])
        if _gap(sent, now):
            return None
        row = _row(p, kind, f"notify the owner: the held {kind} notice from {units.hhmm(at)}", dry_run)
        if dry_run or _show(p, sent, str(rec.get("text") or ""), kind, now, notice_id=rec.get("id")):
            return row
    return None


def _next(p, a, now, dry_run):
    """The ask's next try when due: the first (held back, or none when Orca showed it), then each
    re-notification outside quiet hours; the gap first."""
    sent = _sent(p)
    n, at = due(a, sent)
    if at is None or at > now or (n and _quiet(now)) or _gap(sent, now):
        return None
    asked = units.hhmm(units.parse_iso(a["at"]))
    body = f"OWNER (asked {asked}): {a['text']}" if n else f"OWNER: {a['text']}"
    row = (_row(p, "owner", f"re-notify the owner: {p} ask from {asked} ({n}/{len(RENOTIFY_S)})", dry_run,
                attempt=n, cap=len(RENOTIFY_S)) if n
           else _row(p, "owner", f"notify the owner: {p} ask from {asked} (held back until now)", dry_run))
    if dry_run or notify(p, body, a["kind"], now, ask_at=a["at"]):
        return row
    return None


def _beat(reg, p, now, dry_run) -> list:
    rows, a = [], open_ask(p)
    if a is not None and answered_by(p, a) is not None:
        if not dry_run:
            answer(p, now)
        a = None
    rows += _resumes(p, now, dry_run)
    if guard.paused(p) is not None or not notice.enabled():
        return rows
    cause = fallback(reg, p, now)
    row = (_fallback(p, cause, now, dry_run) if cause is not None
           else _flush(p, now, dry_run) or (_next(p, a, now, dry_run) if a is not None else None))
    return rows + ([row] if row else [])


def heartbeat(reg, now, dry_run=False, keys=None) -> list:
    """The heartbeat's owner step for the projects `keys` (default all): rows in the pass's shape
    ({action: notify|resume, class: owner, project, target, rev, attempt, cap, line, failed}); a dry
    run writes and sends nothing and its lines start with `would`. Never raises."""
    rows = []
    for p in sorted(reg.get("projects", {})):
        if keys is not None and p not in keys:
            continue
        try:
            rows += _beat(reg, p, now, dry_run)
        except Exception as exc:  # noqa: BLE001 - one project never stops the pass
            log.log("hooks", f"owner: heartbeat {p}: {type(exc).__name__}: {exc}")
    return rows


def status(project, now=None):
    """The open ask for `orche status`: {text (≤ LINE_MAX), at, since, minutes, kind, source, unit,
    notified, next (HH:MM; None when none is left or notifications are off), quiet (the next one waits
    for quiet hours to end)}, or None."""
    now = now or units.now()
    a = open_ask(project)
    asked = units.parse_iso((a or {}).get("at"))
    if asked is None:
        return None
    sent = _sent(project)
    n, at = due(a, sent)
    quiet = False
    if at is not None and notice.enabled():
        last = units.parse_iso(sent.get("at"))
        at = max([at, now] + ([last + _dt.timedelta(seconds=NOTIFY_GAP_S)] if last else []))
        quiet = bool(n) and _quiet(at)
    else:
        at = None
    return {"text": one_line(a.get("text"), LINE_MAX), "at": a.get("at"), "since": units.hhmm(asked),
            "minutes": int((now - asked).total_seconds() // 60), "kind": a.get("kind"), "source": a.get("source"),
            "unit": a.get("unit"), "notified": n, "next": None if quiet or at is None else units.hhmm(at), "quiet": quiet}
