"""install.sh driven as a subprocess against a fake world: temp HOME, a local bare repo as the
clone source (no network), fake `claude` / `orca` / `osascript` / `security` on PATH.

The fakes record what they were asked into files next to them. The real `claude plugin` state,
Orca, the keychain and the developer's home are never reached: PATH is the fake bin folder, then
/usr/bin and /bin, and HOME / ORCHE_HOME / SHELL are set per test.
"""
import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from support import ABSOLUTE_PYTHONS, ROOT

INSTALL_SH = ROOT / "install.sh"
WINDOWS_MSG = ("Windows is not supported: Orca is a macOS app and the ORCHE CLI relies on POSIX process "
               "control (ps, process groups, signals) and the macOS keychain. WSL2 is unverified.")
MARKER = "# orche"

FAKE_CLAUDE = r"""#!/bin/sh
# Fake `claude`: logs every call, keeps marketplace/plugin state in files next to itself.
d=$(dirname "$0")
printf '%s\n' "$*" >> "$d/claude-argv.txt"
case "$*" in
  --version) echo "2.1.302 (Claude Code)" ;;
  --help) printf 'Usage: claude [options]\n  --setting-sources <s>\n  --tools <tools...>\n' ;;
  "plugin --help") cat "$d/plugin-help.txt" ;;
  "plugin marketplace list --json")
    if [ -f "$d/marketplace-json" ]; then cat "$d/marketplace-json"
    elif [ -f "$d/marketplace" ]; then
      printf '[{"name": "orche", "source": "directory", "path": "%s"}]\n' "$(cat "$d/marketplace")"
    else echo '[]'; fi ;;
  "plugin marketplace add "*) printf '%s' "$4" > "$d/marketplace" ;;
  "plugin marketplace update orche") [ -f "$d/marketplace" ] || exit 1 ;;
  "plugin list --json")
    if [ -f "$d/plugin-json" ]; then cat "$d/plugin-json"
    elif [ -f "$d/installed" ]; then echo '[{"id": "orche@orche", "enabled": true}]'; else echo '[]'; fi ;;
  "plugin install orche@orche")
    [ -f "$d/marketplace" ] || { echo "marketplace orche not found" >&2; exit 1; }
    [ ! -f "$d/fail-install" ] || { echo "install failed" >&2; exit 1; }
    touch "$d/installed"
    # Like the real CLI: enable the plugin in ~/.claude/settings.json (doctor's "hooks" row).
    python3 -c 'import json, os
p = os.path.expanduser("~/.claude/settings.json")
data = json.load(open(p)) if os.path.exists(p) else {}
data.setdefault("enabledPlugins", {})["orche@orche"] = True
json.dump(data, open(p, "w"), indent=2)' ;;
  "plugin update orche@orche") [ -f "$d/installed" ] || exit 1 ;;
  *) echo "fake claude: unexpected: $*" >&2; exit 3 ;;
esac
"""
PLUGIN_HELP = """Usage: claude plugin|plugins [options] [command]

Commands:
  install|i [options] <plugin>         Install a plugin from available marketplaces
  list [options]                       List installed plugins
  marketplace                          Manage Claude Code marketplaces
  update [options] <plugin>            Update a plugin to the latest version
"""
PLUGIN_HELP_OLD = """Usage: claude plugin [options] [command]

Commands:
  validate [options] <path>            Validate a plugin or marketplace manifest
"""
FAKE_ORCA = r"""#!/bin/sh
printf '%s\n' "$*" >> "$(dirname "$0")/orca-argv.txt"
case "$1 $2" in
  "status "*) echo '{"ok": true, "result": {"running": true}}' ;;
  "repo list") echo '{"ok": true, "result": []}' ;;
  *) echo '{"ok": true, "result": {}}' ;;
esac
"""
FAKE_OSASCRIPT = """#!/bin/sh
# The Jev dialog: always Cancel, so no key is ever typed.
touch "$(dirname "$0")/osascript-called"
echo "execution error: User canceled. (-128)" >&2
exit 1
"""
# The keychain: nothing stored (exit 44 = not found); every call and any stdin is logged.
FAKE_SECURITY = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/security-argv.txt"
case "$1" in -i) cat >> "$d/security-stdin.txt" ;; esac
exit 44
"""
# launchd: every call logged, and every answer 113 (what `launchctl print` says for a job that is not
# loaded), so `orche install` writes the reconcile plist under the temp HOME, its bootstrap fails (a
# warn line) and no first-pass check kicks anything. 0.7.2 review #13: without this stub PATH's
# /bin/launchctl booted out the developer's real com.orche.reconcile and loaded the temp clone's.
FAKE_LAUNCHCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/launchctl-argv.txt"
exit 113
"""
# Finder, System Settings and the clipboard: logged, nothing opened or read.
FAKE_RECORDER = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/$(basename "$0")-argv.txt"
exit 0
"""
# Everything ORCHE or install.sh may run that acts on this machine: each must be a fake in fakebin
# (or nowhere on PATH) before any child starts.
MACHINE_TOOLS = ("claude", "orca", "osascript", "security", "launchctl", "open", "pbcopy", "pbpaste")
OLD_PYTHON = """#!/bin/sh
case "$1" in --version) echo "Python 3.9.6"; exit 0 ;; esac
exit 1
"""


def _write(path: Path, text: str, mode=0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class InstallShTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._cls_tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(cls._cls_tmp.name))
        # The clone source: this checkout's tracked files (working-tree content) in a fresh repo,
        # served as a bare repo. Works from a shallow CI checkout too.
        src = base / "src"
        files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                               capture_output=True).stdout.decode().split("\0")
        for rel in filter(None, files):
            from_path = ROOT / rel
            if from_path.is_file():
                (src / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(from_path, src / rel)
        _git("init", "-q", str(src))
        _git("checkout", "-q", "-B", "main", cwd=src)
        _git("add", "-A", cwd=src)
        _git("commit", "-q", "-m", "snapshot", cwd=src)
        cls.bare = base / "orche.git"
        _git("clone", "-q", "--bare", str(src), str(cls.bare))
        cls.other_bare = base / "other.git"
        _git("clone", "-q", "--bare", str(src), str(cls.other_bare))
        # The script under test, with the absolute interpreter candidates pointed at /nowhere so this
        # machine's own Homebrew python cannot satisfy the "no Python 3.11+" test.
        text = INSTALL_SH.read_text()
        assert ABSOLUTE_PYTHONS in text, "install.sh candidates changed; update this test"
        cls.script = _write(base / "bin-copy" / "install.sh",
                            text.replace(ABSOLUTE_PYTHONS, "/nowhere/python3 /nowhere/local/python3"))

    @classmethod
    def tearDownClass(cls):
        cls._cls_tmp.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.orche_home = self.home / ".claude" / "orche"
        self.dir = self.home / "orche"
        self.fake = self.tmp / "fakebin"
        _write(self.fake / "claude", FAKE_CLAUDE)
        _write(self.fake / "plugin-help.txt", PLUGIN_HELP, 0o644)
        _write(self.fake / "orca", FAKE_ORCA)
        _write(self.fake / "osascript", FAKE_OSASCRIPT)
        _write(self.fake / "security", FAKE_SECURITY)
        _write(self.fake / "launchctl", FAKE_LAUNCHCTL)
        for name in ("open", "pbcopy", "pbpaste"):
            _write(self.fake / name, FAKE_RECORDER)
        (self.fake / "python3").symlink_to(sys.executable)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("ORCHE_", "ORCA_", "CLAUDE", "ZDOTDIR", "SSH_"))}
        self.env.update({"HOME": str(self.home), "ORCHE_HOME": str(self.orche_home), "SHELL": "/bin/bash",
                         "PATH": f"{self.fake}:/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
                         # No /dev/tty for the Jev question unless a test hands one over.
                         "ORCHE_INSTALL_TTY": str(self.tmp / "no-such-tty")})

    def tearDown(self):
        self._tmp.cleanup()

    def fenced(self, env):
        """`env`, after checking that none of MACHINE_TOOLS on its PATH is outside fakebin: a test
        whose PATH lets the real launchctl, open, osascript, security, claude or orca through fails
        here, before anything runs."""
        for name in MACHINE_TOOLS:
            found = shutil.which(name, path=env["PATH"])
            if found is not None and Path(found).parent != self.fake:
                self.fail(f"{name} would run {found}: put a fake in fakebin first")
        return env

    def install(self, *args, env=None, dir_arg=True):
        argv = ["/bin/bash", str(self.script), "--source", str(self.bare)]
        argv += ["--dir", str(self.dir)] if dir_arg else []
        res = subprocess.run(argv + list(args), env=self.fenced(env or self.env), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=180, start_new_session=True)
        res.all = res.stdout + res.stderr
        return res

    def piped_on_a_terminal(self, *args, tty=False):
        """`curl ... | bash -s -- ARGS` with stdout/stderr on a pseudo-terminal (as in a terminal
        window) and no controlling terminal. `tty=True` makes a second pseudo-terminal the
        installer's /dev/tty (ORCHE_INSTALL_TTY) and presses Enter there each time a Jev prompt
        appears (a hidden prompt flushes typed-ahead input, so answers wait for the prompt, as a
        person would); otherwise ORCHE_INSTALL_TTY points at nothing. Returns (exit code or None
        on a timeout, output with \\n line ends)."""
        out_master, out_slave = pty.openpty()
        env = dict(self.env)
        tty_master = tty_slave = None
        if tty:
            tty_master, tty_slave = pty.openpty()
            env["ORCHE_INSTALL_TTY"] = os.ttyname(tty_slave)
        chunks = []

        def drain():
            answered = 0
            while True:
                try:
                    data = os.read(out_master, 65536)
                except OSError:
                    return
                if not data:
                    return
                chunks.append(data)
                if tty_master is not None:
                    prompts = b"".join(chunks).count(b"Enter to skip")
                    while answered < prompts:
                        os.write(tty_master, b"\n")
                        answered += 1

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        argv = ["/bin/bash", "-s", "--", "--source", str(self.bare), "--dir", str(self.dir), *args]
        proc = subprocess.Popen(argv, env=self.fenced(env), stdin=subprocess.PIPE, stdout=out_slave, stderr=out_slave,
                                start_new_session=True, cwd=self.tmp)
        os.close(out_slave)
        proc.stdin.write(self.script.read_bytes())
        proc.stdin.close()
        try:
            code = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)   # its whole process group (bash and orche)
            proc.wait()
            code = None
        reader.join(timeout=10)
        # macOS: closing a pty master while a thread still reads it can block; leak it instead.
        if not reader.is_alive():
            os.close(out_master)
        for fd in (tty_master, tty_slave):
            if fd is not None:
                os.close(fd)
        return code, b"".join(chunks).decode(errors="replace").replace("\r\n", "\n")

    def claude_calls(self):
        path = self.fake / "claude-argv.txt"
        return [ln for ln in path.read_text().splitlines() if ln.startswith("plugin")] if path.exists() else []

    def profile_lines(self, name=".profile"):
        path = self.home / name
        return [ln for ln in path.read_text().splitlines() if ln.endswith(MARKER)] if path.exists() else []

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                      if "fakebin" not in p.parts)

    def assertOk(self, res):
        self.assertEqual(res.returncode, 0, res.all)
        self.assertNotIn("error:", res.stderr)

    # --- full install and re-run -------------------------------------------------------------

    def test_install_then_rerun_is_an_idempotent_update(self):
        res = self.install()
        self.assertOk(res)
        self.assertTrue((self.dir / "bin" / "orche").is_file())
        self.assertTrue((self.orche_home / "install-state.json").is_file())
        self.assertEqual(self.profile_lines(), ['export PATH="$HOME/orche/bin:$PATH"  # orche'])
        calls = self.claude_calls()
        self.assertIn(f"plugin marketplace add {self.dir}", calls)
        self.assertIn("plugin install orche@orche", calls)
        self.assertLess(calls.index(f"plugin marketplace add {self.dir}"), calls.index("plugin install orche@orche"))
        # Not a terminal: no Jev question, no dialog.
        self.assertIn("orche install --mode plugin --no-jev --no-plugin-hint", res.stdout)
        self.assertIn("Jev: skipped (no terminal); add it later with `orche jev setup`", res.stdout)
        self.assertNotIn("/plugin install", res.stdout)   # the script runs the plugin steps itself
        self.assertNotIn("WARN\thooks", res.stdout)        # doctor: the plugin is enabled
        self.assertNotIn("Jev API key", res.all)
        self.assertFalse((self.fake / "osascript-called").exists())
        lines = res.stdout.splitlines()
        self.assertTrue(any(ln.startswith("orche ") and f"installed in {self.dir} (plugin mode)" in ln
                            for ln in lines), res.stdout)
        self.assertIn(f"Open a new terminal (or run: source {self.home / '.profile'})", res.stdout)
        self.assertIn("Next: orche init <project>", res.stdout)
        self.assertIn("doctor:", res.stdout)
        if sys.platform == "darwin":   # the heartbeat job went to the fake launchctl, nothing was kicked
            plist = self.home / "Library" / "LaunchAgents" / "com.orche.reconcile.plist"
            launchctl = (self.fake / "launchctl-argv.txt").read_text().splitlines()
            self.assertIn(f"bootstrap gui/{os.getuid()} {plist}", launchctl)
            self.assertFalse([c for c in launchctl if c.startswith("kickstart")], launchctl)
            self.assertFalse((self.fake / "open-argv.txt").exists())

        again = self.install()
        self.assertOk(again)
        self.assertEqual(len(self.profile_lines()), 1)
        self.assertIn("PATH: already in", again.stdout)
        calls = self.claude_calls()
        self.assertEqual(sum(c.startswith("plugin marketplace add") for c in calls), 1)
        self.assertEqual(calls.count("plugin install orche@orche"), 1)
        self.assertIn("plugin marketplace update orche", calls)
        self.assertIn("plugin update orche@orche", calls)
        self.assertIn(f"update {self.dir} (main)", again.stdout)

    def test_the_fake_world_cannot_reach_this_machine(self):
        # Every tool that acts on the machine resolves to its fake (0.7.2 review #13: /bin/launchctl
        # on this PATH replaced the developer's heartbeat job with the temp clone's).
        for name in MACHINE_TOOLS:
            self.assertEqual(shutil.which(name, path=self.env["PATH"]), str(self.fake / name), name)
        real = _write(self.tmp / "elsewhere" / "launchctl", "#!/bin/sh\nexit 0\n")
        with self.assertRaises(AssertionError) as caught:   # the fence refuses before any spawn
            self.fenced({**self.env, "PATH": f"{real.parent}:{self.env['PATH']}"})
        self.assertIn(f"launchctl would run {real}", str(caught.exception))

    # --- the Jev question through /dev/tty --------------------------------------------------

    def test_jev_question_is_asked_on_the_tty_when_piped(self):
        # Enter at the key prompt (the hidden terminal prompt on Linux); on macOS the fake dialog
        # answers Cancel first. Either way: asked, skipped, nothing stored.
        code, out = self.piped_on_a_terminal(tty=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("error:", out)
        self.assertIn("orche install --mode plugin --no-plugin-hint (Jev question on the terminal)", out)
        self.assertNotIn("--no-jev", out)
        self.assertNotIn("Jev: skipped (no terminal)", out)
        self.assertIn("Jev skipped", out)   # orche install's own skip line: it asked
        state = json.loads((self.orche_home / "install-state.json").read_text())
        self.assertIs(state.get("jev_asked"), True)   # orche install saw a terminal on stdin
        if sys.platform == "darwin":
            self.assertTrue((self.fake / "osascript-called").exists())
        else:
            self.assertIn("Jev API key (optional, Enter to skip)", out)
        for name in ("security-argv.txt", "security-stdin.txt"):
            path = self.fake / name
            self.assertNotIn("add-generic-password", path.read_text() if path.exists() else "")
        self.assertFalse((self.orche_home / "secrets" / "jev.key").exists())
        self.assertIn("Next: orche init <project>", out)

    def test_jev_hidden_prompt_on_the_tty(self):
        # No dialog (an SSH session, or Linux): the hidden terminal prompt reads the tty; Enter skips.
        self.env["SSH_CONNECTION"] = "192.0.2.1 50000 192.0.2.2 22"
        code, out = self.piped_on_a_terminal(tty=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("error:", out)
        self.assertIn("Jev API key (optional, Enter to skip)", out)
        self.assertIn("Jev skipped", out)
        self.assertFalse((self.fake / "osascript-called").exists())
        self.assertFalse((self.orche_home / "secrets" / "jev.key").exists())
        path = self.fake / "security-stdin.txt"
        self.assertNotIn("add-generic-password", path.read_text() if path.exists() else "")

    def test_no_openable_tty_skips_jev_with_one_line(self):
        code, out = self.piped_on_a_terminal()
        self.assertEqual(code, 0, out)
        self.assertNotIn("error:", out)
        self.assertIn("Jev: skipped (no terminal); add it later with `orche jev setup`", out)
        self.assertIn("orche install --mode plugin --no-jev --no-plugin-hint", out)
        self.assertFalse((self.fake / "osascript-called").exists())
        state = json.loads((self.orche_home / "install-state.json").read_text())
        self.assertNotIn("jev_asked", state)

    def test_no_jev_flag_wins_over_a_tty(self):
        code, out = self.piped_on_a_terminal("--no-jev", tty=True)
        self.assertEqual(code, 0, out)
        self.assertIn("orche install --mode plugin --no-jev", out)
        self.assertNotIn("Jev: skipped (no terminal)", out)
        self.assertFalse((self.fake / "osascript-called").exists())

    # --- odd `claude plugin ... --json` output -----------------------------------------------

    def test_unexpected_listing_json_never_fails_the_run(self):
        # Not JSON at all: treated as "not listed", so add + install run.
        (self.fake / "marketplace-json").write_text("Configured marketplaces: (not json)\n")
        (self.fake / "plugin-json").write_text('{"plugins": "unexpected"}\n')
        res = self.install()
        self.assertOk(res)
        calls = self.claude_calls()
        self.assertIn(f"plugin marketplace add {self.dir}", calls)
        self.assertIn("plugin install orche@orche", calls)
        # orche listed, but without any recognisable path field: unknown source, no warning, update.
        (self.fake / "marketplace-json").write_text('[{"name": "orche", "source": {"source": "directory"}}, 7]\n')
        (self.fake / "plugin-json").write_text('[null, {"id": "orche@orche"}]\n')
        again = self.install()
        self.assertOk(again)
        self.assertNotIn("already points at", again.all)
        calls = self.claude_calls()
        self.assertIn("plugin marketplace update orche", calls)
        self.assertIn("plugin update orche@orche", calls)
        # orche listed with a path somewhere else: the warning, and the run still succeeds.
        (self.fake / "marketplace-json").write_text('[{"name": "orche", "path": "/elsewhere/orche"}]\n')
        third = self.install()
        self.assertOk(third)
        self.assertIn("a marketplace named orche already points at /elsewhere/orche", third.stderr)

    def test_existing_bash_profile_is_used(self):
        (self.home / ".bash_profile").write_text("# mine\n")
        res = self.install("--dry-run")
        self.assertOk(res)
        self.assertIn(f"would append to {self.home / '.bash_profile'}", res.stdout)

    def test_zsh_uses_zprofile(self):
        env = {**self.env, "SHELL": "/bin/zsh"}
        res = self.install("--dry-run", env=env)
        self.assertOk(res)
        self.assertIn(f"would append to {self.home / '.zprofile'}", res.stdout)

    def test_no_profile_prints_the_line(self):
        res = self.install("--dry-run", "--no-profile")
        self.assertOk(res)
        self.assertIn('export PATH="$HOME/orche/bin:$PATH"  # orche', res.stdout)
        self.assertNotIn("would append", res.stdout)

    # --- dry run -----------------------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        before = self.tree()
        res = self.install("--dry-run")
        self.assertOk(res)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.dir.exists())
        self.assertFalse((self.home / ".profile").exists())
        self.assertIn("would run: env GIT_TERMINAL_PROMPT=0 git clone", res.stdout)
        self.assertIn("would run: claude plugin install orche@orche", res.stdout)
        self.assertIn("dry run: nothing was written", res.stdout)
        self.assertEqual(self.claude_calls(), ["plugin --help"])

    def test_dry_run_with_a_clone_previews_orche_install(self):
        _git("clone", "-q", str(self.bare), str(self.dir))
        before = self.tree()
        res = self.install("--dry-run")
        self.assertOk(res)
        self.assertEqual(self.tree(), before)
        self.assertIn("orche --dry-run install --mode plugin", res.stdout)
        self.assertIn("dry run: nothing written", res.stdout)   # orche's own dry-run header

    # --- refusals ----------------------------------------------------------------------------

    def test_windows_is_refused(self):
        res = self.install(env={**self.env, "ORCHE_INSTALL_UNAME": "MINGW64_NT-10.0-22631"})
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn(f"error: {WINDOWS_MSG}", res.stderr)
        self.assertFalse(self.dir.exists())

    def test_linux_warns_about_orca(self):
        res = self.install("--dry-run", env={**self.env, "ORCHE_INSTALL_UNAME": "Linux"})
        self.assertOk(res)
        self.assertIn("Orca (a macOS app) is still needed", res.stderr)

    def _old_python_env(self, uname):
        only = self.tmp / "only"
        for tool in ("git", "uname", "head"):
            (only / tool).parent.mkdir(parents=True, exist_ok=True)
            (only / tool).symlink_to(shutil.which(tool))
        _write(only / "python3", OLD_PYTHON)
        return {**self.env, "PATH": str(only), "ORCHE_INSTALL_UNAME": uname}

    def test_no_python_311_exits_with_a_hint(self):
        res = self.install(env=self._old_python_env("Darwin"))
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn("ORCHE needs Python 3.11+, none found (python3 is Python 3.9.6)", res.stderr)
        self.assertIn("brew install python", res.stderr)
        self.assertFalse(self.dir.exists())

    def test_no_python_311_linux_hint(self):
        res = self.install(env=self._old_python_env("Linux"))
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn("sudo apt install", res.stderr)
        self.assertIn("sudo dnf install", res.stderr)

    def test_dirty_clone_is_refused(self):
        _git("clone", "-q", str(self.bare), str(self.dir))
        (self.dir / "scratch.txt").write_text("mine\n")
        res = self.install()
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn("has uncommitted changes", res.stderr)
        self.assertFalse((self.home / ".profile").exists())

    def test_clone_of_another_remote_is_refused(self):
        _git("clone", "-q", str(self.other_bare), str(self.dir))
        res = self.install()
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn("is a clone of", res.stderr)

    def test_existing_non_git_folder_is_refused(self):
        self.dir.mkdir()
        (self.dir / "notes.txt").write_text("x\n")
        res = self.install()
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn("exists but is not a git clone", res.stderr)

    def test_unknown_option(self):
        res = self.install("--bogus")
        self.assertEqual(res.returncode, 1, res.all)
        self.assertIn("error: unknown option '--bogus'", res.stderr)

    # --- settings-mode fallbacks -------------------------------------------------------------

    def _assert_settings_mode(self, res):
        self.assertOk(res)
        self.assertIn("orche install --mode settings --no-jev", res.stdout)
        self.assertIn("plugin step skipped", res.all)
        self.assertIn("(settings mode)", res.stdout)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        commands = [h["command"] for entries in settings.get("hooks", {}).values()
                    for entry in entries for h in entry.get("hooks", [])]
        self.assertTrue(any(f"{self.dir}/hooks/run.sh" in c for c in commands), commands)
        self.assertNotIn("plugin install orche@orche", self.claude_calls())

    def test_claude_without_plugin_install_uses_settings_mode(self):
        (self.fake / "plugin-help.txt").write_text(PLUGIN_HELP_OLD)
        self._assert_settings_mode(self.install())

    def test_missing_claude_uses_settings_mode(self):
        (self.fake / "claude").unlink()
        self._assert_settings_mode(self.install())

    def test_mode_settings_flag(self):
        res = self.install("--mode", "settings")
        self.assertOk(res)
        self.assertIn("orche install --mode settings --no-jev", res.stdout)
        self.assertEqual(self.claude_calls(), ["plugin --help"])

    def test_failed_plugin_install_falls_back_to_settings(self):
        (self.fake / "fail-install").touch()
        res = self.install()
        self.assertOk(res)
        self.assertIn("claude plugin install orche@orche failed", res.stderr)
        self.assertIn("(settings mode)", res.stdout)
        self.assertIn("note: plugin step failed", res.stdout)
        settings = (self.home / ".claude" / "settings.json").read_text()
        self.assertIn(f"{self.dir}/hooks/run.sh", settings)

    # --- --all-projects and running from a clone ---------------------------------------------

    def test_all_projects_runs_apply_all(self):
        res = self.install("--all-projects")
        self.assertOk(res)
        self.assertIn("==> orche apply-all", res.stdout)
        self.assertIn("0 repo(s), 0 processed, 0 failed", res.stdout)
        self.assertIn("repo list --json", (self.fake / "orca-argv.txt").read_text())

    def test_piped_into_bash_like_curl(self):
        # `curl ... | bash -s -- ...`: the script arrives on stdin, so stdin is no terminal (no Jev
        # question) and no child may read the rest of the script; the cwd is not taken as a clone.
        res = subprocess.run(["/bin/bash", "-s", "--", "--source", str(self.bare), "--dir", str(self.dir)],
                             input=self.script.read_text(), env=self.fenced(self.env), capture_output=True, text=True,
                             timeout=180, cwd=ROOT, start_new_session=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("error:", res.stderr)
        self.assertNotIn("using this clone", res.stdout)
        self.assertIn("orche install --mode plugin --no-jev", res.stdout)
        self.assertIn("Next: orche init <project>", res.stdout)
        self.assertTrue((self.dir / ".git").is_dir())

    def test_run_from_a_clone_uses_that_clone(self):
        _git("clone", "-q", str(self.bare), str(self.dir))
        shutil.copy2(self.script, self.dir / "install.sh")
        res = subprocess.run(["/bin/bash", str(self.dir / "install.sh")], env=self.fenced(self.env),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
                             cwd=self.tmp, start_new_session=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("error:", res.stderr)
        self.assertIn(f"using this clone as is: {self.dir}", res.stdout)
        self.assertIn(f"plugin marketplace add {self.dir}", self.claude_calls())


if __name__ == "__main__":
    unittest.main()
