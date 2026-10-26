"""publish: a gated unit's verified done pushes its branch and ensures one ready PR to the base
(design §3.2, §5.11; R6, R7, R28, R29). A temp bare repo is `origin`; the fake gh records calls."""
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import support
from support import TempHomeCase

from orche import publish
from orche.roster import Member

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
BUILD = Member("div-acme-build", "acme", "div", "build", "build")
MAIN = Member("main-acme", "acme", "main", "main", None)


class PublishCase(TempHomeCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.home / "w"
        self.bare = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.root.mkdir()
        self.git_at(self.root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git_at(self.root, "init", "-q", "-b", "main", str(self.repo))
        self.commit(self.repo, "a.txt", "a\n")
        for argv in (("remote", "add", "origin", str(self.bare)), ("push", "-q", "origin", "HEAD:refs/heads/main"),
                     ("fetch", "-q", "origin"), ("remote", "set-head", "origin", "main"),
                     ("checkout", "-q", "-b", "div/build")):
            self.git(self.repo, *argv)
        self.gh = self.root / "gh"
        support.fake_gh(self.gh)
        os.environ["PATH"] = str(self.gh) + os.pathsep + os.environ["PATH"]

    def git_at(self, cwd, *argv):
        return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def git(self, folder, *argv):
        return self.git_at(folder, "-C", str(folder), *argv)

    def commit(self, folder, name, text):
        (Path(folder) / name).write_text(text)
        self.git(folder, "add", name)
        self.git(folder, "commit", "-q", "-m", f"edit {name}")
        return self.git(folder, "rev-parse", "HEAD")

    def clone(self, branch):
        other = self.root / f"other-{branch.replace('/', '-')}"
        self.git_at(self.root, "clone", "-q", "-b", branch, str(self.bare), str(other))
        return other

    def run_publish(self, member=BUILD):
        return publish.publish(member, self.repo, self.repo, unit="acme-x-20261024-1", title="Add x", body="body")

    def gh_verbs(self):
        return [c["argv"][:2] for c in support.gh_calls(self.gh)]


class PublishTest(PublishCase):
    def test_done_pushes_and_opens_once_then_only_pushes(self):
        head = self.commit(self.repo, "b.txt", "b\n")
        out = self.run_publish()
        self.assertEqual(out, publish.Published(1, "https://github.com/acme/acme/pull/1", head))
        self.assertEqual(self.git(self.bare, "rev-parse", "div/build"), head)
        [pr] = support.gh_state(self.gh)["prs"]
        self.assertEqual((pr["baseRefName"], pr["headRefName"], pr["title"], pr["body"]),
                         ("main", "div/build", "Add x", "body"))
        head2 = self.commit(self.repo, "c.txt", "c\n")
        self.assertEqual(self.run_publish(), publish.Published(1, "https://github.com/acme/acme/pull/1", head2))
        self.assertEqual(self.git(self.bare, "rev-parse", "div/build"), head2)
        self.assertEqual(self.gh_verbs(), [["pr", "list"], ["pr", "create"], ["pr", "list"]])

    def test_diverged_remote_is_refused_never_forced(self):
        self.commit(self.repo, "b.txt", "b\n")
        self.run_publish()
        other = self.clone("div/build")
        theirs = self.commit(other, "z.txt", "z\n")
        self.git(other, "push", "-q", "origin", "div/build")
        self.commit(self.repo, "c.txt", "c\n")
        out = self.run_publish()
        self.assertEqual(out, publish.Gap(publish.REJECTED.format(branch="div/build")))
        self.assertEqual(self.git(self.bare, "rev-parse", "div/build"), theirs)

    def test_pr_on_another_base_is_a_gap_never_retargeted(self):
        self.commit(self.repo, "b.txt", "b\n")
        support.fake_gh(self.gh, {"prs": [{"number": 4, "url": "u4", "headRefName": "div/build",
                                                   "baseRefName": "release", "isDraft": False}]})
        out = self.run_publish()
        self.assertIsInstance(out, publish.Gap)
        self.assertIn("PR #4 targets release, not main; ORCHE never retargets", out.text)
        self.assertEqual(self.gh_verbs(), [["pr", "list"]])

    def test_draft_pr_is_made_ready(self):
        head = self.commit(self.repo, "b.txt", "b\n")
        support.fake_gh(self.gh, {"prs": [{"number": 5, "url": "u5", "headRefName": "div/build",
                                                   "baseRefName": "main", "isDraft": True}]})
        self.assertEqual(self.run_publish(), publish.Published(5, "u5", head))
        self.assertEqual(self.gh_verbs(), [["pr", "list"], ["pr", "ready"]])
        self.assertFalse(support.gh_state(self.gh)["prs"][0]["isDraft"])

    def test_two_open_prs_is_a_gap(self):
        self.commit(self.repo, "b.txt", "b\n")
        prs = [{"number": n, "url": f"u{n}", "headRefName": "div/build", "baseRefName": "main", "isDraft": False}
               for n in (6, 7)]
        support.fake_gh(self.gh, {"prs": prs})
        out = self.run_publish()
        self.assertEqual(out.text, "two open PRs for div/build: #6, #7; Build decides")

    def test_gh_not_logged_in_is_a_gap_with_owner_step(self):
        self.commit(self.repo, "b.txt", "b\n")
        support.fake_gh(self.gh, {"prs": [], "fail": {
            "pr list": "To get started with GitHub CLI, please run:  gh auth login"}})
        out = self.run_publish()
        self.assertIn("gh pr list: gh is not logged in", out.text)
        self.assertEqual(out.owner, publish.OWNER_AUTH.format(unit="acme-x-20261024-1"))

    def test_protected_branch_is_a_gap_with_owner_step(self):
        self.commit(self.repo, "b.txt", "b\n")
        support.fake_gh(self.gh, {"prs": [], "fail": {
            "pr create": "GraphQL: Resource not accessible by integration (createPullRequest) HTTP 403"}})
        out = self.run_publish()
        self.assertIn("gh pr create:", out.text)
        self.assertEqual(out.owner, publish.OWNER_PERMISSION)

    def test_no_remote_is_a_gap_with_owner_step(self):
        self.commit(self.repo, "b.txt", "b\n")
        self.git(self.repo, "remote", "remove", "origin")
        out = self.run_publish()
        self.assertIn("push failed: no remote origin", out.text)
        self.assertIn("git remote add origin", out.owner)
        self.assertEqual(self.gh_verbs(), [])

    def test_nothing_against_the_base_publishes_nothing(self):
        """R5: a coordination unit's done (no change against origin/main) pushes nothing and opens no PR,
        on Build's own branch and on main alike."""
        self.assertEqual(self.run_publish(), publish.Unchanged("main"))
        self.git(self.repo, "checkout", "-q", "main")
        self.assertEqual(self.run_publish(), publish.Unchanged("main"))
        self.assertEqual(publish.Unchanged("main").note(), "PR: none (no change against origin/main; nothing published)")
        feat = Member("feat-acme-x", "acme", "feat", "x", "build", entry={"branch": "feat/x", "worktree": str(self.repo)})
        self.assertIn("recorded feat/x", self.run_publish(feat).text)    # a feature off its branch is never "unchanged"
        self.assertEqual(self.gh_verbs(), [])
        self.assertNotIn("div/build", self.git(self.bare, "branch", "--list"))

    def test_only_the_units_own_branch_is_published(self):
        """R12: Build on another unit's branch (or on main with a commit), or a feature off its recorded
        branch, is a gap: nothing is pushed into another unit's PR."""
        self.git(self.repo, "checkout", "-q", "-b", "feat/other")
        self.commit(self.repo, "b.txt", "b\n")
        out = self.run_publish()
        self.assertIsInstance(out, publish.Gap)
        self.assertIn("worktree is on feat/other, recorded div/build: ORCHE publishes only the unit's own branch",
                      out.text)
        self.git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "c.txt", "c\n")
        self.assertIn("worktree is on main, recorded div/build", self.run_publish().text)
        feat = Member("feat-acme-x", "acme", "feat", "x", "build",
                      entry={"branch": "feat/x", "worktree": str(self.repo)})
        self.assertIn("recorded feat/x", self.run_publish(feat).text)
        self.assertEqual(self.gh_verbs(), [])
        self.assertEqual(self.git(self.bare, "branch", "--list", "feat/other"), "")

    def test_no_origin_head_is_refused_with_the_owner_step_never_main(self):
        """R12: without origin/HEAD (or publish.base) ORCHE never assumes main."""
        self.commit(self.repo, "b.txt", "b\n")
        self.git(self.repo, "remote", "set-head", "origin", "-d")
        out = self.run_publish()
        self.assertEqual(out.text, "no base branch: origin/HEAD is not set in the project's checkout")
        self.assertIn("remote set-head origin -a (or set publish.base", out.owner)
        self.assertEqual(self.gh_verbs(), [])
        ok = publish.publish(BUILD, self.repo, self.repo, unit="u", title="t", body="b", cfg={"publish": {"base": "main"}})
        self.assertIsInstance(ok, publish.Published)

    def test_only_gated_members_publish(self):
        self.assertTrue(publish.gated(BUILD))
        self.assertTrue(publish.gated(Member("feat-acme-x", "acme", "feat", "x", "build")))
        self.assertFalse(publish.gated(MAIN))
        self.assertFalse(publish.gated(Member("div-acme-rnd", "acme", "div", "rnd", "rnd")))
        self.assertFalse(publish.gated(None))

    def test_body_masks_caps_and_carries_the_footer(self):
        body = publish.body_of("x" * 5000, unit="u", rev=2, session="s", verdict="unit-test-verified",
                               verifier="v")
        self.assertTrue(body.startswith("x" * publish.BODY_MAX + "\n\n---\nORCHE unit `u` rev 2 · writer `s`"))
        self.assertLessEqual(len(publish.title_of("y" * 100)), publish.TITLE_MAX)


class SyncTest(PublishCase):
    def advance_main(self, name, text):
        other = self.clone("main")
        sha = self.commit(other, name, text)
        self.git(other, "push", "-q", "origin", "main")
        return sha

    def test_behind_branch_merges_main(self):
        self.commit(self.repo, "b.txt", "b\n")
        upstream = self.advance_main("m.txt", "m\n")
        self.assertIsNone(publish.sync(self.repo, "main"))
        self.git(self.repo, "merge-base", "--is-ancestor", upstream, "HEAD")

    def test_conflict_aborts_and_returns_the_note(self):
        before = self.commit(self.repo, "a.txt", "mine\n")
        self.advance_main("a.txt", "theirs\n")
        note = publish.sync(self.repo, "main")
        self.assertEqual(note, publish.SYNC_NOTE.format(files="a.txt", base="main"))
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")

    def test_dirty_tree_is_left_alone(self):
        before = self.git(self.repo, "rev-parse", "HEAD")
        self.advance_main("m.txt", "m\n")
        (self.repo / "a.txt").write_text("wip\n")
        self.assertIsNone(publish.sync(self.repo, "main"))
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), before)

    def test_guard_base_resets_a_branch_forked_from_build(self):
        self.commit(self.repo, "b.txt", "build-only\n")
        self.git(self.repo, "checkout", "-q", "-b", "feat/x")
        self.assertTrue(publish.guard_base(self.repo, "main"))
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.git(self.repo, "rev-parse", "origin/main"))
        self.assertFalse(publish.guard_base(self.repo, "main"))

    def test_base_reads_config_then_origin_head_never_a_guess(self):
        self.assertEqual(publish.base(self.repo), "main")
        self.assertEqual(publish.base(self.repo, {"publish": {"base": "trunk"}}), "trunk")
        self.git(self.repo, "remote", "set-head", "origin", "-d")
        self.assertIsNone(publish.base(self.repo))
        self.assertIsNone(publish.sync(self.repo, None))
        self.assertFalse(publish.guard_base(self.repo, None))


class ReviewBTest(PublishCase):
    def test_a_feature_with_no_diff_is_a_gap_and_build_is_unchanged(self):
        """Review B G4: only Build coordinates without a diff; a feature's empty done never files done."""
        self.git(self.repo, "checkout", "-q", "-b", "feat/x")
        feat = Member("feat-acme-x", "acme", "feat", "x", "build", entry={"branch": "feat/x", "worktree": str(self.repo)})
        self.assertEqual(self.run_publish(feat), publish.Gap("nothing to publish: HEAD changes nothing against "
                                                             "origin/main; a feature's done needs its commit"))
        self.git(self.repo, "checkout", "-q", "div/build")
        self.assertEqual(self.run_publish(), publish.Unchanged("main"))
        self.assertEqual(self.gh_verbs(), [])

    def test_changes_fetches_the_base_first_never_trusting_a_stale_origin(self):
        """Review B G8c: HEAD already on origin/main (pushed elsewhere) is no change though the local ref lags."""
        head = self.commit(self.repo, "x.txt", "x\n")
        self.git(self.repo, "push", "-q", str(self.bare), "HEAD:refs/heads/main")   # by URL: origin/main stays stale
        self.assertNotEqual(self.git(self.repo, "rev-parse", "origin/main"), head)
        self.assertFalse(publish.changes(self.repo, "main"))
        self.assertEqual(self.git(self.repo, "rev-parse", "origin/main"), head)


if __name__ == "__main__":
    unittest.main()
