"""`orche install|uninstall|doctor|apply-all` — set up (and take down) ORCHE in the user's home.

install patches only: `~/.claude/settings.json`, `~/.claude/statusline-command.sh`,
`~/.claude/agents/<ours>.md` (symlinks), `~/.claude/skills/<ours>` (symlinks, settings mode only;
a plugin-mode install removes them), `~/.claude/fable/{fable,active}.md`,
`~/.claude/fable/agents/runner.md`, and moves `~/.codex/AGENTS.md.recovered` into place
(`--yes`). Every changed file is first copied to `<file>.bak-<ts>`; `--dry-run` prints the plan
and writes nothing. `--home PATH` targets another home (tests); ORCHE_HOME then defaults to
`PATH/.claude/orche` unless set.

The first install on a terminal also asks once for an optional Jev key (`jev_setup`); `--no-jev`
skips it, `--jev-key-stdin [--jev-kind gateway|direct]` configures it without prompts. A missing
or failing Jev never fails the install.
"""
import os
from pathlib import Path

from . import jev_setup, paths, util
from .cli import EXIT_OK, EXIT_USER, out
from .install import MIGRATION_LINE, Ctx, backup, agents, apply_all, codex, doctor, fable, settings, skills, statusline

STATE_FILE = "install-state.json"
LEGACY_STATE_FILE = "install.json"  # read (and migrated) for one version


def register(subparsers):
    p = subparsers.add_parser("install", help="patch user settings, status line, agents and Fable rules")
    p.add_argument("--home", help="home directory to patch (default: $HOME)")
    p.add_argument("--mode", choices=("plugin", "settings"), default="plugin",
                   help="plugin: print /plugin install steps (default); settings: register hooks in settings.json")
    p.add_argument("--yes", action="store_true", help="also move ~/.codex/AGENTS.md.recovered into place")
    p.add_argument("--no-jev", action="store_true", help="do not ask for a Jev key (add one later: orche jev setup)")
    p.add_argument("--jev-key-stdin", action="store_true", help="read a Jev key from stdin, no prompts")
    p.add_argument("--jev-kind", choices=("gateway", "direct"), default=None,
                   help="gateway (Vercel AI Gateway, default) or direct (api.typesafe.ai); with "
                        "--jev-key-stdin, or instead of the key-type question")
    p.add_argument("--no-dialog", action="store_true", help="Jev key at a hidden terminal prompt, not the macOS dialog")
    p.add_argument("--no-plugin-hint", action="store_true",
                   help="plugin mode: do not print the two /plugin lines (install.sh runs those steps itself)")
    p.set_defaults(func=cmd_install)
    p = subparsers.add_parser("uninstall", help="remove ORCHE blocks, hook entries and agent symlinks")
    p.add_argument("--home", help="home directory (default: $HOME)")
    p.set_defaults(func=cmd_uninstall)
    p = subparsers.add_parser("doctor", help="check the installation (OK/WARN/FAIL per line)")
    p.add_argument("--home", help="home directory (default: $HOME)")
    p.set_defaults(func=cmd_doctor)
    p = subparsers.add_parser("apply-all", help="init every Orca repo that is not registered yet")
    p.add_argument("--stage", choices=("building", "public"), default="building")
    p.add_argument("--wake", default="", help="comma-separated divisions to start in each repo (default: none)")
    p.set_defaults(func=apply_all.run)


def _ctx(args, **kw) -> Ctx:
    home = Path(os.path.abspath(Path(args.home).expanduser())) if args.home else Path.home()
    if args.home and not os.environ.get("ORCHE_HOME"):
        os.environ["ORCHE_HOME"] = str(home / ".claude" / "orche")
    return Ctx(home, dry_run=args.dry_run, **kw)


def _state_path():
    return paths.orche_home() / STATE_FILE


class StateUnreadable(Exception):
    """install-state.json (or the legacy install.json) exists but cannot be read as an object."""

    def __init__(self, path, reason):
        super().__init__(f"{path.name}: {reason}")
        self.path = path


def _load_state() -> dict:
    """install-state.json, else the legacy install.json (migrated on the next state write).

    Missing -> {}. Present but unreadable -> StateUnreadable (never treated as empty).
    """
    for name in (STATE_FILE, LEGACY_STATE_FILE):
        path = paths.orche_home() / name
        if not path.exists():
            continue
        try:
            state = util.read_json(path, default=None)
        except util.JsonError as exc:
            raise StateUnreadable(path, f"invalid JSON ({exc.reason})") from None
        except OSError as exc:
            raise StateUnreadable(path, exc.strerror or str(exc)) from None
        if not isinstance(state, dict):
            raise StateUnreadable(path, "not a JSON object")
        return state
    return {}


def _state_or_error(ctx, phase, consequence):
    """The state, or None after backing up the unreadable file and recording a step error."""
    try:
        return _load_state()
    except StateUnreadable as exc:
        try:
            backup(ctx, "state", exc.path)
        except OSError as bexc:
            ctx.note("state", "error", None, f"{exc.path.name}: backup failed: {bexc.strerror}")
        ctx.note(phase, "error", None, f"{exc.path.name} unreadable: {consequence} — fix or remove the file "
                                       f"(backup kept) [{exc}]")
        return None


def _save_state(ctx, state) -> None:
    """Write install-state.json (dropping the legacy file); a failure is reported once per run."""
    if ctx.dry_run or getattr(ctx, "state_save_failed", False):
        return
    try:
        util.write_json(_state_path(), state)
        legacy = paths.orche_home() / LEGACY_STATE_FILE
        if legacy.is_file():
            legacy.unlink()
    except OSError as exc:
        ctx.state_save_failed = True
        ctx.note("state", "error", None, f"{STATE_FILE}: {exc.strerror or exc}")


def _run_steps(ctx, phase, steps) -> None:
    """Run every (file name, step); any exception becomes a recorded step error naming the file
    (no traceback, no home paths) and the run goes on."""
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the other steps
            if isinstance(exc, OSError) and exc.strerror:
                where = os.path.basename(exc.filename) if exc.filename else label
                msg = f"{exc.strerror} ({where})"
            else:
                msg = str(exc).replace(str(ctx.home), "~")
            ctx.note(phase, "error", None, f"{label}: {type(exc).__name__}: {msg}")


def _orche_home(ctx) -> None:
    home = paths.orche_home()
    missing = [d for d in paths.SUBDIRS if not (home / d).is_dir()]
    if not missing:
        ctx.note("orche_home", "ok", home, "present")
        return
    if not ctx.dry_run:
        paths.ensure_dirs()
    ctx.note("orche_home", "create", home, f"dirs {','.join(missing)}")


def _jev_step(args, state, new_state):
    """The optional Jev key, asked once (first interactive install); None when nothing was done.

    `--no-jev` skips, `--jev-key-stdin [--jev-kind]` configures without prompts; on a terminal the
    question is asked unless an earlier install asked it or Jev is already set up. Never fails the
    install: ORCHE works without Jev."""
    if args.dry_run:
        return None
    if args.no_jev:
        return {"status": "skipped", "lines": ["jev: skipped (--no-jev); add later with `orche jev setup`"]}
    if args.jev_key_stdin:
        problem = jev_setup.config_problem()
        if problem:
            return {"status": "skipped", "error": problem, "lines": [jev_setup.unreadable_line(problem)]}
        key = jev_setup.stdin_line()
        if not key:
            return {"status": "off", "error": "no key on stdin", "lines": ["jev: no key on stdin; Jev stays off"]}
        return jev_setup.configure(key, args.jev_kind or "gateway")
    # No terminal (and no --jev-key-stdin): skipped without a word, stdin is never read.
    if args.json or state.get("jev_asked") or not jev_setup.interactive():
        return None
    if jev_setup.configured():
        new_state["jev_asked"] = True
        return None
    problem = jev_setup.config_problem()
    if problem:  # not asked, and asked again on the next install once the file is fixed
        return {"status": "skipped", "error": problem, "lines": [jev_setup.unreadable_line(problem)]}
    new_state["jev_asked"] = True
    return jev_setup.prompt_flow(dialog=not args.no_dialog, kind=args.jev_kind)


def cmd_install(args):
    ctx = _ctx(args, yes=args.yes)
    state = _state_or_error(ctx, "install", "install refused, settings.json not touched")
    jev_result = None
    if state is not None:
        new_state = {**state, "plugin_root": str(ctx.plugin_root), "mode": args.mode}
        try:
            _run_steps(ctx, "install", [("ORCHE_HOME", lambda: _orche_home(ctx)),
                                        ("settings.json", lambda: settings.install(ctx, args.mode, new_state))])
            _save_state(ctx, new_state)  # before the riskier file edits: uninstall must see added hooks
            _run_steps(ctx, "install", [("statusline-command.sh", lambda: statusline.install(ctx)),
                                        ("agents", lambda: agents.install(ctx)),
                                        ("skills", lambda: skills.install(ctx, args.mode)),
                                        ("runner.md", lambda: agents.flip_runner(ctx)),
                                        ("fable.md", lambda: fable.install(ctx)),
                                        ("AGENTS.md", lambda: codex.install(ctx))])
            jev_result = _jev_step(args, state, new_state)
        finally:
            _save_state(ctx, new_state)
    instructions = []
    if args.mode == "plugin" and not args.no_plugin_hint:
        instructions = ["Install the ORCHE hooks as a Claude Code plugin (inside Claude Code):",
                        f"  /plugin marketplace add {ctx.plugin_root}",
                        "  /plugin install orche@orche"]
    lines = ([f"dry run: nothing written (home {ctx.home})"] if ctx.dry_run else []) + ctx.lines()
    lines += (jev_result or {}).get("lines", []) + instructions + [MIGRATION_LINE]
    data = {"home": str(ctx.home), "dry_run": ctx.dry_run, "mode": args.mode, "actions": ctx.actions,
            "instructions": instructions, "errors": ctx.errors, "migration": MIGRATION_LINE}
    if jev_result is not None:
        data["jev"] = {k: v for k, v in jev_result.items() if k != "lines"}
    out(args, data, lines)
    return EXIT_USER if ctx.errors else EXIT_OK


def cmd_uninstall(args):
    ctx = _ctx(args)
    state = _state_or_error(ctx, "uninstall", "hooks/values not restored")
    steps = [("statusline-command.sh", lambda: statusline.uninstall(ctx)), ("fable.md", lambda: fable.uninstall(ctx))]
    if state is not None:
        steps.append(("settings.json", lambda: settings.uninstall(ctx, state)))
    steps.append(("agents", lambda: agents.uninstall(ctx, state or {})))
    steps.append(("skills", lambda: skills.uninstall(ctx, state or {})))
    try:
        _run_steps(ctx, "uninstall", steps)
    finally:
        if state is not None:  # never overwrite an unreadable state file
            _save_state(ctx, state)
    lines = ([f"dry run: nothing written (home {ctx.home})"] if ctx.dry_run else []) + ctx.lines()
    lines.append(f"kept: backups (*.bak-*) and ORCHE_HOME {paths.orche_home()}")
    lines.append("kept: a stored Jev key (keychain or ORCHE_HOME/secrets); delete it with `orche jev key rm`")
    out(args, {"home": str(ctx.home), "dry_run": ctx.dry_run, "actions": ctx.actions, "errors": ctx.errors}, lines)
    return EXIT_USER if ctx.errors else EXIT_OK


def cmd_doctor(args):
    ctx = _ctx(args)
    results = doctor.checks(ctx)
    out(args, results, [f"{r['level']}\t{r['check']}\t{r['detail']}" for r in results])
    return EXIT_USER if any(r["level"] == "FAIL" for r in results) else EXIT_OK
