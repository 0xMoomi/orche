"""LaunchAgent helper shared by the janitor schedule and the reconcile job (design c10 §2.4).

A job is a plist under <home>/Library/LaunchAgents, rendered from templates/launchd/<label>.plist,
written atomically 0644. Loading uses the current launchctl verbs: `bootout gui/<uid>/<label>`
(failure ignored: not loaded yet) then `bootstrap gui/<uid> <plist>`; uninstall is bootout + remove.
launchctl only runs when `home` is the user's real home, so `orche install --home <tmp>` writes
the file and never loads it.
"""
import os
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .. import paths, shell, templates, util

DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def plist_path(label, home=None) -> Path:
    return Path(home or Path.home()).expanduser() / "Library" / "LaunchAgents" / f"{label}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def _live(home) -> bool:
    try:
        return Path(home or Path.home()).expanduser().resolve() == Path.home().resolve()
    except OSError:
        return False


def render(label, **vals) -> str:
    """The template for `label` with ORCHE_HOME, PATH and ORCHE_BIN filled in (XML-escaped), plus `vals`."""
    base = {"ORCHE_BIN": str(paths.PLUGIN_ROOT / "bin" / "orche"), "ORCHE_HOME": str(paths.orche_home()),
            "PATH": os.environ.get("PATH") or DEFAULT_PATH}
    base.update({k: str(v) for k, v in vals.items()})
    text = (templates.TEMPLATES_ROOT / "launchd" / f"{label}.plist").read_text(encoding="utf-8")
    return templates.render(text, **{k: escape(v) for k, v in base.items()})


def argv_install(label, home=None) -> list:
    """bootout (ignored when not loaded), enable (a job disabled by 0.6.x's `unload -w` fails to
    bootstrap otherwise; enabling an enabled job is a no-op), bootstrap."""
    return [["launchctl", "bootout", f"{domain()}/{label}"],
            ["launchctl", "enable", f"{domain()}/{label}"],
            ["launchctl", "bootstrap", domain(), str(plist_path(label, home))]]


def argv_uninstall(label) -> list:
    return [["launchctl", "bootout", f"{domain()}/{label}"]]


def install(label, text, home=None, dry_run=False) -> dict:
    """Write the plist and (real home only) load it. {"plist", "argv", "loaded", "error"}."""
    target = plist_path(label, home)
    live = _live(home)
    out = {"plist": str(target), "argv": argv_install(label, home) if live else [], "loaded": False, "error": None}
    if dry_run:
        return out
    util.atomic_write(target, text, mode=0o644)
    if not live:
        return out
    bootout, enable, bootstrap = out["argv"]
    shell.run(bootout, timeout=10)
    shell.run(enable, timeout=10)
    res = shell.run(bootstrap, timeout=10)
    if res.ok:
        out["loaded"] = True
    else:
        out["error"] = (res.err or res.out or f"exit {res.code}").strip()
    return out


def uninstall(label, home=None, dry_run=False) -> dict:
    """Unload (real home only) and remove the plist. {"plist", "argv", "missing", "error"}."""
    target = plist_path(label, home)
    live = _live(home)
    out = {"plist": str(target), "argv": argv_uninstall(label) if live else [], "missing": not target.exists(),
           "error": None}
    if dry_run or out["missing"]:
        return out
    if live:
        res = shell.run(out["argv"][0], timeout=10)
        if not res.ok:
            out["error"] = (res.err or res.out or f"exit {res.code}").strip()
    target.unlink()
    return out


def loaded(label) -> bool:
    """`launchctl print gui/<uid>/<label>` answered: the job is loaded."""
    return shell.run(["launchctl", "print", f"{domain()}/{label}"], timeout=3).ok


def kickstart(label, kill=False):
    """`launchctl kickstart gui/<uid>/<label>`: run the job now; a running instance is left alone (a
    healthy pass finishes), or with `kill` (-k) killed first."""
    return shell.run(["launchctl", "kickstart", *(["-k"] if kill else []), f"{domain()}/{label}"], timeout=10)


RECONCILE = "com.orche.reconcile"


def reconcile_plist(every_s) -> str:
    """The reconcile job: `orche reconcile` every `every_s` s and at load, output to logs/reconcile.log.
    CLAUDE_CONFIG_DIR joins ORCHE_HOME and PATH only when set at install time."""
    import plistlib
    text = render(RECONCILE, EVERY_S=int(every_s))
    extra = os.environ.get("CLAUDE_CONFIG_DIR")
    if not extra:
        return text
    data = plistlib.loads(text.encode("utf-8"))
    data["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] = extra
    return plistlib.dumps(data).decode("utf-8")


def job_loaded(home=None) -> bool:
    """macOS, `home` the real home, and the reconcile job loaded (`launchctl print`)."""
    return sys.platform == "darwin" and _live(home) and loaded(RECONCILE)
