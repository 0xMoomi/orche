"""A woken pane resumes its conversation by session id, not by `--resume <name>` (0.6.1).

Claude Code records `--name <name>` as `custom-title` / `agent-name` lines in the transcript; the
session id is the transcript's file stem. Transcripts here are fakes under the temp home."""
import contextlib
import io
import json
import os
import time
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)
from orche import inbox
from orche.hr import panes
from orche.janitor import discover

ID_OLD = "11111111-1111-4111-8111-111111111111"
ID_NEW = "22222222-2222-4222-8222-222222222222"
ID_OTHER = "33333333-3333-4333-8333-333333333333"


class WakeResumeTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cwd = self.home / "work" / "proj.x"
        self.cwd.mkdir(parents=True)
        self.folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / discover.claude_slug(
            discover.real(str(self.cwd)))

    def transcript(self, sid, *records, age=0):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{sid}.jsonl"
        lines = [{"type": "user", "sessionId": sid, "message": {"content": "hi"}}, *records]
        path.write_text("".join(json.dumps(r) + "\n" for r in lines))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_config_dir_is_inside_the_temp_home(self):
        self.assertTrue(str(panes.claude_projects()).startswith(str(self.home)))

    def test_newest_named_transcript_wins(self):
        self.transcript(ID_OLD, {"type": "custom-title", "customTitle": "u-1", "sessionId": ID_OLD}, age=60)
        self.transcript(ID_NEW, {"type": "custom-title", "customTitle": "u-1", "sessionId": ID_NEW}, age=10)
        self.transcript(ID_OTHER, {"type": "custom-title", "customTitle": "u-2"}, age=0)
        self.assertEqual(panes.resume_flag("u-1", self.cwd), f"--resume {ID_NEW}")

    def test_agent_name_record_matches(self):
        self.transcript(ID_OLD, {"type": "agent-name", "agentName": "u-1", "sessionId": ID_OLD})
        self.assertEqual(panes.resume_flag("u-1", self.cwd), f"--resume {ID_OLD}")

    def test_no_named_transcript_starts_a_named_fresh_session_with_a_note(self):
        self.transcript(ID_OTHER, {"type": "custom-title", "customTitle": "u-2"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(panes.resume_flag("u-1", self.cwd), "--name u-1")
        self.assertEqual(err.getvalue(), "orche: no transcript named u-1; the pane starts a fresh session\n")

    def test_garbage_lines_are_skipped(self):
        self.folder.mkdir(parents=True)
        (self.folder / f"{ID_OLD}.jsonl").write_text('{"type":"custom-title", broken\n'
                                                     + json.dumps({"type": "custom-title", "customTitle": "u-1"}) + "\n")
        self.assertEqual(panes.resume_flag("u-1", self.cwd), f"--resume {ID_OLD}")

    def test_missing_project_dir_starts_a_named_fresh_session(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(panes.resume_flag("u-1", self.cwd), "--name u-1")
            self.assertEqual(panes.resume_flag("u-1", None), "--name u-1")

    def test_pane_command_keeps_env_prefix_and_bypass(self):
        self.transcript(ID_NEW, {"type": "custom-title", "customTitle": "u-1"})
        cmd = panes.pane_command("u-1", resume=True, cwd=self.cwd)
        self.assertTrue(cmd.startswith(panes.session_env("u-1")), cmd)
        self.assertTrue(cmd.endswith(f"--permission-mode bypassPermissions --resume {ID_NEW}"), cmd)
        fresh = panes.pane_command("u-1", resume=False, cwd=self.cwd)
        self.assertTrue(fresh.endswith("--permission-mode bypassPermissions --name u-1"), fresh)


class MainResumesTest(support.TempHomeCase):
    """Main's pane resumes its newest `main-<p>` conversation in the main checkout; onboarding starts fresh."""

    def setUp(self):
        super().setUp()
        self.root = self.home / "repo" / "acme"
        self.root.mkdir(parents=True)
        self.proj = {"main_session": "main-acme", "repo_id": "r1", "path": str(self.root)}
        folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / discover.claude_slug(
            discover.real(str(self.root)))
        folder.mkdir(parents=True)
        (folder / f"{ID_NEW}.jsonl").write_text(json.dumps({"type": "custom-title", "customTitle": "main-acme"}) + "\n")
        self.opened = []
        for name, fn in (("find_terminal_by_title", lambda title: None),):
            p = mock.patch.object(panes.orca, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(panes, "open_pane", lambda wt, title, command, dry_run=False: self.opened.append(command) or {"argv": []})
        p.start()
        self.addCleanup(p.stop)

    def test_pane_command_carries_no_autocompact_window(self):
        """c63: `--autocompact 200000` with the installer's 50 % compacted every pane at about 100K."""
        for resume in (True, False):
            with contextlib.redirect_stderr(io.StringIO()):
                command = panes.pane_command("u-1", resume=resume)
            self.assertIn(" claude --permission-mode bypassPermissions --", command)
            self.assertNotIn("--autocompact", command)

    def test_project_pane_resumes_by_default_and_posts_the_resumed_note(self):
        data, _ = panes.project_pane(self.proj, dry_run=False, switch=False)
        self.assertEqual(data["pane"], "opened")
        self.assertTrue(self.opened[0].endswith(f"bypassPermissions --resume {ID_NEW}"), self.opened[0])
        got = [(p["from"], p["to"], p["status"], p["urgent"], p["summary"]) for _f, p in inbox.pending("main-acme")]
        self.assertEqual(got, [("orche", "main-acme", "info", True, panes.RESUMED)])
        self.assertTrue(panes.RESUMED.startswith("resumed: "))

    def test_project_pane_fresh_posts_nothing(self):
        panes.project_pane(self.proj, dry_run=False, switch=False, resume=False)
        self.assertTrue(self.opened[0].endswith("--name main-acme"), self.opened[0])
        self.assertEqual(inbox.pending("main-acme"), [])

    def test_dry_run_posts_nothing(self):
        data, _ = panes.project_pane(self.proj, dry_run=True, switch=False)
        self.assertEqual(inbox.pending("main-acme"), [])

    def test_onboarding_opens_main_fresh(self):
        import inspect
        from orche.hr import create
        self.assertIn("panes.project_pane(proj, dry_run=dry_run, switch=False, resume=False)",
                      inspect.getsource(create.onboard_project))
