"""Orca panes of units and projects: the pane command, a unit's create argv, project panes.

Division and feature panes run `claude --permission-mode bypassPermissions`; Main and Liaison
panes (`project_pane`) do not.
"""
import shlex

from .. import config, orca, paths

PANE_KINDS = ("main", "liaison")


def session_env(session) -> str:
    """`PATH=<plugin>/bin:$PATH ORCHE_SESSION=<session> ` prefix for every ORCHE pane command.

    ORCHE_SESSION lets hooks know the pane's role without guessing from cwd; the PATH entry puts
    `orche` on the pane's PATH (`$PATH` stays unexpanded: the pane's shell expands it).
    """
    return f"PATH={shlex.quote(str(paths.PLUGIN_ROOT / 'bin'))}:$PATH ORCHE_SESSION={shlex.quote(session)} "


def pane_command(session, *, resume) -> str:
    """`claude --permission-mode bypassPermissions --resume <session>` for a paused unit, `--name
    <session>` for a fresh start: a division or feature pane never stops at a permission prompt.

    Prefixed with `session_env(session)`. Main and liaison panes (`project_pane`) do not use it."""
    flag = "--resume" if resume else "--name"
    return (f"{session_env(session)}{config.load().get('claude_bin', 'claude')} "
            f"--permission-mode bypassPermissions {flag} {session}")


def open_pane(worktree_id, session, command, *, dry_run=False):
    return orca.terminal("create", "--worktree", f"id:{worktree_id}", "--title", session,
                         "--command", command, dry_run=dry_run)


def unit_argv(unit, *, resume) -> tuple:
    """The `orca terminal create` argv of a division's or feature's pane."""
    command = pane_command(unit.session, resume=resume)
    return ("--worktree", f"id:{unit.entry['worktree']}", "--title", unit.session, "--command", command)


def project_pane(proj, kind, *, dry_run, switch=True) -> tuple:
    """Open the project's `main-<proj>` / `liaison-<proj>` pane in its main checkout, or switch to it.

    One title lookup: no pane -> create one (`claude --name`, the liaison with `--agent liaison`);
    a pane -> `terminal switch` to it (with switch=False it is left as it is). Returns (data, line);
    data["pane"] is "opened", "switched" or (switch=False) "open". Under dry_run the create argv is
    printed and no writing orca command runs. Raises orca.OrcaError.
    """
    title = proj["main_session"] if kind == "main" else proj["liaison_session"]
    agent = "--agent liaison " if kind == "liaison" else ""
    command = f"{session_env(title)}{config.load().get('claude_bin', 'claude')} {agent}--name {title}"
    term = orca.find_terminal_by_title(title)
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
    orca.terminal("switch", "--terminal", term["handle"])
    return data, f"{title} pane switched to"
