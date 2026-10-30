"""The CEO Staff (lib/orche/staff.py): its pane, what it knows of every Main, its gates and its notes."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import support
import test_hooks
from orche import inbox, mask, paths, staff
from orche.hr import panes
from orche.janitor import discover

CANARY = "sk-" + "CANARY" + "abcdefghijklmnop" + "0123456789"
FIXTURE = support.FIXTURES / "transcripts" / "staff_main.jsonl"
ROOT = os.path.realpath(str(paths.PLUGIN_ROOT))


class StaffCase(test_hooks.HookCase):
    """The hooks' temp project `acme` (main-acme in <root>/acme) with Main's transcript in its Claude dir."""

    def setUp(self):
        super().setUp()
        self.claude = self.home / "claude"
        self.folder = self.claude / "projects" / discover.claude_slug(str(self.repo))
        self.folder.mkdir(parents=True)

    def main_transcript(self, text=None, name="s1.jsonl"):
        path = self.folder / name
        path.write_text(text if text is not None else FIXTURE.read_text().replace("@CANARY_KEY@", CANARY))
        return path

    def append(self, path, *records):
        with open(path, "a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in records))

    def turn(self, ts, prompt, reply, mid):
        return [{"type": "user", "timestamp": ts, "message": {"role": "user", "content": prompt}},
                {"type": "assistant", "timestamp": ts,
                 "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": reply}]}}]

    def staff_hook(self, hook, fixture, **over):
        return self.run_hook(hook, self.payload(fixture, cwd=ROOT, **over), ORCHE_SESSION="staff",
                             CLAUDE_CONFIG_DIR=str(self.claude))

    def reg(self):
        from orche import registry
        return registry.load()


class PaneTest(StaffCase):
    def fake_repos(self, listed=True, terminals=()):
        repos = [{"id": "repo-orche", "path": ROOT}] if listed else []
        self.fake_orca({"repo list": {"repos": repos}, "terminal list": {"terminals": list(terminals)},
                        "terminal read": {"terminal": {"status": "running"}}})

    def orche(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "ORCHE_", "CLAUDE"))}
        env.update({"ORCHE_HOME": str(self.home), "ORCHE_FAKE_ORCA": "1", "CLAUDE_CONFIG_DIR": str(self.claude),
                    "ORCHE_SESSION": "main-acme"})
        return subprocess.run([sys.executable, str(support.ROOT / "bin" / "orche.py"), *argv], env=env,
                              capture_output=True, text=True, timeout=30)

    def test_the_pane_command_pins_opus_medium_with_the_pane_env(self):
        cmd = staff.pane_command(ROOT)
        self.assertIn("ORCHE_SESSION=staff ", cmd)
        self.assertIn("CLAUDE_CODE_PROMPT_CACHE_TTL=1h ", cmd)
        self.assertNotIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", cmd)
        self.assertTrue(cmd.endswith("claude --permission-mode bypassPermissions --model claude-opus-5-5 "
                                     "--effort medium --name staff"), cmd)
        mine = self.claude / "projects" / discover.claude_slug(ROOT)
        mine.mkdir(parents=True)
        (mine / "abc123.jsonl").write_text(json.dumps({"type": "custom-title", "customTitle": "staff"}) + "\n")
        self.assertTrue(staff.pane_command(ROOT).endswith("--effort medium --resume abc123"))

    def test_on_opens_in_the_orche_checkout_and_off_closes_it(self):
        self.fake_repos()
        res = self.orche("wake", "staff")
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "staff pane opened"), res.stderr)
        [create] = [c for c in self.orca_calls() if c[1:3] == ["terminal", "create"]]
        self.assertEqual(create[create.index("--worktree") + 1], f"id:repo-orche::{ROOT}")
        self.assertEqual(create[create.index("--title") + 1], "staff")
        self.assertIn("--model claude-opus-5-5 --effort medium", create[create.index("--command") + 1])
        self.fake_repos(terminals=[{"handle": "t-staff", "title": "✳ staff"}])
        self.assertEqual(self.orche("wake", "staff").stdout.strip(), "staff pane switched to")
        self.assertEqual(self.orche("sleep", "staff").stdout.strip(), "staff pane closed")
        self.assertIn(["terminal", "close", "--terminal", "t-staff", "--json"], [c[1:] for c in self.orca_calls()])
        self.fake_repos()
        self.assertEqual(self.orche("sleep", "staff").stdout.strip(), "staff pane is not open")

    def test_orca_learns_the_orche_checkout_once(self):
        self.fake_repos(listed=False)
        res = self.orche("wake", "staff")
        self.assertEqual(res.returncode, 1)                  # the fake list never shows it: refused, named
        self.assertIn("not in orca repo list", res.stderr)
        self.assertIn(["repo", "add", "--path", ROOT, "--json"], [c[1:] for c in self.orca_calls()])
        self.assertFalse(any(c[1:3] == ["terminal", "create"] for c in self.orca_calls()))


class KnowledgeTest(StaffCase):
    def test_only_owner_prompts_and_final_replies_masked(self):
        rows = staff.exchanges(self.main_transcript())
        self.assertEqual([(r["at"].strftime("%H:%M"), r["prompt"]) for r in rows],
                         [("01:00", "ship the pricing page"), ("02:00", None), ("03:00", "what about the docs?")])
        self.assertEqual(rows[2]["reply"], "Docs are next.\nBuild starts them at 4.")
        self.assertEqual(rows[1]["reply"], "Pricing landed: PR #12 merged.")
        text = "\n".join(staff.render(dict(r, project="acme")) for r in rows)
        self.assertNotIn(CANARY, text)
        self.assertIn(mask.redact(CANARY)[0], text)
        for gone in ("Looking at it", "TOOL OUTPUT", "noise", "ctx", "meta line", "subagent", "[orche]",
                     "task-notification", "and the blog"):
            self.assertNotIn(gone, text)
        self.assertIn("- acme ", text)

    def test_other_sessions_in_the_checkout_are_not_main(self):
        self.main_transcript()
        self.main_transcript(json.dumps({"type": "custom-title", "customTitle": "staff"}) + "\n"
                             + "".join(json.dumps(r) + "\n" for r in self.turn("2026-10-29T09:00:00Z", "mine",
                                                                                 "not Main", "m9")), "s2.jsonl")
        self.assertEqual([r["prompt"] for r in staff.recent(self.reg(), "acme")],
                         ["what about the docs?", None, "ship the pricing page"])

    def test_session_start_fits_the_cap_newest_first(self):
        path = self.main_transcript()
        base = dt.datetime(2026, 10, 29, 6, tzinfo=dt.timezone.utc)
        for i in range(staff.PER_PROJECT + 3):
            self.append(path, *self.turn((base + dt.timedelta(minutes=i)).isoformat(), f"q{i} " + "x" * 600,
                                         f"a{i} " + "y" * 900, f"m{i}"))
        text = staff.start_text(self.reg(), 6000)
        self.assertLessEqual(len(text), 6000)
        self.assertIn("CEO Staff", text)
        self.assertIn("## Projects\nacme: needs you 0 · running 0 · finished 0", text)
        self.assertIn(staff.TRIMMED, text)
        newest = f"q{staff.PER_PROJECT + 2} "
        self.assertIn(newest, text)
        self.assertNotIn("ship the pricing page", text)        # older than the newest PER_PROJECT
        self.assertLess(text.index(newest), text.index(f"q{staff.PER_PROJECT + 1} "))

    def test_each_prompt_brings_only_what_is_new(self):
        path = self.main_transcript()
        self.assertIn("what about the docs?", staff.start_text(self.reg(), 6000))
        self.assertEqual(staff.prompt_text(self.reg(), inbox.BUDGET), "")
        self.append(path, {"type": "assistant", "timestamp": "2026-10-29T05:00:30Z",
                           "message": {"id": "m5", "role": "assistant", "content": [{"type": "text",
                                                                                     "text": "Blog after docs."}]}})
        text = staff.prompt_text(self.reg(), inbox.BUDGET)
        self.assertIn("owner: and the blog?", text)
        self.assertIn("main: Blog after docs.", text)
        self.assertNotIn("docs?", text.replace("and the blog?", ""))
        self.assertEqual(staff.prompt_text(self.reg(), inbox.BUDGET), "")

    def test_the_prompt_budget_holds(self):
        path = self.main_transcript()
        staff.start_text(self.reg(), 6000)
        base = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
        for i in range(30):
            self.append(path, *self.turn((base + dt.timedelta(minutes=i)).isoformat(), "p" * 700, "r" * 700, f"n{i}"))
        text = staff.prompt_text(self.reg(), inbox.BUDGET)
        self.assertLessEqual(len(text), inbox.BUDGET)
        self.assertTrue(text.endswith(staff.TRIMMED))
        self.assertEqual(staff.prompt_text(self.reg(), inbox.BUDGET), "")

    def test_the_hooks_inject_it_and_skip_the_inbox(self):
        self.main_transcript()
        res = self.staff_hook("session_start.py", "session_start_clear.json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("session: staff", res.stdout)
        self.assertIn("owner: what about the docs?", res.stdout)
        self.assertLessEqual(len(res.stdout), 6001)
        res = self.staff_hook("report_inject.py", "prompt.json")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertFalse((self.home / "sessions").exists())        # no standing orders, no inbox turn
        self.assertEqual(self.orca_calls(), [])


class GateTest(StaffCase):
    def decision(self, hook, tool, tool_input, session="staff"):
        res = self.run_hook(hook, self.payload("pretool_bash.json", cwd=ROOT, tool_name=tool, tool_input=tool_input),
                            ORCHE_SESSION=session)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if res.stdout else None

    def test_writes_are_refused(self):
        for command in ("orche dispatch < brief.txt", "orche queue approve acme --all", "orche queue add u1",
                        "orche hr add acme design", "orche hr audit --apply", "orche report done --summary x",
                        "orche wake main-acme", "git commit -m x", "cd x && git push origin HEAD",
                        "gh pr merge 12", "gh pr create --fill", 'bash -c "orche dispatch"',
                        "python3 bin/orche.py dispatch", "env FOO=1 orche --json dispatch"):
            self.assertEqual(self.decision("env_read_gate.py", "Bash", {"command": command}), staff.DENY, command)
        for tool in ("Edit", "Write", "NotebookEdit"):
            self.assertEqual(self.decision("scope_gate.py", tool, {"file_path": "/tmp/x.md",
                                                                   "notebook_path": "/tmp/x.ipynb"}),
                             staff.DENY, tool)

    def test_reads_are_allowed(self):
        for command in ("orche status", "orche status acme", "orche card acme", 'orche ask acme "why?"',
                        "orche metrics", "orche doctor", "orche --json queue acme", "orche hr log",
                        'orche report info --to acme --summary "tell Main: x"', "git log --oneline | head",
                        "cat README.md"):
            self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": command}), command)
        self.assertIsNone(self.decision("env_read_gate.py", "Read", {"file_path": str(support.ROOT / "README.md")}))

    def test_other_sessions_are_not_touched(self):
        self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": "orche dispatch"}, "main-acme"))
        self.assertIsNone(self.decision("scope_gate.py", "Edit", {"file_path": str(self.repo / "x.md")}, "main-acme"))


class NoteTest(StaffCase):
    def report(self, *argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "ORCHE_", "CLAUDE"))}
        env.update({"ORCHE_HOME": str(self.home), "ORCHE_SESSION": "staff"})
        return subprocess.run([sys.executable, str(support.ROOT / "bin" / "orche.py"), "report", *argv],
                              env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_tell_main_leaves_a_held_note_that_does_not_wake(self):
        (self.home / "config.json").write_text(json.dumps({"inbox": {"wake": True, "quiet_s": 0}}))
        res = self.report("info", "--to", "shop", "--summary", f"tell Main: ship it ({CANARY})")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("note left for main-acme", res.stdout)
        [path] = self.inbox("main-acme")
        ptr = json.loads(path.read_text())
        self.assertEqual((ptr["from"], ptr["status"], ptr["urgent"]), ("staff", "info", False))
        self.assertTrue(ptr["unit"].startswith("adhoc-staff-"))
        self.assertNotIn(CANARY, ptr["summary"])
        self.assertTrue(inbox.held(ptr))
        self.assertFalse(inbox.wakes(ptr))
        self.assertFalse((self.home / "logs" / "nudge.log").exists())

    def test_only_info_notes_to_a_main(self):
        for argv in (("done", "--to", "acme", "--summary", "x"), ("info", "--to", "acme", "--urgent", "--summary", "x"),
                     ("info", "--to", "div-acme-build", "--summary", "x"), ("info", "--summary", "x")):
            res = self.report(*argv)
            self.assertEqual(res.returncode, 1, argv)
        self.assertEqual(self.inbox("main-acme"), [])
        self.assertEqual(self.inbox("div-acme-build"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
