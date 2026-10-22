"""Dispatch relay: route ```dispatch blocks (lib/orche/brief.py parses and stores them) and deliver.

The Stop hook's stop decision (the done gate, then the night predicate) is donegate.may_stop.
"""
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import (brief, config, deliver, donegate, inbox, log, mask, orca, paths, registry, roster, runs, transcript,
               units, util)

# ---- targets -------------------------------------------------------------

def standing_text(reg, proj_key) -> str:
    proj = reg["projects"][proj_key]
    path = Path(units.project_root(reg, proj_key) or ".") / proj["standing"]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.log("hooks", f"dispatch: no STANDING.md at {path}")
        return "(no STANDING.md)"


# ---- relay ---------------------------------------------------------------

@dataclass
class Plan:
    block: brief.Block
    to: str
    proj: str
    prior: brief.Brief | None = None    # amend/stop: the unit's current brief
    unit: str = ""
    external: bool = False          # TO <other project> -> external-request pointer to its Main


def _target_error(r, to):
    return f"{brief.REFUSE}unknown TO {to!r}. Valid targets: {', '.join(r.sessions())}."


def _cross_project_error(r, to, target_proj, sender_proj):
    main = r.main(target_proj)
    suggest = main.session if main else target_proj
    return (f"{brief.REFUSE}{to} belongs to project {target_proj}, not {sender_proj}. Cross-project work goes "
            f"through Main({target_proj}): use `TO {suggest}` (files an external-request).")


def _retired_error(t):
    """The runner and `orche report` refuse a retired target; a dispatch refuses it before any write."""
    return f"{brief.REFUSE}target {t.session} is retired."


def plan_blocks(r, bodies, frm) -> list:
    """Parse + resolve every block first so nothing is written when any block is refused.

    `bodies` are block texts or already-built `brief.Block`s (triage passes a `brief.Block`, so its
    prompt text is never parsed). `frm` is the sending Member.

    Targets must belong to the sender's project (R-XPROJ): a brief whose TO resolves to another
    project's Main becomes an external-request pointer to it; any other cross-project target is refused.
    A retired target is refused. AMEND/STOP need the unit's BRIEF.md.
    """
    plans, errors = [], []
    for body in bodies:
        try:
            block = body if isinstance(body, brief.Block) else brief.parse(body)
            if block.kind == "brief":
                t = r.resolve_to(block.fields["TO"], frm.project)
                if t is None:
                    raise brief.Refusal(_target_error(r, block.fields["TO"]))
                if t.status == "retired":
                    raise brief.Refusal(_retired_error(t))
                if t.project != frm.project:
                    if t.kind != "main":
                        raise brief.Refusal(_cross_project_error(r, t.session, t.project, frm.project))
                    plans.append(Plan(block, t.session, t.project, external=True))
                    continue
                plans.append(Plan(block, t.session, t.project))
                continue
            prior = brief.find(r.reg, block.unit)
            if prior is None or prior.rev == 0:
                raise brief.Refusal(f"{brief.REFUSE}unknown unit {block.unit!r} "
                                    f"(no {brief.UNITS}/{block.unit}/BRIEF.md).")
            if prior.project != frm.project:
                raise brief.Refusal(_cross_project_error(r, block.unit, prior.project, frm.project))
            raw_to = block.fields.get("TO") or prior.to
            t = r.resolve_to(raw_to, prior.project)
            if t is None:
                raise brief.Refusal(_target_error(r, raw_to))
            if t.status == "retired":
                raise brief.Refusal(_retired_error(t))
            if t.project != frm.project:
                raise brief.Refusal(_cross_project_error(r, t.session, t.project, frm.project))
            plans.append(Plan(block, t.session, prior.project, prior, block.unit))
        except brief.Refusal as exc:
            errors.append(str(exc))
    if errors:
        raise brief.Refusal("\n".join(errors))
    return plans


RUNNER_NOT_LIVE = " (no live run)"


def _run_delivery(plan, terms) -> str:
    """Runner delivery, never waiting: a brief starts `orche run`; STOP writes the stop marker and
    stops a live run (runs.stop: the marker, which the run polls every 2 s, plus SIGTERM for a
    `ps`-verified pid; the runner files `info` "stopped by Main"; a run still
    waiting for a slot gives up, "… before start") — `runner-stop-requested` either way, with
    " (no live run)" when nothing was running; AMEND stops a live run and starts a fresh one for
    the new rev (it waits for the stopped run's slot). The `ps` pid check inside runs.stop gets at
    most what is left of the hook budget."""
    kind = plan.block.kind
    if kind == "stop":
        live = runs.stop(plan.proj, plan.unit, timeout=terms.remaining())
        return "runner-stop-requested" + ("" if live else RUNNER_NOT_LIVE)
    if kind == "amend":
        restarted = runs.restart(plan.proj, plan.unit, timeout=terms.remaining())
        return "runner-restarted" if restarted else "runner-started"
    runs.start(plan.unit)
    return "runner-started"


def _deliver(r, plan, b, status, summary, frm, terms, cfg=None, by=None):
    """No Orca call (ruling 2). A runner unit (deliver.runner_wanted) is started, restarted or stopped
    here and gets no target pointer (ruling 6); an exception is `runner-failed`. A pane unit gets
    its pointer (`via: dispatch`, injected at the target's next prompt) and a detached
    `orche deliver <unit>` (`deliverer-started`; `deliverer-failed` when it cannot start), which
    types the inbox line. `b` is the unit's Brief (the one just written, or the prior one for a
    STOP). `by` names who relayed in the events line (default the sender; triage passes `triage`)."""
    rev = b.rev
    by = by or frm
    if deliver.runner_wanted(r, plan.to, cfg if cfg is not None else config.load()):
        try:
            state = _run_delivery(plan, terms)
        except Exception as exc:  # noqa: BLE001 - reported to the sender, urgent
            log.log("hooks", f"dispatch: runner delivery for {plan.unit} failed: {exc}")
            state = "runner-failed"
    else:
        inbox.write(inbox.make(plan.unit, frm, plan.to, status, report_path=str(b.path), brief_rev=rev,
                               summary=summary, via="dispatch"))
        units.touch_activity(plan.to)
        try:
            deliver.spawn(plan.unit)
            state = "deliverer-started"
        except Exception as exc:  # noqa: BLE001 - reported to the sender, urgent
            log.log("hooks", f"dispatch: deliverer for {plan.unit} did not start: {exc}")
            state = "deliverer-failed"
    log.log("events", f"dispatch kind={plan.block.kind} unit={plan.unit} rev={rev} to={plan.to} "
                      f"by={by} delivery={state}")
    return state


URGENT_STATES = ("runner-failed", "deliverer-failed")


def _feedback(frm, unit, to, rev, state, kind=None, by=None):
    """One `info` pointer from orche to the sender: unit id + delivery state. Held until a drain
    trigger, except a failed delivery (URGENT_STATES), which is urgent and names the fix. Pane-state
    hints come from the deliverer's own failure pointer. A brief triage routed from a prompt reads
    "routed <unit> → <to> (<state>)".
    """
    summary = (f"routed {unit} → {to} ({state})" if by == "triage"
               else f"relayed {unit} → {to} rev {rev} ({state})")
    if state == "runner-failed" and kind == "stop":
        summary += " — the STOP did not reach the runner: check `orche status` and send the STOP again"
    elif state == "runner-failed":
        summary += f" — the runner did not start: run `orche deliver {unit}`"
    elif state == "deliverer-failed":
        summary += f" — run `orche deliver {unit}`"
    elif state == "runner-stop-requested" + RUNNER_NOT_LIVE:
        summary += " — already finished (or still queued: a queued run will not start)"
    try:        # best-effort: after a started runner nothing may turn the relay into an error
        inbox.post(inbox.make(unit, "orche", frm, "info", brief_rev=rev, urgent=state in URGENT_STATES,
                              summary=summary))
    except (ValueError, OSError) as exc:
        log.log("hooks", f"dispatch: cannot file feedback for {frm!r}: {exc}")


def _external(plan, frm, sender_proj):
    """TO <projB>: an external-request pointer to Main(B) carrying the brief body (no brief written)."""
    fields = plan.block.fields
    body = "; ".join(f"{k} {' '.join(str(fields[k]).split())}" for k in brief.KEYS if k in fields and k != "TO")
    goal = util.slugify(" ".join(mask.redact(fields.get("GOAL", ""))[0].split()[:4])) or "request"
    plan.unit = f"xreq-{util.slugify(sender_proj or 'unknown')}-{goal}-{units.now():%Y%m%d}"
    inbox.post(inbox.make(plan.unit, frm, plan.to, "external-request", summary=mask.redact(body)[0],
                          from_project=sender_proj))
    units.touch_activity(plan.to)
    log.log("events", f"dispatch kind=external-request unit={plan.unit} to={plan.to} by={frm} "
                      f"from_project={sender_proj}")
    return "external-request filed"


def check_sender(r, who) -> "roster.Member":
    """Only a project's Main or a division (Build dispatches to features) may dispatch; returns the sender.

    An unidentified session (no ORCHE_SESSION, not in a unit worktree with a session) is refused.
    """
    if who.session is None:
        main = r.main(who.place.project)
        hint = main.session if main else "main-<proj>"
        raise brief.Refusal(f"dispatch requires an identified session: set ORCHE_SESSION={hint} "
                      "(ORCHE panes set it automatically)")
    role = who.member.role if who.member else "unknown"
    if role not in ("main", "division"):
        article = "an" if role[0] in "aeiou" else "a"
        raise brief.Refusal(f"only Main or a division may dispatch; {who.session} is {article} {role} session")
    return who.member


def relay(r, bodies, frm, deadline=None) -> list:
    """Write briefs and deliver them for the sending Member `frm`. Returns the unit ids. Raises brief.Refusal."""
    return [d["unit"] for d in relay_detail(r, bodies, frm, deadline)]


def relay_detail(r, bodies, frm, deadline=None, by=None) -> list:
    """relay(), returning `{"unit", "to", "state"}` per block. `by` (e.g. `triage`) is who relayed:
    it names the events line's `by=` and words the sender's feedback pointer.

    `frm` must be the Member returned by `check_sender`; nothing here re-checks the sender's role.
    """
    sender, sender_proj = frm.session, frm.project
    plans = plan_blocks(r, bodies, frm)
    terms = orca.Terminals(deadline if deadline is not None else orca.hook_deadline())   # remaining() only
    cfg = config.load()
    done = []
    for plan in plans:
        block = plan.block
        if plan.external:
            state = _external(plan, sender, sender_proj)
            _feedback(sender, plan.unit, plan.to, 0, state)
        elif block.kind == "stop":
            state = _deliver(r, plan, plan.prior, "stop", block.note or "STOP requested", sender, terms, cfg, by)
            _feedback(sender, plan.unit, plan.to, plan.prior.rev, state, kind="stop", by=by)
        else:
            b = brief.write(block, r.member(plan.to), sender, standing_text(r.reg, plan.proj),
                            prior=plan.prior, timeout=min(0.5, terms.remaining()))
            plan.unit = b.unit
            goal = mask.redact(b.fields.get("GOAL", "").split("\n")[0])[0]
            state = _deliver(r, plan, b, "info", goal, sender, terms, cfg, by)
            _feedback(sender, plan.unit, plan.to, b.rev, state, by=by)
        done.append({"unit": plan.unit, "to": plan.to, "state": state})
    return done


# ---- Stop hook entry -----------------------------------------------------

def _relayed_marker(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or "")) or "unknown"
    return paths.orche_home() / "sessions" / f"{safe}.relayed.json"


def _refusal_to_self(me, message):
    """Under stop_hook_active a refusal must not loop: log it, and park it in the session's own inbox
    when the session is identified (`me` None: hooks.log only). The parked note is `quiet`: it does not
    wake `me` (a woken Main re-emitting the bad block would loop) but is injected at its next prompt."""
    log.log("hooks", f"dispatch refused under stop_hook_active: {message}")
    if not me:
        return
    try:
        inbox.post(inbox.make("dispatch-refused", "orche", me, "question", summary=message, quiet=True))
    except ValueError as exc:
        log.log("hooks", f"dispatch: cannot park refusal for {me!r}: {exc}")


def handle_stop_failure(payload: dict) -> None:
    """StopFailure hook (a turn ended on an API error; Stop does not fire): re-arm the wake as Stop does.
    Claude Code ignores the output; only the side effect matters."""
    who = roster.Roster(registry.load()).identify(payload.get("cwd") or os.getcwd())
    if who.session:
        inbox.rearm(who.session)


def final_text(payload: dict):
    """(msg_id, text) of the turn's final assistant message.

    Claude Code writes the transcript asynchronously, so a Stop hook can see a lagging file; the
    hook input's `last_assistant_message` is authoritative. The transcript is parsed only when the
    field is absent (older Claude Code).
    """
    if "last_assistant_message" in payload:
        text = payload.get("last_assistant_message")
        return None, text if isinstance(text, str) else ""
    tpath = payload.get("transcript_path")
    return transcript.last_assistant(tpath) if tpath else (None, "")


def handle_stop(payload: dict, started=None):
    """(exit_code, stderr_message) for the Stop hook."""
    deadline = orca.hook_deadline(started)
    cwd = payload.get("cwd") or os.getcwd()
    tpath = payload.get("transcript_path")
    msg_id, text = final_text(payload)
    r = roster.Roster(registry.load())
    who = r.identify(cwd)
    me = who.session
    if me:
        units.touch_activity(me)
    s = brief.scan(text)
    # A reply whose dispatch openers yield no relayable block at all (an example inside another fence,
    # an unclosed earlier fence, …) is refused so Main learns why instead of believing it dispatched;
    # a well-formed block outside any fence is relayed even next to an example. ORCHE sessions only.
    unrelayed = s.unrelayed if me else None
    if s.bodies or unrelayed:
        marker = _relayed_marker(payload.get("session_id"))
        seen = util.read_json(marker, default={}) or {}
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if not (seen.get("text_hash") == text_hash or (msg_id is not None and seen.get("msg_id") == msg_id)):
            try:
                if unrelayed:
                    raise brief.Refusal(unrelayed)
                relay(r, list(s.bodies), check_sender(r, who), deadline)
            except brief.Refusal as exc:
                if not payload.get("stop_hook_active"):
                    return 2, str(exc)
                _refusal_to_self(me, str(exc))
            util.write_json(marker, {"text_hash": text_hash, "msg_id": msg_id, "at": util.now_iso()})
    code, msg = donegate.may_stop(text, r, who, cwd, tpath, deadline,
                                  stop_hook_active=bool(payload.get("stop_hook_active")))
    if code == 0 and me:
        inbox.rearm(me)   # reports that landed mid-turn: the turn is over, wake the pane now
    return code, msg
