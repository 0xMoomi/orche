"""Jev onboarding: key storage (fake `security` on PATH / key file), the macOS dialog (fake
`osascript`), `orche install` / `orche jev setup|probe|key`, doctor rows, masking, and the
env-read gate's Jev rules. No network (fake HTTP opener), never the real keychain or a real dialog."""
import contextlib
import email.message
import io
import json
import os
import stat
import subprocess
import sys
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

import support
from orche import cmdscan, config, jev, jev_setup, jevgate, jevkey
from test_install import InstallCase
from test_jev import FakeOpener, answer_all, default_value

# Built at runtime: the repository never holds a key-shaped literal. mask.redact does not know
# this shape, so only the explicit scrub can hide it.
KEY = "tsk-onboard-" + "Q7" * 12
ENV_KEY = "tsk-from-env-" + "W3" * 10

FAKE_SECURITY = r"""#!/bin/sh
d=$(dirname "$0")
printf '%s\n' "$*" >> "$d/argv.log"
if [ -f "$d/fail" ]; then echo "security: SecKeychainItem failed near $(cat "$d/fail")" >&2; exit 1; fi
case "$1" in
  -i)
    IFS= read -r line
    printf '%s\n' "$line" >> "$d/stdin.log"
    printf '%s' "$line" | sed -e 's/.* -w "\(.*\)"$/\1/' > "$d/item"
    exit 0 ;;
  find-generic-password)
    [ -f "$d/item" ] || { echo "security: The specified item could not be found in the keychain." >&2; exit 44; }
    case " $* " in
      *" -w "*) cat "$d/item"; echo ;;
      *) echo 'keychain: "/fake/login.keychain-db"'; echo 'class: "genp"' ;;
    esac
    exit 0 ;;
  delete-generic-password)
    [ -f "$d/item" ] || { echo "security: The specified item could not be found in the keychain." >&2; exit 44; }
    rm "$d/item"; exit 0 ;;
esac
exit 2
"""

FAKE_OSASCRIPT = r"""#!/bin/sh
d=$(dirname "$0")
printf '%s\n' "$*" >> "$d/osascript.log"
case "$(cat "$d/dialog")" in
  save) printf 'button returned:Save, text returned:%s\n' "$(cat "$d/typed")" ;;
  cancel) echo "execution error: User canceled. (-128)" >&2; exit 1 ;;
  denied) echo "execution error: Not authorized to send Apple events to System Events. (-1743)" >&2; exit 1 ;;
esac
"""


def fake_getpass(prompt="Password: "):
    """getpass.getpass on the test's sys.stdin: prompt, one line, EOFError at end of input."""
    sys.stdout.write(prompt)
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\n")


def interrupt(prompt=""):
    raise KeyboardInterrupt


class InterruptAfter(io.StringIO):
    """stdin whose readline raises KeyboardInterrupt once the given lines are used up (Ctrl-C)."""

    def readline(self, *args):
        line = super().readline(*args)
        if not line:
            raise KeyboardInterrupt
        return line


def write_exe(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class OnboardingCase(InstallCase):
    """A temp HOME (from InstallCase), a fake bin dir on PATH, JEV_API_KEY unset, a fake opener."""

    def setUp(self):
        super().setUp()
        self.bin = self.home / "fakebin"
        write_exe(self.bin / "security", FAKE_SECURITY)
        write_exe(self.bin / "osascript", FAKE_OSASCRIPT)
        env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("JEV_API_KEY", None)
        os.environ.pop("SSH_CONNECTION", None)
        self.addCleanup(setattr, jev, "OPENER", jev.OPENER)
        self.opener = jev.OPENER = FakeOpener(answer_all(default_value))

    def keychain(self, dialog=False):
        """Switch the seams to the fakes on PATH (macOS behaviour)."""
        jevkey.PLATFORM = "darwin"
        jevkey.SECURITY = "security"
        if dialog:
            jev_setup.OSASCRIPT = "osascript"

    def dialog(self, mode, typed=KEY):
        (self.bin / "dialog").write_text(mode)
        (self.bin / "typed").write_text(typed)

    def stdin(self, text):
        return mock.patch("sys.stdin", io.StringIO(text))

    def tty(self, value=True, getpass=None):
        """Pretend stdin/stdout are terminals (or not). The hidden prompt then reads the (patched)
        sys.stdin like getpass reads the terminal: EOFError at end of input."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(jev_setup, "interactive", lambda: value))
        stack.enter_context(mock.patch.object(jev_setup, "terminal", lambda: value))
        stack.enter_context(mock.patch.object(jev_setup.getpass, "getpass", getpass or fake_getpass))
        return stack

    def jev_cfg(self):
        return config.load()["jev"]

    def assert_no_key_anywhere(self, *texts):
        for text in texts:
            self.assertNotIn(KEY, text)
        for path in self.home.rglob("*"):
            if path.is_file() and path.name not in ("item", "stdin.log", "jev.key", "typed"):
                self.assertNotIn(KEY, path.read_text(errors="replace"), str(path))


# ---- J1: key storage and resolution ---------------------------------------------------------

class KeychainStoreTest(OnboardingCase):
    def setUp(self):
        super().setUp()
        self.keychain()

    def test_set_get_status_rm_through_security_stdin(self):
        self.assertEqual(jevkey.stored(), (False, None))
        self.assertIsNone(jevkey.get())
        self.assertEqual(jevkey.set_key(f"  {KEY}\n"), "keychain")
        self.assertEqual(jevkey.get(), KEY)
        self.assertEqual(jevkey.stored(), (True, None))
        argv = (self.bin / "argv.log").read_text()
        self.assertNotIn(KEY, argv)  # the key travels on stdin, never in argv
        self.assertEqual((self.bin / "stdin.log").read_text(),
                         f'add-generic-password -U -s "orche" -a "JEV_API_KEY" -w "{KEY}"\n')
        self.assertIn("find-generic-password -s orche -a JEV_API_KEY\n", argv)  # status: no -w
        self.assertTrue(jevkey.remove())
        self.assertFalse(jevkey.remove())
        self.assertIsNone(jevkey.get())

    def test_resolution_order_env_then_keychain_then_none(self):
        self.assertEqual(jevkey.resolve(), (None, None))
        jevkey.set_key(KEY)
        self.assertEqual(jevkey.resolve(), (KEY, "keychain"))
        with mock.patch.dict(os.environ, {"JEV_API_KEY": ENV_KEY}):
            self.assertEqual(jevkey.resolve(), (ENV_KEY, "env"))
            self.assertEqual(jevkey.source(), ("env", None))
        self.assertEqual(jevkey.source(), ("keychain", None))
        c = jev.JevClient(jev.GATEWAY_BASE_URL, model="jev", opener=self.opener)
        self.assertIsNotNone(c.ask("s", [jev.noul("a", "q")]))
        self.assertEqual(self.opener.requests[0]["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(c.key_source, "keychain")

    def test_security_failure_is_scrubbed(self):
        (self.bin / "fail").write_text(KEY)
        with self.assertRaises(jevkey.KeyStoreError) as cm:
            jevkey.set_key(KEY)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("<value>", str(cm.exception))
        present, err = jevkey.stored()
        self.assertFalse(present)
        self.assertIn("exit 1", err)
        self.assertIsNone(jevkey.get())
        with self.assertRaises(jevkey.KeyStoreError):
            jevkey.remove()

    def test_missing_security_binary(self):
        jevkey.SECURITY = str(self.home / "nope" / "security")
        self.assertIsNone(jevkey.get())
        self.assertEqual(jevkey.resolve(), (None, None))
        with self.assertRaises(jevkey.KeyStoreError):
            jevkey.set_key(KEY)

    def test_bad_keys_refused(self):
        for bad in ("", "   ", "a\nb"):
            with self.assertRaises(jevkey.KeyStoreError):
                jevkey.set_key(bad)
        self.assertFalse((self.bin / "item").exists())


class FileStoreTest(OnboardingCase):
    def test_linux_key_file_0600(self):
        jevkey.PLATFORM = "linux"
        self.assertEqual(jevkey.backend(), "file")
        self.assertEqual(jevkey.set_key(KEY), "file")
        path = self.home / "secrets" / "jev.key"
        self.assertEqual(jevkey.key_file(), path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(jevkey.resolve(), (KEY, "file"))
        self.assertEqual(jevkey.source(), ("file", None))
        self.assertTrue(jevkey.remove())
        self.assertFalse(path.exists())
        self.assertEqual(jevkey.resolve(), (None, None))
        self.assertFalse((self.bin / "argv.log").exists())  # no `security` call off macOS


# ---- dialog ----------------------------------------------------------------------------------

class DialogTest(OnboardingCase):
    def setUp(self):
        super().setUp()
        self.keychain(dialog=True)

    def test_dialog_save_reads_text_returned(self):
        self.dialog("save")
        with self.stdin(""):
            self.assertEqual(jev_setup.read_key("prompt: "), KEY)
        argv = (self.bin / "osascript.log").read_text()
        self.assertIn('with hidden answer with title "ORCHE"', argv)
        self.assertIn(jev_setup.DIALOG_TEXT, argv)

    def test_cancel_is_no_key(self):
        self.dialog("cancel")
        with self.stdin(f"{ENV_KEY}\n"):
            self.assertEqual(jev_setup.read_key("prompt: "), "")  # no terminal fallback on Cancel

    def test_no_gui_falls_back_to_terminal(self):
        self.dialog("denied")
        with self.tty(), self.stdin(f"{KEY}\n"), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(jev_setup.read_key("prompt: "), KEY)
        (self.bin / "osascript.log").unlink()
        self.dialog("save", typed="from-dialog")
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}), self.tty(), \
                self.stdin(f"{KEY}\n"), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(jev_setup.read_key("prompt: "), KEY)
        with self.tty(), self.stdin(f"{KEY}\n"), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(jev_setup.read_key("prompt: ", dialog=False), KEY)
        self.assertFalse((self.bin / "osascript.log").exists())
        jevkey.PLATFORM = "linux"
        with self.tty(), self.stdin(f"{KEY}\n"), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(jev_setup.read_key("prompt: "), KEY)
        self.assertFalse((self.bin / "osascript.log").exists())

    def test_no_gui_and_no_terminal_never_reads_the_pipe(self):
        self.dialog("denied")
        piped = io.StringIO(f"{KEY}\n")
        with self.tty(False), mock.patch("sys.stdin", piped):
            with self.assertRaises(jev_setup.NoTerminal):
                jev_setup.read_key("prompt: ")
        self.assertEqual(piped.tell(), 0)

    def test_dialog_never_reads_stdin(self):
        self.dialog("save")
        piped = io.StringIO(f"{ENV_KEY}\n")
        with self.tty(False), mock.patch("sys.stdin", piped):
            code, out, err = self.orche("jev", "key", "set")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(jevkey.get(), KEY)  # the dialog's key, not the piped one
        self.assertEqual(piped.tell(), 0)

    def test_key_set_via_dialog_prints_only_where(self):
        self.dialog("save")
        code, out, err = self.orche("jev", "key", "set")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "Jev key stored in the keychain (service orche, account JEV_API_KEY)\n")
        self.assertEqual(jevkey.get(), KEY)
        self.dialog("cancel")
        code, out, err = self.orche("jev", "key", "set")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no key stored", err)

    def test_setup_cancel_skips_with_exit_0(self):
        self.dialog("cancel")
        with self.tty():
            code, out, err = self.orche("jev", "setup")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(jev_setup.SKIP_LINE, out)
        self.assertEqual(self.opener.requests, [])


# ---- J2: install prompt flows ----------------------------------------------------------------

class InstallJevTest(OnboardingCase):
    def install_with(self, stdin_text, *extra, tty=True):
        with self.tty(tty), self.stdin(stdin_text):
            return self.orche("install", "--home", str(self.h), *extra)

    def test_skip(self):
        code, out, err = self.install_with("\n")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(jev_setup.KEY_PROMPT, out)
        self.assertIn(jev_setup.SKIP_LINE, out)
        self.assertNotIn(jev_setup.KIND_PROMPT, out)
        self.assertFalse(self.jev_cfg()["enabled"])
        self.assertEqual(self.opener.requests, [])
        # asked once: the next interactive install does not ask again
        code, out, _ = self.install_with(f"{KEY}\n")
        self.assertNotIn(jev_setup.KEY_PROMPT, out)
        self.assertEqual(jevkey.resolve(), (None, None))

    def test_gateway_default_probe_success(self):
        code, out, err = self.install_with(f"{KEY}\n\n")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(jev_setup.KIND_PROMPT, out)
        cfg = self.jev_cfg()
        self.assertEqual((cfg["base_url"], cfg["model"], cfg["enabled"]),
                         ("https://ai-gateway.vercel.sh/typesafe", "jev", True))
        self.assertEqual(json.loads((self.home / "config.json").read_text())["claude_bin"], str(self.claude))
        [req] = self.opener.requests
        self.assertEqual(req["url"], "https://ai-gateway.vercel.sh/typesafe/v1/systemone")
        self.assertEqual(req["body"]["model"], "typesafe-ai/jev")
        self.assertEqual(list(req["body"]["questions"]), ["probe"])
        self.assertEqual(req["body"]["questions"]["probe"]["type"], "choice")
        self.assertGreaterEqual(req["timeout"], jev.PROBE_TIMEOUT_S - 0.01)
        self.assertRegex(out, r"Jev on \(gateway, \d+\.\d s\)")
        self.assertIn("Jev key stored in", out)
        self.assertEqual(jevkey.resolve(), (KEY, "file"))
        self.assertRegex((self.home / "logs" / "jev.log").read_text(), r"\tjev\tms=\d+ q=1 ok\n$")
        self.assert_no_key_anywhere(out, err)

    def test_direct_on_keychain(self):
        self.keychain()
        code, out, err = self.install_with(f"{KEY}\n2\n")
        self.assertEqual((code, err), (0, ""), out)
        cfg = self.jev_cfg()
        self.assertEqual((cfg["base_url"], cfg["model"], cfg["enabled"]), ("https://api.typesafe.ai", "jev-1.13.0", True))
        self.assertEqual(self.opener.requests[0]["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(self.opener.requests[0]["body"]["model"], "jev-1.13.0")
        self.assertIn("Jev key stored in the keychain (service orche, account JEV_API_KEY)", out)
        self.assertRegex(out, r"Jev on \(direct, ")
        self.assertNotIn(KEY, (self.bin / "argv.log").read_text())
        self.assert_no_key_anywhere(out, err)

    def test_probe_failure_leaves_jev_off_and_masks(self):
        jev.OPENER = FakeOpener(status=401, raw=json.dumps(
            {"message": f"invalid API key {KEY}", "error_type": "authentication_error"}).encode())
        code, out, err = self.install_with(f"{KEY}\n1\n")
        self.assertEqual((code, err), (0, ""), out)  # Jev never fails the install
        self.assertIn("Jev probe failed: HTTP401: invalid API key <value>", out)
        self.assertIn(jev_setup.OFF_LINE + "; nothing was changed, retry with `orche jev setup`", out)
        cfg = self.jev_cfg()
        self.assertEqual((cfg["base_url"], cfg["enabled"]), ("", False))  # config untouched
        self.assertEqual(jevkey.resolve(), (None, None))  # nothing stored before a successful probe
        self.assertIsNone(jev_setup.load_record())
        self.assert_no_key_anywhere(out, err)
        _code, out, _ = self.doctor_rows()
        self.assertIn("WARN\tjev\tnot configured (Haiku fallback)", out)
        self.assertNotIn("FAIL\tjev", out)

    def doctor_rows(self):
        from test_install import DoctorTest
        runner = DoctorTest.doctor
        probe = DoctorTest.__new__(DoctorTest)
        probe.__dict__.update(self.__dict__)
        probe.login = {}
        probe.login_calls = []
        return runner(probe)

    def test_no_jev_and_non_tty(self):
        code, out, err = self.install_with(f"{KEY}\n", "--no-jev")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn(jev_setup.KEY_PROMPT, out)
        self.assertIn("jev: skipped (--no-jev)", out)
        code, out, _ = self.install_with(f"{KEY}\n", tty=False)
        self.assertNotIn(jev_setup.KEY_PROMPT, out)
        self.assertNotIn("jev", out.lower().replace("jev skipped", ""))
        self.assertEqual(jevkey.resolve(), (None, None))
        self.assertEqual(self.opener.requests, [])

    def test_key_stdin_and_kind_with_json(self):
        code, out, err = self.install_with(f"{KEY}\n", "--jev-key-stdin", "--jev-kind", "direct", "--json", tty=False)
        self.assertEqual((code, err), (0, ""), out)
        data = json.loads(out)
        self.assertEqual({k: data["jev"][k] for k in ("status", "kind", "key", "error")},
                         {"status": "on", "kind": "direct", "key": "file", "error": ""})
        self.assertEqual(self.jev_cfg()["base_url"], "https://api.typesafe.ai")
        self.assert_no_key_anywhere(out, err)

    def test_dry_run_never_asks(self):
        code, out, err = self.install_with(f"{KEY}\n", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn(jev_setup.KEY_PROMPT, out)
        self.assertEqual(jevkey.resolve(), (None, None))

    def test_already_configured_is_not_asked(self):
        jevkey.set_key(KEY)
        jev_setup.update_config(base_url=jev.GATEWAY_BASE_URL, model="jev", enabled=True)
        code, out, _ = self.install_with("\n")
        self.assertEqual(code, 0)
        self.assertNotIn(jev_setup.KEY_PROMPT, out)


# ---- jev setup / probe / key CLI and doctor --------------------------------------------------

class JevCliTest(OnboardingCase):
    def test_probe_offline(self):
        code, out, err = self.orche("jev", "probe")
        self.assertEqual(code, 1)
        self.assertIn("not configured", out)
        jevkey.set_key(KEY)
        jev_setup.update_config(base_url=jev.GATEWAY_BASE_URL, model="jev", enabled=False)
        code, out, err = self.orche("jev", "probe")
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"Jev answered \(gateway, \d+\.\d s, key: file\)")
        self.assertIn("orche jev enable", out)
        self.assertTrue(jev_setup.load_record()["ok"])
        jev.OPENER = FakeOpener(exc=OSError(f"connect failed for {KEY}"))
        code, out, err = self.orche("jev", "probe", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual((data["ok"], data["error"]), (False, "OSError"))
        self.assert_no_key_anywhere(out, err)
        jev.OPENER = FakeOpener(raw=b'{"answers": {}}')
        code, out, _ = self.orche("jev", "probe")
        self.assertIn("unexpected response shape", out)

    def test_key_cli_set_status_rm(self):
        code, out, _ = self.orche("jev", "key", "status")
        self.assertEqual((code, out), (0, "Jev key: none (set one with `orche jev key set`)\n"))
        with self.stdin(f"{KEY}\n"):
            code, out, err = self.orche("jev", "key", "set", "--stdin")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Jev key stored in", out)
        code, out, _ = self.orche("jev", "key", "status", "--json")
        self.assertEqual(json.loads(out)["source"], "file")
        with mock.patch.dict(os.environ, {"JEV_API_KEY": ENV_KEY}):
            code, out, _ = self.orche("jev", "key", "status")
        self.assertEqual(out, "Jev key: env\n")
        self.assertNotIn(ENV_KEY, out)
        code, out, _ = self.orche("jev", "key", "rm")
        self.assertEqual((code, out), (0, "Jev key removed from the file\n"))
        code, out, _ = self.orche("jev", "key", "rm")
        self.assertEqual(out, "no Jev key in the file\n")
        with self.stdin("\n"):
            code, out, err = self.orche("jev", "key", "set", "--stdin")
        self.assertEqual(code, 1)
        self.assertIn("no key stored", err)

    def test_setup_cli_stdin_and_terminal(self):
        with self.stdin(f"{KEY}\n"):
            code, out, err = self.orche("jev", "setup", "--key-stdin", "--kind", "gateway")
        self.assertEqual((code, err), (0, ""), out)
        self.assertRegex(out, r"Jev on \(gateway, ")
        before = (self.home / "config.json").read_bytes()
        piped = io.StringIO(f"{ENV_KEY}\n")
        with self.tty(False), mock.patch("sys.stdin", piped):
            code, out, err = self.orche("jev", "setup")  # no terminal, no flag: skipped, pipe unread
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Jev setup skipped: no terminal (use --key-stdin", out)
        self.assertEqual(piped.tell(), 0)
        self.assertEqual((self.home / "config.json").read_bytes(), before)
        jev.OPENER = FakeOpener(status=500, raw=b"oops")
        with self.tty(), self.stdin(f"{KEY}\n2\n"):
            code, out, err = self.orche("jev", "setup", "--no-dialog")
        self.assertEqual(code, 1)
        self.assertIn("Jev probe failed: HTTP500", out)
        self.assertEqual((self.home / "config.json").read_bytes(), before)  # gateway setup kept
        self.assertTrue(self.jev_cfg()["enabled"])
        self.assert_no_key_anywhere(out, err)

    def test_env_key_note(self):
        with mock.patch.dict(os.environ, {"JEV_API_KEY": ENV_KEY}), self.stdin(f"{KEY}\n"):
            code, out, _ = self.orche("jev", "setup", "--key-stdin")
        self.assertEqual(code, 0)
        self.assertIn("JEV_API_KEY is set in this environment", out)
        self.assertEqual(self.opener.requests[0]["headers"]["Authorization"], f"Bearer {KEY}")  # probes the new key

    def test_doctor_rows(self):
        self.assertEqual(jev_setup.doctor_row(), ("WARN", "jev", "not configured (Haiku fallback)"))
        jev_setup.update_config(base_url=jev.GATEWAY_BASE_URL, model="jev", enabled=True)
        self.assertEqual(jev_setup.doctor_row(), ("WARN", "jev", "not configured (Haiku fallback)"))
        with mock.patch.dict(os.environ, {"JEV_API_KEY": ENV_KEY}):
            self.assertEqual(jev_setup.doctor_row(), ("OK", "jev", "gateway (key: env)"))
        self.keychain()
        jevkey.set_key(KEY)
        self.assertEqual(jev_setup.doctor_row(), ("OK", "jev", "gateway (key: keychain)"))
        self.assertNotIn(" -w", (self.bin / "argv.log").read_text().splitlines()[-1])
        jev_setup.update_config(enabled=False)
        self.assertEqual(jev_setup.doctor_row(), (
            "WARN", "jev", "disabled (gateway configured, key: keychain; turn on: orche jev enable)"))
        jev_setup.save_record({"ok": False, "ms": 5, "kind": "gateway", "error": "HTTP401: <value>"})  # as probed
        failed = ("WARN", "jev", "last probe failed HTTP401: <value>; ORCHE works without Jev; "
                                 "run `orche jev probe`")
        self.assertEqual(jev_setup.doctor_row(), failed)  # off: WARN, never FAIL
        jev_setup.update_config(enabled=True)
        self.assertEqual(jev_setup.doctor_row(), failed)  # on: WARN too (calls fail open)
        jev_setup.save_record({"ok": False, "ms": 5, "kind": "direct", "error": "HTTP401"})
        self.assertEqual(jev_setup.doctor_row(), ("OK", "jev", "gateway (key: keychain)"))  # stale kind
        jev_setup.save_record({"ok": False, "ms": 5, "kind": "gateway", "error": "HTTP401"})
        code, out, _ = self.orche("jev", "disable")
        self.assertEqual(code, 0)
        self.assertIsNone(jev_setup.load_record())  # disable forgets the failed probe
        self.assertEqual(jev_setup.doctor_row()[:2], ("WARN", "jev"))
        self.assertTrue(jev_setup.doctor_row()[2].startswith("disabled ("))
        jev_setup.save_record({"ok": False, "ms": 5, "kind": "gateway", "error": "HTTP401"})
        self.orche("jev", "key", "rm")  # a key change forgets the stale probe
        self.assertIsNone(jev_setup.load_record())
        (self.bin / "fail").write_text("x")
        level, _name, detail = jev_setup.doctor_row()
        self.assertEqual(level, "WARN")
        self.assertIn("keychain lookup failed", detail)

    def test_hooks_client_uses_stored_key(self):
        jevkey.set_key(KEY)
        jev_setup.update_config(base_url=jev.DIRECT_BASE_URL, model="jev-1.13.0", enabled=True)
        c = jev.client()
        self.assertIsNotNone(c)
        self.assertEqual(c.key_source, "file")
        self.assertIsNotNone(jev.card_needs_refresh(["feat: a button"]))
        jevkey.remove()
        self.assertIsNone(jev.client())  # no key anywhere: Jev stays out, Haiku fallback


# ---- fix round: I1 no key from a pipe without --stdin, I2 EOF / Ctrl-C = skip ---------------------

class PipedKeyTest(OnboardingCase):
    """I1: `printf KEY | orche jev key set` stores nothing unless `--stdin` was given."""

    def assert_refused(self, *argv):
        piped = io.StringIO(f"{KEY}\n")
        with self.tty(False), mock.patch("sys.stdin", piped):
            code, out, err = self.orche("jev", "key", "set", *argv)
        self.assertEqual((code, out, err), (1, "", f"orche: {jev_setup.NO_TERMINAL}\n"))
        self.assertEqual(piped.tell(), 0)  # the pipe was never read
        self.assertEqual(jevkey.resolve(), (None, None))

    def test_linux_pipe_refused(self):
        jevkey.PLATFORM = "linux"
        self.assert_refused()
        self.assert_refused("--no-dialog")
        self.assertFalse((self.home / "secrets" / "jev.key").exists())

    def test_macos_pipe_refused_without_a_dialog(self):
        self.keychain(dialog=True)
        self.assert_refused("--no-dialog")
        self.dialog("denied")  # no GUI session: no dialog, no terminal
        self.assert_refused()
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}):
            self.assert_refused()
        self.assertNotIn("add-generic-password", (self.bin / "argv.log").read_text()
                         if (self.bin / "argv.log").exists() else "")
        self.assertFalse((self.bin / "item").exists())

    def test_explicit_stdin_flag_still_works(self):
        for platform in ("linux", "darwin"):
            jevkey.PLATFORM = platform
            if platform == "darwin":
                self.keychain()
            with self.tty(False), self.stdin(f"{KEY}\n"):
                code, _out, err = self.orche("jev", "key", "set", "--stdin")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(jevkey.get(), KEY)

    def test_install_without_terminal_never_reads_the_pipe(self):
        piped = io.StringIO(f"{KEY}\n2\n")
        with self.tty(False), mock.patch("sys.stdin", piped):
            code, out, err = self.orche("install", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(piped.tell(), 0)
        self.assertNotIn(jev_setup.KEY_PROMPT, out)
        self.assertEqual(jevkey.resolve(), (None, None))


class InterruptedPromptTest(OnboardingCase):
    """I2: EOF or Ctrl-C at the key (or key-type) prompt = skip Jev: exit 0, no traceback, nothing changed."""

    def setUp(self):
        super().setUp()
        config.save(config.load())
        self.before = (self.home / "config.json").read_bytes()

    def assert_untouched(self):
        self.assertEqual((self.home / "config.json").read_bytes(), self.before)
        self.assertEqual(jevkey.resolve(), (None, None))
        self.assertIsNone(jev_setup.load_record())
        self.assertEqual(self.opener.requests, [])

    def install(self, stdin, getpass=None):
        with self.tty(getpass=getpass), mock.patch("sys.stdin", stdin):
            return self.orche("install", "--home", str(self.h))

    def test_install_eof_at_key_prompt(self):
        code, out, err = self.install(io.StringIO(""))  # Ctrl-D: getpass raises EOFError
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(jev_setup.SKIP_LINE, out)
        self.before = (self.home / "config.json").read_bytes()  # install itself writes claude_bin
        self.assert_untouched()

    def test_install_ctrl_c_at_key_prompt(self):
        code, out, err = self.install(io.StringIO(f"{KEY}\n"), getpass=interrupt)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(jev_setup.SKIP_LINE, out)
        self.assertNotIn("Traceback", out)
        self.before = (self.home / "config.json").read_bytes()
        self.assert_untouched()

    def test_install_eof_and_ctrl_c_at_kind_prompt(self):
        for stdin in (io.StringIO(f"{KEY}\n"), InterruptAfter(f"{KEY}\n")):  # EOF, then Ctrl-C
            code, out, err = self.install(stdin)
            self.assertEqual((code, err), (0, ""), out)
            self.assertIn(jev_setup.KIND_PROMPT, out)
            self.assertIn(jev_setup.SKIP_LINE, out)
            self.assertEqual(jevkey.resolve(), (None, None))
            self.assertEqual(self.opener.requests, [])
            (self.home / "install-state.json").unlink()  # ask again on the next run

    def test_setup_eof_and_ctrl_c(self):
        for stdin, getpass in ((io.StringIO(""), None), (io.StringIO(f"{KEY}\n"), interrupt),
                               (InterruptAfter(f"{KEY}\n"), None)):
            with self.tty(getpass=getpass), mock.patch("sys.stdin", stdin):
                code, out, err = self.orche("jev", "setup", "--no-dialog", "--json")
            self.assertEqual((code, err), (0, ""), out)
            self.assertEqual(json.loads(out[out.index("\n{") + 1:])["status"], "skipped")  # after the prompt
            self.assert_untouched()

    def test_key_set_ctrl_c_and_eof(self):
        for stdin, getpass in ((io.StringIO(""), None), (io.StringIO(f"{KEY}\n"), interrupt)):
            with self.tty(getpass=getpass), mock.patch("sys.stdin", stdin):
                code, out, err = self.orche("jev", "key", "set", "--no-dialog")
            self.assertEqual(code, 1)
            self.assertIn("no key stored", err)
            self.assert_untouched()

    def test_closed_or_missing_stdin(self):
        closed = io.StringIO(f"{KEY}\n")
        closed.close()
        with mock.patch("sys.stdin", closed):
            self.assertFalse(jev_setup.interactive())
            self.assertEqual(jev_setup.stdin_line(), "")
            code, out, err = self.orche("install", "--home", str(self.h), "--jev-key-stdin")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("jev: no key on stdin; Jev stays off", out)
        with mock.patch("sys.stdin", None):
            code, _out, err = self.orche("jev", "key", "set", "--stdin")
        self.assertEqual(code, 1)
        self.assertIn("no key stored", err)
        self.assertEqual(jevkey.resolve(), (None, None))


# ---- fix round: minors ---------------------------------------------------------------------------

class NoPartialChangeTest(OnboardingCase):
    """(a) config changes only after the probe answered AND the key was stored; else byte-for-byte."""

    def setUp(self):
        super().setUp()
        self.keychain()
        with self.stdin(f"{KEY}\n"):
            code, out, _ = self.orche("jev", "setup", "--key-stdin")  # a working gateway setup
        self.assertEqual(code, 0, out)
        self.before = (self.home / "config.json").read_bytes()
        self.record = jev_setup.record_path().read_bytes()

    def assert_kept(self):
        self.assertEqual((self.home / "config.json").read_bytes(), self.before)
        self.assertEqual(jevkey.get(), KEY)
        self.assertEqual(jev_setup.record_path().read_bytes(), self.record)

    def test_failed_probe_changes_nothing(self):
        jev.OPENER = FakeOpener(status=500, raw=b"down")
        res = jev_setup.configure(ENV_KEY, "direct")
        self.assertEqual(res["status"], "off")
        self.assertIn("nothing was changed", res["lines"][-1])
        self.assert_kept()
        self.assertNotIn(ENV_KEY, (self.bin / "stdin.log").read_text())  # never handed to `security`

    def test_failed_store_changes_nothing(self):
        (self.bin / "fail").write_text("x")
        res = jev_setup.configure(ENV_KEY, "direct")
        self.assertEqual((res["status"], res["key"]), ("off", None))
        self.assertIn("Jev key not stored", res["lines"][0])
        (self.bin / "fail").unlink()
        self.assert_kept()

    def test_failed_config_write_restores_the_file(self):
        real_save = config.save

        def broken(cfg):
            (self.home / "config.json").write_text('{"half": ')  # a partial write, then the error
            raise OSError(28, "No space left on device")

        with mock.patch.object(config, "save", broken):
            res = jev_setup.configure(ENV_KEY, "direct")
        self.assertEqual(res["status"], "off")
        self.assertIn("cannot update config", res["error"])
        self.assertEqual((self.home / "config.json").read_bytes(), self.before)
        self.assertIs(config.save, real_save)
        self.assertNotIn(ENV_KEY, json.dumps(res))
        self.assertNotIn(KEY, json.dumps(res))
        self.assertIn("config.json: put back as it was", res["lines"])
        self.assertIn("Jev key: the previous key is stored again in the keychain", res["lines"])
        self.assert_kept()  # the previous key is back

    def broken_save(self):
        def broken(cfg):
            raise OSError(28, "No space left on device")
        return mock.patch.object(config, "save", broken)

    def test_failed_config_write_removes_a_new_key_when_there_was_none(self):
        jevkey.remove()
        with self.broken_save():
            res = jev_setup.configure(ENV_KEY, "direct")
        self.assertIn("Jev key: the new key was removed again; no key is stored in the keychain", res["lines"])
        self.assertIsNone(jevkey.get())
        self.assertEqual((self.home / "config.json").read_bytes(), self.before)

    def test_failed_rollback_says_which_key_is_stored(self):
        real_set = jevkey.set_key
        calls = []

        def set_once(key):
            calls.append(1)
            if len(calls) > 1:
                raise jevkey.KeyStoreError(f"keychain write failed (exit 1): {key}")
            return real_set(key)

        with self.broken_save(), mock.patch.object(jevkey, "set_key", set_once):
            res = jev_setup.configure(ENV_KEY, "direct")
        line = res["lines"][2]
        self.assertTrue(line.startswith("Jev key: the NEW key is still stored in the keychain (rollback failed: "),
                        line)
        self.assertIn("run `orche jev key rm` or `orche jev key set`", line)
        self.assertNotIn(KEY, json.dumps(res))  # the previous key is scrubbed too
        self.assertEqual(jevkey.get(), ENV_KEY)


class UnreadableConfigTest(OnboardingCase):
    """Round 2: a config.json that is not JSON or not UTF-8 skips Jev with one line, no traceback."""

    BAD = {"invalid JSON": b'{"jev": {"enabled": true,}\n', "not UTF-8": b'\xff\xfe{"jev": {}}\n'}

    def check(self, raw):
        path = self.home / "config.json"
        path.write_bytes(raw)
        with self.tty(), self.stdin(f"{KEY}\n\n"):
            code, out, err = self.orche("install", "--home", str(self.h))
        self.assertEqual((code, err), (0, ""), out)
        [line] = [ln for ln in out.splitlines() if ln.startswith("config.json unreadable (")]
        self.assertTrue(line.endswith("): Jev skipped, nothing changed; fix the file and run `orche jev setup`"))
        self.assertNotIn(jev_setup.KEY_PROMPT, out)
        self.assertNotIn("Traceback", out + err)
        for argv in (("jev", "setup"), ("jev", "setup", "--key-stdin")):
            with self.tty(), self.stdin(f"{KEY}\n\n"):
                code, out, err = self.orche(*argv)
            self.assertEqual((code, out), (1, ""), argv)
            self.assertTrue(err.startswith("config.json unreadable ("), err)
        with self.tty(False), self.stdin(f"{KEY}\n"):
            code, out, _ = self.orche("install", "--home", str(self.h), "--jev-key-stdin")
        self.assertEqual(code, 0)
        self.assertIn("config.json unreadable (", out)
        res = jev_setup.configure(KEY, "gateway")
        self.assertEqual((res["status"], len(res["lines"])), ("off", 1))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(jevkey.resolve(), (None, None))
        self.assertEqual(self.opener.requests, [])

    def test_invalid_json(self):
        self.check(self.BAD["invalid JSON"])

    def test_not_utf8(self):
        self.check(self.BAD["not UTF-8"])


class Redirecting(urllib.request.HTTPSHandler):
    """An https handler that answers every request with a 302 to another host (no network)."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def https_open(self, req):
        self.seen.append((req.full_url, req.unredirected_hdrs.get("Authorization")))
        headers = email.message.Message()
        headers["Location"] = "https://elsewhere.test/collect"
        resp = urllib.response.addinfourl(io.BytesIO(b"<html>moved</html>"), headers, req.full_url, 302)
        resp.msg = "Found"
        return resp


class TransportTest(OnboardingCase):
    """(b) https only; a 3xx is an error, never followed; the key is an unredirected header."""

    def test_http_base_url_never_gets_the_key(self):
        jevkey.set_key(KEY)
        c = jev.JevClient("http://ai-gateway.example.test/typesafe", opener=self.opener)
        self.assertFalse(c.configured())
        self.assertIsNone(c.ask("s", [jev.noul("a", "q")]))
        self.assertEqual((c.outcome, c._key, self.opener.requests), ("error:NotHttps", None, []))
        self.assertEqual(jev.probe(c)["error"], "jev.base_url must be https")
        jev_setup.update_config(base_url="http://api.typesafe.ai", enabled=True)
        self.assertEqual(jev_setup.doctor_row(), (
            "WARN", "jev", "jev.base_url must be https; not configured (Haiku fallback)"))
        self.assertIsNone(jev.client())

    def test_redirect_is_an_error_and_not_followed(self):
        handler = Redirecting()
        opener = jev.build_opener(urllib.request.ProxyHandler({}), handler)
        c = jev.JevClient(jev.GATEWAY_BASE_URL, opener=lambda req, timeout: opener.open(req, timeout=timeout),
                          api_key=KEY)
        res = jev.probe(c)
        self.assertEqual(res, {"ok": False, "ms": res["ms"], "kind": "gateway", "error": "HTTP302"})
        self.assertEqual(handler.seen, [(jev.GATEWAY_BASE_URL + "/v1/systemone", f"Bearer {KEY}")])
        self.assertNotIn(KEY, json.dumps(res))

    def test_key_is_unredirected(self):
        jevkey.set_key(KEY)
        c = jev.JevClient(jev.GATEWAY_BASE_URL, opener=lambda req, timeout: seen.append(req) or b"{}")
        seen = []
        c.ask("s", [jev.noul("a", "q")])
        [req] = seen
        self.assertNotIn("Authorization", req.headers)
        self.assertEqual(req.unredirected_hdrs["Authorization"], f"Bearer {KEY}")


class SmallFixesTest(OnboardingCase):
    def test_keys_with_spaces_quotes_or_backslashes_refused(self):  # M6
        for bad in ('ab"cd', "ab\\cd", "ab cd", "ab\tcd"):
            with self.assertRaises(jevkey.KeyStoreError):
                jevkey.check(bad)
        self.assertEqual(jevkey.check(f"  {KEY}\n"), KEY)

    def test_choice_outside_the_asked_options_dropped(self):  # M7
        q = jev.choice("k", "which?", ["yes", "no"])
        self.assertEqual(jev._answer(q, {"type": "choice", "choice": "yes", "confidence": 0.9}), ("yes", 0.9, {}))
        self.assertIsNone(jev._answer(q, {"type": "choice", "choice": "maybe", "confidence": 0.9}))

    def test_install_kind_flag_answers_the_kind_question(self):  # M5
        with self.tty(), self.stdin(f"{KEY}\n"):
            code, out, err = self.orche("install", "--home", str(self.h), "--jev-kind", "direct")
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn(jev_setup.KIND_PROMPT, out)
        self.assertEqual(self.jev_cfg()["base_url"], jev.DIRECT_BASE_URL)

    def test_uninstall_mentions_the_kept_key(self):  # M8
        _code, out, _err = self.orche("uninstall", "--home", str(self.h))
        self.assertIn("kept: a stored Jev key (keychain or ORCHE_HOME/secrets); delete it with "
                      "`orche jev key rm`", out)

    def test_secrets_dir_is_owner_only(self):  # M9
        jevkey.PLATFORM = "linux"
        jevkey.set_key(KEY)
        self.assertEqual(stat.S_IMODE((self.home / "secrets").stat().st_mode), 0o700)


# ---- amendment: the env-read gate keeps the key away from agents ------------------------------

class GateTest(support.TempHomeCase):
    HOOK = support.ROOT / "hooks" / "env_read_gate.py"

    def gate(self, tool, tool_input):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "ORCHE_", "CLAUDE"))}
        env["ORCHE_HOME"] = str(self.home)
        res = subprocess.run([sys.executable, str(self.HOOK)], input=json.dumps(
            {"tool_name": tool, "tool_input": tool_input}), env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        if not res.stdout:
            return "silent"
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertIn("orche jev key set", out["permissionDecisionReason"])
        return out["permissionDecision"]

    def test_jev_key_reads_and_stdin_feeds_denied(self):  # through the hook process
        deny = [("Bash", "security find-generic-password -s orche -a JEV_API_KEY -w"),
                ("Bash", "/usr/bin/security dump-keychain -d login.keychain"),
                ("Bash", "cat ~/.claude/orche/secrets/jev.key"),
                ("Bash", "echo tsk-abc | orche jev key set --stdin"),
                ("Bash", "echo tsk-abc | orche install --jev-key-stdin"),
                ("Read", "/Users/x/.claude/orche/secrets/jev.key"),
                ("Grep", "/Users/x/.claude/orche/secrets/jev.key")]
        for tool, arg in deny:
            key = "command" if tool == "Bash" else "file_path" if tool == "Read" else "path"
            self.assertEqual(self.gate(tool, {key: arg}), "deny", arg)

    def test_normal_jev_commands_allowed(self):
        for command in ("orche jev key status", "orche jev key set", "orche jev probe", "orche jev setup",
                        "orche install --no-jev", "security list-keychains", "orche doctor | grep jev",
                        'git commit -m "docs: jev.key is never printed"', "rg jev.key docs/",
                        "security find-generic-password -s orche -a JEV_API_KEY"):
            self.assertEqual(self.gate("Bash", {"command": command}), "silent", command)
        self.assertEqual(self.gate("Read", {"file_path": "/x/lib/orche/jevkey.py"}), "silent")


def _load_gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("env_read_gate_under_test", GateTest.HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GateRulesTest(support.TempHomeCase):
    """The Jev rules in process: only command words count, never text that mentions them (I5)."""

    @classmethod
    def setUpClass(cls):
        cls.g = _load_gate()

    def bash(self, command):
        return self.g.jev_denied("Bash", {"command": command})

    def test_commands_that_read_or_feed_the_key_are_denied(self):
        kf = "~/.claude/orche/secrets/jev.key"
        for command in (
                "security find-generic-password -s orche -a JEV_API_KEY -w",
                "security find-generic-password -s orche -a JEV_API_KEY -g",
                "security find-generic-password -gs orche",
                "security find-internet-password -s example.com -w",
                "/usr/bin/security dump-keychain -d login.keychain",
                "sudo -u me security find-generic-password -w -s x",
                "FOO=1 security find-generic-password -s x -w 2>/dev/null",
                "true && security find-generic-password -s x -w",
                "true\nsecurity find-generic-password -s x -w",
                'echo "$(security find-generic-password -s x -w)"',
                "x=`security find-generic-password -w -s x`",
                'bash -c "security find-generic-password -s x -w"',
                "printf 'find-generic-password -w\\n' | security -i",
                f"cat {kf}", f'cat "{kf}"', f"head -c 3 < {kf}", f"cp {kf} /tmp/k",
                "cd ~/.claude/orche/secrets && cat jev.key", "cat ~/.claude/orche/secrets/jev*",
                "echo tsk-abc | orche jev key set --stdin", "orche jev key set --stdin <<< tsk-abc",
                "printf %s tsk-abc | orche jev setup --key-stdin --kind direct",
                "echo tsk-abc | orche install --jev-key-stdin", "echo tsk-abc | orche install --jev-key",
                "echo tsk-abc | python3 bin/orche.py jev key set --st",
                'python3 -c "from orche import jevkey; print(jevkey.get())"',
                "python3 - <<'EOF'\nfrom orche import jevkey\nprint(jevkey.resolve())\nEOF"):
            self.assertTrue(self.bash(command), command)

    def test_text_that_mentions_them_passes(self):
        for command in (
                'git commit -m "docs: never cat secrets/jev.key"',
                'git commit -m "fix: jev.key is never printed"',
                "rg jev.key docs/",  # a bare jev.key outside ORCHE_HOME/secrets is just a word
                "grep -rn jev.key README.md SPEC.md",
                'rg "security find-generic-password -w" docs/',
                'git log --grep "orche jev key set --stdin"',
                "python3 -m unittest discover -s tests  # security find-generic-password -w",
                "python3 - <<'EOF'\n# mentions --jev-key-stdin, security find-generic-password -w\nimport orche\nEOF",
                "cat <<EOF > notes.md\nnever cat secrets/jev.key\nEOF",  # relative path in text: a mention
                "security find-generic-password -s orche -a JEV_API_KEY",  # attributes only
                "security find-generic-password -s gw -a me",
                "security find-internet-password -s example.com",
                "security dump-keychain login.keychain",
                "security list-keychains",
                "orche jev key set", "orche jev key set --no-dialog", "orche jev setup --kind direct",
                "orche install --no-jev --json", "echo done 2>&1 | tee log"):
            self.assertFalse(self.bash(command), command)

    def test_grep_tool(self):
        home = str(self.home)
        deny = [{"path": f"{home}/secrets/jev.key"}, {"path": f"{home}/secrets"},
                {"path": "/Users/x/.claude/orche/secrets", "pattern": "tsk"},
                {"path": home, "glob": "jev.*"}, {"path": home, "glob": "*.key"}]
        deny += [{"path": home}, {"path": home + "/"}, {"path": os.path.dirname(home), "glob": "**/jev.key"},
                 {"path": "/Users/x/.claude/orche"}, {"path": f"{home}/secrets/../secrets"}]
        allow = [{"path": "docs/", "glob": "jev.key"}, {"path": home, "glob": "*.json"},
                 {"path": "/x/project/lib/orche"}, {"glob": "jev.key"}, {"path": os.path.dirname(home)}]
        for tool_input in deny:
            self.assertTrue(self.g.jev_denied("Grep", tool_input), tool_input)
        for tool_input in allow:
            self.assertFalse(self.g.jev_denied("Grep", tool_input), tool_input)


S = "security find-generic-password -s orche -a JEV_API_KEY -w"
KF = "~/.claude/orche/secrets/jev.key"
SD = "~/.claude/orche/secrets"


class GateRound2Test(support.TempHomeCase):
    """Round 2: inline interpreter code as text, wrappers by basename, fail closed, normalised
    paths, reads under the secrets dir, search patterns exempt, prefilter."""

    def reason(self, command):
        return jevgate.reason("Bash", {"command": command})

    def assert_denied(self, *commands):
        for command in commands:
            self.assertEqual(self.reason(command), jevgate.REASON, command)

    def assert_allowed(self, *commands):
        for command in commands:
            self.assertIsNone(self.reason(command), command)

    def test_inline_interpreter_code(self):
        self.assert_denied(
            "python3 -c \"from pathlib import Path; print(Path.home().joinpath('.claude/orche/secrets/jev.key')"
            ".read_text())\"",
            "python3 - <<EOF\nprint(open('/Users/x/.claude/orche/secrets/jev.key').read())\nEOF",
            "python3 -c \"import os; open(os.path.join(os.path.expanduser('~'), '.claude/orche', 'secrets', "
            "'jev.key'))\"",
            "python3 -c \"import subprocess; subprocess.run(['security','find-generic-password','-s','x','-w'])\"",
            f"python3 <<EOF\nimport os; os.system('{S}')\nEOF",
            "python3 -c 'import orche.jevkey as j; print(j.resolve(\"JEV_API_KEY\"))'",
            "python3 -c 'from orche import jev; print(jev.client().key())'",
            "python3 -c 'import os; print(os.environ[\"JEV_API_KEY\"])'",
            "cat <<'EOF' | python3\nfrom orche import jevkey\nprint(jevkey.get())\nEOF",
            f"echo \"import os; os.system('{S}')\" | python3",
            f"node -e \"require('child_process').execSync('{S}')\"", "node -e 'console.log(process.env.JEV_API_KEY)'",
            f"perl -e 'system(\"{S}\")'", f"ruby -e 'system(\"{S}\")'", f"osascript -e 'do shell script \"{S}\"'",
            f"awk 'BEGIN {{ system(\"{S}\") }}'", "php -r 'echo file_get_contents(\"/h/.claude/orche/secrets/jev.key\");'",
            "python3 -c \"import glob; print(open(glob.glob('/h/.claude/orche/secrets/?ev.key')[0]).read())\"",
            "python3 -c 'import subprocess; subprocess.run([\"security\", \"-i\"])'",
            "python3 -c 'import subprocess; subprocess.run([\"security\", \"dump-keychain\", \"-d\"])'")
        self.assert_allowed(
            "python3 -m unittest discover -s tests", "python3 -c 'print(1)'",
            "python3 - <<'EOF'\n# don't call jevkey.get() here\nprint(1)\nEOF",  # a comment line is stripped
            "node -e 'console.log(1)'")

    def test_trailing_python_comments_stripped(self):
        self.assert_allowed(
            "python3 -c \"print(1)  # don't call jevkey.get()\"",
            "python3 - <<'EOF'\nimport os\nx = 1  # never os.environ['JEV_API_KEY']\nprint(x)\nEOF")
        self.assert_denied(
            "python3 -c \"jevkey.get()  # fine\"",
            "python3 -c \"print('# not a comment', jevkey.get())\"",  # a # inside a string is not a comment
            "python3 -c \"jevkey.get(  # unbalanced: does not tokenize, so nothing is stripped\"")
        self.assertEqual(jevgate.strip_python_comments("a = 1  # c\r\nb = '#x'\n"), "a = 1  \nb = '#x'\n")
        self.assertEqual(jevgate.strip_python_comments("f(  # c"), "f(  # c")

    def test_shell_strings_rescanned_as_commands(self):
        self.assert_denied(f"echo '{S}' | bash", f'bash -c "{S}"', f"eval '{S}'", f"exec {S}", f"command {S}",
                           f"watch -n1 {S}", f"script -c '{S}' /dev/null", f"flock -c '{S}' /tmp/l",
                           f"env -S '{S}'")

    def test_wrappers_by_basename(self):
        for prefix in ("timeout 5", "gtimeout 5", "/opt/homebrew/bin/gtimeout -s KILL -k 2 5", "/usr/bin/time",
                       "time -p", "stdbuf -o0", "stdbuf -o 0", "nice -n 5", "nice -5", "nohup", "command -p",
                       "exec -a foo", "/usr/bin/env -i", "env -u X A=1", "caffeinate -i", "sudo -u root", "doas -u me",
                       "xargs -I{}", "watch -n 1", "script -q /dev/null", "unbuffer", "chrt -f 10", "ionice -c 3",
                       "setsid", "flock /tmp/l", "busybox", "builtin", "arch -arm64", "timeout --signal=KILL 5"):
            self.assert_denied(f"{prefix} {S}")
        self.assertEqual(cmdscan.argv(cmdscan.scan("timeout -k 2 5 sudo -u me /usr/bin/time ls -l")[0]),
                         ["ls", "-l"])

    def test_fail_closed_past_the_limits(self):
        nested = "echo " + "$(" * 450 + "security x" + ")" * 450
        quoted = "echo " + '"$(' * 450 + S + ')"' * 450
        for command in (nested, quoted, "echo " + "$(echo " * 9 + S + ")" * 9):
            self.assertEqual(self.reason(command), jevgate.TOO_COMPLEX, command[:40])
        evals = S
        for _ in range(6):
            evals = "eval " + json.dumps(evals)
        self.assertEqual(self.reason(evals), jevgate.TOO_COMPLEX)
        with mock.patch.object(jevgate, "_commands_denied", side_effect=RecursionError):
            self.assertEqual(self.reason("security list-keychains"), jevgate.TOO_COMPLEX)
        # the prefilter: no key-shaped word anywhere, no scan, allowed however deep
        self.assertIsNone(self.reason("echo " + "$(" * 450 + "true" + ")" * 450))

    def test_key_path_forms(self):
        self.assert_denied(
            f'git commit -m "docs: never cat {KF}"', f"cat <<'EOF' > notes.md\ncat {KF}\nEOF",
            "echo $HOME/.claude/orche/secrets/jev.key", "echo ${HOME}/.claude/orche//secrets/./jev.key",
            'echo "$ORCHE_HOME"/secrets/jev.key', "echo $ORCHE_HOME/secrets/jev.key",
            "echo /Users/x/.claude/orche/secrets/../secrets/jev.key",
            f"echo {self.home}/secrets/jev.key",  # the configured ORCHE_HOME
            "cat secrets//jev.key", "cat secrets/./jev.key", "cat secrets/../secrets/jev.key",
            "cat ./secrets/jev.key", f"cd {SD}/ && cat ./jev.key", f"pushd {SD} && cat jev.key",
            f"stat {KF}")  # the full path anywhere; `orche jev key status|rm` exist
        self.assert_allowed("rg 'secrets/jev.key' lib/", "rg secrets/jev.key lib/", "git grep -n secrets/jev.key",
                            "grep -rn -e 'secrets/jev.key' docs", 'git commit -m "docs: never cat secrets/jev.key"',
                            "rg jev.key docs/")

    def test_reads_under_the_secrets_dir(self):
        for reader in ("cat", "head -c 10", "tail", "less", "more", "xxd", "od -c", "strings", "base64", "cp",
                       "mv", "tee", "open", "source", "."):
            self.assert_denied(f"{reader} {SD}/other.json")
        self.assert_denied(f"dd if={SD}/x of=/tmp/y", f"base64 < {SD}/x", f"wc -c < {SD}/x",
                           f"cd {SD} && cat other.json")
        self.assert_allowed(f"ls -la {SD}", f"stat {SD}/other.json", f"test -f {SD}/other.json",
                            f"file {SD}/other.json", f"rm {SD}/other.json", "cat secrets/config.json")

    def test_search_tools_over_orche_home(self):
        self.assert_denied(f"rg . {SD}", "rg . ~/.claude/orche", f"grep -r x {SD}/", f"rg x {self.home}",
                           f"ag x {SD}", f"git grep x -- {SD}", f"rg -g 'jev.*' x {os.path.dirname(str(self.home))}")
        self.assert_allowed("rg x ~/.claude/orche-notes", "rg -g '*.py' x lib")

    def test_read_edit_write_paths_normalised(self):
        for path in ("/x/.claude/orche/secrets//jev.key", "/x/.claude/orche/secrets/./jev.key",
                     "/x/.claude/orche/secrets/../secrets/jev.key", f"{SD}/jev.key", f"{SD}/other.json"):
            for tool in ("Read", "Edit", "Write"):
                self.assertEqual(jevgate.reason(tool, {"file_path": path}), jevgate.REASON, (tool, path))
        self.assertIsNone(jevgate.reason("Read", {"file_path": "/x/docs/jev.key.md"}))

    def test_source_folder_named_secrets_is_not_the_store(self):
        """0.3.4: the store rule is tied to ORCHE_HOME's secrets dir, not any `…/orche/secrets/`."""
        repo = "/Users/x/projects/ORCHE"
        self.assert_allowed("cat lib/orche/secrets/store.py", f"cat {repo}/lib/orche/secrets/store.py",
                            "rg foo lib/orche/secrets/", f"cd {repo}/lib/orche/secrets && cat share.py")
        for tool in ("Read", "Edit", "Write"):
            self.assertIsNone(jevgate.reason(tool, {"file_path": f"{repo}/lib/orche/secrets/store.py"}), tool)
        self.assertIsNone(jevgate.reason("Grep", {"pattern": "def", "path": "lib/orche/secrets"}))
        self.assertIsNone(jevgate.reason("Grep", {"pattern": "def", "path": f"{repo}/lib/orche/secrets"}))
        # the real store, every spelling, stays refused; so does any `secrets/jev.key`
        self.assert_denied(f"cat {SD}/store.json", "cat ~/.claude/orche/secrets/audit.log",
                           "cat $HOME/.claude/orche/secrets/x", "cat $ORCHE_HOME/secrets/x",
                           f"cat {self.home}/secrets/x", "cat lib/orche/secrets/jev.key")
        self.assertEqual(jevgate.reason("Grep", {"pattern": "x", "path": f"{self.home}/secrets"}), jevgate.REASON)


class CmdScanTest(support.TempHomeCase):
    def test_words_quotes_heredocs_and_substitutions(self):
        cmds = cmdscan.scan("FOO=1 git commit -m 'a; b' -m \"c $(id -u) d\" 2>/dev/null && ls\n"
                            "cat <<-'EOF' | wc -l\n\tbody line\n\tEOF\necho `whoami` # trailing comment")
        words = [c.words for c in cmds]
        self.assertIn(["id", "-u"], words)
        self.assertIn(["FOO=1", "git", "commit", "-m", "a; b", "-m", "c  d"], words)
        self.assertIn(["ls"], words)
        self.assertIn(["whoami"], words)
        cat = next(c for c in cmds if c.words == ["cat"])
        self.assertEqual(cat.stdin, ["\tbody line"])
        git = next(c for c in cmds if c.words[:1] == ["FOO=1"])
        self.assertEqual(git.redirects, ["/dev/null"])  # `2>`: the fd is not a word
        self.assertEqual(cmdscan.argv(git)[:2], ["git", "commit"])
        self.assertEqual(cmdscan.argv(cmdscan.scan("sudo -u me env A=1 security x")[0]), ["security", "x"])
