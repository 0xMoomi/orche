"""Python version guard, imported by `bin/orche.py` and every hook BEFORE any other orche module.

Keep this file importable by old interpreters (no annotations, no 3.10+ syntax): it is the one
thing that must still run on macOS's stock /usr/bin/python3 (3.9) to say why ORCHE is idle.
"""
import os
import sys

MINIMUM = (3, 11)


def ok(version=None):
    return tuple((version or sys.version_info)[:2]) >= MINIMUM


def message(version=None, executable=None):
    v = version or sys.version_info
    return "ORCHE needs Python 3.11+ (found {}.{} at {})".format(v[0], v[1], executable or sys.executable)


def _log(text):
    """Append to ORCHE_HOME/logs/hooks.log without importing orche (fail-silent)."""
    try:
        home = os.environ.get("ORCHE_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "orche")
        logs = os.path.join(os.path.expanduser(home), "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "hooks.log"), "a", encoding="utf-8") as fh:
            fh.write("python-guard\thooks\t{}\n".format(text))
    except Exception:  # noqa: BLE001
        pass


def check_cli():
    """CLI: print the reason and exit 1 on an old interpreter."""
    if not ok():
        sys.stderr.write("orche: " + message() + "\n")
        sys.exit(1)


def check_hook(name):
    """Hooks: log the reason and return False (the caller exits 0 — fail-open, gates allow)."""
    if ok():
        return True
    _log("{}: {}".format(name, message()))
    return False
