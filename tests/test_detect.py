"""detect: a repository's team conventions (merged PRs' base, issue-keyed remote branches, Entire), Entire's
commit linking in a unit's worktree and the doctor's `entire` row (c73). A temp bare repo is origin; the
fake gh answers the merged PRs."""
import json
import os
import subprocess
from unittest import mock

import support
from support import TempHomeCase

from orche import detect, registry
from orche.hr import onboarding
from orche.install import doctor

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class DetectCase(TempHomeCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bare, self.repo = self.home / "origin.git", self.home / "repo"
        self.git(self.home, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git(self.home, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "a.txt").write_text("a\n")
        for argv in (("add", "a.txt"), ("commit", "-qm", "a"), ("remote", "add", "origin", str(self.bare))):
            self.git(self.repo, *argv)
        self.branches("main")
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "remote", "set-head", "origin", "main")
        self.gh = self.home / "gh"
        support.fake_gh(self.gh)

    def git(self, cwd, *argv):
        subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)

    def branches(self, *names):
        for name in names:
            self.git(self.repo, "push", "-q", "origin", f"HEAD:refs/heads/{name}")

    def merged(self, n, to, rest="main"):
        prs = [{"number": i, "state": "MERGED", "headRefOid": "0" * 40, "baseRefName": to if i <= 20 else rest}
               for i in range(1, n + 1)]
        (self.gh / "spec.json").write_text(json.dumps({"prs": prs}))

    def found(self):
        return detect.conventions(self.repo, "acme")


class ConventionsTest(DetectCase):
    def test_base_from_merged_prs(self):
        self.merged(24, "develop")
        self.assertEqual(self.found(), {"base": "develop"})
        self.assertEqual(support.gh_calls(self.gh)[0]["argv"],
                         ["pr", "list", "--state", "merged", "--limit", "30", "--json", "baseRefName"])

    def test_too_few_prs_or_the_default_branch_name_no_base(self):
        self.merged(10, "develop")
        self.assertEqual(self.found(), {})
        self.merged(30, "main")
        self.assertEqual(self.found(), {})          # origin/HEAD already names it
        self.merged(30, "develop", rest="release")  # 20 of 30: exactly two thirds
        self.assertEqual(self.found(), {"base": "develop"})

    def test_prefix_and_issue_key_from_remote_branches(self):
        self.branches("dev/des-1-a", "dev/des-2-b", "dev/des-3-c", "feature/x", "dev/some-topic")
        self.assertEqual(self.found(), {"branch_prefix": "dev", "issues": "DES"})

    def test_two_keyed_branches_are_not_enough(self):
        self.branches("dev/des-1-a", "dev/des-2-b", "feature/x")
        self.assertEqual(self.found(), {})

    def test_entire(self):
        (self.repo / ".entire").mkdir()
        (self.repo / ".entire" / "settings.json").write_text("{}\n")
        self.assertEqual(self.found(), {"entire": True})

    def test_gh_failing_still_reads_the_rest(self):
        (self.gh / "spec.json").write_text(json.dumps({"fail": "HTTP 401: auth token"}))
        self.branches("dev/des-1-a", "dev/des-2-b", "dev/des-3-c")
        self.assertEqual(self.found(), {"branch_prefix": "dev", "issues": "DES"})

    def test_no_origin_detects_nothing(self):
        self.git(self.repo, "remote", "remove", "origin")
        self.merged(24, "develop")
        self.assertEqual(self.found(), {"base": "develop"})   # gh still answers; origin/HEAD is gone with origin
        (self.gh / "spec.json").write_text(json.dumps({"fail": "no repo"}))
        self.assertEqual(self.found(), {})


class TeamTest(DetectCase):
    def write(self, data):
        (self.repo / ".claude").mkdir(exist_ok=True)
        (self.repo / ".claude" / "settings.json").write_text(json.dumps(data))

    def test_the_first_enabled_plugin_the_repository_declares(self):
        self.assertIsNone(detect.team(self.repo))
        support.declare_team(self.repo)
        self.assertEqual(detect.team(self.repo), support.TEAM_PLUGIN)
        self.assertEqual(self.found(), {"team": support.TEAM_PLUGIN})
        market = {"source": {"source": "github", "repo": "x/x"}}
        self.write({"enabledPlugins": {"b@x": True, "a@x": True}, "extraKnownMarketplaces": {"x": market}})
        self.assertEqual(detect.team(self.repo), "a@x")
        self.write({"enabledPlugins": {"a@x": False}, "extraKnownMarketplaces": {"x": market}})
        self.assertIsNone(detect.team(self.repo))
        (self.repo / ".claude" / "settings.json").write_text("{not json")
        self.assertIsNone(detect.team(self.repo))

    def test_an_ordinary_plugin_is_never_a_team_plugin(self):
        self.write({"enabledPlugins": {"vercel@claude-plugins-official": True}})
        self.assertIsNone(detect.team(self.repo))
        self.assertEqual(self.found(), {})
        self.write({"enabledPlugins": {"vercel@claude-plugins-official": True},
                    "extraKnownMarketplaces": {"claude-plugins-official": {"source": {"source": "github",
                                                                                      "repo": "a/b"}}}})
        self.assertEqual(detect.team(self.repo), "vercel@claude-plugins-official")
        self.write({"enabledPlugins": {"vercel@claude-plugins-official": True, "team@acme": True},
                    "extraKnownMarketplaces": {"acme": {}}})
        self.assertEqual(detect.team(self.repo), "team@acme")

    def test_plugin_install_path_and_the_doctor_row(self):
        from orche.hr import panes
        reg = registry.empty()
        reg["projects"]["acme"] = {"path": str(self.repo), "team": support.TEAM_PLUGIN}
        self.assertIsNone(panes.plugin_install_path(support.TEAM_PLUGIN))
        self.assertEqual(doctor.team_rows(reg), [("WARN", "team", f"acme: plugin {support.TEAM_PLUGIN} is not "
                                                                  'installed for this user: say "install the team plugin"')])
        support.team_plugin(self.home, scope="project")
        self.assertIsNone(panes.plugin_install_path(support.TEAM_PLUGIN))     # only a user-scope install counts
        folder = support.team_plugin(self.home)
        self.assertEqual(panes.plugin_install_path(support.TEAM_PLUGIN), folder)
        self.assertEqual(doctor.team_rows(reg), [("OK", "team", f"acme: plugin {support.TEAM_PLUGIN} installed, "
                                                                "owner-word mode")])
        del reg["projects"]["acme"]["team"]
        self.assertEqual(doctor.team_rows(reg), [])


class EntireLinkTest(DetectCase):
    def test_worktree_local_setting(self):
        wt = self.home / "wt"
        wt.mkdir()
        self.assertEqual(onboarding.link_entire(self.repo, wt), (None, False))
        self.assertFalse((wt / ".entire").exists())
        (self.repo / ".entire").mkdir()
        (self.repo / ".entire" / "settings.json").write_text('{"enabled": true}\n')
        local = wt / ".entire" / "settings.local.json"
        self.assertEqual(onboarding.link_entire(self.repo, wt), (local, True))
        self.assertEqual(json.loads(local.read_text()), {"commit_linking": "always"})
        self.assertEqual(onboarding.link_entire(self.repo, wt), (local, False))
        local.write_text('{"log_level": "debug", "commit_linking": "prompt"}\n')
        onboarding.link_entire(self.repo, wt)
        self.assertEqual(json.loads(local.read_text()), {"log_level": "debug", "commit_linking": "always"})
        self.assertEqual((self.repo / ".entire" / "settings.json").read_text(), '{"enabled": true}\n')


class DoctorEntireTest(DetectCase):
    def test_entire_row(self):
        reg = registry.empty()
        reg["projects"]["acme"] = {"path": str(self.repo)}
        self.assertEqual(doctor.entire_rows(reg, which=lambda name: None), [])   # no project uses Entire
        (self.repo / ".entire").mkdir()
        (self.repo / ".entire" / "settings.json").write_text("{}\n")
        self.assertEqual(doctor.entire_rows(reg, which=lambda name: None),
                         [("WARN", "entire", "acme uses Entire but the entire command is not on PATH; its commits "
                                         "will not be linked to their sessions")])
        self.assertEqual(doctor.entire_rows(reg, which=lambda name: "/usr/local/bin/entire"),
                         [("OK", "entire", "found; used by acme")])
