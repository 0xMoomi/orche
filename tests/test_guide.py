"""c61: `guide.lines`, the "Any time" list of the setup report and of `orche doctor`'s next steps: one line per
feature, off first, then on, then the always-present lines; at most 12 lines, each under 200 characters."""
import copy
import os
import re
import subprocess
from pathlib import Path
from unittest import mock

import support
from orche import config, guide, janitor, jevkey, registry, scale

SHAPE = re.compile(r'^- [^:]+: .+?\. Say "[^;]+"; .+\.$')


class GuideTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.user = self.home / "user"
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["review"]["cross_vendor"] = "required"
        self.reg = registry.empty()
        self.codex = None   # shutil.which("codex")
        self.notifier = None   # shutil.which("terminal-notifier")

    def lines(self):
        with mock.patch.object(guide.shutil, "which", lambda name: {"codex": self.codex, "terminal-notifier": self.notifier}.get(name)):
            return guide.lines(self.cfg, self.reg, self.user)

    def line(self, feature):
        [found] = [ln for ln in self.lines() if ln.startswith(f"- {feature}")]
        return found

    def project(self, key):
        root = self.home / "repos" / key
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        self.reg["projects"][key] = {"path": str(root), "divisions": {}}
        return root

    def check_shape(self, lines):
        self.assertLessEqual(len(lines), guide.MAX_LINES)
        for ln in lines:
            self.assertRegex(ln, SHAPE)
            self.assertLess(len(ln), 200, ln)
            self.assertNotRegex(ln, "[\u2013\u2014\uac00-\ud7a3]")

    def test_a_fresh_machine_lists_every_feature_off_first(self):
        lines = self.lines()
        self.check_shape(lines)
        self.assertEqual(lines[0], '- Jev (the triage classifier and gates): off. Say "turn on Jev"; '
                                   'Main runs `orche jev setup`.')
        self.assertIn(": Codex is not installed. ", lines[1])
        self.assertIn("`npm install -g @openai/codex`", lines[1])
        self.assertIn("Background-run token: not set.", lines[2])
        self.assertIn("Janitor (daily cleanup): off.", lines[3])
        self.assertIn("More agents at once: preset default.", lines[4])
        self.assertEqual(lines[5], '- Notification click: opens Orca (enough for most; the exact tab needs '
                                   'terminal-notifier). Say "make notification clicks open the tab"; '
                                   'Main runs `brew install terminal-notifier`; you allow it once.')
        self.assertEqual(lines[-5:], list(guide.ALWAYS))
        for words in ("set up again", "`orche init --refresh`", "`orche update`", "`orche card <project>`",
                      "`orche metrics`", "`orche doctor`"):
            self.assertIn(words, "\n".join(guide.ALWAYS))

    def test_each_feature_turns_on(self):
        self.cfg["jev"]["enabled"] = True
        jevkey.set_key("k-123")
        self.assertIn(": on. ", self.line("Jev"))
        self.codex = "/usr/local/bin/codex"
        self.assertIn(": on. ", self.line("Codex cross-check"))
        self.cfg["review"]["cross_vendor"] = "off"
        self.assertIn(": cross-vendor review is off. ", self.line("Codex cross-check"))
        jevkey.set_key("t-123", account=jevkey.OAUTH_ACCOUNT)
        self.assertIn(": set. ", self.line("Background-run token"))
        janitor.enabled_flag().touch()
        self.assertIn(": off. ", self.line("Janitor"))   # enabled, but no daily schedule
        plist = self.user / "Library" / "LaunchAgents" / "com.orche.janitor.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("x")
        self.assertIn(": on. ", self.line("Janitor"))
        conf, limits = scale.target(self.cfg, "30")
        for key, value in conf.items():
            scale._put(self.cfg, key, value)
        self.reg["limits"].update(limits)
        self.assertIn(": preset 30. ", self.line("More agents"))
        self.notifier = "/opt/homebrew/bin/terminal-notifier"
        self.assertIn(": opens the project's Main tab. ", self.line("Notification click"))
        lines = self.lines()
        self.check_shape(lines)
        self.assertTrue(lines[0].startswith("- Codex cross-check"), lines)   # the only one still off

    def test_jev_enabled_without_a_key_is_off(self):
        self.cfg["jev"]["enabled"] = True
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_API_KEY", None)
            self.assertIn(": off. ", self.line("Jev"))

    def test_a_project_without_the_queue_gate_gets_a_line(self):
        self.project("acme")
        self.assertIn('no base branch. Say "set up the merge queue for acme"; Main runs `orche init acme --refresh`',
                      self.line("Merge-queue gate for acme"))
        self.cfg["publish"] = {"base": "main"}
        self.assertEqual(self.line("Merge-queue gate for acme"),
                         '- Merge-queue gate for acme: no scripts/verify/ci.sh on origin/main. Say "set up the merge '
                         'queue for acme"; Build\'s first unit adds it from templates/verify/ci.sh.')
        self.check_shape(self.lines())

    def test_many_projects_share_one_gate_line_and_the_list_stays_short(self):
        for key in ("a-very-long-project-name-one", "a-very-long-project-name-two", "a-very-long-project-name-3"):
            self.project(key)
        lines = self.lines()
        self.check_shape(lines)
        self.assertEqual(len(lines), 12)
        self.assertIn("- Merge-queue gate: missing in a-very-long-project-name-3 and 2 more projects. ", lines[6])

    def test_a_folder_that_is_not_a_git_checkout_is_skipped(self):
        root = self.home / "plain"
        root.mkdir()
        self.reg["projects"]["plain"] = {"path": str(root), "divisions": {}}
        self.assertFalse(any("Merge-queue" in ln for ln in self.lines()))


if __name__ == "__main__":
    import unittest
    unittest.main()
