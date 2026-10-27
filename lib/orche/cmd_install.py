"""`orche install|uninstall|doctor|apply-all` — set up (and take down) ORCHE in the user's home.

install patches only: `~/.claude/settings.json`, `~/.claude/statusline-command.sh`,
`~/.claude/agents/<ours>.md` (symlinks), `~/.claude/skills/<ours>` (symlinks, settings mode only;
a plugin-mode install removes them), `~/.claude/fable/{fable,active}.md`,
`~/.claude/fable/agents/runner.md`, `~/Library/LaunchAgents/com.orche.reconcile.plist` (macOS; the
heartbeat job, loaded only when the target home is the real one; `--no-reconcile` skips it),
`~/Library/LaunchAgents/com.orche.janitor.plist` (macOS; the janitor's daily run, loaded the same way; the
first install also turns the janitor on, `orche janitor disable` turns it off for good), and
moves `~/.codex/AGENTS.md.recovered` into place (`--yes`). Every changed file is first copied to `<file>.bak-<ts>`; `--dry-run` prints the plan
and writes nothing. `--home PATH` targets another home (tests); ORCHE_HOME then defaults to
`PATH/.claude/orche` unless set.

The first install on a terminal also asks once for an optional Jev key (`jev_setup`); `--no-jev`
skips it, `--jev-key-stdin [--jev-kind gateway|direct]` configures it without prompts. A missing
or failing Jev never fails the install.

Once the reconcile job is loaded (macOS, real home) the install waits up to 45 s (a progress line
first) for a pass of the job itself, kickstarting it (plain) when none came yet: `reconcile job:
first pass OK (<took_s> s)`, else — printed last — the privacy guidance (Finder and Full Disk Access
opened) when a project is under a protected folder, else where to look; the install still exits 0.
`--grant` does only the grant: nothing to grant when no project is under a protected folder; already
granted? else the steps, Finder at the job's Python.app (or its bare interpreter) and System Settings
at Full Disk Access, then up to 5 min for a job pass (privacy.GRANTED / privacy.BLOCKED, exit 1).
Ctrl-C during a wait: one line, exit 130.
"""
import os
import sys
from pathlib import Path

from . import jev_setup, paths, privacy, reconcile, registry, units, util
from .cli import EXIT_OK, EXIT_USER, fail, out
from .install import (RECONCILE_STEP, STATE_FILE, Ctx, StateUnreadable, backup, agents,
                      apply_all, codex, doctor, fable, installed_mode, launchd, load_state, settings, skills,
                      statusline)


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
    p.add_argument("--no-reconcile", action="store_true",
                   help="do not install the reconcile LaunchAgent (run `orche reconcile` every 5 min yourself)")
    p.add_argument("--grant", action="store_true",
                   help="macOS: let the heartbeat job read projects under Downloads, Documents, Desktop, "
                        "iCloud Drive, cloud storage or /Volumes (opens Finder and Full Disk Access, then "
                        "checks); installs nothing")
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


def _state_or_error(ctx, phase, consequence):
    """The state, or None after backing up the unreadable file and recording a step error."""
    try:
        return load_state()
    except StateUnreadable as exc:
        try:
            backup(ctx, "state", exc.path)
        except OSError as bexc:
            ctx.note("state", "error", None, f"{exc.path.name}: backup failed: {bexc.strerror}")
        ctx.note(phase, "error", None, f"{exc.path.name} unreadable: {consequence} — fix or remove the file "
                                       f"(backup kept) [{exc}]")
        return None


def _save_state(ctx, state) -> None:
    """Write install-state.json; a failure is reported once per run."""
    if ctx.dry_run or getattr(ctx, "state_save_failed", False):
        return
    try:
        util.write_json(_state_path(), state)
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


OFF_MACOS = "not macOS: run `orche reconcile` every 5 min (cron/systemd)"
JANITOR_STEP = "janitor"


def _reconcile_job(ctx, args, new_state) -> None:
    """The heartbeat LaunchAgent (design c10 §2.4): written always, loaded only for the real home."""
    if sys.platform != "darwin":
        ctx.note(RECONCILE_STEP, "skip", None, OFF_MACOS)
        return
    if args.no_reconcile:
        ctx.note(RECONCILE_STEP, "skip", None, "--no-reconcile; run `orche reconcile` every 5 min yourself")
        return
    every = reconcile.every_s(reconcile.settings())
    res = launchd.install(launchd.RECONCILE, launchd.reconcile_plist(every), ctx.home, ctx.dry_run)
    if not ctx.dry_run:
        new_state["reconcile_plist"] = res["plist"]
    if res["argv"]:
        how = "would load" if ctx.dry_run else ("loaded" if res["loaded"] else "not loaded")
    else:
        how = "not loaded: --home is not the real home"
    ctx.note(RECONCILE_STEP, "write", res["plist"], f"every {every}s, {how}")
    ctx.job_loaded = bool(res["loaded"])
    if res["error"]:
        ctx.note(RECONCILE_STEP, "warn", None, f"launchctl bootstrap failed: {res['error']}; see orche doctor")


def cmd_grant(args) -> int:
    """`orche install --grant`: a job pass within GRANT_QUICK_S means granted already; else the steps,
    Finder at the Python.app and System Settings at Full Disk Access, then up to GRANT_S for a job
    pass, the job kickstarted every KICK_EVERY_S. 0 granted (or not macOS); 1 still blocked or no job."""
    ctx = _ctx(args)
    if sys.platform != "darwin":
        out(args, {"granted": None}, ["not macOS: nothing to grant"])
        return EXIT_OK
    try:
        reg = registry.load()
    except registry.RegistryError:
        reg = None   # cannot tell: go on
    if reg is not None and not privacy.protected_projects(reg):
        out(args, {"granted": None, "protected": []}, [privacy.NOTHING_TO_GRANT])
        return EXIT_OK
    app = privacy.python_app()
    if args.dry_run:
        out(args, {"dry_run": True, "app": str(app)},
            privacy.steps(app) + [f"would run: open -R {app}", f"would run: open {privacy.SETTINGS_URL}"])
        return EXIT_OK
    if not launchd.job_loaded(ctx.home):
        return fail(EXIT_USER, privacy.NOT_LOADED.format(mode=installed_mode(ctx.home)))
    started = units.now()
    kicks = []

    def kick():   # -k once, first (a pass stuck since before the grant); then plain: a running pass stays
        launchd.kickstart(launchd.RECONCILE, kill=not kicks)
        kicks.append(1)
    progress = sys.stderr if args.json else sys.stdout
    try:
        print(privacy.grant_wait(privacy.GRANT_QUICK_S), file=progress, flush=True)
        hb = privacy.wait_job_pass(started, privacy.GRANT_QUICK_S, kick, privacy.KICK_EVERY_S)
        if hb is None:
            if not args.json:
                print("\n".join(privacy.steps(app)), flush=True)
            privacy.reveal(app)
            print(privacy.grant_wait(privacy.GRANT_S), file=progress, flush=True)
            hb = privacy.wait_job_pass(started, privacy.GRANT_S, kick, privacy.KICK_EVERY_S)
    except KeyboardInterrupt:
        return privacy.interrupted()
    out(args, {"granted": hb is not None, "app": str(app), "took_s": (hb or {}).get("took_s")},
        [privacy.GRANTED if hb is not None else privacy.blocked(app)])
    return EXIT_OK if hb is not None else EXIT_USER


def _janitor_job(ctx, state, new_state) -> None:
    """The janitor, on by default: the first install writes its enabled flag (recorded as
    `janitor_default`, so an owner's `orche janitor disable` stays), every install writes and loads its
    daily LaunchAgent like the reconcile job."""
    from . import janitor
    if sys.platform != "darwin":
        ctx.note(JANITOR_STEP, "skip", None, "not macOS: run `orche janitor run` daily (cron/systemd)")
        return
    if not state.get("janitor_default"):
        flag = janitor.enabled_flag()
        if not ctx.dry_run:
            util.atomic_write(flag, f"enabled {util.now_iso()} by orche install\n")
            new_state["janitor_default"] = True
        ctx.note(JANITOR_STEP, "set", flag, "on by default; `orche janitor disable` turns it off")
    res = launchd.install(launchd.JANITOR, launchd.render(launchd.JANITOR), ctx.home, ctx.dry_run)
    if not ctx.dry_run:
        new_state["janitor_plist"] = res["plist"]
    if res["argv"]:
        how = "would load" if ctx.dry_run else ("loaded" if res["loaded"] else "not loaded")
    else:
        how = "not loaded: --home is not the real home"
    ctx.note(JANITOR_STEP, "write", res["plist"], f"daily at {launchd.daily_at()}, {how}")
    if res["error"]:
        ctx.note(JANITOR_STEP, "warn", None, f"launchctl bootstrap failed: {res['error']}; see orche doctor")


def _janitor_job_remove(ctx, state) -> None:
    if sys.platform != "darwin":
        return
    res = launchd.uninstall(launchd.JANITOR, ctx.home, ctx.dry_run)
    if res["missing"]:
        return
    ctx.note(JANITOR_STEP, "remove", res["plist"], "unloaded" if res["argv"] else "")
    if state is not None and not ctx.dry_run:
        state.pop("janitor_plist", None)


def _reconcile_job_remove(ctx, state) -> None:
    if sys.platform != "darwin":
        return
    res = launchd.uninstall(launchd.RECONCILE, ctx.home, ctx.dry_run)
    if res["missing"]:
        return
    ctx.note(RECONCILE_STEP, "remove", res["plist"], "unloaded" if res["argv"] else "")
    if res["error"]:
        ctx.note(RECONCILE_STEP, "warn", None, f"launchctl bootout failed: {res['error']}")
    if state is not None and not ctx.dry_run:
        state.pop("reconcile_plist", None)


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
    if getattr(args, "grant", False):
        return cmd_grant(args)
    started = units.now()
    ctx = _ctx(args, yes=args.yes)
    state = _state_or_error(ctx, "install", "install refused, settings.json not touched")
    jev_result = None
    if state is not None:
        new_state = {**state, "plugin_root": str(ctx.plugin_root), "mode": args.mode}
        if (ctx.plugin_root / ".git").exists():   # the clone `orche update` pulls (not a plugin cache copy)
            new_state["clone"] = str(ctx.plugin_root)
        try:
            _run_steps(ctx, "install", [("ORCHE_HOME", lambda: _orche_home(ctx)),
                                        ("settings.json", lambda: settings.install(ctx, args.mode, new_state))])
            _save_state(ctx, new_state)  # before the riskier file edits: uninstall must see added hooks
            _run_steps(ctx, "install", [("statusline-command.sh", lambda: statusline.install(ctx)),
                                        ("agents", lambda: agents.install(ctx)),
                                        ("skills", lambda: skills.install(ctx, args.mode)),
                                        ("runner.md", lambda: agents.flip_runner(ctx)),
                                        ("fable.md", lambda: fable.install(ctx)),
                                        (RECONCILE_STEP, lambda: _reconcile_job(ctx, args, new_state)),
                                        (JANITOR_STEP, lambda: _janitor_job(ctx, state, new_state)),
                                        ("AGENTS.md", lambda: codex.install(ctx))])
            jev_result = _jev_step(args, state, new_state)
        finally:
            if not ctx.errors:   # a failed install keeps the old version: `orche update` resumes
                from . import __version__
                new_state["version"] = __version__
            _save_state(ctx, new_state)
    instructions = []
    if args.mode == "plugin" and not args.no_plugin_hint:
        instructions = ["Install the ORCHE hooks as a Claude Code plugin (inside Claude Code):",
                        f"  /plugin marketplace add {ctx.plugin_root}",
                        "  /plugin install orche@orche"]
    lines = ([f"dry run: nothing written (home {ctx.home})"] if ctx.dry_run else []) + ctx.lines()
    lines += (jev_result or {}).get("lines", []) + instructions
    data = {"home": str(ctx.home), "dry_run": ctx.dry_run, "mode": args.mode, "actions": ctx.actions,
            "instructions": instructions, "errors": ctx.errors}
    if jev_result is not None:
        data["jev"] = {k: v for k, v in jev_result.items() if k != "lines"}
    if not args.json:
        out(args, data, lines)
    if getattr(ctx, "job_loaded", False) and not ctx.dry_run and getattr(args, "first_pass", True):
        try:   # last: the guidance is what the owner acts on next
            first = privacy.first_pass_check(started, progress=sys.stderr if args.json else None)
        except KeyboardInterrupt:
            return privacy.interrupted()
        data["reconcile_first_pass"] = {k: v for k, v in first.items() if k != "lines"}
        if not args.json:
            print("\n".join(first["lines"]), flush=True)
    if args.json:
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
    steps.append((RECONCILE_STEP, lambda: _reconcile_job_remove(ctx, state)))
    steps.append((JANITOR_STEP, lambda: _janitor_job_remove(ctx, state)))
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
    steps = doctor.next_steps(ctx)
    out(args, {"checks": results, "next_steps": steps},
        [f"{r['level']}\t{r['check']}\t{r['detail']}" for r in results]
        + ["", "Next steps (optional, any time):"] + steps)
    return EXIT_USER if any(r["level"] == "FAIL" for r in results) else EXIT_OK
