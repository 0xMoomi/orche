"""Shared test helpers: put lib/ on sys.path and isolate ORCHE_HOME per test."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

# Process-wide default for every test module (TempHomeCase re-applies it per test): the Jev key
# store never touches the real keychain and no real dialog opens.
from orche import jev_setup as _jev_setup, jevkey as _jevkey  # noqa: E402

_jevkey.PLATFORM = "linux"
_jevkey.SECURITY = "/nonexistent/orche-test/security"
_jev_setup.OSASCRIPT = "/nonexistent/orche-test/osascript"


_NO_CLAUDE_DIR = None
NO_CLAUDE = "#!/bin/sh\necho 'tests never run the real claude (support.NO_CLAUDE)' >&2\nexit 1\n"


def no_claude_dir() -> str:
    """A directory whose `claude` always fails. It goes first on PATH for the whole test process
    (below), so code that falls back to the default `claude_bin` (e.g. the intake at the end of
    `orche init`) can never start the developer's real Claude Code, whatever the test's base
    class; tests that want a worker set config `claude_bin`."""
    global _NO_CLAUDE_DIR
    if _NO_CLAUDE_DIR is None:
        import atexit
        import shutil
        _NO_CLAUDE_DIR = tempfile.mkdtemp(prefix="orche-no-claude-")
        atexit.register(shutil.rmtree, _NO_CLAUDE_DIR, True)
        stub = Path(_NO_CLAUDE_DIR) / "claude"
        stub.write_text(NO_CLAUDE)
        stub.chmod(0o755)
    return _NO_CLAUDE_DIR


os.environ["PATH"] = no_claude_dir() + os.pathsep + os.environ.get("PATH", "")


class TempHomeCase(unittest.TestCase):
    """Runs each test with ORCHE_HOME pointing at a fresh temporary directory (and PATH restored
    afterwards, the failing `claude` of no_claude_dir still first)."""

    def setUp(self):
        super().setUp()
        self._saved_path = os.environ.get("PATH", "")
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # An ORCHE pane exports ORCHE_SESSION / ORCA_TERMINAL_HANDLE (and maybe more): every test
        # starts without any ORCHE_* / ORCA_* variable, and the caller's values come back after.
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith(("ORCHE_", "ORCA_"))}
        for key in self._saved_env:
            del os.environ[key]
        os.environ["ORCHE_HOME"] = str(self.home)
        # The Jev key store must never reach the developer's real keychain nor pop a real dialog:
        # every test starts on the file backend (under this temp ORCHE_HOME) with no `security`
        # or `osascript`; keychain/dialog tests put fakes on PATH and switch these seams back.
        from orche import jev_setup, jevkey
        self._jev_seams = (jevkey.PLATFORM, jevkey.SECURITY, jev_setup.OSASCRIPT)
        jevkey.PLATFORM = "linux"
        jevkey.SECURITY = str(self.home / "no-such-security")
        jev_setup.OSASCRIPT = str(self.home / "no-such-osascript")

    def tearDown(self):
        from orche import jev_setup, jevkey
        jevkey.PLATFORM, jevkey.SECURITY, jev_setup.OSASCRIPT = self._jev_seams
        for key in [k for k in os.environ if k.startswith(("ORCHE_", "ORCA_"))]:
            del os.environ[key]
        os.environ.update(self._saved_env)
        os.environ["PATH"] = self._saved_path
        self._tmp.cleanup()
        super().tearDown()


ABSOLUTE_PYTHONS = "/opt/homebrew/bin/python3 /usr/local/bin/python3"


def plugin_copy(dest: Path, *rels: str) -> Path:
    """Copy plugin files (e.g. `hooks/run.sh`, `lib/find_python.sh`) under `dest`, keeping modes.

    The interpreter finder's absolute candidates are pointed at /nowhere, so this machine's own
    Homebrew python cannot satisfy a "no Python 3.11+ found" test.
    """
    for rel in rels:
        src, out = ROOT / rel, dest / rel
        text = src.read_text()
        if rel == "lib/find_python.sh":
            assert ABSOLUTE_PYTHONS in text, "finder candidates changed; update support.plugin_copy"
            text = text.replace(ABSOLUTE_PYTHONS, "/nowhere/python3 /nowhere/local/python3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        out.chmod(src.stat().st_mode & 0o777)
    return dest


# A fake `claude` for runner/doctor tests, selected through config `claude_bin` (never the real one).
FAKE_CLAUDE = """#!/bin/sh
d=$(dirname "$0")
case "$1" in
  --help) cat "$d/help.txt"; exit 0 ;;
  --version) if [ -f "$d/version" ]; then cat "$d/version"; else echo "9.9.9 (Claude Code)"; fi; exit 0 ;;
esac
if [ -f "$d/ignore_term" ]; then trap '' TERM; fi
cat > "$d/stdin.txt"
printf '%s\\n' "$@" > "$d/argv.txt"
printf '%s\\n' "$ORCHE_SESSION" > "$d/session.txt"
pwd -P > "$d/cwd.txt"
printf '%s\\n' "$PATH" > "$d/path.txt"
if [ -f "$d/report_to" ]; then echo "worker wrote this" > "$(cat "$d/report_to")"; fi
env > "$d/env.txt"
if [ -f "$d/action.sh" ]; then sh "$d/action.sh"; fi
if [ -f "$d/sleep" ]; then echo $$ > "$d/pid"; sleep "$(cat "$d/sleep")"; fi
if [ -f "$d/reply.json" ]; then cat "$d/reply.json"; fi
exit "$(cat "$d/exit" 2>/dev/null || echo 0)"
"""
HELP_FULL = ("Usage: claude [options]\n  --setting-sources <s>\n  --append-system-prompt-file <f>\n"
             "  --disallowedTools, --disallowed-tools <tools...>\n  --tools <tools...>\n")
HELP_OLD = "Usage: claude [options]\n  --setting-sources <s>\n  --append-system-prompt <p>\n"
# claude 2.1.302: the file form only appears inside another option's description.
HELP_BRACKET = ("Usage: claude [options]\n  --append-system-prompt <prompt>  Append a system prompt\n"
                "  --bare  Minimal mode: ... --system-prompt[-file],\n"
                "          --append-system-prompt[-file], --add-dir\n  --setting-sources <s>\n")


def write_fake_claude(folder: Path, help_text=HELP_FULL) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    (folder / "help.txt").write_text(help_text)
    return script
