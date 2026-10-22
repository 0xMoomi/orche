"""orche auth + the OAuth token headless runs use (0.6.1).

The token is built at runtime from an obviously fake sentinel; it must never reach argv, stdout,
stderr, a dry run, a log, or any file other than the 0600 store file."""
import contextlib
import io
import json
import os
import stat
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)
from orche import headless, jevkey, oauth
from orche.hr import panes
from test_headless import project as call
from test_jev_onboarding import OnboardingCase

TOKEN = "tok-oauth-" + "Q7" * 12
ENV_TOKEN = "tok-env-" + "W3" * 10


class AuthCase(OnboardingCase):
    def setUp(self):
        super().setUp()
        os.environ.pop(oauth.ENV, None)
        self.store = self.home / "secrets" / "oauth.token"

    def assert_token_nowhere(self, *texts):
        for text in texts:
            self.assertNotIn(TOKEN, text)
        for path in self.home.rglob("*"):
            if path.is_file() and path != self.store and path.name not in ("item", "stdin.log", "typed"):
                self.assertNotIn(TOKEN, path.read_text(errors="replace"), str(path))


class InjectTest(AuthCase):
    def test_stored_token_reaches_headless_env(self):
        jevkey.set_key(TOKEN, account=oauth.ENV)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assertEqual(headless.env(call(), {})[oauth.ENV], TOKEN)

    def test_inherited_token_wins_and_store_is_not_read(self):
        jevkey.set_key(TOKEN, account=oauth.ENV)
        with mock.patch.dict(os.environ, {oauth.ENV: ENV_TOKEN}), \
                mock.patch.object(jevkey, "read", side_effect=AssertionError("store read")):
            self.assertEqual(headless.env(call(), {})[oauth.ENV], ENV_TOKEN)

    def test_no_token_leaves_env_alone(self):
        self.assertNotIn(oauth.ENV, headless.env(call(), {}))
        self.assertEqual(oauth.inject({}), "login")
        self.assertFalse((self.home / "secrets").exists())  # a read creates nothing

    def test_pane_command_never_carries_the_token(self):
        jevkey.set_key(TOKEN, account=oauth.ENV)
        for resume in (False, True):
            with contextlib.redirect_stderr(io.StringIO()):   # the no-transcript note
                cmd = panes.pane_command("u-1", resume=resume)
            self.assertNotIn(TOKEN, cmd)
            self.assertNotIn(oauth.ENV, cmd)

    def test_doctor_row_both_states(self):
        self.assertEqual(oauth.doctor_row(), ("WARN", "oauth token", oauth.UNSET_LINE))
        jevkey.set_key(TOKEN, account=oauth.ENV)
        self.assertEqual(oauth.doctor_row(), ("OK", "oauth token", oauth.SET_LINE))

    def test_keychain_account_is_separate_from_jev(self):
        self.keychain()
        self.assertEqual(jevkey.set_key(TOKEN, account=oauth.ENV), "keychain")
        self.assertNotIn(TOKEN, (self.bin / "argv.log").read_text())
        self.assertIn('-a "CLAUDE_CODE_OAUTH_TOKEN"', (self.bin / "stdin.log").read_text())
        self.assertEqual(oauth.source(), ("keychain", None))


    # -- T2: a stored token that cannot be read leaves one line, never the value -----------------
    def hooks_log(self):
        path = self.home / "logs" / "hooks.log"
        return path.read_text() if path.exists() else ""

    def test_unreadable_stored_token_logs_one_line_and_uses_the_login(self):
        self.keychain()
        jevkey.set_key(TOKEN, account=oauth.ENV)
        (self.bin / "fail").write_text("find")              # the fake security exits 1 from now on
        env = {}
        self.assertEqual(oauth.inject(env), "login")
        self.assertNotIn(oauth.ENV, env)
        lines = self.hooks_log().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("oauth: stored token unreadable (exit 1); headless run uses the login"))
        self.assertNotIn("SecKeychainItem", self.hooks_log())
        self.assert_token_nowhere(self.hooks_log())

    def test_security_past_the_timeout_logs_exit_124(self):
        slow = self.bin / "slow-security"
        slow.write_text("#!/bin/sh\nsleep 5\n")
        slow.chmod(0o755)
        self.keychain()
        jevkey.SECURITY = str(slow)
        with mock.patch.object(jevkey, "SECURITY_TIMEOUT", 0.3):
            self.assertEqual(jevkey.read(account=oauth.ENV), (None, "exit 124"))
            self.assertEqual(oauth.inject({}), "login")
        self.assertIn("oauth: stored token unreadable (exit 124); headless run uses the login", self.hooks_log())

    def test_nothing_stored_in_the_keychain_logs_nothing(self):
        self.keychain()
        self.assertEqual(jevkey.read(account=oauth.ENV), (None, None))
        self.assertEqual(oauth.inject({}), "login")
        self.assertEqual(self.hooks_log(), "")

class AuthCliTest(AuthCase):
    def test_option_form_value_is_masked_in_the_usage_error(self):
        secret = "s" + "k-ant-oat01-" + "Zq9" * 12      # built here: no key literal in the source
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            self.cli.main(["auth", "set", f"--token={secret}"])   # not self.orche: SystemExit skips its buffers
        self.assertEqual(exit_.exception.code, 1)
        self.assertIn("unrecognized arguments: --token=[REDACTED]", err.getvalue())
        self.assertNotIn(secret, err.getvalue())

    def test_set_from_stdin_when_not_a_tty(self):
        with self.tty(False), self.stdin(f"{TOKEN}\n"):
            code, out, err = self.orche("auth", "set")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("stored in the file", out)
        self.assertEqual(self.store.read_text().strip(), TOKEN)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)
        self.assert_token_nowhere(out, err)

    def test_set_from_hidden_prompt_on_a_tty(self):
        with self.tty(True), self.stdin(f"{TOKEN}\n"):
            code, out, err = self.orche("auth", "set")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.read_text().strip(), TOKEN)
        self.assert_token_nowhere(out.replace("Claude OAuth token (from `claude setup-token`): ", ""), err)

    def test_token_on_argv_is_refused_without_echo(self):
        code, out, err = self.orche("auth", "set", TOKEN)
        self.assertEqual(code, 1)
        self.assertIn("never taken from the command line", err)
        self.assertFalse(self.store.exists())
        self.assert_token_nowhere(out, err)

    def test_dry_run_stores_nothing_and_reads_nothing(self):
        with self.tty(False), self.stdin(f"{TOKEN}\n"):
            code, out, err = self.orche("--dry-run", "--json", "auth", "set")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"dry_run": True, "backend": "file"})
        self.assertFalse(self.store.exists())
        self.assert_token_nowhere(out, err)

    def test_empty_input_stores_nothing(self):
        with self.tty(False), self.stdin(""):
            code, _, err = self.orche("auth", "set")
        self.assertEqual(code, 1)
        self.assertIn("no token stored", err)

    def test_status_and_rm(self):
        code, out, _ = self.orche("auth", "status")
        self.assertEqual(code, 0)
        self.assertIn(oauth.UNSET_LINE, out)
        jevkey.set_key(TOKEN, account=oauth.ENV)
        code, out, _ = self.orche("--json", "auth", "status")
        self.assertEqual(json.loads(out)["source"], "file")
        with mock.patch.dict(os.environ, {oauth.ENV: ENV_TOKEN}):
            code, out, _ = self.orche("auth", "status")
        self.assertIn("oauth token: env", out)
        self.assertNotIn(ENV_TOKEN, out)
        code, out, _ = self.orche("auth", "rm")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        self.assertFalse(self.store.exists())
        code, out, _ = self.orche("auth", "rm")
        self.assertIn("no Claude OAuth token", out)
        self.assert_token_nowhere(out)

    def test_jev_key_untouched_by_auth(self):
        jevkey.set_key("tsk-jev-" + "K5" * 10)
        jevkey.set_key(TOKEN, account=oauth.ENV)
        self.orche("auth", "rm")
        self.assertTrue((self.home / "secrets" / "jev.key").exists())
