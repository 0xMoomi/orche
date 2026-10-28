"""`orche update` — bring this ORCHE checkout to the newest `main`, reinstall, refresh every project.

The checkout is the clone `orche install` recorded (install-state.json `clone`; install.sh runs the
install from its clone), else the repository holding this package (`paths.PLUGIN_ROOT`): in plugin mode
the `orche` on PATH is the plugin cache copy, which has no `.git`. install.sh clones `--ref main`, so the
update follows `origin/main`. Steps, each one a progress line on stderr:

1. refuse (exit 2, nothing changed) only a checkout without `.git` (`not a git checkout: <path>; reinstall
   with the README prompt`). A development checkout — uncommitted changes, another branch than `main`, or
   commits not on `origin/main` — pulls nothing (`development checkout (<why>): nothing pulled`; the first
   two are known before the fetch, so no fetch runs) and, when the install state records another version
   than the checkout's, runs steps 4-5 with the checkout's code; a current one is `already up to date
   (<v>; development checkout, nothing pulled)`;
2. `git fetch --quiet origin main` (never a terminal prompt, at most 120 s) and read the new version
   from `origin/main:lib/orche/__init__.py`; the same version is `already up to date (<v>)`, exit 0 —
   unless the install state records another version than the checkout's (an earlier update stopped
   after the merge): then steps 4-5 run again, so a re-run resumes;
3. `git merge --ff-only origin/main` (never a reset, never a merge commit);
4. the NEW code reinstalls: `<this python> <clone>/bin/orche.py install --mode <mode> --no-jev` (the
   mode of the last install, as doctor reads it), then in plugin mode `claude plugin marketplace update
   orche` and `claude plugin update orche@orche`, as install.sh does on a re-run;
5. the NEW code refreshes every registered project: `<clone>/bin/orche.py init <key> --refresh` (ORCHE's
   own files: card link, personas, worktree settings, hooks wiring; no intake, no Orca, never the
   user's files).

The closing block: `ORCHE <old> → <new>`, one line per project, the install's `reconcile job` line (and its
`janitor: remove` line when it removed the janitor's old 04:00 LaunchAgent), and
a restart line when a card, skill, template or agent file changed. `--dry-run` fetches without moving
any ref (FETCH_HEAD only) and prints the plan. Exit 0 ok, 1 a step failed (named; the checkout keeps
whatever the finished steps did, a re-run resumes), 2 refused. With `--json` every outcome is one JSON
object on stdout, carrying `development` (the reason, or null) unless refused.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from . import mask, paths, registry
from .cli import EXIT_OK, EXIT_USER, out

EXIT_REFUSED = 2
REF = "main"   # install.sh's default --ref; a checkout on any other ref is a development checkout
ROOT = paths.PLUGIN_ROOT   # the fallback when the install recorded no clone (a seam for tests)
FETCH_TIMEOUT_S = 120
RESTART = "open panes keep running; they read the new cards and skills at their next start"
SESSION_FILES = ("skills", "templates", "agents", ".claude-plugin")
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def register(subparsers):
    p = subparsers.add_parser("update", help="pull the newest ORCHE, reinstall it and refresh every project")
    p.set_defaults(func=cmd_update)


class StepFailed(Exception):
    def __init__(self, step, detail):
        super().__init__(f"step {step} failed: {detail}")
        self.step = step


def progress(line) -> None:
    print(f"update: {line}", file=sys.stderr, flush=True)


def git(root, *argv, check=True, timeout=None) -> str:
    """git in `root`; never a terminal prompt; a failure's text is masked."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        res = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StepFailed(f"git {argv[0]}", f"timed out after {timeout} s") from None
    except OSError as exc:
        raise StepFailed(f"git {argv[0]}", mask.redact(str(exc))[0]) from None
    if check and res.returncode:
        detail = (res.stderr or res.stdout).strip() or f"exit {res.returncode}"
        raise StepFailed(f"git {argv[0]}", mask.redact(detail)[0])
    return res.stdout


def run_step(step, argv) -> str:
    """Run one install/refresh step; its output is echoed (stderr) and returned. Raises StepFailed."""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise StepFailed(step, str(exc)) from None
    text = (res.stdout or "") + (res.stderr or "")
    for line in text.splitlines():
        print(f"  {line}", file=sys.stderr)
    if res.returncode:
        raise StepFailed(step, f"exit {res.returncode}")
    return text


def version_of(text) -> str:
    match = VERSION_RE.search(text or "")
    return match.group(1) if match else "unknown"


def _state() -> dict:
    from . import install
    try:
        return install.load_state()
    except install.StateUnreadable:
        return {}


def _root(state) -> Path:
    """The clone the install recorded, else this package's own repository."""
    clone = state.get("clone")
    return Path(clone) if isinstance(clone, str) and clone else ROOT


def _development(root):
    """Why the checkout is a development one (known before any fetch), else None."""
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        return "uncommitted changes"
    branch = git(root, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip()
    if branch != REF:
        return f"on {branch or 'a detached HEAD'}, not {REF}"
    return None


def _mode() -> str:
    from . import install
    return install.installed_mode(Path.home())


def cmd_update(args):
    from .install import JANITOR_STEP, RECONCILE_STEP
    dry_run = args.dry_run
    state = _state()
    root = _root(state)
    if not (root / ".git").exists():
        return _refuse(args, f"not a git checkout: {root}; reinstall with the README prompt")
    try:
        old = version_of((root / "lib" / "orche" / "__init__.py").read_text())
        why, new, target, changed = _development(root), old, None, []
        if not why:
            progress(f"fetching {REF} from origin" + (" (dry run: no ref moves)" if dry_run else ""))
            if dry_run:   # FETCH_HEAD only: --refmap= keeps origin/main where it is
                git(root, "fetch", "--quiet", "--refmap=", "origin", REF, timeout=FETCH_TIMEOUT_S)
                target = "FETCH_HEAD"
            else:
                git(root, "fetch", "--quiet", "origin", REF, timeout=FETCH_TIMEOUT_S)
                target = f"origin/{REF}"
            ahead = int(git(root, "rev-list", "--count", f"{target}..HEAD").strip() or 0)
            why = f"{ahead} commit(s) not on origin/{REF}" if ahead else None
        if why:
            progress(f"development checkout ({why}): nothing pulled")
        else:
            new = version_of(git(root, "show", f"{target}:lib/orche/__init__.py"))
            changed = [p for p in git(root, "diff", "--name-only", "HEAD", target).splitlines()
                       if p.split("/", 1)[0] in SESSION_FILES]
        # resume: an earlier update stopped after the merge, or the development checkout runs another version
        resume = new == old and state.get("version") != old
        if new == old and not resume:
            note = "; development checkout, nothing pulled" if why else ""
            out(args, {"status": "current", "version": old, "development": why}, f"already up to date ({old}{note})")
            return EXIT_OK
    except StepFailed as exc:
        return _failed(args, exc, [])
    if resume:
        old = state.get("version") or "unknown"
    mode, keys = _mode(), sorted(registry.load()["projects"])
    py, orche_py = sys.executable, str(root / "bin" / "orche.py")
    install = [py, orche_py, "install", "--mode", mode, "--no-jev"] + (["--no-plugin-hint"] if mode == "plugin" else [])
    plugin = [["claude", "plugin", "marketplace", "update", "orche"], ["claude", "plugin", "update", "orche@orche"]] \
        if mode == "plugin" else []
    if dry_run:
        lines = [f"ORCHE {old} → {new} (dry run: nothing changed)"]
        lines += [] if resume else [f"would run: git merge --ff-only origin/{REF}"]
        lines += [f"would run: orche install --mode {mode} --no-jev"]
        lines += [f"would run: {' '.join(argv)}" for argv in plugin]
        lines += [f"would refresh {k}: orche init {k} --refresh" for k in keys]
        if changed:
            lines.append(f"then: {RESTART}")
        out(args, {"status": "dry-run", "old": old, "new": new, "mode": mode, "projects": keys,
                   "session_files": changed, "resume": resume, "development": why}, lines)
        return EXIT_OK
    done = [f"ORCHE {old} → {new}"]
    try:
        if resume:
            progress(f"resuming: the install records {old}, the checkout is {new}")
        else:
            progress(f"fast-forwarding to origin/{REF} ({new})")
            git(root, "merge", "--ff-only", "--quiet", target)
        progress(f"reinstalling ({mode} mode) with the new code")
        text = run_step("install", install).splitlines()
        heartbeat = [ln for ln in text if ln.startswith(RECONCILE_STEP)]
        old_janitor = [ln for ln in text if ln.startswith(f"{JANITOR_STEP}: remove")]
        for argv in plugin:
            progress(" ".join(argv))
            run_step("plugin update", argv)
    except StepFailed as exc:
        return _failed(args, exc, done)
    failed = []
    for key in keys:
        progress(f"refreshing {key}")
        try:
            text = run_step(f"refresh {key}", [py, orche_py, "init", key, "--refresh"])
            done.append(next((ln for ln in text.splitlines() if ln.startswith("refreshed ")), f"refreshed {key}"))
        except StepFailed as exc:
            failed.append(exc)
            done.append(f"{key}: {exc}")
    done += heartbeat[-1:] + old_janitor
    if changed or plugin or resume:
        done.append(RESTART)
    data = {"status": "failed" if failed else "updated", "old": old, "new": new, "mode": mode, "projects": keys,
            "session_files": changed, "resume": resume, "development": why, "lines": done}
    if failed:
        done.append(f"orche update: {len(failed)} project refresh(es) failed (above); re-run: orche init <project> --refresh")
    out(args, data, done)
    return EXIT_USER if failed else EXIT_OK


def _refuse(args, why) -> int:
    """Exit 2, nothing changed."""
    if args.json:
        out(args, {"status": "refused", "reason": why}, [])
    else:
        print(why)
    return EXIT_REFUSED


def _failed(args, exc, done) -> int:
    lines = done + [f"orche update: {exc}"]
    if exc.step == "install":
        lines.append("the checkout is updated; run orche update again to finish the install and the refresh")
    out(args, {"status": "failed", "step": exc.step, "error": str(exc), "lines": lines}, lines)
    return EXIT_USER
