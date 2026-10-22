"""`orche doctor`: one OK/WARN/FAIL line per check. FAIL only for python < 3.11 (this interpreter,
or no 3.11+ the launchers' finder can pick in a login shell), an invalid registry or an unwritable
ORCHE_HOME. Jev is optional and fails open, so its row is WARN at worst (not configured, disabled,
or a failed last probe)."""
import os
import re
import shlex
import shutil
import sys

from .. import __version__, config, headless, jev_setup, orca, paths, procs, registry, runs, shell
from . import careful, settings, statusline

TOOLS = ("gcloud", "gh", "vercel", "age", "zstd")


def _orche_home():
    home = paths.orche_home()
    if home.is_dir():
        if not os.access(home, os.W_OK | os.X_OK):
            return "FAIL", f"ORCHE_HOME {home} is not writable"
        missing = [d for d in paths.SUBDIRS if not (home / d).is_dir()]
        if missing:
            return "WARN", f"ORCHE_HOME {home} lacks {','.join(missing)} (run: orche install)"
        return "OK", f"ORCHE_HOME {home}"
    parent = home.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if os.access(parent, os.W_OK | os.X_OK):
        return "WARN", f"ORCHE_HOME {home} not created yet (run: orche install)"
    return "FAIL", f"ORCHE_HOME {home} cannot be created ({parent} is not writable)"


PY_HINT = "brew install python; put /opt/homebrew/bin before /usr/bin in PATH"
PY_ROW = "python3 (login shell)"
ORCHE_ROW = "orche on PATH"


def login_shell(command):
    """(shell, Result) of `$SHELL -lc <command>`: what a fresh login shell (a new pane) resolves."""
    sh = os.environ.get("SHELL") or "/bin/sh"
    return sh, shell.run([sh, "-lc", command], timeout=15)


FINDER = paths.PLUGIN_ROOT / "lib" / "find_python.sh"
LAUNCHERS_PICK = " (launchers pick a 3.11+ interpreter themselves)"


def finder_command() -> str:
    """Login-shell command running the launchers' own search (lib/find_python.sh) under /bin/sh."""
    probe = '. "$0" && orche_find_python && command -v "$ORCHE_PY"'
    return f"/bin/sh -c {shlex.quote(probe)} {shlex.quote(str(FINDER))}"


def python3_login():
    """(level, name, detail) for the `python3` a login shell resolves.

    Below 3.11 (or missing) this is a WARN while lib/find_python.sh, which `bin/orche` and
    `hooks/run.sh` use, still finds a 3.11+ interpreter in the login shell; FAIL only when it finds none.
    """
    sh, res = login_shell("command -v python3 && python3 --version 2>&1")
    exe = next((ln.strip() for ln in (res.out or "").splitlines() if ln.strip().startswith("/")), "")
    m = re.search(r"Python (\d+)\.(\d+)(?:\.(\d+))?", (res.out or "") + (res.err or "")) if exe else None
    if exe and not m:
        return "WARN", PY_ROW, f"{exe}: cannot read its version"
    if m and (int(m.group(1)), int(m.group(2))) >= (3, 11):
        return "OK", PY_ROW, f"{exe} {'.'.join(g for g in m.groups() if g is not None)}"
    detail = (f"{exe} is {'.'.join(g for g in m.groups() if g is not None)} (needs 3.11+): {PY_HINT}" if m
              else f"{sh} -lc finds no python3 (needs 3.11+): {PY_HINT}")
    _, found = login_shell(finder_command())
    if found.ok and (found.out or "").strip():
        return "WARN", PY_ROW, detail + LAUNCHERS_PICK
    return "FAIL", PY_ROW, detail


def orche_login():
    """(level, name, detail): can a login shell resolve `orche`? (Panes add it themselves.)"""
    sh, res = login_shell("command -v orche")
    found = (res.out or "").strip().splitlines()
    if res.ok and found:
        return "OK", ORCHE_ROW, found[-1].strip()
    return "WARN", ORCHE_ROW, (f"{sh} -lc cannot find orche; add to your shell profile: "
                               f"export PATH=\"{paths.PLUGIN_ROOT / 'bin'}:$PATH\"")


def claude_checks() -> list:
    """(level, name, detail) rows from headless.check: the configured `claude` binary, its version,
    and whether its `--help` lists every flag a headless call uses."""
    return headless.check(config.load())


MIN_FREE = 1 << 30          # WARN below 1 GiB free on ORCHE_HOME's filesystem
MAX_RUN_LOGS = 200 << 20    # WARN above 200 MiB of logs/runs


def _existing(path):
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def disk_checks() -> list:
    """(level, name, detail): free space where ORCHE_HOME lives (a full disk keeps the runner
    from filing: reconcile files it later, once writes work again) and the size of logs/runs."""
    home = paths.orche_home()
    rows = []
    try:
        st = os.statvfs(_existing(home))
        free = st.f_bavail * st.f_frsize
        gib = f"{free / (1 << 30):.1f} GiB free"
        rows.append(("OK", "disk", f"{gib} at {home}") if free >= MIN_FREE else
                    ("WARN", "disk", f"{gib} at {home} (under 1 GiB: runs may fail to file their result)"))
    except OSError as exc:
        rows.append(("WARN", "disk", f"cannot read free space at {home}: {exc}"))
    total = 0
    for log_file in runs.logs_dir().glob("*.log"):
        try:
            total += log_file.stat().st_size
        except OSError:
            continue
    mib = f"{total / (1 << 20):.1f} MiB in {runs.logs_dir()}"
    rows.append(("OK", "run logs", mib) if total <= MAX_RUN_LOGS else
                ("WARN", "run logs", f"{mib} (over 200 MiB: lower runner.log_keep_days or run "
                                     "`orche janitor run`)"))
    return rows


def runner_lock_checks(reg, dry_run=False) -> list:
    """runs.reconcile() of every project (not on a dry run): one WARN row per lost run it settled
    (an `orche run` that died, or whose pid is another program now: `failed` filed, leftovers
    killed, locks removed)."""
    if dry_run:
        return []
    return [("WARN", "runner lock", f"{act['project']}: {runs.describe(act)}")
            for key in sorted(reg.get("projects", {}))
            for act in runs.reconcile(reg, key, budget_s=runs.RECONCILE_BUDGET_S)]


def process_checks(reg) -> list:
    """(level, name, detail): whether the process table can be read (the env sweep and reconcile
    need `ps`; without it nothing a run left behind is ever killed) and one WARN row per pending
    sweep (a lost run whose leftovers are not swept yet)."""
    rows = [("OK", "process table", "readable") if procs.table() is not None else
            ("WARN", "process table", "cannot read the process table (`ps` failed or timed out): "
                                      "run sweeps and reconcile cannot kill what a run left running")]
    for key in sorted(reg.get("projects", {})):
        for rec in runs.pending_sweeps(key):
            rows.append(("WARN", "pending sweep",
                         f"{key}: {rec.get('unit')} (run {rec.get('run_id')}, claude pid {rec.get('child_pid')}): "
                         f"leftovers not swept yet ({int(rec.get('sweep_failures') or 0)} unreadable "
                         "process table(s) so far)"))
    return rows


def upgrade_checks(reg) -> list:
    """One WARN row per project whose `orche_version` is missing or older than this ORCHE."""
    projects = reg.get("projects", {})
    return [("WARN", "upgrade", registry.upgrade_text(key, up)) for key in sorted(projects)
            for up in [registry.upgrade(projects[key], __version__)] if up]


def checks(ctx) -> list:
    out = []

    def add(level, name, detail):
        out.append({"check": name, "level": level, "detail": detail})

    py = sys.version_info
    add("OK" if py >= (3, 11) else "FAIL", "python", f"{py.major}.{py.minor}.{py.micro}"
        + ("" if py >= (3, 11) else " (needs 3.11+)"))
    add(*python3_login())
    add(*orche_login())
    try:
        orca.status(timeout=5)
        add("OK", "orca", "orca status answered")
    except orca.OrcaError as exc:
        add("WARN", "orca", f"orca status failed: {exc}")
    for row in claude_checks():
        add(*row)
    for tool in TOOLS:
        found = shutil.which(tool)
        add("OK" if found else "WARN", f"tool {tool}", found or "not found on PATH")
    add(*jev_setup.doctor_row())
    level, detail = _orche_home()
    add(level, "orche_home", detail)
    for row in disk_checks():
        add(*row)
    try:
        reg = registry.load()
        add("OK", "registry", f"valid ({len(reg['projects'])} project(s))")
        for row in runner_lock_checks(reg, ctx.dry_run):
            add(*row)
        for row in process_checks(reg):
            add(*row)
        for row in upgrade_checks(reg):
            add(*row)
    except registry.RegistryError as exc:
        add("FAIL", "registry", str(exc).splitlines()[0] if str(exc) else "invalid")
    data, err = settings.load(ctx)
    if err:
        add("WARN", "settings", err)
        data = {}
    in_settings = settings.settings_hooks(data, ctx.plugin_root)
    shapes = {"run.sh" if settings.legacy_command(ctx.plugin_root, cmd) else "python3" for _, cmd in in_settings}
    if in_settings and settings.plugin_enabled(data):
        add("WARN", "hooks", f"hooks registered twice (plugin + {len(in_settings)} settings.json entries): "
                             "run orche install (plugin mode removes the settings entries) or disable the plugin")
    elif len(shapes) == 2:
        add("WARN", "hooks", "settings.json holds both hook shapes (python3 .../hooks/<hook>.py and "
                             "hooks/run.sh <hook>): run orche install --mode settings to replace the old ones")
    elif settings.plugin_enabled(data):
        add("OK", "hooks", "orche plugin enabled")
    else:
        missing = settings.registered(data, ctx.plugin_root)
        add("OK" if not missing else "WARN", "hooks",
            "all six plugin hooks registered in settings.json" if not missing else
            f"plugin not enabled and {len(missing)} hook(s) missing from settings.json "
            "(run /plugin install orche@orche or orche install --mode settings)")
    script = statusline.script_path(ctx)
    if statusline.present(ctx) and statusline.after_exit(script.read_text(encoding="utf-8")):
        add("WARN", "statusline", f"ORCHE block in {script} sits after an `exit` line and never runs "
                                  "(re-run orche install)")
    else:
        add("OK" if statusline.present(ctx) else "WARN", "statusline",
            f"ORCHE block {'present' if statusline.present(ctx) else 'missing'} in {script}")
    days = data.get("cleanupPeriodDays")
    add("OK" if days == settings.CLEANUP_DAYS else "WARN", "cleanupPeriodDays",
        f"{days if days is not None else '(absent)'}" + ("" if days == settings.CLEANUP_DAYS
                                                         else f" (want {settings.CLEANUP_DAYS})"))
    # Author-specific files: absent is normal for most users ("skipped"), not a warning.
    agents = ctx.home / ".codex" / "AGENTS.md"
    recovered = agents.with_name("AGENTS.md.recovered")
    if agents.exists():
        add("OK", "codex AGENTS.md", f"{agents} present")
    elif recovered.is_file():
        add("WARN", "codex AGENTS.md", f"{agents} missing but {recovered.name} exists (run: orche install --yes)")
    else:
        add("OK", "codex AGENTS.md", "skipped (not present)")
    script = careful.script(ctx.home)
    add("OK", "careful hook", f"{script} present" if script.is_file() else "skipped (not present)")
    return out
