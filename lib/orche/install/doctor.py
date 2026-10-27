"""`orche doctor`: one OK/WARN/FAIL line per check. FAIL only for python < 3.11 (this interpreter,
or no 3.11+ the launchers' finder can pick in a login shell), an invalid registry or an unwritable
ORCHE_HOME. Jev is optional and fails open, so its row is WARN at worst (not configured, disabled,
or a failed last probe). The human output ends with "Next steps (optional, any time)": `guide.lines`, which
never changes the exit code."""
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from .. import (__version__, config, github, headless, jev_setup, ladder, mask, oauth, orca, paths, procs, registry,
                runs, shell, units, vendors)
from . import settings, statusline

TOOLS = ("gcloud", "gh", "vercel", "age", "zstd")
WRITE_PERMS = ("ADMIN", "MAINTAIN", "WRITE")     # gh viewerPermission the merge queue can merge with
DEPLOY_FILES = ("vercel.json", "netlify.toml")


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


# ---- 0.9.0 scale rows (design §9.2, Appendix C.5): (level, name, detail) ---------------------------

def _roots(reg):
    """(key, root) of the registered projects whose path is a directory."""
    for key in sorted(reg.get("projects", {})):
        path = (reg["projects"][key] or {}).get("path")
        if path and Path(path).is_dir():
            yield key, Path(path)


def gh_rows(reg) -> list:
    """`gh` per project: the account gh acts as, its viewerPermission on the project's repository (the
    merge queue needs WRITE or more) and the project's base (publish.base, else GitHub's default branch); gh not
    installed: one WARN."""
    from .. import publish
    if shutil.which("gh") is None:
        return [("WARN", "gh", "gh is not installed: the merge queue cannot open or merge PRs — brew install gh, "
                               "then gh auth login")]
    try:
        login = github.run(None, "api", "user", "--jq", ".login", timeout=10).strip() or "?"
    except github.GhError:
        login = "?"
    rows = []
    for key, root in _roots(reg):
        url = shell.git(root, "remote", "get-url", "origin", timeout=10)
        if not url.ok or not url.out.strip():
            rows.append(("WARN", "gh", f"{key}: no origin remote — the merge queue lands PRs on GitHub"))
            continue
        try:
            data, err = github.data(None, "repo", "view", url.out.strip(), "--json",
                                    "nameWithOwner,viewerPermission,defaultBranchRef", timeout=10), ""
        except github.GhError as exc:
            data, err = None, exc.result.err if exc.result else ""
        if not isinstance(data, dict):
            why = mask.redact(((err or "").strip().splitlines() or ["no answer"])[0][:120])[0]
            rows.append(("WARN", "gh", f"{key}: gh cannot read the repository ({why}) — gh auth login, or check "
                                       "its origin remote"))
            continue
        perm, repo = str(data.get("viewerPermission") or "NONE"), data.get("nameWithOwner") or "?"
        base = publish.base(root, project=key) or (data.get("defaultBranchRef") or {}).get("name") or "?"
        rows.append(("OK", "gh", f"{key}: gh as {login} · {perm} on {repo} · base {base}") if perm in WRITE_PERMS
                    else ("WARN", "gh", f"{key}: gh account {login} has {perm} on {repo} — the queue cannot merge: "
                                        "gh auth switch -u <an account with write>, or gh auth login"))
    return rows


def entire_rows(reg, which=shutil.which) -> list:
    """`entire`: WARN when a registered project uses Entire (`.entire/settings.json`) and the `entire`
    command is not on PATH (its workers' commits are then not linked to their sessions); OK when it is;
    no row when no project uses Entire."""
    from .. import detect
    users = [key for key, root in _roots(reg) if detect.uses_entire(root)]
    if not users:
        return []
    if which("entire"):
        return [("OK", "entire", f"found; used by {', '.join(users)}")]
    return [("WARN", "entire", f"{key} uses Entire but the entire command is not on PATH; its commits will not be "
                               "linked to their sessions") for key in users]


def _deploys(root) -> list:
    found = [n for n in DEPLOY_FILES if (root / n).is_file()]
    for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        try:
            text = wf.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if "push" in text and "deploy" in text:
            found.append(f".github/workflows/{wf.name}")
    return found


def gate_rows(reg, cfg) -> list:
    """`queue gate` (scripts/verify/ci.sh on origin/<base>) and `deploy on merge` (a deploy config that
    makes every queue merge a deploy: WARN under queue.land auto, R1) per project with a git checkout; a pushed
    orche/verify-gate branch (verifygate.pending) is the open PR adding the script, named as init recorded it."""
    from .. import publish, queue, verifygate
    rows = []
    for key, root in _roots(reg):
        if not (root / ".git").exists():
            continue
        base, ok = publish.gate(root, cfg, key)
        rows.append(("WARN", "queue gate", f"{key}: {publish.no_base(root)}") if not base else
                    ("OK", "queue gate", f"{key}: scripts/verify/ci.sh on origin/{base}") if ok else
                    ("WARN", "queue gate", f"{key}: waiting for {verifygate.awaiting(reg['projects'][key])}")
                    if verifygate.pending(root) else
                    ("WARN", "queue gate", f"{key}: no check script on origin/{base} and no PR; "
                                           f"orche init --refresh {key} opens one"))
        found = _deploys(root)
        if found and queue.land_mode(cfg, key) == "owner":
            rows.append(("OK", "deploy on merge", f"{key}: {found[0]} — merges land on the owner's word (queue.land owner)"))
        elif found:
            rows.append(("WARN", "deploy on merge", f"{key}: {found[0]} — every queue merge deploys {base} (queue.land "
                                                    f"auto); for owner-approved landing set queue.per_project.{key}.land owner"))
    return rows


def queue_rows(reg) -> list:
    """`queue` per project with a merge queue: open/paused/closed, waiting and testing entries, whether
    its integrator runs; an entry testing with no integrator is a lost one (the heartbeat restarts it)."""
    from .. import queue
    rows, have = [], set(queue.projects())
    for key in sorted(reg.get("projects", {})):
        if key not in have:
            continue
        try:
            s = queue.summary(key)
        except Exception as exc:  # noqa: BLE001 - a doctor row never fails the command
            rows.append(("WARN", "queue", f"{key}: queue.json unreadable: {exc}"))
            continue
        waiting, testing, state, live = s["waiting"], s["testing"], s["state"], queue.running(key)
        text = f"{key}: {state} · {waiting} waiting · {testing} testing · integrator {'running' if live else 'idle'}"
        if testing and not live:
            rows.append(("WARN", "queue", f"{text} — an entry is testing with no integrator (lost): the heartbeat "
                                          "restarts it"))
        elif state == "closed":
            rows.append(("WARN", "queue", f"{text} ({queue.closed_text(key, s['closed'])})"))
        elif state == "paused":
            rows.append(("WARN", "queue", f"{text} — orche queue resume {key}"))
        else:
            rows.append(("OK", "queue", text))
    return rows


def review_rows(cfg, reg) -> list:
    """`review`: the cross-vendor mode, the reviewer of each writer vendor (the first of review.order of
    the other vendor), the Build review, pending Build reviews (review/build.py, when present)."""
    from .. import reconcile
    r = cfg.get("review") or {}
    order = [m for m in r.get("order") or [] if isinstance(m, str)]

    def other(vendor):
        return next((m for m in order if vendors.vendor_of(m) != vendor), "-")
    from ..review import build
    pending = 0
    for key in sorted(reg.get("projects", {})):
        try:
            pending += len(list(build.pending(key) or []))
        except Exception:  # noqa: BLE001 - a doctor row never fails the command
            continue
    text = (f"{r.get('cross_vendor', 'required')} · claude → {other('claude')} · codex → {other(vendors.CODEX)} · "
            f"head review {'on' if r.get('build', True) else 'off'} · {pending} pending")
    if shutil.which("codex") is None and r.get("cross_vendor", "required") != "off":
        return [("WARN", "review", f"{text} — codex is not installed: Claude writers get the same-vendor fallback")]
    return [("OK", "review", text)]


def scale_rows(cfg, reg) -> list:
    """`scale`: the active preset (or custom) and its caps; `ladder`: the TIER table (ladder.doctor_row)."""
    from .. import scale
    name = scale.current(cfg, reg)
    if name in scale.presets(cfg):
        text = f"preset {name}: {scale.summary(cfg, name)}"
    else:
        per, tot = runs.caps(cfg)
        panes = "features keep a pane" if cfg.get("feature_panes", True) else "features headless"
        text = (f"custom: runs {per}/project, {tot} total · {panes} · spend cap "
                f"${(cfg.get('flow') or {}).get('max_project_cost_usd')} · min free "
                f"{(cfg.get('runner') or {}).get('min_free_mb')} MB")
    return [("OK", "scale", text), ("OK", "ladder", ladder.doctor_row(cfg))]


def usage_rows(now=None) -> list:
    """`usage`: each vendor's active usage hold (flow/usage.py), else OK."""
    from ..flow import usage
    now = now or units.now()
    rows = [("WARN", "usage", f"{h.get('vendor')} held until {usage.hhmm(usage.until_of(h), now)} "
                              f"({len(usage.held_units(h))} units) — they re-run then")
            for h in usage.holds() if usage.is_active(h, now)]
    return rows or [("OK", "usage", "no usage hold")]


def memory_rows(cfg, free=None, total=None) -> list:
    """`memory`: free memory (procs.free_mb) against runner.min_free_mb (the runner's memory guard)."""
    free = procs.free_mb() if free is None else free
    want = (cfg.get("runner") or {}).get("min_free_mb") or 0
    if free is None:
        return [("WARN", "memory", "cannot read free memory: the runner's memory guard is off")]
    total = procs.mem_total_mb() if total is None else total
    text = f"{free / 1024:.1f} GiB free" + (f" ({free * 100 // total} %)" if total else "")
    return [("OK", "memory", f"{text} ≥ {want} MB") if free >= want else
            ("WARN", "memory", f"{text} < {want} MB: new runs wait for memory (runner.min_free_mb)")]


def scale_checks(reg, cfg=None) -> list:
    """Every 0.9.0 row, in C.5's order."""
    cfg = config.load() if cfg is None else cfg
    return (gh_rows(reg) + gate_rows(reg, cfg) + queue_rows(reg) + review_rows(cfg, reg) + scale_rows(cfg, reg)
            + usage_rows() + memory_rows(cfg))


def upgrade_checks(reg) -> list:
    """One WARN row per project whose `orche_version` is missing or older than this ORCHE."""
    projects = reg.get("projects", {})
    return [("WARN", "upgrade", registry.upgrade_text(key, up)) for key in sorted(projects)
            for up in [registry.upgrade(projects[key], __version__)] if up]


def job_state(home) -> dict:
    """What the heartbeat and privacy rows read once (0.7.2): macOS, the reconcile job loaded
    (`launchctl print`), when it was loaded (its plist's mtime, whole seconds: orche install writes it
    right before the bootstrap) and the registered projects under a protected folder."""
    import datetime as dt
    from .. import privacy
    from . import launchd
    st = {"darwin": sys.platform == "darwin", "loaded": False, "load_at": None, "protected": []}
    try:
        st["protected"] = privacy.protected_projects(registry.load())
    except registry.RegistryError:
        pass
    if st["darwin"]:
        try:
            mtime = launchd.plist_path(launchd.RECONCILE, home).stat().st_mtime
        except OSError:
            return st
        st["load_at"] = dt.datetime.fromtimestamp(int(mtime)).astimezone()
        st["loaded"] = launchd.loaded(launchd.RECONCILE)
    return st


def heartbeat_rows(job=None) -> list:
    """The reconcile heartbeat (lib/orche/reconcile.py): WARN when the job is blocked (a privacy
    abort, or loaded with no pass yet), stale or never; OK otherwise."""
    from .. import reconcile
    try:
        return [reconcile.doctor_row(job=job)]
    except Exception as exc:  # noqa: BLE001 - a doctor row never fails the doctor
        return [("WARN", "heartbeat", f"unreadable: {type(exc).__name__}: {exc}")]


def privacy_rows(job) -> list:
    """`privacy` (0.7.2): OK when no registered project is under a folder macOS keeps from the
    heartbeat job (Desktop, Documents, Downloads, iCloud Drive), or the job has passed since it loaded
    and since its last privacy abort; WARN otherwise — run: orche install --grant."""
    from .. import reconcile, units
    name = "privacy"
    if not job["darwin"]:
        return [("OK", name, "not macOS")]
    prot = job["protected"]
    if not prot:
        return [("OK", name, "no project under Desktop, Documents, Downloads, iCloud Drive, cloud storage or /Volumes")]
    where = f"{len(prot)} project(s) under a protected folder ({prot[0][1]})"
    if not job["loaded"]:
        return [("OK", name, f"{where}; the heartbeat job is not loaded (see reconcile job)")]
    hb = reconcile.read_heartbeat() or {}
    job_at = units.parse_iso(hb.get("job_at"))
    if hb.get("unreadable"):   # the job's own pass could not list them (a denial)
        return [("WARN", name, f"{where}; the heartbeat job cannot read {', '.join(map(str, hb['unreadable']))} "
                               "(macOS privacy) — run: orche install --grant")]
    ab = reconcile.read_abort() or {}
    marks = [job["load_at"], units.parse_iso(ab.get("at")) if ab.get("hint") == "privacy" else None]
    since = max((t for t in marks if t is not None), default=None)
    if job_at is not None and (since is None or job_at >= since):
        return [("OK", name, f"{where}; the heartbeat job reads them (last pass {job_at.astimezone():%H:%M})")]
    return [("WARN", name, f"{where}; no heartbeat job pass since it loaded — run: orche install --grant")]


def reconcile_job_rows(home) -> list:
    """The reconcile LaunchAgent (design c10 §2.4): loaded, and its StartInterval matches every_s."""
    import plistlib
    from .. import reconcile
    from . import launchd
    name = "reconcile job"
    if sys.platform != "darwin":
        return [("OK", name, "not macOS: run `orche reconcile` every 5 min (cron/systemd)")]
    cfg = reconcile.settings()
    if not cfg.get("enabled", True):
        return [("OK", name, "off (reconcile.enabled false)")]
    path = launchd.plist_path(launchd.RECONCILE, home)
    if not path.is_file():
        return [("WARN", name, "not installed — run orche install")]
    try:
        interval = plistlib.loads(path.read_bytes()).get("StartInterval")
    except Exception as exc:  # noqa: BLE001 - a doctor row never fails the doctor
        return [("WARN", name, f"plist unreadable ({type(exc).__name__}) — run orche install")]
    every = reconcile.every_s(cfg)
    if interval != every:
        return [("WARN", name, f"plist runs every {interval}s, reconcile.every_s is {every} — run orche install")]
    if not launchd.loaded(launchd.RECONCILE):
        return [("WARN", name, f"not loaded: launchctl bootstrap {launchd.domain()} {path}")]
    return [("OK", name, f"loaded, every {every}s")]


def janitor_rows(home) -> list:
    """The janitor's daily LaunchAgent: OK when its plist is there (on, or report only); WARN only when it
    is missing after an install (install writes it); OK before any install."""
    from .. import janitor
    from . import StateUnreadable, launchd, load_state
    name = "janitor"
    if sys.platform != "darwin":
        return [("OK", name, "not macOS: run `orche janitor run` daily (cron/systemd)")]
    if launchd.plist_path(launchd.JANITOR, home).is_file():
        how = "on" if janitor.enabled_flag().exists() else "report only (orche janitor enable lets it act)"
        return [("OK", name, f"daily at {launchd.daily_at()}, {how}")]
    try:
        installed = bool(load_state())
    except StateUnreadable:
        installed = True
    return [("WARN", name, "daily schedule missing: run orche install") if installed else
            ("OK", name, "not scheduled yet (orche install adds it)")]


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
    try:
        vreg = registry.load()
    except registry.RegistryError:
        vreg = None   # the registry row below reports it
    for row in vendors.doctor_rows(config.load(), vreg):
        add(*row)
    for tool in TOOLS:
        found = shutil.which(tool)
        add("OK" if found else "WARN", f"tool {tool}", found or "not found on PATH")
    add(*jev_setup.doctor_row())
    add(*oauth.doctor_row())
    level, detail = _orche_home()
    add(level, "orche_home", detail)
    job = job_state(ctx.home)
    for row in (disk_checks() + heartbeat_rows(job) + reconcile_job_rows(ctx.home) + janitor_rows(ctx.home)
                + privacy_rows(job)):
        add(*row)
    try:
        reg = registry.load()
        add("OK", "registry", f"valid ({len(reg['projects'])} project(s))")
        for row in runner_lock_checks(reg, ctx.dry_run):
            add(*row)
        for row in process_checks(reg):
            add(*row)
        for row in upgrade_checks(reg) + entire_rows(reg):
            add(*row)
        for row in scale_checks(reg):
            add(*row)
    except registry.RegistryError as exc:
        add("FAIL", "registry", str(exc).splitlines()[0] if str(exc) else "invalid")
    data, err = settings.load(ctx)
    if err:
        add("WARN", "settings", err)
        data = {}
    in_settings = settings.settings_hooks(data, ctx.plugin_root)
    if in_settings and settings.plugin_enabled(data):
        add("WARN", "hooks", f"hooks registered twice (plugin + {len(in_settings)} settings.json entries): "
                             "run orche install (plugin mode removes the settings entries) or disable the plugin")
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
    if any(settings.CAREFUL_MARKER in cmd for _, cmd in settings.iter_commands(data)):
        add("WARN", "careful hook", "careful hook registered in settings.json — its approval dialogs stall "
                                    "unattended ORCHE panes; remove it or keep it only for your own sessions")
    return out


def next_steps(ctx) -> list:
    """`guide.lines` for the closing block; an invalid registry (its own FAIL row) lists no project gates."""
    from .. import guide
    try:
        reg = registry.load()
    except registry.RegistryError:
        reg = registry.empty()
    return guide.lines(config.load(), reg, ctx.home)
