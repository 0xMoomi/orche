"""`orche stage set <proj> public|building [--yes] [--no-wake]`.

public:   stage -> `stage` event -> wake `ops` (unless --no-wake; the awake limit applies) ->
          print the ops checklist -> hr-log + notify.
building: stage -> `stage` event -> sleep an active `ops` (R-STAGE-5; HANDOFF.md must be fresh —
          a refusal is reported and the stage change is kept). Ops cannot be woken while building.
The real run needs --yes (going public opens a pane).
"""
from . import cmd_hr, cmd_sleep_wake, registry, units
from .hr import EXIT_NOT_FOUND, EXIT_OK, EXIT_USER, fail, out, refresh_status_line, warn

OPS_CHECKLIST = (
    "monitoring hook: wire uptime/error alerts to the ops inbox",
    "release ladder: preview -> staging -> prod, with a rollback step per rung",
    "backup verification schedule: restore-test the latest backup weekly",
    "runbooks: create docs/runbooks/ with one file per alert",
)


def register(subparsers):
    stage = subparsers.add_parser("stage", help="project stage (building | public)")
    sub = stage.add_subparsers(dest="stage_command", metavar="<stage-command>", required=True)
    p = sub.add_parser("set", help="set a project's stage")
    p.add_argument("project")
    p.add_argument("stage", choices=registry.STAGES)
    p.add_argument("--yes", action="store_true", help="confirm the real run")
    p.add_argument("--no-wake", action="store_true", help="do not wake ops when going public")
    p.set_defaults(func=cmd_set)


def _wake_ops(reg, key, proj, *, dry_run):
    """(woke?, message). Missing/active/retired ops is reported, not an error."""
    ops = proj["divisions"].get("ops")
    if ops is None:
        return False, "no ops division"
    unit = units.find_unit(reg, ops["session"]) if ops.get("session") else None
    if unit is None:
        return False, "ops has no session yet"
    if unit.entry.get("status") == "onboarding":
        return False, "ops is still onboarding; re-run orche init"
    if unit.entry.get("status") in ("active", "retired"):
        return False, f"ops is {unit.entry['status']}"
    try:
        _, line = cmd_sleep_wake.wake_unit(reg, unit, dry_run=dry_run)
    except cmd_sleep_wake.Blocked as exc:
        return False, exc.msg
    return not dry_run, line


def _sleep_ops(reg, proj, *, dry_run):
    """(slept?, message) — return Ops to sleep after a revert to building (HANDOFF check applies)."""
    ops = proj["divisions"].get("ops")
    unit = units.find_unit(reg, ops["session"]) if ops and ops.get("session") else None
    if unit is None or unit.entry.get("status") != "active":
        return False, None
    try:
        _, line = cmd_sleep_wake.sleep_unit(reg, unit, dry_run=dry_run, require_handoff=True)
    except cmd_sleep_wake.Blocked as exc:
        return False, f"ops not slept: {exc.msg}"
    return not dry_run, line


def cmd_set(args):
    reg = registry.load()
    found = registry.project(reg, args.project)
    if found is None:
        return fail(EXIT_NOT_FOUND, f"unknown project: {args.project}")
    key, proj = found
    if not args.yes and not args.dry_run:
        return fail(EXIT_USER, f"setting {key} to {args.stage} has side effects; re-run with --yes")
    changed, by = proj["stage"] != args.stage, units.actor()
    if changed and not args.dry_run:
        proj["stage"] = args.stage
        registry.append_event(reg, type="stage", project=key, unit=args.stage, by=by)
        registry.save(reg)
        units.hr_log(f"stage {key} -> {args.stage} by {by}")
    woke, checklist, notes = False, [], []
    if args.stage == "public":
        checklist = list(OPS_CHECKLIST)
        if not args.no_wake:
            if args.dry_run and changed:
                proj["stage"] = "public"   # local copy only: lets the dry-run show the ops wake
            woke, note = _wake_ops(reg, key, proj, dry_run=args.dry_run)
            notes.append(note)
            if not woke and not args.dry_run and note.startswith("awake limit"):
                warn(f"ops not woken: {note}")
        if changed and not args.dry_run:
            cmd_hr.notify(f"{key} is public" + (" — ops woken" if woke else ""))
    else:
        slept, note = _sleep_ops(reg, proj, dry_run=args.dry_run)
        if note:
            notes.append(note)
            if note.startswith("ops not slept"):
                warn(note)
    if not args.dry_run:
        refresh_status_line(registry.load(), key)
    verb = "would set" if args.dry_run else ("set" if changed else "already")
    lines = [f"{verb} {key} stage={args.stage}"] + notes + [f"ops checklist: {c}" for c in checklist]
    out(args, {"project": key, "stage": args.stage, "changed": changed and not args.dry_run, "woke": woke,
               "checklist": checklist, "dry_run": args.dry_run}, lines)
    return EXIT_OK
