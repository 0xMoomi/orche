"""Merge ORCHE's keys into `~/.claude/settings.json` (and take its hook entries out again).

Only our keys are touched: `cleanupPeriodDays`, `env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, and hook
entries matched by their exact command string. Every other key and hook entry is preserved in
order. `env.CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` is deliberately left untouched.
"""
import copy
import json
import re
from pathlib import Path

from .. import util
from . import write_file

STEP = "settings"
# The gstack careful hook is not ORCHE's: never registered or removed, only flagged by doctor.
CAREFUL_MARKER = "check-careful.sh"
CLEANUP_DAYS = 3650
ENV_SET = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
ENV_UNTOUCHED = "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"


def settings_path(ctx) -> Path:
    return ctx.claude / "settings.json"


def plugin_hooks(plugin_root) -> list:
    """[(event, matcher or None, command)] from hooks/hooks.json with the plugin root substituted."""
    data = util.read_json(Path(plugin_root) / "hooks" / "hooks.json", default={}) or {}
    out = []
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                out.append((event, entry.get("matcher"), cmd))
    return out


RUN_SH = re.compile(r'/hooks/run\.sh"? ([a-z_]+)$')


def legacy_command(plugin_root, command):
    """The pre-run.sh shape (`python3 "<root>/hooks/<hook>.py"`) of a `run.sh <hook>` command, else None."""
    m = RUN_SH.search(command)
    return f'python3 "{plugin_root}/hooks/{m.group(1)}.py"' if m else None


def current_command(plugin_root, command):
    """The `run.sh` shape of a legacy `python3 "<root>/hooks/<hook>.py"` command, else None."""
    m = re.fullmatch(r'python3 "(.*)/hooks/([a-z_]+)\.py"', command or "")
    return f'"{m.group(1)}/hooks/run.sh" {m.group(2)}' if m and m.group(1) == str(plugin_root) else None


def iter_commands(data):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for event, entries in (hooks or {}).items() if isinstance(hooks, dict) else ():
        for entry in entries if isinstance(entries, list) else ():
            for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else ():
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    yield event, hook["command"]


def has_command(data, event, command) -> bool:
    return any(ev == event and cmd == command for ev, cmd in iter_commands(data))


def add_hook(data, event, matcher, command) -> None:
    entry = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        entry = {"matcher": matcher, **entry}
    data.setdefault("hooks", {}).setdefault(event, []).append(entry)


def remove_commands(data, pairs) -> list:
    """Drop hook handlers whose (event, command) is in `pairs`; drop entries/events left empty. [(event, cmd)]."""
    removed = []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return removed
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept.append(entry)
                continue
            inner = [h for h in entry["hooks"] if not (isinstance(h, dict) and (event, h.get("command")) in pairs)]
            removed += [(event, h["command"]) for h in entry["hooks"] if h not in inner]
            if len(inner) == len(entry["hooks"]):
                kept.append(entry)
            elif inner:
                kept.append({**entry, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return removed


def dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load(ctx):
    """(data, error message or None). A missing file is an empty object."""
    path = settings_path(ctx)
    try:
        data = util.read_json(path, default={})
    except util.JsonError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"{path}: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict) \
            or not isinstance(data.get("env", {}), dict):
        return None, f"{path}: unexpected shape (top level, hooks and env must be objects)"
    return data, None


ORIG_CLEANUP = "cleanupPeriodDays"


def snapshot(data) -> dict:
    """The user's values of the keys ORCHE sets, recorded once (first install) for uninstall."""
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    out = {"env_present": "env" in data,
           ORIG_CLEANUP: {"present": ORIG_CLEANUP in data, "value": data.get(ORIG_CLEANUP)}}
    for key in ENV_SET:
        out[f"env.{key}"] = {"present": key in env, "value": env.get(key)}
    return out


def install(ctx, mode, state) -> None:
    """Merge our keys. Outside dry-run, `state` gains `originals` (first time only) and every hook
    command ORCHE added (`added_hooks`), recorded only after settings.json was written."""
    path = settings_path(ctx)
    data, err = load(ctx)
    if err:
        ctx.note(STEP, "error", path, f"{err}; left untouched")
        return
    originals = snapshot(data)
    new = copy.deepcopy(data)
    changes, added = [], []
    if new.get(ORIG_CLEANUP) != CLEANUP_DAYS:
        changes.append(f"set {ORIG_CLEANUP} {new.get(ORIG_CLEANUP, '(absent)')} -> {CLEANUP_DAYS}")
        new[ORIG_CLEANUP] = CLEANUP_DAYS
    env = new.setdefault("env", {})
    for key, value in ENV_SET.items():
        if env.get(key) != value:
            changes.append(f"set env.{key} {env.get(key, '(absent)')} -> {value}")
            env[key] = value
    ctx.note(STEP, "keep", None, f"env.{ENV_UNTOUCHED} left untouched "
                                 f"({'set' if ENV_UNTOUCHED in env else 'absent'}; ORCHE never writes it)")
    replaced = []
    if mode == "plugin":
        # The plugin's hooks.json registers the hooks: settings-mode entries left by an earlier
        # `--mode settings` install (either shape) would run every hook twice.
        replaced = remove_commands(new, orche_hook_pairs(ctx.plugin_root, state))
        changes += [f"remove hook {event} {command} (plugin mode: the plugin registers the hooks)"
                    for event, command in replaced]
    else:
        hooks = plugin_hooks(ctx.plugin_root)
        legacy = {(event, legacy_command(ctx.plugin_root, command)) for event, _, command in hooks}
        replaced = remove_commands(new, legacy)
        changes += [f"replace hook {event} {command} (now hooks/run.sh)" for event, command in replaced]
        for event, matcher, command in hooks:
            if has_command(new, event, command):
                ctx.note(STEP, "ok", None, f"hook {event} already registered: {command}")
                continue
            add_hook(new, event, matcher, command)
            changes.append(f"add hook {event}{f'[{matcher}]' if matcher else ''} {command}")
            added.append({"event": event, "command": command})
    if not changes:
        ctx.note(STEP, "ok", path, "unchanged")
    else:
        for change in changes:
            verb, _, detail = change.partition(" ")
            ctx.note(STEP, verb, None, detail)
        write_file(ctx, STEP, path, dump(new), f"{len(changes)} change(s)")
    if ctx.dry_run:
        return
    state.setdefault("originals", originals)
    known = state.get("added_hooks") if isinstance(state.get("added_hooks"), list) else []
    # Keep a recorded hook only while its exact command is still in settings.json. A recorded shape
    # that is gone but whose other shape is present (old python3 record, run.sh entry kept) is
    # re-recorded in the present shape, so `orche uninstall` still finds it.
    present = []
    for h in [*known, *added]:
        if not isinstance(h, dict):
            continue
        event, command = h.get("event"), h.get("command") or ""
        if not has_command(new, event, command):
            other = legacy_command(ctx.plugin_root, command) or current_command(ctx.plugin_root, command)
            if not (other and has_command(new, event, other)):
                continue
            h = {"event": event, "command": other}
        if h not in present:
            present.append(h)
    known[:] = present
    state["added_hooks"] = known


def orche_hook_pairs(plugin_root, state=None) -> set:
    """(event, command) of every settings-mode ORCHE hook entry: the six plugin hooks in both shapes
    (`"<root>/hooks/run.sh" <hook>` and the older `python3 "<root>/hooks/<hook>.py"`), plus the
    recorded `added_hooks` in both shapes."""
    pairs = set()
    for event, _, command in plugin_hooks(plugin_root):
        pairs |= {(event, command), (event, legacy_command(plugin_root, command))}
    for event, command in _added_pairs(state or {}, plugin_root):
        if command:
            pairs.add((event, command))
    return pairs


def settings_hooks(data, plugin_root) -> list:
    """[(event, command)] ORCHE hook entries present in settings.json (either shape)."""
    pairs = orche_hook_pairs(plugin_root)
    return [(event, command) for event, command in iter_commands(data) if (event, command) in pairs]


def _added_pairs(state, plugin_root) -> set:
    """Recorded hook commands, each in both shapes (`python3 "<root>/hooks/x.py"` and `run.sh x`).
    A gstack careful entry an older ORCHE recorded is skipped: ORCHE no longer owns it."""
    hooks = state.get("added_hooks") if isinstance(state.get("added_hooks"), list) else []
    pairs = {(h.get("event"), h.get("command")) for h in hooks
             if isinstance(h, dict) and CAREFUL_MARKER not in str(h.get("command"))}
    for event, command in list(pairs):
        other = legacy_command(plugin_root, command or "") or current_command(plugin_root, command)
        if other:
            pairs.add((event, other))
    return pairs


def _restore_one(container, key, label, orig, ours, changes, kept) -> None:
    """Restore/remove `key` only while it still holds the value ORCHE set; a later user edit is kept."""
    if not isinstance(orig, dict) or not orig or key not in container and not orig.get("present"):
        return
    current = container.get(key)
    if orig.get("present") and current == orig.get("value"):
        return
    if key not in container or current != ours:
        kept.append(f"{label}={current if key in container else '(absent)'}")
        return
    if orig.get("present"):
        changes.append(f"restore {label} -> {orig.get('value')}")
        container[key] = orig.get("value")
    else:
        changes.append(f"remove {label} (absent before install)")
        del container[key]


def _restore(new, originals):
    """Put back the user's pre-install values (or drop keys that were absent): (changes, kept)."""
    changes, kept = [], []
    _restore_one(new, ORIG_CLEANUP, ORIG_CLEANUP, originals.get(ORIG_CLEANUP), CLEANUP_DAYS, changes, kept)
    env = new.get("env") if isinstance(new.get("env"), dict) else None
    if env is not None:
        for key, ours in ENV_SET.items():
            _restore_one(env, key, f"env.{key}", originals.get(f"env.{key}"), ours, changes, kept)
        if env == {} and originals.get("env_present") is False:
            del new["env"]
    return changes, kept


def uninstall(ctx, state) -> None:
    """Remove only the hook commands ORCHE recorded as added and restore the recorded originals.
    Outside dry-run, the consumed `added_hooks`/`originals` are dropped from `state`."""
    path = settings_path(ctx)
    if not path.exists():
        ctx.note(STEP, "skip", path, "not present")
        return
    data, err = load(ctx)
    if err:
        ctx.note(STEP, "error", path, f"{err}; left untouched")
        return
    new = copy.deepcopy(data)
    removed = remove_commands(new, _added_pairs(state, ctx.plugin_root))
    originals = state.get("originals") if isinstance(state.get("originals"), dict) else {}
    restored, kept = _restore(new, originals)
    for item in kept:
        ctx.note(STEP, "kept", item, "changed after install")
    if not removed and not restored:
        ctx.note(STEP, "ok", path, "no ORCHE hook entries or values to restore")
    else:
        for event, command in removed:
            ctx.note(STEP, "remove", None, f"hook {event} {command}")
        for change in restored:
            verb, _, detail = change.partition(" ")
            ctx.note(STEP, verb, None, detail)
        write_file(ctx, STEP, path, dump(new), f"{len(removed)} hook(s) removed, {len(restored)} value(s) restored")
    if not ctx.dry_run:
        for key in ("added_hooks", "originals"):
            state.pop(key, None)


def registered(data, plugin_root) -> list:
    """Plugin hook commands missing from `data` (empty list = all six registered)."""
    return [cmd for ev, _, cmd in plugin_hooks(plugin_root) if not has_command(data, ev, cmd)]


def plugin_enabled(data) -> bool:
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    return isinstance(plugins, dict) and any(str(k).split("@")[0] == "orche" and v for k, v in plugins.items())
