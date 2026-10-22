"""Typing into a pane, detached from every hook: `orche nudge <session>` (the wake, design §4.4) and
`orche deliver <unit>` (a dispatched brief, design §4.5). No hook calls Orca to type (ruling 2).

A waking pointer posted to a session starts `orche nudge <session>` (inbox.wake). The nudger holds
`inbox/<session>/.deliver.lock`, and types the fixed WAKE_TEXT only when a waking pointer is still
pending, the pane's agent is idle (`orca terminal wait --for tui-idle`: status running, no
blockedReason, satisfied) and the composer draft is empty (`orca terminal read`). The `.nudged`
marker is written before the send and removed when the send fails (a receipt without a proven
turn start counts as failed: `_receipt`); the prompt hook removes it after it drains, when the
wake (or any prompt) arrives. A draft that is ORCHE's own stale line is `nudge-failed`, never typed
over. The text typed is never pointer content.

The Stop hook writes a pane unit's pointer (`via: dispatch`) and starts `orche deliver <unit>`
(spawn). The deliverer holds the same lock, waits up to DELIVER_WAIT_S for the target to be idle
(then types anyway: a division pane has no human and Claude Code queues typed input), never types
into a draft or a dialog, and types `[orche] inbox: <unit> rev <N>`; a send into a pane that was still
busy, accepted without a turn start, is `queued` (not failed). A failed delivery posts an
urgent `info` to the brief's sender naming the fix. A runner unit is started (`runs.start`) unless
it runs or already ran.
"""
import contextlib
import fcntl
import sys
import time

from . import brief, config, inbox, log, orca, paths, procs, roster, runs, util

LOCK = ".deliver.lock"
LOCK_WAIT_S = 150.0
POLL_S = 0.5
SUBMIT_WAIT_S = 20         # `send --wait-submit`: UserPromptSubmit hooks (ORCHE's 3 s budget plus others) run first
SEND = ("--enter", "--wait-submit", str(SUBMIT_WAIT_S))
IDLE_WAIT_S = 120          # `terminal wait --for tui-idle` before a wake
DELIVER_WAIT_S = 60        # … before a brief nudge (then it is typed anyway)
WAIT_SLACK_S = 10          # the wait call's timeout beyond its --timeout-ms
CALL_S = 20                # list and read (send: SUBMIT_WAIT_S + WAIT_SLACK_S)
OK_STATES = ("nudged", "already-nudged", "nothing-pending")
FAILED = ("no-pane", "needs-wake", "wake-requested", "dialog", "no-agent", "refused", "nudge-failed", "locked")


@contextlib.contextmanager
def _lock(session, wait_s=LOCK_WAIT_S, poll_s=POLL_S):
    """Hold `inbox/<session>/.deliver.lock` (flock, polled every `poll_s` for up to `wait_s`);
    yields True when held, False when another deliverer kept it."""
    folder = inbox.box(session)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / LOCK, "a") as fh:
        end, ok = time.monotonic() + wait_s, False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ok = True
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    break
                time.sleep(poll_s)
        try:
            yield ok
        finally:
            if ok:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _result(res, key) -> dict:
    """The receipt's `key` object (`{"wait": {...}}` → {...}); a flat receipt is taken as is."""
    if not isinstance(res, dict):
        return {}
    inner = res.get(key)
    return inner if isinstance(inner, dict) else res


def _type(session, text, *, strict, wait_s, still_needed, before_send=None, on_fail=None):
    """Type `text` + Enter into `session`'s pane (design §4.4 steps 3-7). (state, waited seconds).

    The pane title is the session name. strict: not idle after `wait_s` → `busy` (else send anyway: Claude Code queues typed input,
    and an accepted send with no `turn_started` is then `queued`, not `nudge-failed`).
    still_needed(): checked after the wait; False → `nothing-pending`. before_send() runs right
    before the send; on_fail() after a send that failed (`refused`, `dialog`, `nudge-failed`).
    """
    started = time.monotonic()

    def done(state):
        return state, int(time.monotonic() - started)

    try:
        term = orca.match_title(orca.terminals_of(orca.terminal("list", timeout=CALL_S)), session)
    except Exception as exc:  # noqa: BLE001 - an unreachable Orca is a failed nudge
        log.log("hooks", f"deliver: terminal list for {session}: {exc}")
        return done("nudge-failed")
    if term is None or not term.get("handle"):
        return done("no-pane")
    handle = str(term["handle"])
    try:
        wait = _result(orca.terminal("wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms",
                                     str(int(wait_s * 1000)), timeout=wait_s + WAIT_SLACK_S), "wait")
    except orca.OrcaError as exc:
        if "timeout" not in exc.code:
            log.log("hooks", f"deliver: terminal wait for {session}: {exc}")
            return done("nudge-failed")
        wait = {"satisfied": False}
    if wait.get("blockedReason"):
        return done("dialog")
    if wait.get("status") is not None and wait.get("status") != "running":
        return done("no-agent")
    busy = not wait.get("satisfied")
    if strict and busy:
        return done("busy")
    if not still_needed():
        return done("nothing-pending")
    try:
        draft = _result(orca.terminal("read", "--terminal", handle, timeout=CALL_S), "terminal").get("draft")
    except Exception as exc:  # noqa: BLE001 - an unread composer is never typed into
        log.log("hooks", f"deliver: terminal read for {session}: {exc}")
        return done("nudge-failed")
    draft = str(draft or "").strip()
    if draft and _stale(draft):
        # ORCHE's own unsubmitted line: it cannot be cleared from here and is never typed again.
        log.log("hooks", f"deliver: {session} composer holds a stale ORCHE line; not typing")
        return done("nudge-failed")
    if draft:
        return done("user-typing")
    if before_send is not None:
        before_send()
    try:
        send = _result(orca.terminal("send", "--terminal", handle, "--text", text, *SEND,
                                          timeout=SUBMIT_WAIT_S + WAIT_SLACK_S), "send")
    except Exception as exc:  # noqa: BLE001 - never resend on silence
        log.log("hooks", f"deliver: terminal send to {session}: {exc}")
        state = "nudge-failed"
    else:
        state = _receipt(send)
        if state == "nudge-failed" and busy and _queued(send):
            state = "queued"    # typed into a working pane: Claude Code holds it for the next turn
        elif state == "nudge-failed":
            log.log("hooks", f"deliver: send to {session} not submitted: {send.get('prompt')}")
    if state not in ("nudged", "queued") and on_fail is not None:
        on_fail()
    return done(state)


def _stale(draft: str) -> bool:
    """The composer holds an ORCHE line (a wake or a brief nudge whose Enter never submitted)."""
    return draft == inbox.WAKE_TEXT or bool(inbox.NUDGE_RE.match(draft))


def _receipt(send: dict) -> str:
    """The state of a `terminal send` receipt: `refused`, `dialog`, `nudge-failed` or `nudged`.

    `accepted: false` → refused. The `prompt` object (Orca's agent-prompt receipt) decides the
    rest: `observation: permission` → dialog; `observation: incarnation_replaced` (the process was
    replaced) → nudge-failed; a provider other than `unsupported`/`old-host` whose `stages` lack
    `turn_started` → nudge-failed (the Enter may have been swallowed and the text sits as a draft).
    A receipt with no `prompt` object at all (an older host) counts as sent.
    """
    if send.get("accepted") is False:
        return "refused"
    prompt = send.get("prompt")
    if not isinstance(prompt, dict):
        return "nudged"
    if prompt.get("observation") == "permission":
        return "dialog"
    if prompt.get("observation") == "incarnation_replaced":
        return "nudge-failed"
    stages = prompt.get("stages")
    if prompt.get("provider") not in ("unsupported", "old-host") and (
            not isinstance(stages, list) or "turn_started" not in stages):
        return "nudge-failed"
    return "nudged"


def _queued(send: dict) -> bool:
    """An accepted send whose only shortfall is a missing `turn_started` (no dialog, same process).

    Into a busy pane (the non-strict wait ended unsatisfied) that is Claude Code queueing the typed
    line for its next turn, not a swallowed Enter: `_type` reports it as `queued`."""
    prompt = send.get("prompt")
    return (send.get("accepted") is True and isinstance(prompt, dict)
            and prompt.get("observation") not in ("permission", "incarnation_replaced"))


def _waking(session) -> int:
    return sum(1 for _x, p in inbox.pending(session) if inbox.wakes(p))


def _mark(session) -> None:
    (inbox.box(session) / inbox.NUDGED).write_text(util.now_iso() + "\n", encoding="utf-8")


def _unmark(session) -> None:
    """Remove the `.nudged` marker after a failed send; never raises (the state stays the send's)."""
    try:
        (inbox.box(session) / inbox.NUDGED).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        log.log("hooks", f"deliver: cannot remove {session}'s wake marker: {exc}")


def _event(session, state, pending, waited) -> None:
    log.log("events", f"nudge session={session} state={state} pending={pending} waited={waited}s")


def nudge(session) -> tuple:
    """`orche nudge <session>` (design §4.4): (exit code, state). Raises ValueError for an unsafe
    session name (before anything is touched); every other failure is a state."""
    inbox.box(session)
    started, state, waited = time.monotonic(), "nudge-failed", 0
    try:
        with _lock(session, LOCK_WAIT_S, POLL_S) as ok:
            if not ok:
                state, waited = "locked", int(time.monotonic() - started)
            elif inbox.nudged(session):
                state = "already-nudged"
            elif not _waking(session):
                state = "nothing-pending"
            else:
                state, waited = _type(session, inbox.WAKE_TEXT, strict=True,
                                      wait_s=IDLE_WAIT_S, still_needed=lambda: _waking(session) > 0,
                                      before_send=lambda: _mark(session), on_fail=lambda: _unmark(session))
    except Exception as exc:  # noqa: BLE001 - the events line is always written
        log.log("hooks", f"deliver: nudge {session}: {type(exc).__name__}: {exc}")
        state, waited = "nudge-failed", int(time.monotonic() - started)
    finally:
        try:
            pending = _waking(session)
        except Exception:  # noqa: BLE001
            pending = 0
        _event(session, state, pending, waited)
    return (0 if state in OK_STATES else 1), state


# ---- `orche deliver <unit>` (J1, design §4.5) ---------------------------------------

def wake_state(r, session: str) -> str:
    """The state of a target without a pane (nothing here creates panes): `wake-requested` when the
    member asks to be woken on a dispatch (default: Main and Build), `needs-wake` when it sleeps."""
    m = r.member(session)
    entry = (m.entry if m else None) or {}
    default = bool(m and (m.kind == "main" or (m.kind == "div" and m.name == "build")))
    if entry.get("wake_on_dispatch", default):
        return "wake-requested"
    if entry.get("status") in ("dormant", "paused"):
        return "needs-wake"
    return "no-pane"


def runner_wanted(r, to, cfg) -> bool:
    """A unit for a division/feature goes through the runner unless config `delivery` is `pane`;
    Main targets keep the pane (a human reads them)."""
    if cfg.get("delivery", "runner") != "runner":
        return False
    m = r.member(to)
    return m is not None and m.owns_worktree


def spawn(unit) -> None:
    """Start `orche deliver <unit>` detached (logs/deliver.log); never waits. Raises what Popen raises."""
    procs.spawn_detached([sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), "deliver", "--", unit],
                         paths.orche_home() / "logs" / "deliver.log")


def _ran(b) -> bool:
    """RUN.json or REPORT.md is at least as new as BRIEF.md: this revision already ran."""
    try:
        since = b.path.stat().st_mtime
    except OSError:
        return False
    for name in ("RUN.json", "REPORT.md"):
        try:
            if (b.folder / name).stat().st_mtime >= since:
                return True
        except OSError:
            pass
    return False


def _dispatched(to, unit):
    """The newest pending `via: dispatch` pointer for `unit` in `to`'s inbox: (path, pointer) or None."""
    found = [(p, x) for p, x in inbox.pending(to) if x.get("via") == "dispatch" and x.get("unit") == unit]
    return found[-1] if found else None


def _hint(r, to, unit, state) -> str:
    again = f"`orche deliver {unit}`"
    m = r.member(to)
    if state in ("no-pane", "needs-wake", "wake-requested") and m is not None and m.owns_worktree:
        return f"target has no pane: run `orche wake {to}`, then {again}"
    if state == "needs-wake":
        return f"target is asleep: run `orche wake {to}`, then {again}"
    if state == "wake-requested":
        return ""
    if state == "dialog":
        return f"the pane shows a dialog; answer or STOP it, then {again}"
    return f"retry with {again}"


def _not_delivered(r, unit, to, rev, state, sender) -> None:
    """The urgent `info` from orche to the brief's sender (posting it wakes the sender's pane)."""
    hint = _hint(r, to, unit, state)
    summary = f"not delivered: {unit} → {to} rev {rev} ({state})" + (f" — {hint}" if hint else "")
    try:
        inbox.post(inbox.make(unit, "orche", sender, "info", brief_rev=rev, urgent=True, summary=summary))
    except (ValueError, OSError) as exc:
        log.log("hooks", f"deliver: cannot file the failure for {sender!r}: {exc}")


def _runner(b) -> str:
    if runs.running(b.project, b.unit):
        return "running"
    if _ran(b):
        return "already-ran"          # never re-runs finished work: `orche run <unit>` does that on purpose
    try:
        runs.start(b.unit)
    except Exception as exc:  # noqa: BLE001 - reported as a state
        log.log("hooks", f"deliver: runner for {b.unit} did not start: {exc}")
        return "runner-failed"
    return "runner-started"


def _pane(r, b, to, sent):
    """Steps 3-7: (state, waited). `sent` receives the pending pointer once found."""
    found = _dispatched(to, b.unit)
    if found is None:
        return "picked-up", 0
    sent.append(found[1])
    path = found[0]
    started = time.monotonic()
    with _lock(to, LOCK_WAIT_S, POLL_S) as ok:
        if not ok:
            return "locked", int(time.monotonic() - started)
        if not path.exists():
            return "picked-up", int(time.monotonic() - started)
        state, waited = _type(to, f"[orche] inbox: {b.unit} rev {b.rev}", strict=False, wait_s=DELIVER_WAIT_S,
                              still_needed=path.exists)
    if state == "nothing-pending":
        state = "picked-up"
    elif state == "no-pane":
        state = wake_state(r, to)
    return state, waited


def deliver_unit(reg, unit, cfg=None) -> tuple:
    """`orche deliver <unit>` (design §4.5): (exit code, state). Exit 2 (`no-brief`, `unknown-target`,
    `retired-target`) touches nothing; else one events line, and a failure (FAILED, exit 1) posts an
    urgent `info` to the pending pointer's sender. `user-typing` and `queued` (typed into a busy pane,
    accepted with no turn start yet) are exit 0: the pane's next prompt injects the pointer."""
    r = roster.Roster(reg)
    b = brief.find(reg, unit)
    if b is None or b.rev == 0:
        return 2, "no-brief"
    t = r.resolve_to(b.to, b.project)
    if t is None:
        return 2, "unknown-target"
    if t.status == "retired":
        return 2, "retired-target"
    to, state, waited, sent = t.session, "nudge-failed", 0, []
    started = time.monotonic()
    try:
        try:
            if runner_wanted(r, to, config.load() if cfg is None else cfg):
                state = _runner(b)
            else:
                state, waited = _pane(r, b, to, sent)
        except Exception as exc:  # noqa: BLE001 - the events line is always written
            log.log("hooks", f"deliver: {unit}: {type(exc).__name__}: {exc}")
            state, waited = "nudge-failed", int(time.monotonic() - started)
        if state in FAILED and sent:
            _not_delivered(r, unit, to, b.rev, state, str(sent[0].get("from") or ""))
    finally:        # last: once it is written, this deliverer touches nothing more
        log.log("events", f"deliver unit={unit} rev={b.rev} to={to} state={state} waited={waited}s")
    return (1 if state in FAILED or state == "runner-failed" else 0), state
