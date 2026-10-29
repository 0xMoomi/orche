"""Onboarding: `orche init --new` makes the repository (git, the first commit, GitHub, Orca) before the setup;
`orche hr choices` asks where the project is when the folder has no repository and recommends a preset from the
repository (detect.stage); the build preset's standing order; the intake skip of an empty repository; the merge
queue's check script (verifygate: in a new repository's first commit, else one PR)."""
import copy
import json
import os
import subprocess
import unittest
from unittest import mock

import support
import test_hr
from orche import cmd_init, config, detect, registry, shell, verifygate
from orche.install import doctor
from orche.hr import choices

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git_out(cwd, *argv) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True).stdout


class NewRepoTest(test_hr.HrCase):
    """The project folder starts empty and unknown to Orca; `orca repo add` makes Orca list it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.repo.resolve()
        self.added = False
        self.gh = self.home / "gh"
        support.fake_gh(self.gh)

    def fake_runner(self, argv, **kw):
        if argv[:3] == ["orca", "repo", "add"]:
            self.added = True
        if argv[:3] == ["orca", "repo", "list"] and not self.added:
            self.calls.append(argv)
            return shell.Result(0, json.dumps({"ok": True, "result": {"repos": []}}), "", argv)
        return super().fake_runner(argv, **kw)

    def init(self, *flags):
        return self.orche("init", str(self.repo), "--new", *flags, "--no-intake")

    def report(self):
        return (self.repo / ".orche" / "SETUP.md").read_text()

    def test_new_private_by_default(self):
        code, out, err = self.orche("init", str(self.repo), "--new", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual(git_out(self.repo, "log", "--format=%s"), "init\n")
        self.assertEqual(git_out(self.repo, "branch", "--show-current"), "main\n")
        self.assertEqual((self.repo / "README.md").read_text(), "# acme-shop\n")
        self.assertEqual([c["argv"] for c in support.gh_calls(self.gh) if c["argv"][:2] == ["repo", "create"]],
                         [["repo", "create", "acme-shop", "--private", "--source", ".", "--push"]])
        self.assertEqual(self.orca_calls("repo add"), [["orca", "repo", "add", "--path", str(self.repo), "--json"]])
        lines = out.splitlines()
        self.assertEqual(lines[:4], [f"git init: {self.repo} (branch main)", "first commit: init",
                                     "GitHub: https://github.com/acme/acme-shop (private)",
                                     f"added {self.repo} to Orca"])
        self.assertIn("- Repository: created here (private, https://github.com/acme/acme-shop).\n", self.report())
        self.assertIn("acme-shop", test_hr.registry.load()["projects"])

    def test_the_first_commit_has_the_check_script_and_the_queue_is_on(self):
        (self.repo / "go.mod").write_text("module acme\n")
        code, _, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertIn("scripts/verify/ci.sh", git_out(self.repo, "show", "--name-only", "--format=", "HEAD"))
        self.assertIn("\ngo test ./...\n", git_out(self.repo, "show", "HEAD:scripts/verify/ci.sh"))
        self.assertTrue(os.access(self.repo / "scripts" / "verify" / "ci.sh", os.X_OK))
        self.assertIn("approved PR; lands it by itself when that passes); this project: on.\n", self.report())
        self.assertFalse([c for c in support.gh_calls(self.gh) if c["argv"][:2] == ["pr", "create"]])

    def test_new_public(self):
        code, _, err = self.init("public")
        self.assertEqual(code, 0, err)
        self.assertIn("--public", support.gh_calls(self.gh)[0]["argv"])
        self.assertIn("- Repository: created here (public, https://github.com/acme/acme-shop).\n", self.report())

    def test_new_local_calls_no_gh(self):
        (self.repo / "app.py").write_text("print(1)\n")   # a folder with files keeps them and gets no README
        code, _, err = self.init("local")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.repo / "README.md").exists())
        self.assertIn("app.py", git_out(self.repo, "ls-files"))
        self.assertFalse([c for c in support.gh_calls(self.gh) if c["argv"][:2] == ["repo", "create"]])
        self.assertIn("- Repository: created here (local, no remote).\n", self.report())
        self.assertNotIn("- GitHub:", self.report())

    def test_gh_failure_goes_on_as_local(self):
        (self.gh / "spec.json").write_text(json.dumps({"fail": {"repo create": "not logged in"}}))
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertIn("GitHub: not created (not logged in); going on without a remote", out)
        report = self.report()
        self.assertIn("- Repository: created here (local, no remote).\n", report)
        self.assertIn('- GitHub: not created (not logged in); say "publish this repository to GitHub"; Main runs gh '
                      "repo create\n", report)
        self.assertTrue(self.orca_calls("repo add"))

    def test_a_folder_with_git_is_refused(self):
        test_hr.git_repo(self.repo)
        code, out, err = self.init()
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.strip().endswith(f"{self.repo} is already a git repository; run orche init without --new"),
                        err)
        self.assertFalse(support.gh_calls(self.gh))
        self.assertFalse(self.orca_calls("repo add"))

    def test_an_existing_project_gets_no_repository_line(self):
        test_hr.git_repo(self.repo)
        self.added = True
        self.assertEqual(self.orche("init", "Acme Shop", "--no-intake")[0], 0)
        self.assertNotIn("- Repository:", self.report())


class WhereTest(support.TempHomeCase):
    def build(self, here):
        return choices.build(None, copy.deepcopy(config.DEFAULTS), None, choices_ok(),
                             {"ram_mb": 65536, "free_disk_mb": 200000}, here=here)

    def test_no_git_asks_where_first(self):
        folder = self.home / "acme"
        folder.mkdir()
        where, gh = self.build(str(folder))["ask"]["where"]
        self.assertEqual(where["tool"]["question"], "Where is the project?")
        self.assertEqual([o["label"] for o in where["tool"]["options"]],
                         ["Create a new repository in this folder (acme) (recommended)",
                          "Use another folder (type its path)"])
        self.assertEqual(list(where["values"].values()), [{"new": True}, {"path": "*"}])
        self.assertEqual(gh["tool"]["header"], "GitHub")
        self.assertEqual(gh["values"], {"Private (recommended)": "private", "Public": "public",
                                        "Local only (no GitHub yet)": "local"})
        self.assertEqual(list(self.build(str(folder))["ask"])[0], "where")

    def test_a_repository_asks_nothing_more(self):
        folder = self.home / "acme"
        test_hr.git_repo(folder)
        (folder / "sub").mkdir()
        for here in (folder, folder / "sub", None):
            self.assertIsNone(self.build(str(here) if here else None)["ask"]["where"])


def commit_all(repo, msg="x"):
    git_out(repo, "add", "-A")
    git_out(repo, "commit", "-q", "-m", msg)


class StageTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        git_out(self.repo, "init", "-q", "-b", "main")

    def files(self, n):
        for i in range(n):
            (self.repo / f"f{i}.txt").write_text(f"{i}\n")
        commit_all(self.repo)

    def test_empty_and_plain_code_are_build(self):
        self.assertEqual((detect.stage(self.repo), detect.tracked(self.repo)), ("build", 0))
        self.files(30)
        self.assertEqual((detect.stage(self.repo), detect.tracked(self.repo)), ("build", 30))

    def test_a_deploy_config_makes_operations(self):
        self.files(30)
        (self.repo / "vercel.json").write_text("{}\n")
        self.assertEqual(detect.stage(self.repo), "operations")
        (self.repo / "vercel.json").unlink()
        (self.repo / "package.json").write_text("{}\n")       # a manifest is code even with few files
        wf = self.repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\njobs:\n  test: {}\n")
        self.assertEqual(detect.stage(self.repo), "build")
        (wf / "release.yml").write_text("on: push\n")
        self.assertEqual(detect.stage(self.repo), "operations")
        self.assertEqual(detect.stage(self.home), "build")       # not a repository: no code

    def test_a_tag_on_origin_makes_operations(self):
        self.files(30)
        bare = self.home / "origin.git"
        git_out(self.home, "init", "-q", "--bare", "-b", "main", str(bare))
        git_out(self.repo, "remote", "add", "origin", str(bare))
        git_out(self.repo, "push", "-q", "origin", "main")
        other = self.home / "other"
        git_out(self.home, "clone", "-q", str(bare), str(other))
        git_out(other, "tag", "v1.0.0")
        git_out(other, "push", "-q", "origin", "v1.0.0")
        self.assertEqual(detect.stage(self.repo), "build")
        git_out(self.repo, "fetch", "-q", "origin")
        self.assertEqual(detect.stage(self.repo), "operations")

    def test_verify_command_from_the_manifest(self):
        root = self.repo
        self.assertIsNone(detect.verify_command(root))
        (root / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "verify": "x"}}))
        self.assertEqual(detect.toolchain(root), ("npm install", "npm run verify"))
        (root / "package-lock.json").write_text("{}")
        self.assertEqual(detect.toolchain(root), ("npm ci", "npm run verify"))
        (root / "pnpm-lock.yaml").write_text("")
        (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
        self.assertEqual(detect.toolchain(root), ("pnpm install --frozen-lockfile", "pnpm test"))
        for name in ("package.json", "package-lock.json", "pnpm-lock.yaml"):
            (root / name).unlink()
        (root / "requirements.txt").write_text("pytest\n")
        self.assertEqual(detect.toolchain(root), ("pip install -r requirements.txt", "python -m pytest"))
        (root / "requirements.txt").unlink()
        (root / "go.mod").write_text("module x\n")
        self.assertEqual(detect.toolchain(root), (None, "go test ./..."))
        text = verifygate.script(root)
        self.assertTrue(text.startswith("#!/bin/sh\n# The merge queue's gate"))
        self.assertIn("\ngo test ./...\n", text)
        self.assertTrue(text.endswith('echo "verdict: unit-test-verified"\n'))
        (root / "go.mod").unlink()
        self.assertTrue(verifygate.script(root).endswith('echo "verdict: type-check-only"\n'))

    def test_choices_recommend_the_preset_of_the_repository(self):
        cfg, machine = copy.deepcopy(config.DEFAULTS), {"ram_mb": 65536, "free_disk_mb": 200000}
        self.files(30)
        data = choices.build(None, cfg, None, choices_ok(), machine, here=str(self.repo))
        setup = data["ask"]["setup"][0]
        self.assertEqual((data["recommended_preset"], data["ask"]["where"]), ("build", None))
        self.assertEqual([o["label"] for o in setup["tool"]["options"]],
                         ["Build-focused (recommended)", "Operations", "Pick them"])
        self.assertEqual([o["label"] for o in data["ask"]["setup"][1]["tool"]["options"]][0],
                         "Fitted to each (recommended)")   # the models question as before
        (self.repo / "Dockerfile").write_text("FROM x\n")
        data = choices.build(None, cfg, None, choices_ok(), machine, here=str(self.repo))
        setup = data["ask"]["setup"][0]
        self.assertEqual([o["label"] for o in setup["tool"]["options"]],
                         ["Operations (recommended)", "Build-focused", "Pick them"])
        self.assertEqual(setup["values"]["Operations (recommended)"]["divisions"],
                         ["build", "design", "rnd", "research", "gtm"])


class PresetInitTest(test_hr.HrCase):
    def setUp(self):
        super().setUp()
        test_hr.git_repo(self.repo)

    def setup_file(self, data):
        path = self.home / "setup.json"
        path.write_text(json.dumps(data))
        return str(path)

    def standing(self):
        return (self.repo / ".orche" / "STANDING.md").read_text()

    def test_the_build_preset_adds_the_specialist_order_once(self):
        f = self.setup_file({"preset": "build"})
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", f, "--no-intake")
        self.assertEqual(code, 0, err)
        proj = registry.load()["projects"]["acme-shop"]
        self.assertEqual((proj["preset"], sorted(proj["divisions"])), ("build", ["build", "design", "research", "rnd"]))
        self.assertEqual(self.standing().count(cmd_init.SPECIALISTS), 1)
        self.assertRegex(self.standing(), r"\n\d+\. Build's workers are specialists: front \(screens")
        report = (self.repo / ".orche" / "SETUP.md").read_text()
        self.assertIn("- Preset: build-focused (Build's workers specialise in front, back and infra).\n", report)
        self.assertEqual(self.orche("init", "Acme Shop", "--setup-file", f, "--no-intake")[0], 0)
        self.assertEqual(self.orche("init", "acme-shop", "--refresh")[0], 0)
        self.assertEqual(self.standing().count(cmd_init.SPECIALISTS), 1)
        self.assertIn("- Preset: build-focused", (self.repo / ".orche" / "SETUP.md").read_text())

    def test_the_operations_preset_appends_nothing(self):
        f = self.setup_file({"preset": "operations", "divisions": ["build", "design", "rnd", "research", "gtm"],
                             "custom": {"design": {}}})
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", f, "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertNotIn("specialists", self.standing())
        self.assertIn("- Preset: operations.\n", (self.repo / ".orche" / "SETUP.md").read_text())
        self.assertIn("gtm", registry.load()["projects"]["acme-shop"]["divisions"])

    def test_no_preset_no_line(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--no-intake")[0], 0)
        self.assertNotIn("- Preset:", (self.repo / ".orche" / "SETUP.md").read_text())
        f = self.setup_file({"preset": "light"})
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", f)
        self.assertEqual(code, 1)
        self.assertIn("setup: preset must be one of build|operations, got 'light'", err)

    def test_an_empty_repository_skips_the_intake(self):
        (self.home / "config.json").write_text(json.dumps(support.no_wake({"claude_bin": str(self.home / "nope")})))
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build")
        self.assertEqual(code, 0, err)
        self.assertIn("intake skipped: the repository is empty; run `orche intake acme-shop` later", out)
        self.assertIn('- Intake: skipped (the repository is empty); say "run the intake" once there is code to read',
                      (self.repo / ".orche" / "SETUP.md").read_text())


class GatePullRequestTest(test_hr.HrCase):
    """An existing repository whose origin/main lacks scripts/verify/ci.sh gets one PR adding it."""

    def setUp(self):
        super().setUp()
        test_hr.git_repo(self.repo)
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
        (self.repo / "pnpm-lock.yaml").write_text("")
        commit_all(self.repo, "app")
        self.bare = self.home / "origin.git"
        git_out(self.home, "init", "-q", "--bare", "-b", "main", str(self.bare))
        git_out(self.repo, "remote", "add", "origin", str(self.bare))
        git_out(self.repo, "push", "-q", "origin", "main")
        git_out(self.repo, "fetch", "-q", "origin")
        git_out(self.repo, "remote", "set-head", "origin", "main")
        self.gh = self.home / "gh"
        support.fake_gh(self.gh)

    def creates(self):
        return [c["argv"] for c in support.gh_calls(self.gh) if c["argv"][:2] == ["pr", "create"]]

    def test_one_pull_request_never_twice(self):
        code, out, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        [create] = self.creates()
        self.assertEqual(create[create.index("--head") + 1], verifygate.BRANCH)
        self.assertEqual(create[create.index("--base") + 1], "main")
        self.assertEqual(create[create.index("--title") + 1], "Add the merge queue's check script")
        [pr] = support.gh_state(self.gh)["prs"]
        self.assertEqual(pr["body"], "Adds scripts/verify/ci.sh, the check ORCHE's merge queue runs on each approved "
                                     "pull request: `pnpm test`.\nThe queue lands pull requests only on the owner's "
                                     "word; merging this one turns it on.\n")
        script = git_out(self.bare, "show", f"{verifygate.BRANCH}:scripts/verify/ci.sh")
        self.assertIn("\npnpm install --frozen-lockfile\npnpm test\n", script)
        self.assertEqual(git_out(self.bare, "log", "-1", "--format=%s", verifygate.BRANCH),
                         "chore(verify): the merge queue's check script\n")
        self.assertEqual(git_out(self.bare, "rev-parse", "main"), git_out(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(git_out(self.repo, "status", "--porcelain"), "")    # the owner's checkout is untouched
        self.assertNotIn("orche-verify-", git_out(self.repo, "worktree", "list"))
        url = "https://github.com/acme/acme/pull/1"
        self.assertIn(f"merge queue: waiting for PR #1 ({url}) to be merged", out)
        report = (self.repo / ".orche" / "SETUP.md").read_text()
        self.assertIn(f"this project: waiting for PR #1 ({url}) to be merged.\n", report)
        self.assertNotIn("- Merge queue for acme-shop", report)                # no "Any time" line while it is open
        self.assertEqual(self.orche("init", "Acme Shop", "--no-intake")[0], 0)
        self.assertEqual(len(self.creates()), 1)
        code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates()), 1)
        self.assertIn(f"merge queue: waiting for PR #1 ({url}) to be merged", out)
        self.assertIn(f"this project: waiting for PR #1 ({url}) to be merged.\n",
                      (self.repo / ".orche" / "SETUP.md").read_text())
        self.assertEqual(registry.load()["projects"]["acme-shop"]["verify_pr"], {"number": 1, "url": url})

    def test_gh_failure_writes_the_waiting_line(self):
        (self.gh / "spec.json").write_text(json.dumps({"fail": "not logged in to any GitHub hosts"}))
        code, _, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.creates())
        report = (self.repo / ".orche" / "SETUP.md").read_text()
        self.assertIn('this project: waiting; the check script could not be pushed (not logged in to any GitHub '
                      'hosts).\n', report)
        self.assertIn('Say "set up the merge queue for acme-shop"; Main runs `orche init --refresh acme-shop`.\n',
                      report)
        self.assertNotIn(verifygate.BRANCH, git_out(self.bare, "branch", "--list"))

    def test_dry_run_opens_nothing(self):
        code, _, err = self.orche("--dry-run", "init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertFalse(support.gh_calls(self.gh))

    def gate_row(self):
        [row] = [r for r in doctor.gate_rows(registry.load(), config.load()) if r[1] == "queue gate"]
        return row

    def test_a_refresh_opens_the_pull_request_once(self):
        """`orche update` runs `init --refresh` per project, so an existing project gets its one PR there."""
        (self.gh / "spec.json").write_text(json.dumps({"fail": "not logged in to any GitHub hosts"}))
        self.assertEqual(self.orche("init", "Acme Shop", "--no-intake")[0], 0)
        self.assertEqual(self.gate_row(), ("WARN", "queue gate", "acme-shop: no check script on origin/main and no "
                                                                 "PR; orche init --refresh acme-shop opens one"))
        (self.gh / "spec.json").write_text(json.dumps({"prs": []}))
        calls = len(support.gh_calls(self.gh))
        code, out, err = self.orche("--dry-run", "init", "acme-shop", "--refresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(support.gh_calls(self.gh)), calls)              # a dry run opens nothing
        self.assertNotIn("merge queue:", out)
        url = "https://github.com/acme/acme/pull/1"
        code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates()), 1)
        self.assertIn(f"merge queue: waiting for PR #1 ({url}) to be merged", out)
        report = (self.repo / ".orche" / "SETUP.md").read_text()
        self.assertIn(f"this project: waiting for PR #1 ({url}) to be merged.\n", report)
        self.assertNotIn("- Merge queue for acme-shop", report)
        self.assertEqual(self.gate_row(), ("WARN", "queue gate", f"acme-shop: waiting for PR #1 ({url})"))
        self.assertEqual(self.orche("init", "acme-shop", "--refresh")[0], 0)
        self.assertEqual(len(self.creates()), 1)                              # the second refresh finds it
        verifygate.write(self.repo)
        commit_all(self.repo, "gate")
        git_out(self.repo, "push", "-q", "origin", "main")
        code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual(code, 0, err)
        self.assertIn("merge queue: on", out)
        self.assertIn("this project: on.\n", (self.repo / ".orche" / "SETUP.md").read_text())
        self.assertEqual(self.gate_row(), ("OK", "queue gate", "acme-shop: scripts/verify/ci.sh on origin/main"))
        self.assertEqual(len(self.creates()), 1)

    def test_a_team_repository_waits_for_the_owner_s_word(self):  # R-TEAM-5
        support.declare_team(self.repo)
        commit_all(self.repo, "team")
        git_out(self.repo, "push", "-q", "origin", "main")
        line = 'gate PR: waits for the owner\'s word (say "set up the merge queue for acme-shop")'
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": "div-acme-shop-build"}):
            code, out, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertIn(line, out)
        self.assertNotIn("merge queue:", out)
        self.assertEqual(self.gate_row(), ("WARN", "queue gate", "acme-shop: no check script on origin/main and no "
                                                                 'PR; say "set up the merge queue for acme-shop" '
                                                                 "(Main opens the PR)"))
        code, out, err = self.orche("init", "acme-shop", "--refresh")   # no session: `orche update`, the dev session
        self.assertEqual(code, 0, err)
        self.assertIn(line, out)
        self.assertFalse(self.creates())
        main = registry.load()["projects"]["acme-shop"]["main_session"]
        with mock.patch.dict(os.environ, {"ORCHE_SESSION": main}):
            code, out, err = self.orche("init", "acme-shop", "--refresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.creates()), 1)
        self.assertIn("merge queue: waiting for PR #1 (https://github.com/acme/acme/pull/1) to be merged", out)

    def test_the_script_on_main_means_on(self):
        verifygate.write(self.repo)
        commit_all(self.repo, "gate")
        git_out(self.repo, "push", "-q", "origin", "main")
        code, out, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertIn("merge queue: on", out)
        self.assertFalse(self.creates())
        self.assertIn("this project: on.\n", (self.repo / ".orche" / "SETUP.md").read_text())


def choices_ok():
    from orche import vendors
    return vendors.Availability("ok", "codex-cli 0.50.0", "")


if __name__ == "__main__":
    import unittest
    unittest.main()


class PushReasonTest(unittest.TestCase):
    """A refused push's reason is the hook's failing line, not its first progress line."""

    def test_reason(self):
        def res(err, code=1):
            return shell.Result(code, "", err, ["git", "push"])
        self.assertEqual(verifygate.reason(res("INF 1 commits scanned.\nWRN leaks found: 1\n\n")),
                         "WRN leaks found: 1")
        self.assertEqual(verifygate.reason(res("INF 1 commits scanned.\nerror: failed to push some refs\n"
                                               "hint: pull first\n")), "error: failed to push some refs")
        self.assertEqual(verifygate.reason(res(" ! [REJECTED] main -> main (fetch first)\nhint: x\n")),
                         "! [REJECTED] main -> main (fetch first)")
        self.assertEqual(verifygate.reason(res("", 128)), "exit 128")
