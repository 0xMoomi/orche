"""`orche doctor`: one OK/WARN/FAIL line per check. FAIL only for python < 3.11 (this interpreter
or the `python3` on PATH the hooks run), an invalid registry, or an unwritable ORCHE_HOME."""
import os
import re
import shutil
import sys

from .. import orca, paths, registry, shell
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


def python3_on_path():
    """(level, name, detail) for the `python3` the plugin hooks run (a bare `python3` on PATH)."""
    exe = shutil.which("python3")
    if not exe:
        return "FAIL", "python3 on PATH", "not found (the hooks run `python3`)"
    res = shell.run([exe, "--version"], timeout=10)
    m = re.search(r"Python (\d+)\.(\d+)", (res.out or "") + (res.err or ""))
    if not m:
        return "WARN", "python3 on PATH", f"{exe}: cannot read its version"
    version = (int(m.group(1)), int(m.group(2)))
    if version < (3, 11):
        return "FAIL", "python3 on PATH", f"{exe} is {version[0]}.{version[1]}: hooks do nothing (needs 3.11+)"
    return "OK", "python3 on PATH", f"{exe} {version[0]}.{version[1]}"


def checks(ctx) -> list:
    out = []

    def add(level, name, detail):
        out.append({"check": name, "level": level, "detail": detail})

    py = sys.version_info
    add("OK" if py >= (3, 11) else "FAIL", "python", f"{py.major}.{py.minor}.{py.micro}"
        + ("" if py >= (3, 11) else " (needs 3.11+)"))
    add(*python3_on_path())
    try:
        orca.status(timeout=5)
        add("OK", "orca", "orca status answered")
    except orca.OrcaError as exc:
        add("WARN", "orca", f"orca status failed: {exc}")
    for tool in TOOLS:
        found = shutil.which(tool)
        add("OK" if found else "WARN", f"tool {tool}", found or "not found on PATH")
    level, detail = _orche_home()
    add(level, "orche_home", detail)
    try:
        reg = registry.load()
        add("OK", "registry", f"valid ({len(reg['projects'])} project(s))")
    except registry.RegistryError as exc:
        add("FAIL", "registry", str(exc).splitlines()[0] if str(exc) else "invalid")
    data, err = settings.load(ctx)
    if err:
        add("WARN", "settings", err)
        data = {}
    twice = settings.double_registered(data, ctx.plugin_root)
    if twice:
        add("WARN", "hooks", "hooks registered twice (plugin + settings): remove one "
                             "(orche uninstall or disable the plugin)")
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
