"""The Orche plugin as Claude Code itself reports it, per Claude config folder.

`claude plugin list --json` prints an array with one object per installation; `id` (`name@marketplace`) and
`enabled` ("whether the plugin is enabled in your merged settings") are always present. It is asked once per
folder, with that folder as CLAUDE_CONFIG_DIR (unset for the default `~/.claude`), with config `claude_bin`
as every headless call. No claude, an error or any other answer counts as "not on".
"""
import json
import os
from pathlib import Path

from .. import config, shell

PLUGIN_ID = "orche@orche"
LIST_S = 30


def env_for(config_dir) -> dict:
    """This process's environment for a session of `config_dir`."""
    env = dict(os.environ)
    if Path(config_dir) == Path.home() / ".claude":
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def claude(config_dir, *argv, timeout=LIST_S):
    """`claude <argv>` for a session of `config_dir` (a shell.Result)."""
    return shell.run([config.load().get("claude_bin") or "claude", *argv], timeout=timeout, env=env_for(config_dir))


def enabled(config_dir) -> bool:
    """Whether `claude plugin list --json` shows orche@orche enabled for `config_dir`."""
    res = claude(config_dir, "plugin", "list", "--json")
    try:
        rows = json.loads(res.out) if res.ok else []
    except ValueError:
        return False
    return any(isinstance(row, dict) and row.get("id") == PLUGIN_ID and row.get("enabled") is True
               for row in (rows if isinstance(rows, list) else ()))


def on(config_dirs) -> bool:
    """The plugin is on for every one of `config_dirs` (each asked at most once)."""
    return all(enabled(d) for d in config_dirs)
