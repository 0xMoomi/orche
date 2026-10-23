"""0.7.2: `orche setup` — install, auth status, init per --project, the heartbeat's first pass, doctor."""
import json
import os
import sys
import unittest
from unittest import mock

import support
import test_hr
from orche import cmd_setup, jevkey, oauth, privacy, registry
from orche.install import launchd


class SetupTest(test_hr.HrCase):
    def setUp(self):
        super().setUp()
        self.h = self.home / "h"
        (self.h / ".claude").mkdir(parents=True)
        claude = support.write_fake_claude(self.home / "fake-claude")   # doctor probes it
        cfg = json.loads((self.home / "config.json").read_text())
        (self.home / "config.json").write_text(json.dumps({**cfg, "claude_bin": str(claude)}))

    def setup_cmd(self, *extra):
        return self.orche("setup", "--home", str(self.h), *extra)

    def test_a_new_machine_in_one_command_and_again(self):
        code, out, err = self.setup_cmd("--project", str(self.repo))
        self.assertEqual(code, 0, out + err)
        heads = [ln for ln in out.splitlines() if ln.startswith("== ")]
        self.assertEqual(heads, ["== install ==", "== auth ==", f"== init {self.repo} ==", "== doctor =="])
        self.assertIn(f"oauth token: {oauth.UNSET_LINE}\nstore a token for headless runs (setup never reads it):\n"
                      "  claude setup-token\n  orche auth set\n", out)
        self.assertTrue(out.endswith("setup: done\n"))
        self.assertNotIn("FAIL\t", out)
        self.assertIn("privacy\t", out)
        self.assertEqual(sorted(registry.load()["projects"]), ["acme-shop"])
        hooks = json.loads((self.h / ".claude" / "settings.json").read_text())["hooks"]
        self.assertTrue(hooks)
        panes = len(self.orca_calls("terminal create"))
        code, out, err = self.setup_cmd("--project", str(self.repo))     # idempotent
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(registry.load()["projects"]), ["acme-shop"])
        self.assertEqual(len(self.orca_calls("terminal create")), panes)
        self.assertEqual(json.loads((self.h / ".claude" / "settings.json").read_text())["hooks"], hooks)

    def mode(self):
        return json.loads((self.home / "install-state.json").read_text())["mode"]

    def test_setup_installs_in_the_mode_the_last_install_used(self):
        code, out, err = self.setup_cmd("--no-auth")             # nothing installed yet: settings
        self.assertEqual((code, self.mode()), (0, "settings"), out + err)
        self.assertIn("jev: skipped (--no-jev)", out)
        state = json.loads((self.home / "install-state.json").read_text())
        (self.home / "install-state.json").write_text(json.dumps({**state, "mode": "plugin"}))
        code, out, err = self.setup_cmd("--no-auth")
        self.assertEqual((code, self.mode()), (0, "plugin"), out + err)
        self.assertIn("/plugin install orche@orche", out)       # the plugin is not enabled yet: its two lines

    def test_an_enabled_plugin_means_plugin_mode(self):
        (self.h / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"orche@orche": True}}))
        code, out, err = self.setup_cmd("--no-auth")
        self.assertEqual((code, self.mode()), (0, "plugin"), out + err)
        self.assertNotIn("/plugin install", out)

    def test_a_stored_token_and_no_auth(self):
        jevkey.set_key("sk-test-not-a-real-token", account=oauth.ENV)
        code, out, _ = self.setup_cmd()
        self.assertEqual(code, 0)
        self.assertIn("oauth token: file — headless runs use it", out)
        self.assertNotIn("claude setup-token", out)
        self.assertNotIn("sk-test", out)
        code, out, _ = self.setup_cmd("--no-auth")
        self.assertNotIn("== auth ==", out)

    def test_a_failed_init_is_reported_and_exits_1(self):
        code, out, _ = self.setup_cmd("--project", str(self.home / "not-a-repo"))
        self.assertEqual(code, 1)
        self.assertTrue(out.endswith(f"setup: needs attention: init {self.home / 'not-a-repo'}\n"))
        self.assertIn("== doctor ==", out)

    def test_dry_run_writes_nothing(self):
        code, out, err = self.setup_cmd("--project", str(self.repo), "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.h / ".claude" / "settings.json").exists())
        self.assertEqual(registry.load()["projects"], {})
        self.assertNotIn("== heartbeat ==", out)

    @unittest.skipUnless(sys.platform == "darwin", "the reconcile LaunchAgent is macOS only")
    def test_the_heartbeat_step_runs_after_the_inits_when_the_job_is_loaded(self):
        seen = []   # (the cmd_*.py modules are loaded afresh per CLI call: patch what they call)

        def wait(since, timeout_s, kick=None, kick_every_s=None):
            seen.append((timeout_s, len(registry.load()["projects"])))
            return {"took_s": 0.3}
        with mock.patch.object(launchd, "_live", lambda home: True), \
                mock.patch.object(launchd, "loaded", lambda label: True), \
                mock.patch.object(privacy, "wait_job_pass", wait):
            code, out, err = self.setup_cmd("--project", str(self.repo))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [(privacy.FIRST_PASS_S, 1)])    # once, after the init registered the project
        self.assertLess(out.index(f"== init {self.repo} =="),
                        out.index(f"== heartbeat ==\n{privacy.FIRST_PASS_WAIT}\nreconcile job: first pass OK (0.3 s)\n"))
        self.assertLess(out.index("== heartbeat =="), out.index("== doctor =="))

    def blocked_job(self):
        """The job loaded, the repo under a protected folder, and no job pass ever (macOS privacy)."""
        seen, root = [], os.path.realpath(self.repo)

        def wait(since, timeout_s, kick=None, kick_every_s=None):
            seen.append(timeout_s)
            return None
        patches = [mock.patch.object(launchd, "_live", lambda home: True),
                   mock.patch.object(launchd, "loaded", lambda label: True),
                   mock.patch.object(privacy, "protected", lambda path, home=None: str(path).startswith(root)),
                   mock.patch.object(privacy, "wait_job_pass", wait),
                   mock.patch.object(privacy, "reveal", lambda app: seen.append("reveal"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return seen

    @unittest.skipUnless(sys.platform == "darwin", "the reconcile LaunchAgent is macOS only")
    def test_a_blocked_heartbeat_is_the_one_step_left(self):     # review #5
        seen = self.blocked_job()
        code, out, err = self.setup_cmd("--project", str(self.repo))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(seen, [privacy.FIRST_PASS_S, "reveal"])     # once: init left it to setup
        self.assertTrue(out.endswith(cmd_setup.BLOCKED_LINE + "\n"), out)
        self.assertIn("orche install --grant", out[out.index("== heartbeat =="):out.index("== doctor ==")])

    @unittest.skipUnless(sys.platform == "darwin", "the reconcile LaunchAgent is macOS only")
    def test_init_of_a_protected_project_checks_the_job(self):   # review #4
        seen = self.blocked_job()
        code, out, err = self.orche("init", "--no-intake", "--", str(self.repo))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(seen, [privacy.FIRST_PASS_S, "reveal"])
        tail = out[out.index(privacy.FIRST_PASS_WAIT):]
        self.assertIn("orche install --grant", tail)
        code, out, err = self.orche("init", "--no-intake", "--", str(self.repo))   # registered already
        self.assertEqual((code, len(seen)), (0, 2), out + err)
        self.assertNotIn(privacy.FIRST_PASS_WAIT, out)
