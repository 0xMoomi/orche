"""Orche's own status line: `templates/statusline.sh` copied to `ORCHE_HOME/statusline.sh`, and
`statusLine.command` set to `bash <that copy>` in every settings.json install manages (settings.session_dirs).
Orche never edits a status line file that belongs to the user or to another tool.

Modes (install-state.json `statusline`: {"mode", "inner", "asked", "previous"}):
- orche: the script prints Orche's line (branch, model and effort, plan usage), then the project line;
- keep: the script runs the user's own command (`inner`, also written to `ORCHE_HOME/statusline.inner`, the
  one file the script reads) with the same stdin, then the project line;
- off: each settings file's statusLine is its own again (removed when there was none); Orche adds nothing.

`previous` maps each managed settings.json (absolute path) to its whole `statusLine` value before Orche (null:
none), recorded when Orche takes the file over and never replaced by Orche's own value; uninstall and `off`
put it back exactly. A value that is not a command line (a string, a list, another `type`) is kept there too,
but it is no `inner`: keep falls back to the other file's command, else Orche's line.

Default (`--statusline` absent): no mode recorded -> keep when a settings file names a foreign command (that
command is `inner`), else orche; a recorded orche or keep whose settings name a foreign command again (the
user or another tool changed it) -> keep with that command, pointed back at the script. When the home and the
profile settings name different foreign commands, the profile's (the one sessions read) wins. `asked` is set
by an explicit `--statusline`: the setup report then stops offering the choice. The first default keep prints
one note (TAKEN), which `orche update` forwards.

Migration: the guarded block (`# >>> orche >>>`) older versions appended to `~/.claude/statusline-command.sh`
or to the script a `statusLine.command` runs is removed (with a backup); nothing adds a block any more.
"""
import copy
import os
import shlex
from pathlib import Path

from .. import paths, util
from . import has_block, read_text, remove_block, write_file
from . import settings as settings_mod

STEP = "statusline"
START, END = "# >>> orche >>>", "# <<< orche <<<"
MODES = ("orche", "keep", "off")
SCRIPT, INNER = "statusline.sh", "statusline.inner"
OURS = "orche/" + SCRIPT   # any Orche status line script, whatever its ORCHE_HOME
OLD_SCRIPT = "statusline-command.sh"
WORDS = {"orche": "Orche's status line",
         "keep": "your status line with Orche's project line under it",
         "off": "off: Orche adds nothing to the status line"}
TAKEN = ("your status line is kept and Orche's project line is shown under it; say \"use Orche's status line\" "
         "to switch, or \"turn the status line off\"")
NO_KEEP = ("there is no status line of your own to keep (no statusLine command in settings.json and none "
           "stored); use --statusline orche or --statusline off")


def script_path() -> Path:
    return paths.orche_home() / SCRIPT


def inner_path() -> Path:
    return paths.orche_home() / INNER


def command() -> str:
    return f"bash {shlex.quote(str(script_path()))}"


def ours(cmd) -> bool:
    return isinstance(cmd, str) and (str(script_path()) in cmd or OURS in cmd)


def recorded(state) -> dict:
    rec = state.get("statusline") if isinstance(state, dict) else None
    if not isinstance(rec, dict) or rec.get("mode") not in MODES:
        return {}
    inner = rec.get("inner") if isinstance(rec.get("inner"), str) and rec.get("inner").strip() else None
    return {"mode": rec["mode"], "inner": None if ours(inner) else inner, "asked": bool(rec.get("asked"))}


def _command_of(line):
    """The command a statusLine value runs, else None (absent, or not a `type: command` object)."""
    if not isinstance(line, dict) or line.get("type", "command") != "command":
        return None
    cmd = line.get("command")
    return cmd.strip() if isinstance(cmd, str) and cmd.strip() else None


def _current(ctx) -> list:
    """[(settings path, data or None, statusLine.command or None)] for every session folder."""
    out = []
    for folder in settings_mod.session_dirs(ctx):
        path = folder / "settings.json"
        data, err = settings_mod.load(ctx, path)
        out.append((path, None if err else data, _command_of(data.get("statusLine")) if data else None))
    return out


def _previous(state) -> dict:
    rec = state.get("statusline") if isinstance(state, dict) else None
    prev = rec.get("previous") if isinstance(rec, dict) else None
    return dict(prev) if isinstance(prev, dict) else {}


def _foreign(rows, ctx=None) -> str:
    """The foreign command sessions read: the last session folder's (the profile's) wins; a note when two differ."""
    found = [(path, cmd) for path, _d, cmd in rows if cmd and not ours(cmd)]
    if ctx is not None and len({cmd for _p, cmd in found}) > 1:
        ctx.note(STEP, "note", None, f"settings files name different status lines; using {found[-1][0]}'s "
                                     "(the one sessions read)")
    return found[-1][1] if found else None


def keep_problem(ctx, state, wanted):
    """The plain error of `--statusline keep` with no command to keep, else None."""
    if wanted != "keep":
        return None
    return None if (_foreign(_current(ctx)) or recorded(state).get("inner")) else NO_KEEP


def decide(ctx, state, wanted, rows) -> dict:
    """The {"mode", "inner", "asked"} this install applies."""
    rec, found = recorded(state), _foreign(rows, ctx)
    if wanted:
        return {"mode": wanted, "inner": found or rec.get("inner"), "asked": True}
    if not rec:
        if found:
            ctx.note(STEP, "note", None, TAKEN)
        return {"mode": "keep" if found else "orche", "inner": found, "asked": False}
    if rec["mode"] != "off" and found:
        ctx.note(STEP, "note", None, f"your status line changed after install ({found}); Orche runs it now and "
                                     "adds its project line under it")
        return {**rec, "mode": "keep", "inner": found}
    return {**rec, "inner": found or rec.get("inner")}


def _set_command(ctx, path, data, cmd) -> None:
    """statusLine.command = `cmd` (type command; the user's other statusLine keys kept); None removes the
    key. An unreadable file is left untouched."""
    if data is None:
        ctx.note(STEP, "skip", path, "unreadable; left untouched (the settings step reports it)")
        return
    line = data.get("statusLine") if isinstance(data.get("statusLine"), dict) else {}
    if line.get("command") == cmd and line.get("type") == "command":
        ctx.note(STEP, "ok", path, "statusLine unchanged")
        return
    new = copy.deepcopy(data)
    new["statusLine"] = {**line, "type": "command", "command": cmd}
    write_file(ctx, STEP, path, settings_mod.dump(new), f"statusLine.command -> {cmd}")


def _restore(ctx, path, data, value) -> None:
    """statusLine = `value` exactly as it was before Orche; None removes the key."""
    if data is None:
        ctx.note(STEP, "skip", path, "unreadable; left untouched (the settings step reports it)")
        return
    if data.get("statusLine") == value and (value is not None or "statusLine" not in data):
        return
    new = copy.deepcopy(data)
    if value is None:
        new.pop("statusLine", None)
        detail = "statusLine removed (none before Orche)"
    else:
        new["statusLine"] = copy.deepcopy(value)
        detail = "statusLine restored"
    write_file(ctx, STEP, path, settings_mod.dump(new), detail)


def _script_of(ctx, cmd):
    """The script file a `bash <path>`, `sh <path>` or bare-path command runs, else None. `~` and `$HOME` are
    the target home's; under another home (`--home`, tests) only a file inside it."""
    try:
        argv = shlex.split(cmd or "")
    except ValueError:
        return None
    if len(argv) == 2 and os.path.basename(argv[0]) in ("bash", "sh"):
        argv = argv[1:]
    if len(argv) != 1:
        return None
    raw = argv[0]
    for lead in ("~/", "$HOME/", "${HOME}/"):
        if raw.startswith(lead):
            raw = str(ctx.home / raw[len(lead):])
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return None
    if ctx.home.resolve() != Path.home().resolve() and not Path(os.path.realpath(path)).is_relative_to(ctx.home.resolve()):
        return None
    return path


def _remove_blocks(ctx, rows) -> None:
    """The old guarded block out of `~/.claude/statusline-command.sh` and every script a statusLine runs."""
    seen = set()
    for path in [ctx.claude / OLD_SCRIPT, *(_script_of(ctx, cmd) for _p, _d, cmd in rows)]:
        if path is None or not path.is_file() or os.path.realpath(path) in seen:
            continue
        seen.add(os.path.realpath(path))
        try:
            text = read_text(path)
        except (OSError, UnicodeDecodeError):   # a compiled program or another encoding: no block in it
            continue
        if has_block(text, START, END):
            write_file(ctx, STEP, path, remove_block(text, START, END), "old Orche block removed")


def _write_own(ctx, rel, text) -> None:
    path = paths.orche_home() / rel
    if path.is_file() and read_text(path) == text:
        return
    if not ctx.dry_run:
        util.atomic_write(path, text)
    ctx.note(STEP, "write", path, "")


def _drop_own(ctx, rel) -> None:
    path = paths.orche_home() / rel
    if path.is_file():
        if not ctx.dry_run:
            path.unlink()
        ctx.note(STEP, "remove", path, "")


def install(ctx, state, wanted=None) -> None:
    """Migrate the old block, decide the mode, write the script (and `statusline.inner`) and point every
    session folder's statusLine at it (mode off: the user's command back). Records `state["statusline"]`."""
    rows = _current(ctx)
    _remove_blocks(ctx, rows)
    previous = _previous(state)
    for path, data, _cmd in rows:   # first takeover, or a foreign value set since: that file's own value
        if data is not None and not ours(_command_of(data.get("statusLine"))):
            previous[str(path)] = copy.deepcopy(data.get("statusLine"))
    sl = decide(ctx, state, wanted, rows)
    if sl["mode"] == "keep" and not sl["inner"]:   # a stored keep lost its command: Orche's line
        sl["mode"] = "orche"
    _write_own(ctx, SCRIPT, read_text(Path(ctx.plugin_root) / "templates" / SCRIPT))
    if sl["mode"] == "keep":
        _write_own(ctx, INNER, sl["inner"] + "\n")
    else:
        _drop_own(ctx, INNER)
    for path, data, cmd in rows:
        if sl["mode"] != "off":
            _set_command(ctx, path, data, command())
        elif ours(cmd):
            _restore(ctx, path, data, previous.get(str(path)))
    ctx.note(STEP, "mode", None, WORDS[sl["mode"]])
    if not ctx.dry_run:
        state["statusline"] = {**sl, "previous": previous}


def uninstall(ctx, state) -> None:
    """Each settings file's statusLine back to its own previous value (removed when there was none), the
    script, `statusline.inner` and any leftover block removed."""
    rows = _current(ctx)
    previous = _previous(state)
    for path, data, cmd in rows:
        if ours(cmd):
            _restore(ctx, path, data, previous.get(str(path)))
    _remove_blocks(ctx, rows)
    _drop_own(ctx, SCRIPT)
    _drop_own(ctx, INNER)
    if not ctx.dry_run and isinstance(state, dict):
        state.pop("statusline", None)


def rows(ctx, state) -> list:
    """`orche doctor`'s statusline rows: (level, check, detail)."""
    rec = recorded(state) or {"mode": None}
    if rec["mode"] is None:
        return [("WARN", "statusline", "not set up; say \"repair the setup\"; Main runs orche install")]
    if rec["mode"] == "off":
        return [("OK", "statusline", WORDS["off"])]
    current = _current(ctx)
    unreadable = [str(path) for path, data, _c in current if data is None]
    if unreadable:
        return [("WARN", "statusline", f"cannot read {', '.join(unreadable)}; fix the file, then say "
                                       "\"repair the setup\"; Main runs orche install")]
    if rec["mode"] == "keep" and not _stored_inner():
        return [("WARN", "statusline", "your status line command is not stored; Orche shows its own line; say "
                                       "\"repair the setup\"; Main runs orche install")]
    off = [str(path) for path, _d, cmd in current if not ours(cmd)]
    if off or not script_path().is_file():
        where = ", ".join(off) if off else str(script_path()) + " missing"
        return [("WARN", "statusline", f"{WORDS[rec['mode']]} is set, but {where} does not run Orche's script; "
                                       "say \"repair the setup\"; Main runs orche install")]
    return [("OK", "statusline", WORDS[rec["mode"]])]


def _stored_inner() -> str:
    try:
        return read_text(inner_path()).strip()
    except (OSError, UnicodeDecodeError):
        return ""


def asked_line(state):
    """The setup report's open choice, once: install kept an existing status line and nobody chose yet."""
    rec = recorded(state)
    if rec.get("mode") != "keep" or rec.get("asked"):
        return None
    return ("- Status line: Orche found your own status line and kept it, with Orche's project line under it. "
            "Ask the owner once with AskUserQuestion (header \"status line\"), three options: \"keep mine and add "
            "Orche's project line under it (current)\" -> `orche install --statusline keep`; \"use Orche's status "
            "line (branch, model and effort, plan usage, project line)\" -> `orche install --statusline orche`; "
            "\"mine only, without Orche's line\" -> `orche install --statusline off`. Run the picked command.")


def mode_words(state) -> str:
    rec = recorded(state)
    return WORDS[rec["mode"]] if rec else "not set up"

