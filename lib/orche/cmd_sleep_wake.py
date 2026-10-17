"""`orche sleep <unit>` / `orche wake <unit>` for division and feature-agent sessions.

sleep: HANDOFF.md must be fresh (< 30 min) -> close the pane -> status `paused` -> event.
wake:  respect `limits.max_awake_sessions` -> open a pane running `claude --resume <session>`
       (`claude --name <session>` for a never-started dormant unit) -> status `active` -> event. Both honor --dry-run (orca argv printed, nothing mutated).
"""
import json
import shlex
import sys
import time

from . import config, orca, registry, statusline, units, util

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3


def register(subparsers):
    for name, func, text in (("sleep", cmd_sleep, "close a unit's pane and mark it paused"),
                             ("wake", cmd_wake, "resume a unit's session in a new pane")):
        p = subparsers.add_parser(name, help=text)
        p.add_argument("unit", help="div-<proj>-<division> or feat-<proj>-<feature>")
        p.set_defaults(func=func)


def _out(args, data, line):
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else line)


def _fail(code, msg):
    print(f"orche: {msg}", file=sys.stderr)
    return code


def _resolve(reg, name):
    unit = units.find_unit(reg, name)
    if unit is None:
        return None, _fail(EXIT_NOT_FOUND, f"unknown unit: {name} (expected div-<proj>-<division> "
                                           "or feat-<proj>-<feature>)")
    if unit.entry.get("status") == "retired":
        return None, _fail(EXIT_BLOCKED, f"{name} is retired")
    if unit.entry.get("status") == "onboarding":
        return None, _fail(EXIT_BLOCKED, onboarding_message(unit))
    return unit, None


def onboarding_message(unit) -> str:
    rerun = f"orche init {unit.project}" if unit.kind == "div" else f"orche feat new {unit.project} {unit.name}"
    return f"onboarding unfinished: re-run `{rerun}`"


def handoff_problem(unit):
    """None when HANDOFF.md was modified in the last 30 minutes, else the reason."""
    try:
        age = time.time() - unit.handoff.stat().st_mtime
    except OSError:
        return f"missing: {unit.handoff}"
    if age > units.STALE_HANDOFF_SECONDS:
        return f"modified {int(age // 60)} min ago: {unit.handoff}"
    return None


def _record(reg, unit, status, event):
    unit.entry["status"] = status
    registry.append_event(reg, type=event, project=unit.project, unit=unit.event_unit, by=units.actor())
    registry.save(reg)
    units.hr_log(f"{event} {unit.session} by {units.actor()}")
    statusline.refresh(reg, unit.project)


class Blocked(Exception):
    """A sleep/wake refused by policy or a failed Orca call: `code` is the exit code."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def sleep_unit(reg, unit, *, dry_run=False, require_handoff=True) -> tuple:
    """Close the unit's pane and mark it paused. Returns (data, line); raises Blocked.

    `require_handoff=False` is for `hr audit --apply` on units idle past the limit (their
    HANDOFF.md is necessarily old; the session itself stays resumable).
    """
    if unit.entry.get("status") != "active":
        return ({"unit": unit.session, "status": unit.entry["status"], "changed": False},
                f"{unit.session} is already {unit.entry['status']}")
    problem = handoff_problem(unit) if require_handoff else None
    if problem:
        raise Blocked(EXIT_BLOCKED, f"HANDOFF.md is stale ({problem}); update it before sleeping")
    if dry_run:
        result = orca.terminal("close", "--terminal", f"<{unit.session}>", dry_run=True)
        return ({"unit": unit.session, "dry_run": True, "argv": result["argv"]},
                f"would sleep {unit.session} (dry-run)")
    try:
        term = orca.find_terminal_by_title(unit.session)
        if term is not None:
            orca.terminal("close", "--terminal", term["handle"])
    except orca.OrcaError as exc:
        raise Blocked(EXIT_USER, f"could not close the pane for {unit.session}: {exc}") from None
    _record(reg, unit, "paused", "sleep")
    return ({"unit": unit.session, "status": "paused", "pane_closed": term is not None},
            f"{unit.session} paused" + ("" if term is not None else " (no open pane)"))


def session_env(session) -> str:
    """`ORCHE_SESSION=<session> ` prefix so hooks know the pane's role without guessing from cwd."""
    return f"ORCHE_SESSION={shlex.quote(session)} "


def pane_command(session, *, resume) -> str:
    """`claude --resume <session>` for a paused unit, `claude --name <session>` for a fresh start.

    Prefixed with `ORCHE_SESSION=<session> `."""
    flag = "--resume" if resume else "--name"
    return f"{session_env(session)}{config.load().get('claude_bin', 'claude')} {flag} {session}"


def wake_unit(reg, unit, *, dry_run=False) -> tuple:
    """Open a pane for the unit and mark it active. Returns (data, line); raises Blocked.

    A `paused` unit resumes its session; a `dormant` one (never started) starts a named session.
    """
    if unit.entry.get("status") == "active":
        return ({"unit": unit.session, "status": "active", "changed": False},
                f"{unit.session} is already active")
    if unit.entry.get("status") == "onboarding":
        raise Blocked(EXIT_BLOCKED, onboarding_message(unit))
    stage = reg["projects"][unit.project].get("stage")
    if unit.entry.get("gated_by_stage") and stage == "building":
        raise Blocked(EXIT_BLOCKED, f"{unit.session} is gated by stage and {unit.project} is building "
                                    f"(run: orche stage set {unit.project} public --yes)")
    awake, limit = len(registry.awake_sessions(reg)), reg["limits"]["max_awake_sessions"]
    if awake + 1 > limit:
        raise Blocked(EXIT_BLOCKED, f"awake limit reached ({awake}/{limit}); sleep a unit first")
    if unit.kind == "feat":
        parent = unit.event_unit.split("/", 1)[0]
        parent_status = reg["projects"][unit.project]["divisions"][parent].get("status")
        if parent_status != "active":
            print(f"orche: warning: parent division {parent} is {parent_status}", file=sys.stderr)
    command = pane_command(unit.session, resume=unit.entry.get("status") != "dormant")
    argv = ("--worktree", f"id:{unit.entry['worktree']}", "--title", unit.session, "--command", command)
    if dry_run:
        result = orca.terminal("create", *argv, dry_run=True)
        return ({"unit": unit.session, "dry_run": True, "argv": result["argv"]},
                f"would wake {unit.session} (dry-run)")
    try:
        orca.terminal("create", *argv)
    except orca.OrcaError as exc:
        raise Blocked(EXIT_USER, f"could not open a pane for {unit.session}: {exc}") from None
    unit.entry["last_activity"] = util.now_iso()
    units.touch_activity(unit.session)
    _record(reg, unit, "active", "wake")
    return {"unit": unit.session, "status": "active"}, f"{unit.session} active"


def run_unit(args, action, name) -> int:
    """CLI glue shared by `orche sleep|wake` and `orche feat sleep|wake`."""
    reg = registry.load()
    unit, err = _resolve(reg, name)
    if unit is None:
        return err
    try:
        data, line = action(reg, unit, dry_run=args.dry_run)
    except Blocked as exc:
        return _fail(exc.code, exc.msg)
    _out(args, data, line)
    return EXIT_OK


def cmd_sleep(args):
    return run_unit(args, sleep_unit, args.unit)


def cmd_wake(args):
    return run_unit(args, wake_unit, args.unit)
