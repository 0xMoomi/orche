#!/usr/bin/env python3
"""UserPromptSubmit hook: triage a Main prompt, then inject the session's pending pointers.

Triage (lib/orche/triage.py, off unless `triage.enabled`) may route a simple prompt straight to a
division and print `{"decision": "block", ...}` so Main spends no turn; otherwise the prompt reaches
Main and the inbox text (plus any triage note) is printed and injected. Fail-open.
"""
import json
import os
import sys
import time

STARTED = time.monotonic()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from orche import pyguard  # noqa: E402  (before any other orche module)

if not pyguard.check_hook("report_inject"):
    sys.exit(0)  # fail-open: the hook does nothing, a gate allows


def _kick():
    """Start a reconcile pass when the heartbeat is stale (reconcile.kick); never fails the hook."""
    try:
        from orche import reconcile
        reconcile.kick()
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"reconcile kick: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass


BARE_DRAIN = r"\s*(\[drain\]|/drain)\s*"


def _owner_seen(payload, who):
    """The owner typed in Main: restart the project's autonomy window (lib/orche/flow/guard.py) and
    answer the project's open ask (lib/orche/flow/owner.py: no more re-notifications). ORCHE's own
    prompts (the inbox wake, the report-due reminder) and a bare drain do not count."""
    try:
        import re
        from orche import deliver, inbox
        from orche.flow import guard, owner
        m = who.member
        prompt = str(payload.get("prompt") or "")
        if (m is None or m.kind != "main" or inbox.NUDGE_RE.match(prompt) or deliver.REMIND_RE.match(prompt)
                or re.fullmatch(BARE_DRAIN, prompt, re.IGNORECASE)):
            return
        guard.owner_seen(m.project)
        owner.answer(m.project)
    except Exception as exc:  # noqa: BLE001 - fail-open
        from orche import log
        log.log("hooks", f"report_inject: owner-seen: {type(exc).__name__}: {exc}")


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("agent_id"):
            return 0
        from orche import inbox, registry, roster, triage
        r = roster.Roster(registry.load())
        who = r.identify(payload.get("cwd") or os.getcwd())
        _owner_seen(payload, who)          # before triage: a routed prompt is still the owner's word
        outcome = triage.handle(payload, STARTED, r, who)   # never raises
        if outcome.block is not None:
            print(json.dumps(outcome.block, ensure_ascii=False))
            if who.session:
                inbox.unmark(who.session)  # every prompt removes the wake marker
                inbox.rearm(who.session)   # Main spends no turn: pending reports still need their wake
            _kick()
            return 0
        # The hook budget runs from STARTED, less the time a classifier call was in flight.
        text = inbox.handle_prompt(payload, STARTED + outcome.model_s, r, who)
        text = "\n\n".join(t for t in (outcome.note, text) if t)
        _kick()
    except Exception as exc:  # noqa: BLE001 - fail-open
        try:
            from orche import log
            log.log("hooks", f"report_inject: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
