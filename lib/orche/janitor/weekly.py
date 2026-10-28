"""The janitor's weekly pass (SPEC §11.4): part of `orche janitor run` once per WEEK (`--weekly`: now),
dated in the janitor's state file (`weekly.last`). Per registered project:

1. The audit's mistake classes (hr/mistakes.py, the last AUDIT_DAYS days). Classes not yet in
   STANDING.md's Enforcement table are written there and Main gets one `info` pointer (POINTER);
   nothing when there is none.
2. The verify skill's drift check (SPEC §5.4): a project with `.claude/skills/verify-<app>/SKILL.md`
   gets one unit to Build (DRIFT, through the dispatch relay with Main as the sender and the janitor
   as the relayer, the way triage relays), one app per week in turn; never while the last drift unit
   (`weekly.drift.<project>`) is still open. No skill, no unit.

The pass changes nothing in the repository but STANDING.md; a failure is a line in the output.
"""
import datetime as _dt
from pathlib import Path

from .. import brief, dispatch, inbox, log, roster, units
from ..flow import lineage

WEEK = _dt.timedelta(days=7)
SENDER = "janitor"
SKILLS = Path(".claude") / "skills"
POINTER = 'audit: {n} new mistake classes in STANDING.md Enforcement; say "enforce <class>" to dispatch it'
DRIFT = {   # the drift unit's brief; {app} is the skill's app name
    "TO": "build",
    "GOAL": "Re-verify the verify-{app} feature map against the current app",
    "SCOPE": ".claude/skills/verify-{app}/**",
    "CONTEXT": "The weekly drift check of the verify-{app} skill (SPEC 5.4), filed by the janitor.\n"
               "Run the skill once as it stands. Where the app changed, correct the feature map.\n"
               "Where the product regressed, report it; do not change the map to hide it.",
    "ACCEPT": "One live pass of the skill; map corrections only; product regressions reported, not papered over",
    "VERIFY": "The skill's own Doctor and Drive steps (.claude/skills/verify-{app}/SKILL.md)",
    "TIMEBOX": "60 min",
    "FORBID": "Editing product code; deleting evidence; more than one PR",
    "REPORT": "At most 15 lines: the pass result, the map corrections, the regressions found",
    "TIER": "mechanical",
}


def last(state):
    """When the weekly pass last ran (aware datetime), or None."""
    return units.parse_iso((state.get("weekly") or {}).get("last"))


def due(state, now) -> bool:
    at = last(state)
    return at is None or now - at >= WEEK


def apps(root) -> list:
    """The app names of the repository's verify skills, sorted."""
    base = Path(root or ".") / SKILLS
    return sorted(p.parent.name[len("verify-"):] for p in base.glob("verify-*/SKILL.md")) if root else []


def is_open(reg, key, unit) -> bool:
    """Whether `unit` of `key` has a brief and is not done, failed or stopped."""
    b = brief.find(reg, unit, project=key)
    if b is None or not b.rev:
        return False
    return any(m.unit == unit and s not in lineage.TERMINAL for m, _f, s in lineage.members(reg, key, b.lineage))


def audit(reg, key, proj, now, r) -> str | None:
    from ..hr import mistakes      # hr imports janitor: loaded here, not at import
    found = mistakes.classes(key, proj, now)
    known = mistakes.table(proj)
    new = [c for c in found if c["key"] not in known]
    if not new:
        return None
    mistakes.apply(key, proj, found)
    main = r.main(key)
    if main is None:
        return f"{key}: {len(new)} new mistake classes in STANDING.md Enforcement (no Main to tell)"
    inbox.post(inbox.make("hr-audit", SENDER, main.session, "info", brief_rev=0, summary=POINTER.format(n=len(new))))
    return f"{key}: {len(new)} new mistake classes in STANDING.md Enforcement; Main told"


def drift(reg, key, proj, r, drifts) -> str | None:
    names = apps(proj.get("path"))
    if not names:
        return None
    prev = drifts.get(key) or {}
    if prev.get("unit") and is_open(reg, key, prev["unit"]):
        return f"{key}: drift unit {prev['unit']} is still open; none filed"
    app = names[(names.index(prev["app"]) + 1) % len(names)] if prev.get("app") in names else names[0]
    main = r.main(key)
    if main is None:
        return f"{key}: no Main to send the verify-{app} drift unit"
    fields = {k: v.format(app=app) for k, v in DRIFT.items()}
    out = dispatch.relay_outcome(r, [brief.Block("brief", fields)], main, by=SENDER)
    if out.refused:
        return f"{key}: verify-{app} drift unit refused: {out.refused[0].removeprefix(brief.REFUSE)}"
    unit = out.done[0]["unit"]
    drifts[key] = {"unit": unit, "app": app}
    return f"{key}: verify-{app} drift unit {unit} filed to {out.done[0]['to']}"


def run(reg, state, *, now=None, force=False) -> list:
    """The weekly pass when due (or `force`): its lines; `state` (the janitor's) is updated, not saved."""
    now = now or units.now()
    if not force and not due(state, now):
        return []
    weekly = state.setdefault("weekly", {})
    drifts = weekly.setdefault("drift", {})
    r = roster.Roster(reg)
    lines = []
    for key, proj in sorted(reg.get("projects", {}).items()):
        for step in (lambda: audit(reg, key, proj, now, r), lambda: drift(reg, key, proj, r, drifts)):
            try:
                line = step()
            except Exception as exc:  # noqa: BLE001 - one project never stops the pass
                line = f"{key}: weekly pass failed: {type(exc).__name__}: {exc}"
            if line:
                lines.append(line)
    weekly["last"] = units.iso(now)
    log.log("janitor", f"weekly pass: {len(lines)} lines")
    return [f"weekly pass ({now.date().isoformat()}):", *(f"  {ln}" for ln in lines or ["nothing to do"])]
