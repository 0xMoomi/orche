"""Orca panes of units and projects: the pane command, a unit's create argv, project panes.

Every ORCHE pane (Main, divisions, features) runs `claude --permission-mode bypassPermissions`:
a woken pane runs with nobody watching and must not stop at a permission prompt. A pane carries no
autocompact window of its own: the installer's `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` applies to the
model's window (with a 200K window it compacted at about 100K, under the fixed prefix of a session
with many plugins, so Main compacted every turn).

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

RESUMED = "resumed: say in five lines what was done last, what runs now and what waits on the owner"


def session_env(session) -> str:
    """`PATH=<plugin>/bin:$PATH ORCHE_SESSION=<session> CLAUDE_CODE_PROMPT_CACHE_TTL=1h [NAME=value ...] ` prefix for every ORCHE pane command.

    ORCHE_SESSION lets hooks know the pane's role without guessing from cwd; the PATH entry puts
    `orche` on the pane's PATH (`$PATH` stays unexpanded: the pane's shell expands it); each config
    `team.env` pair follows, shell-quoted (the team's hook switches). Every pane also gets
    `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`: on usage credits Claude Code drops the prompt cache to five minutes,
    so a pane waking after a longer idle gap would rewrite its whole context (headless runs have no idle
    gap and do not get it). Main (`main-<project>`) and division heads (`div-<project>-<division>`) also
    get `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`: their subagents and shells finish inside the turn, so no
    finished background task starts a turn that re-reads the pane's whole context (ORCHE wakes them).
    Feature panes do not.
    """
    team = "".join(f"{name}={shlex.quote(value)} " for name, value in config.team_env(config.load()).items())
    foreground = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 " if str(session).startswith(("main-", "div-")) else ""
    return (f"PATH={shlex.quote(str(paths.PLUGIN_ROOT / 'bin'))}:$PATH ORCHE_SESSION={shlex.quote(session)} "
            f"CLAUDE_CODE_PROMPT_CACHE_TTL=1h {foreground}{team}")


def _claude_dir() -> Path:
    """Claude Code's config dir: `$CLAUDE_CONFIG_DIR`, else `~/.claude`."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude"


def claude_projects() -> Path:
    """Claude Code's transcript root: `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`."""
    return _claude_dir() / "projects"


def plugin_install_path(plugin_id) -> Path | None:
    """Where the team plugin `plugin_id` is installed for this user: the `installPath` of its user-scope entry
    (`plugins[<id>]`, a dict or a list of dicts) in `<claude config dir>/plugins/installed_plugins.json`, when
    that directory exists; else None (not installed, or the file unreadable)."""
    try:
        data = json.loads((_claude_dir() / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        entries = data["plugins"][plugin_id]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for entry in entries if isinstance(entries, list) else [entries]:
        if isinstance(entry, dict) and entry.get("scope") == "user" and isinstance(entry.get("installPath"), str):
            where = Path(entry["installPath"]).expanduser()
            if where.is_dir():
                return where
    return None


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
    """`claude --permission-mode bypassPermissions` + `resume_flag(session, cwd)` for a reopened pane (Main included), `--name <session>` for a fresh
    start: no ORCHE pane ever stops at a permission prompt.

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


def project_pane(proj, *, dry_run, switch=True, resume=True) -> tuple:
    """Open the project's `main-<proj>` pane in its main checkout, or switch to it.

    One title lookup: no pane -> create one (`pane_command`: resume the newest transcript named
    `main-<proj>` in the main checkout, else `--name main-<proj>`; onboarding passes resume=False),
    then, when resuming, post one urgent `info` from orche (RESUMED) so Main says where things stand
    once it is idle; a pane -> `terminal switch` to it
    (with switch=False it is left as it is). A pane found is verified by handle (`live_pane`; after
    the switch attempt when switching): one Orca no longer has — the list lagged a close — is
    created anew, and a failed switch to a live pane raises. Returns (data, line); data["pane"] is
    "opened", "switched" or (switch=False) "open". Under dry_run the create argv is printed and no
    writing orca command runs. Raises orca.OrcaError.
    """
    title = proj["main_session"]
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
        command = pane_command(title, resume=resume, cwd=proj["path"])
        result = open_pane(f"{proj['repo_id']}::{proj['path']}", title, command, dry_run=dry_run)
        if dry_run:
            data["argv"] = result["argv"]
        elif resume:
            from .. import inbox, log
            try:
                inbox.post(inbox.make(title, "orche", title, "info", urgent=True, summary=RESUMED))
            except OSError as exc:   # the pane is open; Main still has the card
                log.log("hooks", f"project_pane: {title}: resumed note not posted: {exc}")
        return data, (f"would open {title} pane (dry-run)" if dry_run else f"{title} pane opened")
    if not switch:
        return data, f"{title} pane already open"
    if dry_run:
        return data, f"would switch to {title}"
    return data, f"{title} pane switched to"
