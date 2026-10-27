"""Dispatch relay: route brief / AMEND / STOP blocks (lib/orche/brief.py parses and stores them) and
deliver them. `orche dispatch` (cmd_dispatch) and triage are the callers.

The Stop hook relays nothing; its stop decision (the done gate, then the night predicate) is
donegate.may_stop.
"""
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import (brief, config, deliver, donegate, inbox, log, mask, orca, registry, roster, runs, transcript,
               units, util, vendors)
from .flow import guard, lineage, owner

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


ASLEEP = ("dormant", "paused")


def _check_reachable(r, t):
    """Refuse a target the brief can never reach: asleep and stage-gated (the rule `orche wake` applies,
    hr.limits.stage_gated), or a division/feature whose recorded worktree is gone."""
    if t.status in ASLEEP:
        from .hr import limits
        if limits.stage_gated(r.reg, t):
            stage = r.reg["projects"][t.project].get("stage")
            raise brief.Refusal(f"{brief.REFUSE}{t.session} is asleep and gated by stage ({t.project} is {stage}): "
                                f"re-target, or run orche stage set {t.project} public --yes")
    wt = t.worktree
    if wt is not None and not wt.exists():
        raise brief.Refusal(f"{brief.REFUSE}{t.session}'s worktree {wt} is missing: re-target, or recreate it.")


def _check_pin(r, t, block, cfg) -> None:
    """Refuse a MODEL pin the target division cannot run (ladder.pin_error): another vendor, a model
    priced above the division's, or Codex while it is unavailable."""
    pin = str(block.fields.get("MODEL") or "").strip()
    if not pin:
        return
    from . import ladder
    err = ladder.pin_error(cfg, pin, vendors.of(r.reg, t) if t.owns_worktree else "claude-opus-5-5", t.session)
    if err:
        raise brief.Refusal(f"{brief.REFUSE}{err}")


def plan_blocks(r, bodies, frm) -> list:
    """Parse + resolve every block before any write: one Plan per block, or its refusal text.

    `bodies` are block texts or already-built `brief.Block`s (triage passes a `brief.Block`, so its
    prompt text is never parsed). `frm` is the sending Member.

    Targets must belong to the sender's project (R-XPROJ): a brief whose TO resolves to another
    project's Main becomes an external-request pointer to it; any other cross-project target is refused.
    A retired target is refused; so are an asleep stage-gated target and a missing worktree (not for
    STOP). AMEND/STOP need the unit's BRIEF.md.
    """
    plans = []
    cfg = config.load()
    accepted = 0        # briefs and AMENDs planned so far (G3 counts them before they are written)

    def guarded(block, to, runner=False):
        return guard.safe(guard.check, frm.session, block.unit, r.reg, block, frm, to=to, runner=runner,
                          accepted=accepted)

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
                    hit = guarded(block, t.session)
                    plans.append(hit if hit is not None else Plan(block, t.session, t.project, external=True))
                    continue
                _check_reachable(r, t)
                _check_pin(r, t, block, cfg)
                hit = guarded(block, t.session)
                plans.append(hit if hit is not None else Plan(block, t.session, t.project))
                accepted += hit is None
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
            if block.kind != "stop":
                _check_reachable(r, t)
                _check_pin(r, t, block, cfg)
            hit = guarded(block, t.session, block.kind == "amend" and deliver.runner_wanted(r, t.session, cfg))
            plans.append(hit if hit is not None else Plan(block, t.session, prior.project, prior, block.unit))
            accepted += hit is None and block.kind != "stop"
        except brief.Refusal as exc:
            plans.append(str(exc))
    return plans


def body_hash(body) -> str | None:
    """A block text's identity for `orche dispatch`'s delivered map (None for a built Block)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] if isinstance(body, str) else None


def _partial_refusal(lines, refused, total):
    """One Refusal for a dispatch whose blocks were not all delivered. A single block keeps its bare reason."""
    if total == 1:
        return brief.Refusal(refused[0])
    if len(refused) == total:
        head = (f"{brief.REFUSE}all {total} blocks refused; no block was delivered. Fix them and "
                "send them again.")
        return brief.Refusal("\n".join([head, *lines]))
    head = (f"{brief.REFUSE}{len(refused)} of {total} blocks refused. Re-send ONLY the refused "
            f"block{'s' if len(refused) > 1 else ''}; the delivered ones went out and must not be sent again.")
    return brief.Refusal("\n".join([head, *lines]))


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


SANDBOX, DEFERRED, DEFERRED_TEXT = runs.SANDBOX, runs.DEFERRED, runs.DEFERRED_TEXT


def _defer(plan, b, status, summary, frm, terms) -> None:
    """A run started from inside a Codex sandbox would inherit it (its `claude -p` could not write its
    own worktree): write the pane path's dispatch pointer, marked `deferred` and dated `since_ns`, and
    leave the start to the heartbeat (reconcile retries it at its first pass through deliver.spawn,
    whose runner path honours a later STOP and restarts a live run of an older revision). An AMEND
    first stops the live run of the older revision (runs.stop: the marker, older than `since_ns`)."""
    if plan.block.kind == "amend":
        runs.stop(plan.proj, plan.unit, timeout=terms.remaining())
    inbox.write(inbox.make(plan.unit, frm, plan.to, status, report_path=str(b.path), brief_rev=b.rev,
                           summary=summary, via="dispatch", deferred="codex-sandbox", since_ns=time.time_ns()))
    log.log("events", f"dispatch deferred=codex-sandbox unit={plan.unit} rev={b.rev}")


def _deliver(r, plan, b, status, summary, frm, terms, cfg=None, by=None):
    """No Orca call (ruling 2). A runner unit (deliver.runner_wanted) is started, restarted or stopped
    here and gets no target pointer (ruling 6); an exception is `runner-failed`. A pane unit gets
    its pointer (`via: dispatch`, injected at the target's next prompt) and a detached
    `orche deliver <unit>` (`deliverer-started`; `deliverer-failed` when it cannot start), which
    types the inbox line. `b` is the unit's Brief (the one just written, or the prior one for a
    STOP). `by` names who relayed in the events line (default the sender; triage passes `triage`)."""
    rev = b.rev
    by = by or frm
    runner = deliver.runner_wanted(r, plan.to, cfg if cfg is not None else config.load())
    if runner and plan.block.kind != "stop" and runs.in_sandbox():
        _defer(plan, b, status, summary, frm, terms)
        state = DEFERRED
    elif runner:
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


def _skipped(frm, routed, by=None):
    """A block identical to one already delivered from this session (the `delivered` map, within
    DELIVERED_TTL): not relayed again; an events line and an `info` pointer to the sender say so."""
    unit, _, to = routed.partition(" → ")
    log.log("events", f"dispatch skipped by={by or frm} unit={unit or '-'} reason=already delivered")
    try:
        inbox.post(inbox.make(unit or "dispatch-skipped", "orche", frm, "info", brief_rev=0,
                              summary=f"skipped {routed}: already delivered (an identical block went out "
                                      f"in the last {DELIVERED_TTL // 60} min); not sent again"))
    except (ValueError, OSError) as exc:
        log.log("hooks", f"dispatch: cannot file skip feedback for {frm!r}: {exc}")


@dataclass
class Outcome:
    done: list          # {"unit", "to", "state", "rev"} per delivered block, in order
    lines: list         # one "block N: …" line per block, in order
    refused: list       # the refusal texts
    delivered: dict     # {body_hash: "<unit> → <to>"} for the blocks delivered now


def relay_outcome(r, bodies, frm, deadline=None, by=None, skip=None) -> Outcome:
    """Plan every block, then deliver each valid one; never raises for a refused block.

    `by` (e.g. `triage`) is who relayed: it names the events line's `by=` and words the sender's
    feedback pointer. `skip` ({body_hash: "<unit> → <to>"}) holds blocks already delivered from this
    session (`orche dispatch`'s delivered map): an identical block is not relayed again.
    `frm` must be the Member returned by `check_sender`; nothing here re-checks the sender's role.
    """
    sender, sender_proj = frm.session, frm.project
    skip = dict(skip or {})
    hashes = [body_hash(b) for b in bodies]
    fresh = [i for i, h in enumerate(hashes) if h is None or h not in skip]
    planned = dict(zip(fresh, plan_blocks(r, [bodies[i] for i in fresh], frm)))
    terms = orca.Terminals(deadline if deadline is not None else orca.hook_deadline())   # remaining() only
    cfg = config.load()
    flow_on = (cfg.get("flow") or {}).get("enabled", True)
    done, lines, refused, delivered, rules = [], [], [], {}, []
    line_of = None      # the lineage this call's first new brief started (Main and triage)
    counts = iter(guard.streak(sender, sender_proj, [not isinstance(planned[i], str) for i in fresh]))
    for i in range(len(bodies)):
        plan = planned.get(i)
        if plan is None:
            _skipped(sender, skip[hashes[i]], by)
            lines.append(f"block {i + 1}: skipped — identical to {skip[hashes[i]]}, delivered in the last "
                         f"{DELIVERED_TTL // 60} min; not sent again")
            continue
        if isinstance(plan, str):
            n = next(counts)
            if isinstance(plan, guard.Refused):
                guard.logged(plan.rule, sender, plan.unit, plan.n)
                rules.append(plan.rule)
            if flow_on and n >= guard.STREAK:
                guard.logged("G7", sender, getattr(plan, "unit", "-"), n)
                plan = f"{plan} ({guard.STREAK_TEXT.format(n=n)})"
            refused.append(plan)
            lines.append(f"block {i + 1}: refused — {plan.removeprefix(brief.REFUSE)}")
            continue
        next(counts)
        block = plan.block
        if plan.external:
            rev = 0
            state = _external(plan, sender, sender_proj)
            _feedback(sender, plan.unit, plan.to, rev, state)
        elif block.kind == "stop":
            rev = plan.prior.rev
            state = _deliver(r, plan, plan.prior, "stop", block.note or "STOP requested", sender, terms, cfg, by)
            _feedback(sender, plan.unit, plan.to, rev, state, kind="stop", by=by)
        else:
            lin, parent = lineage.assign(r.reg, frm, by) if block.kind == "brief" else (None, None)
            lin = lin or line_of
            b = brief.write(block, r.member(plan.to), sender, standing_text(r.reg, plan.proj),
                            prior=plan.prior, timeout=min(0.5, terms.remaining()), lineage=lin, parent=parent,
                            model=vendors.of(r.reg, r.member(plan.to)))
            if block.kind == "brief" and frm.kind != "div" and line_of is None:
                line_of = b.lineage
                if by not in lineage.AUTOMATED:
                    lineage.started(frm, b.lineage)
            plan.unit, rev = b.unit, b.rev
            goal = mask.redact(b.fields.get("GOAL", "").split("\n")[0])[0]
            state = _deliver(r, plan, b, "info", goal, sender, terms, cfg, by)
            _feedback(sender, plan.unit, plan.to, rev, state, by=by)
        done.append({"unit": plan.unit, "to": plan.to, "state": state, "rev": rev})
        lines.append(f"block {i + 1}: delivered {plan.unit} → {plan.to} (rev {rev}, {state})"
                     + (f" — {DEFERRED_TEXT}" if state == DEFERRED else ""))
        if hashes[i] is not None:
            delivered[hashes[i]] = f"{plan.unit} → {plan.to}"
    if refused:
        log_refusal(sender, len(bodies), len(refused), rules)
    return Outcome(done, lines, refused, delivered)


def relay_detail(r, bodies, frm, deadline=None, by=None) -> list:
    """relay_outcome(), returning `{"unit", "to", "state", "rev"}` per delivered block; when any block
    is refused, one Refusal lists each block in order (the valid ones were still delivered)."""
    o = relay_outcome(r, bodies, frm, deadline, by)
    if o.refused:
        raise _partial_refusal(o.lines, o.refused, len(bodies))
    return o.done


DELIVERED_TTL = 600     # seconds `orche dispatch` remembers a delivered block (no identical re-send)


def log_refusal(me, blocks, refused, rules=()):
    """The events line for a dispatch with refused blocks: ids and counts only, never the text."""
    rules = ",".join(dict.fromkeys(rules)) or "-"
    log.log("events", f"dispatch refused by={me or 'unknown'} blocks={blocks} refused={refused} rules={rules}")


# ---- Stop hook entry -----------------------------------------------------


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
    """(exit_code, stderr_message) for the Stop hook. It relays nothing: briefs go out through
    `orche dispatch`, and a fenced block left in the reply is plain text. A Main session's turn that
    may stop (also under stop_hook_active) hands its closing line to flow.owner (the owner's voice)."""
    deadline = orca.hook_deadline(started)
    cwd = payload.get("cwd") or os.getcwd()
    tpath = payload.get("transcript_path")
    _msg_id, text = final_text(payload)
    r = roster.Roster(registry.load())
    who = r.identify(cwd)
    me = who.session
    if me:
        units.touch_activity(me)
    code, msg = donegate.may_stop(text, r, who, cwd, tpath, deadline,
                                  stop_hook_active=bool(payload.get("stop_hook_active")))
    if code == 0 and who.member is not None and who.member.kind == "main":
        _closing(who.member.project, text, me)
    if code == 0 and me:
        inbox.rearm(me)   # reports that landed mid-turn: the turn is over, wake the pane now
    return code, msg


def _closing(project, text, session) -> None:
    """flow.owner.closing for Main's final message, with the turn's start (the prompt hook's turn record,
    lineage.read_turn): a tag covers the reports that turn read; never fails the hook (the type only: no text)."""
    try:
        turn = lineage.read_turn(session) if session else None
        owner.closing(project, text, units.now(), turn_at=units.parse_iso((turn or {}).get("at")))
    except Exception as exc:  # noqa: BLE001 - fail-open
        log.log("hooks", f"dispatch: closing line for {project}: {type(exc).__name__}")
