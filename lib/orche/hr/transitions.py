"""Wake, sleep and stage change (SPEC §2.3, §2.5, §14.12).

Precondition of `wake` and `sleep`: `unit.owns_worktree` (the CLI routes Main to
`panes.project_pane`). Both return (data, line) or raise book.Refused(code, msg).
"""
import sys
import time

from .. import cli, orca, roster, units, util, vendors
from . import book, limits, panes

STALE_HANDOFF_SECONDS = 30 * 60
OPS_CHECKLIST = (
    "monitoring hook: wire uptime/error alerts to the ops inbox",
    "release ladder: preview -> staging -> prod, with a rollback step per rung",
    "backup verification schedule: restore-test the latest backup weekly",
    "runbooks: create docs/runbooks/ with one file per alert",
)


def _onboarding_message(unit) -> str:
    rerun = f"orche init {unit.project}" if unit.kind == "div" else f"orche feat new {unit.project} {unit.name}"
    return f"onboarding unfinished: re-run `{rerun}`"


def _refuse_retired_or_onboarding(unit) -> None:
    if unit.status == "retired":
        raise book.Refused(cli.EXIT_BLOCKED, f"{unit.session} is retired")
    if unit.status == "onboarding":
        raise book.Refused(cli.EXIT_BLOCKED, _onboarding_message(unit))


def _refuse_codex(reg, unit) -> None:
    """A Codex unit never gets a pane (wake and reopen alike): headless only (v1)."""
    if vendors.vendor_of(vendors.of(reg, unit)) == vendors.CODEX:
        raise book.Refused(cli.EXIT_BLOCKED, f"{unit.session} runs on codex: headless only (v1); "
                           "its briefs run through orche run")


def handoff_problem(unit):
    """None when HANDOFF.md was modified in the last 30 minutes, else the reason."""
    handoff = unit.memory / "HANDOFF.md"
    try:
        age = time.time() - handoff.stat().st_mtime
    except OSError:
        return f"missing: {handoff}"
    if age > STALE_HANDOFF_SECONDS:
        return f"modified {int(age // 60)} min ago: {handoff}"
    return None


def _record(reg, unit, status, verb, **fields) -> None:
    by = roster.actor()
    book.record(reg, f"{verb} {unit.session} by {by}",
                change=lambda r: book.entry_in(r, unit).update(fields, status=status),
                event={"type": verb, "project": unit.project, "unit": unit.event_unit, "by": by})


def sleep(reg, unit, *, dry_run=False, require_handoff=True) -> tuple:
    """Close a division's or feature's pane and mark it paused. Returns (data, line); raises Refused.

    Order: retired / onboarding (3) -> not active: "already <status>" -> HANDOFF.md under 30 min (3;
    `require_handoff=False` for `hr audit --apply` on units idle past the limit: their HANDOFF.md is
    necessarily old, the session itself stays resumable) -> close the pane titled with the session
    (1 on an Orca error) -> record.
    """
    _refuse_retired_or_onboarding(unit)
    if unit.status != "active":
        return ({"unit": unit.session, "status": unit.entry["status"], "changed": False},
                f"{unit.session} is already {unit.entry['status']}")
    problem = handoff_problem(unit) if require_handoff else None
    if problem:
        raise book.Refused(cli.EXIT_BLOCKED, f"HANDOFF.md is stale ({problem}); update it before sleeping")
    if dry_run:
        result = orca.terminal("close", "--terminal", f"<{unit.session}>", dry_run=True)
        return ({"unit": unit.session, "dry_run": True, "argv": result["argv"]},
                f"would sleep {unit.session} (dry-run)")
    try:
        term = orca.find_terminal_by_title(unit.session)
        if term is not None:
            orca.terminal("close", "--terminal", term["handle"])
    except orca.OrcaError as exc:
        raise book.Refused(cli.EXIT_USER, f"could not close the pane for {unit.session}: {exc}") from None
    _record(reg, unit, "paused", "sleep")
    return ({"unit": unit.session, "status": "paused", "pane_closed": term is not None},
            f"{unit.session} paused" + ("" if term is not None else " (no open pane)"))


def wake(reg, unit, *, dry_run=False) -> tuple:
    """Open a pane for a division or feature and mark it active. Returns (data, line); raises Refused.

    Order: retired / onboarding (3) -> active: reopen a missing pane (no record) -> stage gate (3) ->
    awake limit (3, AwakeLimit) -> parent-division warning (feature) -> pane (`--resume`, `--name`
    for a dormant unit; 1 on an Orca error) -> last_activity + activity touch -> record.
    """
    _refuse_retired_or_onboarding(unit)
    _refuse_codex(reg, unit)
    if unit.status == "active" and unit.entry.get("pane") is False:
        return _pane_for_headless(reg, unit, dry_run=dry_run)
    if unit.status == "active":
        return _reopen(unit, dry_run=dry_run)
    limits.unit_gate(reg, unit)
    limits.need_awake(reg)
    if unit.kind == "feat":
        parent = unit.division
        parent_status = reg["projects"][unit.project]["divisions"][parent].get("status")
        if parent_status != "active":
            print(f"orche: warning: parent division {parent} is {parent_status}", file=sys.stderr)
    argv = panes.unit_argv(unit, resume=unit.status != "dormant")
    if dry_run:
        result = orca.terminal("create", *argv, dry_run=True)
        return ({"unit": unit.session, "dry_run": True, "argv": result["argv"]},
                f"would wake {unit.session} (dry-run)")
    try:
        orca.terminal("create", *argv)
    except orca.OrcaError as exc:
        raise book.Refused(cli.EXIT_USER, f"could not open a pane for {unit.session}: {exc}") from None
    units.touch_activity(unit.session)
    _record(reg, unit, "active", "wake", last_activity=util.now_iso())
    return {"unit": unit.session, "status": "active"}, f"{unit.session} active"


def _pane_for_headless(reg, unit, *, dry_run=False) -> tuple:
    """An `active` feature created headless (`pane: false`): open its pane (awake limit applies, 3),
    then it counts as awake like any other (`pane` dropped). No record: the status stays `active`."""
    limits.need_awake(reg)
    argv = panes.unit_argv(unit, resume=True)
    if dry_run:
        result = orca.terminal("create", *argv, dry_run=True)
        return ({"unit": unit.session, "dry_run": True, "argv": result["argv"]},
                f"would open a pane for {unit.session} (dry-run)")
    try:
        orca.terminal("create", *argv)
    except orca.OrcaError as exc:
        raise book.Refused(cli.EXIT_USER, f"could not open a pane for {unit.session}: {exc}") from None
    now = util.now_iso()

    def change(r):
        entry = book.entry_in(r, unit)
        entry.pop("pane", None)
        entry["last_activity"] = now
    book.write(reg, change)
    return {"unit": unit.session, "status": "active", "pane": True}, f"{unit.session} active (pane opened)"


def _reopen(unit, *, dry_run=False) -> tuple:
    """An `active` unit: keep "already active" while a live pane carries its title, else reopen one.

    A closed pane would otherwise leave the unit stuck (sleep wants a fresh HANDOFF.md, wake said
    "already active"). The pane found by title is verified by handle (`panes.live_pane`): one Orca
    still lists right after it was closed does not count. The pane resumes the session; the status
    stays `active`, so the awake limit and stage gate do not apply. The lookup is read-only and also
    runs under --dry-run.
    """
    try:
        term = orca.find_terminal_by_title(unit.session)
        if term is not None:
            term = panes.live_pane(term, unit.session)
    except orca.OrcaError as exc:
        raise book.Refused(cli.EXIT_USER, f"could not list panes for {unit.session}: {exc}") from None
    if term is not None:
        return ({"unit": unit.session, "status": "active", "changed": False},
                f"{unit.session} is already active")
    argv = panes.unit_argv(unit, resume=True)
    if dry_run:
        result = orca.terminal("create", *argv, dry_run=True)
        return ({"unit": unit.session, "dry_run": True, "argv": result["argv"]},
                f"would reopen {unit.session} pane (dry-run)")
    try:
        orca.terminal("create", *argv)
    except orca.OrcaError as exc:
        raise book.Refused(cli.EXIT_USER, f"could not reopen a pane for {unit.session}: {exc}") from None
    units.touch_activity(unit.session)
    book.log(f"reopen {unit.session} pane by {roster.actor()}")
    return ({"unit": unit.session, "status": "active", "changed": False, "pane_reopened": True},
            f"{unit.session} pane reopened")


def set_stage(reg, key, stage, *, wake_ops=True, dry_run=False) -> tuple:
    """`orche stage set` (R-STAGE-4/5). Returns (data, lines); never raises Refused: a refused ops
    wake or sleep becomes a note and the stage change is kept.

    public:   stage -> `stage` event -> wake `ops` (unless wake_ops=False; an AwakeLimit refusal is
              also a warning) -> the ops checklist -> notification.
    building: stage -> `stage` event -> sleep an active `ops` (HANDOFF.md must be fresh; a refusal
              is a warning).
    """
    proj = reg["projects"][key]
    changed, by = proj["stage"] != stage, roster.actor()
    if changed and not dry_run:
        book.record(reg, f"stage {key} -> {stage} by {by}",
                    change=lambda r: r["projects"][key].update(stage=stage),
                    event={"type": "stage", "project": key, "unit": stage, "by": by})
    woke, checklist, notes = False, [], []
    if stage == "public":
        checklist = list(OPS_CHECKLIST)
        if wake_ops:
            if dry_run and changed:
                proj["stage"] = "public"   # local copy only: lets the dry-run show the ops wake
            woke, note = _wake_ops(reg, proj, dry_run=dry_run)
            notes.append(note)
        if changed and not dry_run:
            book.notify(f"{key} is public" + (" — ops woken" if woke else ""), key)
    else:
        note = _sleep_ops(reg, proj, dry_run=dry_run)
        if note:
            notes.append(note)
    verb = "would set" if dry_run else ("set" if changed else "already")
    data = {"project": key, "stage": stage, "changed": changed and not dry_run, "woke": woke,
            "checklist": checklist, "dry_run": dry_run}
    return data, [f"{verb} {key} stage={stage}"] + notes + [f"ops checklist: {c}" for c in checklist]


def _wake_ops(reg, proj, *, dry_run) -> tuple:
    """(woke?, note). Missing, active or retired ops is a note; so is a refused wake."""
    ops = proj["divisions"].get("ops")
    if ops is None:
        return False, "no ops division"
    unit = roster.Roster(reg).member(ops["session"]) if ops.get("session") else None
    if unit is None:
        return False, "ops has no session yet"
    if unit.status in ("active", "retired"):
        return False, f"ops is {unit.status}"
    if vendors.vendor_of(vendors.of(reg, unit)) == vendors.CODEX:
        return False, "ops runs on codex (headless only); not woken"
    try:
        _, line = wake(reg, unit, dry_run=dry_run)
    except limits.AwakeLimit as exc:
        if not dry_run:
            cli.warn(f"ops not woken: {exc.msg}")
        return False, exc.msg
    except book.Refused as exc:
        return False, exc.msg
    return not dry_run, line


def _sleep_ops(reg, proj, *, dry_run):
    """The note of putting an active ops back to sleep after a revert to building, or None."""
    ops = proj["divisions"].get("ops")
    unit = roster.Roster(reg).member(ops["session"]) if ops and ops.get("session") else None
    if unit is None or unit.status != "active":
        return None
    try:
        _, line = sleep(reg, unit, dry_run=dry_run, require_handoff=True)
    except book.Refused as exc:
        cli.warn(f"ops not slept: {exc.msg}")
        return f"ops not slept: {exc.msg}"
    return line
