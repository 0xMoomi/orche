"""c61: `guide.lines`, the "Any time" list of the setup report and of `orche doctor`'s next steps: one line per
feature, off first, then on, then the always-present lines; at most 12 lines, each under 320 characters.
c75: each line says what the feature is before its state, with real numbers."""
import copy
import os
import re
import subprocess
from pathlib import Path
from unittest import mock

import support
from orche import cmd_init, config, guide, janitor, jevkey, registry, scale

SHAPE = re.compile(r'^- [^:]+: [^.]+\. .+?\. Say "[^;]+"; .+\.$')   # what it is. State. Say ...
ALWAYS_SHAPE = re.compile(r'^- [^:]+: .+?\. Say "[^;]+"; .+\.$')


class GuideTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.user = self.home / "user"
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["review"]["cross_vendor"] = "required"
        self.reg = registry.empty()
        self.codex = None   # shutil.which("codex")

    def lines(self):
        with mock.patch.object(guide.shutil, "which", lambda name: {"codex": self.codex}.get(name)):
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
            self.assertRegex(ln, ALWAYS_SHAPE if ln in guide.ALWAYS else SHAPE)
            self.assertLess(len(ln), 320, ln)
            self.assertNotRegex(ln, "[\u2013\u2014\uac00-\ud7a3]")

    def test_a_fresh_machine_lists_every_feature_off_first(self):
        lines = self.lines()
        self.check_shape(lines)
        self.assertEqual(lines[0], '- Jev: a small model that sorts incoming requests and checks gates before Main '
                                   'spends tokens. Off. Say "turn on Jev"; Main runs `orche jev setup`.')
        self.assertIn("Codex cross-check: a second AI vendor reviews each pull request before a person does. "
                      "Off: Codex is not installed. ", lines[1])
        self.assertIn("`npm install -g @openai/codex`", lines[1])
        self.assertIn("Background-run token: lets ORCHE start agents while no Claude window is open. Not set.", lines[2])
        self.assertIn("Janitor: cleans up daily; retired agents' workspaces and build caches or scratch files idle 7 "
                      "days go to a trash emptied after 30 days. Off.", lines[3])
        self.assertIn("More agents at once: how many workers may run in parallel. Up to 4 at once (default).", lines[4])
        self.assertEqual(len(lines), 10)
        self.assertFalse(any("otification" in ln for ln in lines))
        self.assertEqual(lines[-5:], list(guide.ALWAYS))
        for words in ("set up again", "`orche init --refresh`", "`orche update`", "`orche card <project>`",
                      "`orche metrics`", "`orche doctor`"):
            self.assertIn(words, "\n".join(guide.ALWAYS))

    def test_each_feature_turns_on(self):
        self.cfg["jev"]["enabled"] = True
        jevkey.set_key("k-123")
        on, line = guide.jev_line(self.cfg)
        self.assertTrue(on)
        self.assertIn(". On. ", line)
        self.assertFalse(any(ln.startswith("- Jev") for ln in self.lines()))   # on: the report's Running now
        self.codex = "/usr/local/bin/codex"
        self.assertIn(". On. ", self.line("Codex cross-check"))
        self.cfg["review"]["cross_vendor"] = "off"
        self.assertIn(". Off: cross-vendor review is off. ", self.line("Codex cross-check"))
        jevkey.set_key("t-123", account=jevkey.OAUTH_ACCOUNT)
        self.assertIn(". Set. ", self.line("Background-run token"))
        janitor.enabled_flag().touch()
        self.assertIn(". Off. ", self.line("Janitor"))   # enabled, but no daily schedule
        plist = self.user / "Library" / "LaunchAgents" / "com.orche.janitor.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("x")
        on, line = guide.janitor_line(self.cfg, self.user)
        self.assertTrue(on)
        self.assertIn(". On. ", line)
        self.assertFalse(any(ln.startswith("- Janitor") for ln in self.lines()))
        self.cfg["janitor"] = {"trash_days": 14}
        self.assertIn("a trash emptied after 14 days", guide.janitor_line(self.cfg, self.user)[1])
        conf, limits = scale.target(self.cfg, "30")
        for key, value in conf.items():
            scale._put(self.cfg, key, value)
        self.reg["limits"].update(limits)
        self.assertIn(". Up to 20 at once (preset 30). ", self.line("More agents"))
        lines = self.lines()
        self.check_shape(lines)
        self.assertTrue(lines[0].startswith("- Codex cross-check"), lines)   # the only one still off

    def test_jev_enabled_without_a_key_is_off(self):
        self.cfg["jev"]["enabled"] = True
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_API_KEY", None)
            self.assertIn(". Off. ", self.line("Jev"))

    def test_a_project_without_the_queue_gate_gets_a_line(self):
        self.project("acme")
        self.assertIn('No base branch found yet, so a person merges each PR. Say "set up the merge queue for acme"; '
                      'Main runs `orche init --refresh acme`', self.line("Merge queue for acme"))
        self.cfg["publish"] = {"base": "main"}
        self.assertEqual(self.line("Merge queue for acme"),
                         '- Merge queue for acme: lands approved pull requests by itself after its checks pass. The '
                         'check script (scripts/verify/ci.sh) could not be pushed to main, so a person merges each PR. '
                         'Say "set up the merge queue for acme"; Main runs `orche init --refresh acme`.')
        self.check_shape(self.lines())

    def test_many_projects_share_one_gate_line_and_the_list_stays_short(self):
        for key in ("a-very-long-project-name-one", "a-very-long-project-name-two", "a-very-long-project-name-3"):
            self.project(key)
        lines = self.lines()
        self.check_shape(lines)
        self.assertEqual(len(lines), 11)
        self.assertIn("- Merge queue: lands approved pull requests by itself after its checks pass. "
                      "a-very-long-project-name-3 and 2 more projects lack the check script", lines[5])

    def test_the_more_agents_line_shows_a_custom_number(self):
        self.cfg["runner"]["max_parallel"] = 7
        self.assertIn(". Up to 7 at once (custom). ", self.line("More agents"))
        self.assertEqual(guide.parallel(self.cfg, self.reg), (7, "custom"))

    def test_running_now_has_the_real_values(self):
        """c75: the setup report's "Running now": heartbeat interval, janitor time and rules, the project's queue."""
        root = self.project("acme")
        self.cfg["publish"] = {"base": "main"}
        self.cfg["reconcile"] = {"enabled": True, "every_s": 120}
        with mock.patch.object(cmd_init.Path, "home", classmethod(lambda cls: self.user)):
            off = cmd_init._running_lines(self.reg, "acme", root, self.cfg)
            janitor.enabled_flag().touch()
            plist = self.user / "Library" / "LaunchAgents" / "com.orche.janitor.plist"
            plist.parent.mkdir(parents=True)
            plist.write_text("x")
            lines = cmd_init._running_lines(self.reg, "acme", root, self.cfg)
        self.assertEqual(off[1], '- Janitor: off; say "let the janitor clean up" (listed below).')
        self.assertEqual(lines, [
            "- Heartbeat: every 120 s (the reconcile job): wakes Main and the divisions when work waits.",
            "- Janitor: daily at 04:00; moves retired units' workspaces, and build caches of units idle for 7 days, "
            "to a trash it empties after 30 days; touches nothing in the repository.",
            "- Merge queue: on when the repository has scripts/verify/ci.sh on main (runs that script on each "
            "approved PR; lands it by itself when that passes); this project: waiting; no check script on main and "
            "no PR.",
            '- Jev: off; say "turn on Jev" (listed below).',
            "- Notifications: Orca shows one when Main finishes a reply; ORCHE reminds you after 30 minutes when a "
            "decision still waits."])
        self.cfg["queue"]["land"] = "owner"
        self.assertIn('lands it when you say "merge"', cmd_init._running_lines(self.reg, "acme", root, self.cfg)[2])
        for ln in lines:
            self.assertNotRegex(ln, "[\u2013\u2014]")

    def test_a_folder_that_is_not_a_git_checkout_is_skipped(self):
        root = self.home / "plain"
        root.mkdir()
        self.reg["projects"]["plain"] = {"path": str(root), "divisions": {}}
        self.assertFalse(any("Merge queue" in ln for ln in self.lines()))


if __name__ == "__main__":
    import unittest
    unittest.main()
