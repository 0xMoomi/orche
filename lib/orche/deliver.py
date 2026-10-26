"""Typing into a pane, detached from every hook: `orche nudge <session>` (the wake, design §4.4) and
`orche deliver <unit>` (a dispatched brief, design §4.5). No hook calls Orca to type (ruling 2).

A waking pointer posted to a session starts `orche nudge <session>` (inbox.wake). The nudger holds
`inbox/<session>/.wake.lock`, and types the fixed WAKE_TEXT only when a waking pointer is still
pending, the pane's agent is idle (`orca terminal wait --for tui-idle`: status running, no
blockedReason, satisfied) and the composer draft is empty (`orca terminal read`). The `.nudged`
marker is written before the send and removed when the send fails (a receipt without a proven
turn start counts as failed: `_receipt`); the prompt hook removes it after it drains, when the
wake (or any prompt) arrives. A draft that is ORCHE's own stale line is `nudge-failed`, never typed
over. The text typed is never pointer content.

The Stop hook writes a pane unit's pointer (`via: dispatch`) and starts `orche deliver <unit>`
(spawn). The deliverer holds the target's own `.deliver.lock` (never the wake lock: a brief is not
held up behind a wake's quiet window), waits up to DELIVER_WAIT_S for the target to be idle
(then types anyway: a division pane has no human and Claude Code queues typed input), never types
into a draft or a dialog, and types `[orche] inbox: <unit> rev <N>`; a send into a pane that was still
busy, accepted without a turn start, is `queued` (not failed). A failed delivery posts an
urgent `info` to the brief's sender naming the fix. A runner unit is started (`runs.start`) unless
it runs or already ran.
"""
import contextlib
import datetime as _dt
import fcntl
import re
import time

from . import brief, config, inbox, log, orca, paths, procs, roster, runs, shell, units, util, vendors

WAKE_LOCK = ".wake.lock"        # one nudger per session
DELIVER_LOCK = ".deliver.lock"  # one deliverer per target pane
LOCK_WAIT_S = 600.0             # a deliverer waits this long for earlier deliverers to the same pane
POLL_S = 0.5
LAST_NUDGE = ".last-nudge"  # {state, at} of the session's newest `orche nudge` (reconcile's escalation names it)
QUIET_POLL_S = 2.0         # the quiet window's poll (config inbox.quiet_s / quiet_max_s)
SUBMIT_WAIT_S = 20         # `send --wait-submit`: UserPromptSubmit hooks (ORCHE's 3 s budget plus others) run first
SEND = ("--enter", "--wait-submit", str(SUBMIT_WAIT_S))
IDLE_WAIT_S = 120          # `terminal wait --for tui-idle` before a wake
DELIVER_WAIT_S = 60        # … before a brief nudge (then it is typed anyway)
WAIT_SLACK_S = 10          # the wait call's timeout beyond its --timeout-ms
CALL_S = 20                # list and read (send: SUBMIT_WAIT_S + WAIT_SLACK_S)
# A nudger holds the wake lock for list + (idle wait + read) twice + send, plus its quiet window
# (inbox.quiet_max_s at most): a later nudger waits that long (wake_wait_s), then sees the marker
# (`already-nudged`), never a spurious `locked`.
WAKE_HOLD_S = CALL_S + 2 * (IDLE_WAIT_S + WAIT_SLACK_S + CALL_S) + SUBMIT_WAIT_S + 2 * WAIT_SLACK_S
OK_STATES = ("nudged", "already-nudged", "nothing-pending")
FAILED = ("no-pane", "needs-wake", "wake-requested", "dialog", "no-agent", "refused", "nudge-failed", "locked")


def wake_wait_s(cfg) -> float:
    """How long a nudger waits for the wake lock: WAKE_HOLD_S plus the quiet cap (`inbox.quiet_max_s`)."""
    return WAKE_HOLD_S + float(cfg.get("quiet_max_s", 240))


@contextlib.contextmanager
def _lock(session, name, wait_s, poll_s=POLL_S):
    """Hold `inbox/<session>/<name>` (flock, polled every `poll_s` for up to `wait_s`);
    yields True when held, False when another process kept it."""
    folder = inbox.box(session)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / name, "a") as fh:
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


def _type(session, text, *, strict, wait_s, still_needed, before_send=None, on_fail=None, settle=None, reopen=None):
    """Type `text` + Enter into `session`'s pane (design §4.4 steps 3-7). (state, waited seconds).

    The pane title is the session name. strict: not idle after `wait_s` → `busy` (else send anyway: Claude Code queues typed input,
    and an accepted send with no `turn_started` is then `queued`, not `nudge-failed`).
    still_needed(): checked after the wait; False → `nothing-pending`. before_send() runs right
    before the send; on_fail() after a send that failed (`refused`, `dialog`, `nudge-failed`).
    settle(): runs once, after the first empty-draft read; when it waited (returns > 0 seconds) the
    idle wait, still_needed() and the draft read run again, since the pane may have changed.
    reopen(handle): a dead pane (listed `disconnected`, `no-agent`, or a failed send whose terminal
    is then listed `disconnected`) is closed and reopened once (`reopen_pane`); the new handle, or
    None (the attempt then ends `no-agent`).
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

    def repair(old):
        nonlocal reopen
        fix, reopen = reopen, None
        return fix(old) if fix is not None else None
    if reopen is not None and orca.disconnected(term):
        handle = repair(handle)
        if handle is None:
            return done("no-agent")
    while True:
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
            handle = repair(handle) if reopen is not None else None
            if handle is not None:
                continue
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
        if settle is not None:
            settled, settle = settle(), None
            if settled:
                continue
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
        if state == "nudge-failed" and reopen is not None and _dead(session, handle):
            handle = repair(handle)
            if handle is not None:
                continue
        break
    if state not in ("nudged", "queued") and on_fail is not None:
        on_fail()
    return done(state)


REOPEN_LOCK = ".reopen.lock"    # one reopen per pane at a time (a deliverer, a nudger, the heartbeat)
REOPEN_WAIT_S = 30              # for the reopened pane to be listed; the tui-idle wait covers its boot


def _listed(session):
    """`session`'s pane as `terminal list` shows it now, or None (also when Orca cannot list)."""
    try:
        return orca.match_title(orca.terminals_of(orca.terminal("list", timeout=CALL_S)), session)
    except Exception:  # noqa: BLE001 - an unlisted pane is no pane
        return None


def _dead(session, handle) -> bool:
    term = _listed(session)
    return term is not None and str(term.get("handle")) == str(handle) and orca.disconnected(term)


def reopen_pane(reg, session, handle, done=None):
    """Close `session`'s dead pane `handle` and reopen it through the wake path (Main: `hr.project_pane`;
    a division or feature: `hr.wake`, which reopens an active unit's pane), then wait for the new pane to
    be listed connected. The new handle, or None. Logs `reopen session=… state=reopened` and appends
    the time to `done`. A connected pane another process already reopened is taken as it is."""
    from . import hr
    m = roster.Roster(reg).member(session)
    if m is None:
        return None
    with _lock(session, REOPEN_LOCK, REOPEN_WAIT_S + CALL_S) as ok:
        now = _listed(session)
        if now is not None and now.get("handle") and str(now["handle"]) != str(handle) \
                and not orca.disconnected(now):
            return str(now["handle"])
        if not ok:
            return None
        try:
            orca.terminal("close", "--terminal", str(handle), timeout=CALL_S)
        except Exception as exc:  # noqa: BLE001 - a pane Orca already dropped is closed
            log.log("hooks", f"deliver: close {session} pane {handle}: {exc}")
        try:
            if m.kind == "main":
                hr.project_pane(reg["projects"][m.project], dry_run=False, switch=False)
            else:
                hr.wake(reg, m)
        except Exception as exc:  # noqa: BLE001 - the attempt ends no-agent; the heartbeat retries
            log.log("hooks", f"deliver: reopen {session}: {type(exc).__name__}: {exc}")
            return None
        log.log("events", f"reopen session={session} state=reopened")
        if done is not None:
            done.append(units.now().isoformat())
        end = time.monotonic() + REOPEN_WAIT_S
        while True:
            now = _listed(session)
            if now is not None and now.get("handle") and str(now["handle"]) != str(handle) \
                    and not orca.disconnected(now):
                return str(now["handle"])
            if time.monotonic() >= end:
                return None
            time.sleep(POLL_S)


REMIND_RE = re.compile(r"^\s*\[orche\] report due:")


def _stale(draft: str) -> bool:
    """The composer holds an ORCHE line (a wake, a brief nudge or a reminder whose Enter never submitted)."""
    return draft == inbox.WAKE_TEXT or bool(inbox.NUDGE_RE.match(draft)) or bool(REMIND_RE.match(draft))


def lock_free(session, name=DELIVER_LOCK) -> bool:
    """Probe `inbox/<session>/<name>`: no process holds it. A missing file is free."""
    path = inbox.box(session) / name
    try:
        fh = open(path, "r")
    except OSError:
        return True
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


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


def _event(session, state, pending, waited, quiet=0) -> None:
    log.log("events", f"nudge session={session} state={state} pending={pending} waited={waited}s quiet={quiet}s")


def _quiet(session, cfg) -> int:
    """The quiet window: wait (polling every QUIET_POLL_S) while the newest waking pointer is younger
    than `quiet_s` and none is urgent, for at most `quiet_max_s`; returns the seconds waited."""
    quiet_s, cap = float(cfg.get("quiet_s", 90)), float(cfg.get("quiet_max_s", 240))
    started = time.monotonic()
    while True:
        items = [(x, p) for x, p in inbox.pending(session) if inbox.wakes(p)]
        if not items or any(inbox.is_urgent(p) for _x, p in items):
            break
        stamps = [t.timestamp() for t in (inbox.pointer_time(x, p) for x, p in items) if t is not None]
        age = time.time() - max(stamps) if stamps else quiet_s
        left = min(quiet_s - age, cap - (time.monotonic() - started))
        if left <= 0:
            break
        time.sleep(min(QUIET_POLL_S, left))
    return int(time.monotonic() - started)


def _reopener(session):
    """nudge's reopen callback (reopen_pane with the registry read when a pane is found dead)."""
    def fix(handle):
        from . import registry
        return reopen_pane(registry.load(), session, handle)
    return fix


def nudge(session) -> tuple:
    """`orche nudge <session>` (design §4.4): (exit code, state). Raises ValueError for an unsafe
    session name (before anything is touched); every other failure is a state."""
    inbox.box(session)
    started, state, waited, quiet = time.monotonic(), "nudge-failed", 0, [0]

    def settle():
        quiet[0] = _quiet(session, config.load()["inbox"])
        return quiet[0]
    try:
        with _lock(session, WAKE_LOCK, wake_wait_s(config.load()["inbox"])) as ok:
            if not ok:
                state, waited = "locked", int(time.monotonic() - started)
            else:
                try:
                    if inbox.nudged(session):
                        state = "already-nudged"
                    elif not _waking(session):
                        state = "nothing-pending"
                    else:
                        state, waited = _type(session, inbox.WAKE_TEXT, strict=True,
                                              wait_s=IDLE_WAIT_S, still_needed=lambda: _waking(session) > 0,
                                              before_send=lambda: _mark(session), on_fail=lambda: _unmark(session),
                                              settle=settle, reopen=_reopener(session))
                except Exception as exc:  # noqa: BLE001 - recorded below, under the lock
                    log.log("hooks", f"deliver: nudge {session}: {type(exc).__name__}: {exc}")
                    state, waited = "nudge-failed", int(time.monotonic() - started)
                _record_nudge(session, state)      # under the lock; a `locked` nudger records nothing
    except Exception as exc:  # noqa: BLE001 - the events line is always written
        log.log("hooks", f"deliver: nudge {session}: {type(exc).__name__}: {exc}")
        state, waited = "nudge-failed", int(time.monotonic() - started)
    finally:
        try:
            pending = _waking(session)
        except Exception:  # noqa: BLE001
            pending = 0
        _event(session, state, pending, waited, quiet[0])
    return (0 if state in OK_STATES else 1), state


def _record_nudge(session, state) -> None:
    try:
        util.write_json(inbox.box(session) / LAST_NUDGE, {"state": state, "at": util.now_iso()})
    except (OSError, ValueError) as exc:
        log.log("hooks", f"deliver: cannot record {session}'s last nudge: {exc}")


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
    Main targets keep the pane (a human reads them). A Codex target always takes the runner (no pane)."""
    m = r.member(to)
    if m is not None and vendors.vendor_of(vendors.of(r.reg, m)) == vendors.CODEX:
        return True
    if cfg.get("delivery", "runner") != "runner":
        return False
    return m is not None and m.owns_worktree


def spawn(unit, retry=False) -> None:
    """Start `orche deliver [--retry] <unit>` detached (logs/deliver.log); never waits. Raises what
    Popen raises. `retry` (reconcile's attempts): a failure posts no pointer of its own."""
    procs.spawn_orche(["deliver"] + (["--retry"] if retry else []) + ["--", unit],
                      paths.orche_home() / "logs" / "deliver.log")


def drain_dispatch(to, unit, rev) -> int:
    """Drain the pending `via: dispatch` pointers for `unit` at brief_rev <= `rev` in `to`'s inbox
    (the unit is settled or running: nothing is left to deliver). Returns how many moved."""
    stale = [path for path, p in inbox.pending(to) if p.get("via") == "dispatch" and p.get("unit") == unit
             and _rev(p) <= rev]
    inbox.drain(stale)
    return len(stale)


def _rev(ptr) -> int:
    try:
        return int(ptr.get("brief_rev") or 1)
    except (TypeError, ValueError):
        return 1


REPORTS = ("done", "failed", "blocked", "question", "stop")


def _reported(b, to) -> bool:
    """`to` already filed a report (done, failed, blocked, question, stop) on this revision of `b` to
    its sender (pending or drained); a dispatch pointer is never one."""
    if not b.sender:
        return False
    try:
        return any(_rev(p) == b.rev and p.get("via") != "dispatch" and p.get("status") in REPORTS
                   for _x, p in inbox.about(b.sender, b.unit, frm=to))
    except ValueError:
        return False


def _live_at(b, rec) -> bool:
    """`rec` (runs.running) is a live run of this very revision of `b` (the lock's `rev`)."""
    return rec is not None and units.num(rec.get("rev")) == b.rev


def _settled(b, to) -> bool:
    """Nothing is left to deliver for this revision: it ran (_ran), runs (a live lock of this rev) or
    was reported on (_reported). A run or report of an older revision never settles a newer one."""
    return _ran(b) or _live_at(b, runs.running(b.project, b.unit)) or _reported(b, to)


def _ran(b) -> bool:
    """RUN.json records a run that started at or after BRIEF.md's mtime: this revision already ran.
    `started` is whole seconds, so a start in BRIEF.md's own second counts. REPORT.md is no proof:
    it carries no revision (a run of rev N may write it after an AMEND rewrote BRIEF.md)."""
    try:
        since = _dt.datetime.fromtimestamp(int(b.path.stat().st_mtime), _dt.timezone.utc)
    except OSError:
        return False
    return runs.ran_since(b.folder, since)


def drain_now(to, unit, rev) -> bool:
    """drain_dispatch under `to`'s `.deliver.lock`, taken non-blocking (design §2.6): a deliverer that
    holds it re-checks and drains, or types, itself. True when drained."""
    with _lock(to, DELIVER_LOCK, 0) as ok:
        if ok:
            drain_dispatch(to, unit, rev)
        return ok


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
    """The urgent `info` from orche to the brief's sender (posting it wakes the sender's pane); not filed
    again while the same failure is still pending in the sender's inbox."""
    hint = _hint(r, to, unit, state)
    summary = f"not delivered: {unit} → {to} rev {rev} ({state})" + (f" — {hint}" if hint else "")
    try:
        if any(x.get("from") == "orche" and x.get("summary") == summary for _p, x in inbox.pending(sender)):
            return
        inbox.post(inbox.make(unit, "orche", sender, "info", brief_rev=rev, urgent=True, summary=summary))
    except (ValueError, OSError) as exc:
        log.log("hooks", f"deliver: cannot file the failure for {sender!r}: {exc}")


def _runner(r, b, to) -> str:
    """A runner unit's brief (the heartbeat's retry of a deferred dispatch, or a manual deliver):
    `running` while a run of this revision is live, `already-ran` when it ran; a STOP written after
    this revision was requested (_requested) drains its dispatch pointers (`stopped`); a live run of
    an older revision (an AMEND deferred from a Codex sandbox) is stopped and a fresh one started
    (`runner-started`); `runner-deferred` inside a Codex sandbox (runs.defer)."""
    live = runs.running(b.project, b.unit)
    if _live_at(b, live):
        return "running"
    if _ran(b):
        return "already-ran"          # never re-runs finished work: `orche run <unit>` does that on purpose
    if runs.stop_requested(b.project, b.unit, since=_requested(b, to)):
        drain_now(to, b.unit, b.rev)
        return "stopped"
    try:
        if live is not None:
            runs.stop(b.project, b.unit, reg=r.reg)     # runs.restart: the marker is older than the new start
        if not runs.start(b.unit):
            return runs.DEFERRED
    except Exception as exc:  # noqa: BLE001 - reported as a state
        log.log("hooks", f"deliver: runner for {b.unit} did not start: {exc}")
        return "runner-failed"
    return "runner-started"


def _requested(b, to) -> int:
    """When this revision's run was requested (ns): BRIEF.md's mtime, or the pending dispatch
    pointer's `since_ns` (a deferred dispatch, dated after an AMEND's own stop) when later."""
    try:
        since = b.path.stat().st_mtime_ns
    except OSError:
        since = 0
    found = _dispatched(to, b.unit)
    try:
        return max(since, int(found[1].get("since_ns") or 0) if found else 0)
    except (TypeError, ValueError):
        return since


def _stop_run(r, b, to, locked) -> str:
    """A STOP while a run of the unit is live: runs.stop (the marker, SIGTERM for a verified pid; the
    run files `info` "stopped by Main"), then the unit's dispatch pointers are drained (`stopped`)."""
    runs.stop(b.project, b.unit, reg=r.reg)
    if locked:
        drain_dispatch(to, b.unit, b.rev)
    else:
        drain_now(to, b.unit, b.rev)
    return "stopped"


def _pane(r, b, to, sent, reopened=None):
    """Steps 3-7: (state, waited). `sent` receives the pending pointer once found.

    A STOP pointer stops a live run of the unit (`stopped`), is drained when the target has no pane
    and nothing runs (`stop-idle`, never a headless run), and is otherwise typed like a brief (also
    after a report at that rev). Any other pointer of a settled revision is drained (`already-ran`),
    also when the unit settled while this deliverer waited for the lock (R12)."""
    found = _dispatched(to, b.unit)
    if found is None:
        return "picked-up", 0
    sent.append(found[1])
    path, stop = found[0], found[1].get("status") == "stop"
    started = time.monotonic()
    if stop and runs.running(b.project, b.unit) is not None:
        return _stop_run(r, b, to, locked=False), 0
    if not stop and _settled(b, to):
        drain_now(to, b.unit, b.rev)
        return "already-ran", 0
    with _lock(to, DELIVER_LOCK, LOCK_WAIT_S) as ok:
        if not ok:
            return "locked", int(time.monotonic() - started)
        if not path.exists():
            return "picked-up", int(time.monotonic() - started)
        if stop and runs.running(b.project, b.unit) is not None:
            return _stop_run(r, b, to, locked=True), int(time.monotonic() - started)
        if not stop and _settled(b, to):
            drain_dispatch(to, b.unit, b.rev)
            return "already-ran", int(time.monotonic() - started)
        state, waited = _type(to, f"[orche] inbox: {b.unit} rev {b.rev}", strict=False, wait_s=DELIVER_WAIT_S,
                              still_needed=path.exists, reopen=lambda h: reopen_pane(r.reg, to, h, reopened))
        if stop and state == "no-pane":
            drain_dispatch(to, b.unit, b.rev)
            return "stop-idle", waited
    if state == "nothing-pending":
        state = "picked-up"
    elif state == "no-pane":
        state = wake_state(r, to)
    return state, waited


def _fallback(r, b, to, state) -> str:
    """A division/feature target without a pane (no-pane, needs-wake, wake-requested): start the
    headless run instead (`runner-fallback`; a live run of an older revision is restarted on this
    one, as an AMEND to a runner unit), or `already-ran` when this revision ran or runs. Main keeps
    the state."""
    m = r.member(to)
    if state not in ("no-pane", "needs-wake", "wake-requested") or m is None or not m.owns_worktree:
        return state
    live = runs.running(b.project, b.unit)
    if _live_at(b, live) or _ran(b):
        drain_now(to, b.unit, b.rev)
        return "already-ran"
    try:
        if live is not None:
            runs.restart(b.project, b.unit, reg=r.reg)
        elif not runs.start(b.unit):
            return runs.DEFERRED
        if runs.in_sandbox():
            return runs.DEFERRED
    except Exception as exc:  # noqa: BLE001 - reported as the pane state
        log.log("hooks", f"deliver: runner fallback for {b.unit} did not start: {exc}")
        return state
    return "runner-fallback"


def deliver_unit(reg, unit, cfg=None, retry=False) -> tuple:
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
    to, state, waited, sent, reopened = t.session, "nudge-failed", 0, [], []
    started = time.monotonic()
    try:
        try:
            if runner_wanted(r, to, config.load() if cfg is None else cfg):
                state = _runner(r, b, to)
            else:
                state, waited = _pane(r, b, to, sent, reopened)
                state = _fallback(r, b, to, state)
        except Exception as exc:  # noqa: BLE001 - the events line is always written
            log.log("hooks", f"deliver: {unit}: {type(exc).__name__}: {exc}")
            state, waited = "nudge-failed", int(time.monotonic() - started)
        if state in FAILED and sent and not retry:
            _not_delivered(r, unit, to, b.rev, state, str(sent[0].get("from") or ""))
    finally:        # last: once it is written, this deliverer touches nothing more
        if state != "locked":
            _record_delivery(to, unit, b.rev, state, reopened[-1] if reopened else None)
        if reopened:            # deliver.log: the pane was dead and was reopened before the brief was typed
            log.log("deliver", f"deliver unit={unit} rev={b.rev} to={to} state=reopened")
        log.log("events", f"deliver unit={unit} rev={b.rev} to={to} state={state} waited={waited}s")
    return (1 if state in FAILED or state == "runner-failed" else 0), state


LAST_DELIVER = ".last-deliver-"   # + unit: {state, rev, at[, reopened]} of the unit's newest deliverer (reconcile's escalation)


def _record_delivery(to, unit, rev, state, reopened=None) -> None:
    rec = {"state": state, "rev": rev, "at": util.now_iso()} | ({"reopened": reopened} if reopened else {})
    try:
        util.write_json(inbox.box(to) / f"{LAST_DELIVER}{unit}", rec)
    except (OSError, ValueError) as exc:
        log.log("hooks", f"deliver: cannot record {unit}'s last delivery: {exc}")


def last_delivery(to, unit):
    """The unit's newest recorded deliverer state in `to`'s inbox ({state, rev, at}), or None."""
    try:
        last = util.read_json(inbox.box(to) / f"{LAST_DELIVER}{unit}", default=None)
    except (OSError, ValueError, util.JsonError):
        return None
    return last if isinstance(last, dict) else None


# ---- `orche deliver --remind <unit>` (reconcile's silent pane unit, design §2.3) --------------

REMIND_OK = ("nudged", "queued", "user-typing", "already-reported")
REMIND_REASON = re.compile(r"idle \d{1,4} min|TIMEBOX \d{1,2}h\d{1,2}m passed")   # the only reasons (fullmatch)


def remind_text(unit, rev, reason) -> str:
    return (f"[orche] report due: {unit} rev {rev} — {reason}; file your report now with what you have "
            f"(orche report …), then stop at a safe boundary. Already reported? Ignore this line.")


def remind(reg, unit, reason) -> tuple:
    """Type the report-due line into the unit's target pane once: (exit code, state). Exit 2 as
    deliver_unit, and `bad-reason` for a reason that is not REMIND_REASON; `already-reported` (exit 0)
    when the report arrived before the send."""
    if not REMIND_REASON.fullmatch(str(reason or "")):
        return 2, "bad-reason"
    r = roster.Roster(reg)
    b = brief.find(reg, unit)
    if b is None or b.rev == 0:
        return 2, "no-brief"
    t = r.resolve_to(b.to, b.project)
    if t is None:
        return 2, "unknown-target"
    if t.status == "retired":
        return 2, "retired-target"
    to, state, waited = t.session, "nudge-failed", 0

    def needed():
        return not _reported(b, to)
    started = time.monotonic()
    try:
        if not needed():
            state = "already-reported"
        else:
            with _lock(to, DELIVER_LOCK, LOCK_WAIT_S) as ok:
                if not ok:
                    state, waited = "locked", int(time.monotonic() - started)
                else:
                    state, waited = _type(to, remind_text(unit, b.rev, reason), strict=False,
                                          wait_s=DELIVER_WAIT_S, still_needed=needed)
            if state == "nothing-pending":
                state = "already-reported"
    except Exception as exc:  # noqa: BLE001 - the events line is always written
        log.log("hooks", f"deliver: remind {unit}: {type(exc).__name__}: {exc}")
        state, waited = "nudge-failed", int(time.monotonic() - started)
    finally:
        log.log("events", f"remind unit={unit} rev={b.rev} to={to} state={state} waited={waited}s")
    return (0 if state in REMIND_OK else 1), state
