"""A woken pane resumes its conversation by session id, not by `--resume <name>` (0.6.1).

Claude Code records `--name <name>` as `custom-title` / `agent-name` lines in the transcript; the
session id is the transcript's file stem. Transcripts here are fakes under the temp home."""
import contextlib
import io
import json
import os
import time
from pathlib import Path

import support  # noqa: F401  (puts lib/ on sys.path)
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
