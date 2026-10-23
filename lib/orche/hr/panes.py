"""Orca panes of units and projects: the pane command, a unit's create argv, project panes.

Every ORCHE pane (Main, divisions, features) runs `claude --permission-mode bypassPermissions`:
a woken pane runs with nobody watching and must not stop at a permission prompt.

A woken unit resumes its conversation by session id, never by name: units re-started under the
same name leave several sessions with that name, and `claude --resume <name>` then opens the
interactive picker (and fails on a name whose transcript Claude Code has cleaned up), so the
unattended pane never starts. `resume_flag` looks the newest named transcript's id up instead.
"""
import json
import os
import shlex
import sys
from pathlib import Path

from .. import config, orca, paths
from ..janitor import discover


def session_env(session) -> str:
    """`PATH=<plugin>/bin:$PATH ORCHE_SESSION=<session> ` prefix for every ORCHE pane command.

    ORCHE_SESSION lets hooks know the pane's role without guessing from cwd; the PATH entry puts
    `orche` on the pane's PATH (`$PATH` stays unexpanded: the pane's shell expands it).
    """
    return f"PATH={shlex.quote(str(paths.PLUGIN_ROOT / 'bin'))}:$PATH ORCHE_SESSION={shlex.quote(session)} "


def claude_projects() -> Path:
    """Claude Code's transcript root: `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "projects"


def _names(path, session) -> bool:
    """True when the transcript records `session` as its name (`custom-title` or `agent-name`)."""
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"custom-title"' not in raw and b'"agent-name"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if session in (rec.get("customTitle"), rec.get("agentName")):
                    return True
    except OSError:
        pass
    return False


def session_id(folder, session):
    """Id (file stem) of the newest transcript in `folder` named `session`, else None."""
    try:
        files = [f for f in Path(folder).glob("*.jsonl") if f.is_file() and not f.is_symlink()]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return next((f.stem for f in files if _names(f, session)), None)


def resume_flag(session, cwd) -> str:
    """`--resume <id>` for the newest transcript of `session` in `cwd`'s project dir; otherwise
    (none named, no project dir, no cwd) `--name <session>`, a fresh session (HANDOFF.md carries
    the context), with one stderr note."""
    folder = claude_projects() / discover.claude_slug(discover.real(str(cwd)) or cwd) if cwd else None
    sid = session_id(folder, session) if folder is not None and folder.is_dir() else None
    if sid:
        return f"--resume {shlex.quote(sid)}"
    print(f"orche: no transcript named {session}; the pane starts a fresh session", file=sys.stderr)
    return f"--name {shlex.quote(session)}"


def pane_command(session, *, resume, cwd=None) -> str:
    """`claude --permission-mode bypassPermissions` + `resume_flag(session, cwd)` for a paused unit,
    `--name <session>` for a fresh start (and for Main): no ORCHE pane ever stops at a permission
    prompt.

    Prefixed with `session_env(session)`. The one builder for every ORCHE pane, Main included."""
    flag = resume_flag(session, cwd) if resume else f"--name {session}"
    return (f"{session_env(session)}{config.load().get('claude_bin', 'claude')} "
            f"--permission-mode bypassPermissions {flag}")


def open_pane(worktree_id, session, command, *, dry_run=False):
    return orca.terminal("create", "--worktree", f"id:{worktree_id}", "--title", session,
                         "--command", command, dry_run=dry_run)


def unit_argv(unit, *, resume) -> tuple:
    """The `orca terminal create` argv of a division's or feature's pane."""
    command = pane_command(unit.session, resume=resume, cwd=unit.worktree)
    return ("--worktree", f"id:{unit.entry['worktree']}", "--title", unit.session, "--command", command)


def live_pane(term, title):
    """The live pane behind `term` (found by `title` in a `terminal list`), else None.

    A list read right after a pane was closed can still carry it, so Orca's `terminal read` of the
    handle decides: `running` is live; a timeout cannot tell (kept: never a second pane on a slow
    Orca) — any error naming one, as the CLI's own 10 s cut-off arrives as `bad_output` ("timeout
    after 10s"); another error or status (`exited`) means that pane is gone. When the read says
    neither, or the pane is gone, a fresh `terminal list` decides (a gone handle never counts).
    Raises orca.OrcaError when that list fails.
    """
    handle = str((term or {}).get("handle") or "")
    dead = not handle
    if handle:
        try:
            res = orca.terminal("read", "--terminal", handle, timeout=10)
        except orca.OrcaError as exc:
            if "timeout" in f"{exc.code} {exc}".lower():
                return term
            dead = True
        else:
            info = res.get("terminal") if isinstance(res, dict) else None
            status = info.get("status") if isinstance(info, dict) else None
            if status == "running":
                return term
            dead = status is not None
    want = orca.normalize_title(title)
    fresh = [t for t in orca.terminals_of(orca.terminal("list", timeout=10))
             if orca.normalize_title(t.get("title")) == want and not (dead and str(t.get("handle") or "") == handle)]
    return fresh[0] if fresh else None


def project_pane(proj, *, dry_run, switch=True) -> tuple:
    """Open the project's `main-<proj>` pane in its main checkout, or switch to it.

    One title lookup: no pane -> create one (`pane_command`: `claude --permission-mode
    bypassPermissions --name main-<proj>`, like every ORCHE pane); a pane -> `terminal switch` to it
    (with switch=False it is left as it is). A pane found is verified by handle (`live_pane`; after
    the switch attempt when switching): one Orca no longer has — the list lagged a close — is
    created anew, and a failed switch to a live pane raises. Returns (data, line); data["pane"] is
    "opened", "switched" or (switch=False) "open". Under dry_run the create argv is printed and no
    writing orca command runs. Raises orca.OrcaError.
    """
    title = proj["main_session"]
    command = pane_command(title, resume=False)
    term = orca.find_terminal_by_title(title)
    if term is not None and (dry_run or not switch):
        term = live_pane(term, title)
    elif term is not None:
        try:
            orca.terminal("switch", "--terminal", term["handle"])
            failed = None
        except orca.OrcaError as exc:
            failed = exc
        live = live_pane(term, title)
        if live is not None and failed is not None:
            raise failed
        if live is not None and str(live.get("handle")) != str(term.get("handle")):
            orca.terminal("switch", "--terminal", live["handle"])
        term = live
    data = {"session": title, "pane": "opened" if term is None else "switched" if switch else "open"}
    if dry_run:
        data["dry_run"] = True
    if term is None:
        result = open_pane(f"{proj['repo_id']}::{proj['path']}", title, command, dry_run=dry_run)
        if dry_run:
            data["argv"] = result["argv"]
        return data, (f"would open {title} pane (dry-run)" if dry_run else f"{title} pane opened")
    if not switch:
        return data, f"{title} pane already open"
    if dry_run:
        return data, f"would switch to {title}"
    return data, f"{title} pane switched to"
