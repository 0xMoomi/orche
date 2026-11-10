"""The CEO Staff (lib/orche/staff.py), one per project: its pane in its own workspace under Main, what it
knows of that project's Main, its gates and its notes."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import support
import test_hooks
from orche import inbox, mask, orca, registry, staff
from orche.janitor import discover

CANARY = "sk-" + "CANARY" + "abcdefghijklmnop" + "0123456789"
FIXTURE = support.FIXTURES / "transcripts" / "staff_main.jsonl"
GREETING_EN = (
    "Hi, I'm the Staff of acme: a window under its Main for your everyday questions.\n"
    "I know everything you've told acme's Main and what it answered, and I catch up again with every question.\n"
    "I don't do the work myself: no code, no pushes, no merges. Work always goes through Main.\n"
    'To hand something over, say "tell Main: ..." and I\'ll leave acme\'s Main a note.\n'
    "I run at a lighter effort than Main, so ask away, even about small things.")
# The Korean greeting lives only in templates/staff/greeting.ko.txt (Hangul only in such per-language
# text files, HangulTest): pinned by its hash here, its shape below.
GREETING_KO_SHA256 = "af3b84870bc8a5b7e580e29c2570a108f266e742bc8a1be5f92e191264ce7537"
KO_PROMPT = "\uc624\ub298 \ubc30\ud3ec \uc0c1\ud669 \uc54c\ub824 \uc918"   # a Korean owner prompt


def child_env(home, claude, session):
    """The test's own environment minus Orca/Orche/Claude names, pointed at the temp home and fake Orca."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "ORCHE_", "CLAUDE"))}
    env.update({"ORCHE_HOME": str(home), "ORCHE_FAKE_ORCA": "1", "CLAUDE_CONFIG_DIR": str(claude)})
    if session:
        env["ORCHE_SESSION"] = session
    return env


class StaffCase(test_hooks.HookCase):
    """The hooks' temp project `acme` (main-acme in <root>/acme, repo `repo-acme`) with Main's transcript in
    its Claude dir; its Staff's workspace, when Orca has one, is <root>/staff."""

    def setUp(self):
        super().setUp()
        self.claude = self.home / "claude"
        self.folder = self.claude / "projects" / discover.claude_slug(str(self.repo))
        self.folder.mkdir(parents=True)
        self.ws = self.root / "staff"
        self.ws.mkdir()
        self.ws_id = f"repo-acme::{self.ws}"

    def main_transcript(self, text=None, name="s1.jsonl", folder=None):
        path = (folder or self.folder) / name
        path.write_text(text if text is not None else FIXTURE.read_text().replace("@CANARY_KEY@", CANARY))
        return path

    def append(self, path, *records):
        with open(path, "a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in records))

    def turn(self, ts, prompt, reply, mid):
        return [{"type": "user", "timestamp": ts, "message": {"role": "user", "content": prompt}},
                {"type": "assistant", "timestamp": ts,
                 "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": reply}]}}]

    def named(self, session, *turns):
        return (json.dumps({"type": "custom-title", "customTitle": session}) + "\n"
                + "".join(json.dumps(r) + "\n" for t in turns for r in t))

    def staff_hook(self, hook, fixture, session="staff-acme", **over):
        return self.run_hook(hook, self.payload(fixture, cwd=str(self.ws), **over), ORCHE_SESSION=session,
                             CLAUDE_CONFIG_DIR=str(self.claude))

    def reg(self):
        return registry.load()

    def add_beta(self):
        """A second project `beta` (main-beta in <root>/beta) with its own Main transcript."""
        beta = self.root / "beta"
        beta.mkdir()
        registry.update(lambda r: r["projects"].__setitem__("beta", {
            "repo_id": "repo-beta", "path": str(beta), "stage": "building", "aliases": [],
            "main_session": "main-beta", "card": "x", "standing": ".orche/STANDING.md", "divisions": {}}))
        folder = self.claude / "projects" / discover.claude_slug(str(beta))
        folder.mkdir(parents=True)
        self.main_transcript(self.named("main-beta", self.turn("2026-10-29T08:00:00Z", "beta question",
                                                               "beta answer", "b1")), folder=folder)


class PaneTest(StaffCase):
    def fake(self, listed=False, terminals=()):
        """Orca with the Staff workspace listed (else created on demand) and `terminals` open."""
        item = {"id": self.ws_id, "repoId": "repo-acme", "path": str(self.ws), "displayName": "staff",
                "branch": "staff"}
        self.fake_orca({"worktree list": {"worktrees": [item] if listed else []},
                        "worktree create": {"worktree": item},
                        "terminal list": {"terminals": list(terminals)},
                        "terminal read": {"terminal": {"status": "running"}}})

    def orche(self, *argv, session="main-acme"):
        return subprocess.run([sys.executable, str(support.ROOT / "bin" / "orche.py"), *argv],
                              env=child_env(self.home, self.claude, session), capture_output=True, text=True,
                              timeout=30)

    def calls(self, group_sub):
        return [c[1:] for c in self.orca_calls() if " ".join(c[1:3]) == group_sub]

    def creates(self):
        return [c[c.index("--title") + 1] for c in self.calls("terminal create")]

    def test_one_session_per_project_named_like_main_and_the_divisions(self):
        reg = self.reg()
        self.assertEqual(staff.session_of("acme"), "staff-acme")
        for session, ok in (("staff-acme", True), ("staff-gone", True), ("staff", False), ("staff-", False),
                            ("main-acme", False), ("div-acme-build", False), (None, False)):
            self.assertIs(staff.is_staff(session), ok, session)
        self.assertEqual(staff.project_of(reg, "staff-acme"), "acme")
        for session in ("staff-gone", "staff", "main-acme", None):
            self.assertIsNone(staff.project_of(reg, session), session)

    def test_the_pane_command_pins_opus_medium_with_the_pane_env(self):
        cmd = staff.pane_command("staff-acme", str(self.ws))
        self.assertIn("ORCHE_SESSION=staff-acme ", cmd)
        self.assertIn("CLAUDE_CODE_PROMPT_CACHE_TTL=1h ", cmd)
        self.assertNotIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", cmd)
        self.assertTrue(cmd.endswith("claude --permission-mode bypassPermissions --model claude-opus-5-5 "
                                     "--effort medium --name staff-acme"), cmd)
        mine = self.claude / "projects" / discover.claude_slug(str(self.ws))
        mine.mkdir(parents=True)
        (mine / "abc123.jsonl").write_text(json.dumps({"type": "custom-title", "customTitle": "staff-acme"}) + "\n")
        self.assertTrue(staff.pane_command("staff-acme", str(self.ws)).endswith("--effort medium --resume abc123"))

    def test_on_opens_its_own_workspace_under_main_and_off_closes_it(self):
        self.fake()
        res = self.orche("wake", "staff-acme")
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "staff-acme pane opened"), res.stderr)
        [create] = self.calls("worktree create")      # created the first time, a child of Main's worktree
        self.assertEqual(create, ["worktree", "create", "--repo", "id:repo-acme", "--name", "staff",
                                  "--parent-worktree", f"id:repo-acme::{self.repo}", "--json"])
        [term] = self.calls("terminal create")        # its own terminal in that workspace, never Main's tab
        self.assertEqual(term[term.index("--worktree") + 1], f"id:{self.ws_id}")
        self.assertEqual(term[term.index("--title") + 1], "staff-acme")
        self.assertIn("ORCHE_SESSION=staff-acme ", term[term.index("--command") + 1])
        self.assertIn("--model claude-opus-5-5 --effort medium", term[term.index("--command") + 1])
        self.assertEqual(self.reg()["projects"]["acme"]["staff"], {"status": "awake"})
        self.fake(listed=True, terminals=[{"handle": "t-staff", "title": "\u2733 staff-acme"}])
        self.assertEqual(self.orche("wake", "staff-acme").stdout.strip(), "staff-acme pane switched to")
        self.assertEqual(self.orche("sleep", "staff-acme").stdout.strip(), "staff-acme pane closed")
        self.assertIn(["terminal", "close", "--terminal", "t-staff", "--json"], self.calls("terminal close"))
        self.assertEqual(self.reg()["projects"]["acme"]["staff"], {"status": "asleep"})
        self.fake(listed=True)
        self.assertEqual(self.orche("sleep", "staff-acme").stdout.strip(), "staff-acme pane is not open")
        self.assertEqual(len(self.calls("worktree create")), 1)

    def test_an_existing_workspace_is_reused(self):
        self.fake(listed=True)
        self.assertEqual(self.orche("wake", "staff-acme").stdout.strip(), "staff-acme pane opened")
        self.assertEqual(self.calls("worktree create"), [])
        [term] = self.calls("terminal create")
        self.assertEqual(term[term.index("--worktree") + 1], f"id:{self.ws_id}")

    def test_a_failed_listing_creates_nothing(self):
        """Never a second Staff workspace: when Orca cannot list the worktrees, nothing is created."""
        failed = orca.OrcaError("orca is down", "bad_output")
        with mock.patch.object(orca, "find_terminal_by_title", return_value=None), \
                mock.patch.object(orca, "worktree", side_effect=failed) as worktree, \
                mock.patch.object(orca, "terminal") as terminal:
            with self.assertRaises(orca.OrcaError):
                staff.open_pane("acme", self.reg()["projects"]["acme"])
        self.assertEqual([c.args[0] for c in worktree.call_args_list], ["list"])
        terminal.assert_not_called()

    def test_bare_staff_names_the_callers_projects_staff(self):
        self.fake(listed=True)
        self.assertEqual(self.orche("wake", "staff").stdout.strip(), "staff-acme pane opened")   # from main-acme
        res = self.orche("wake", "staff", session=None)
        self.assertEqual(res.returncode, 1)
        self.assertIn("staff-<project>", res.stderr)
        res = self.orche("sleep", "staff-gone")
        self.assertEqual(res.returncode, 2)
        self.assertIn("no project 'gone'", res.stderr)


class AutoOpenTest(PaneTest):
    """A project's Staff opens by default with every Main pane Orche opens for it, once, and stays off once
    put to sleep."""

    def test_opening_main_opens_its_staff_first_once(self):
        self.fake()
        res = self.orche("wake", "main-acme")
        self.assertEqual((res.returncode, res.stdout.splitlines()),
                         (0, ["main-acme pane opened", "staff-acme pane opened"]), res.stderr)
        self.assertEqual(self.creates(), ["staff-acme", "main-acme"])    # Staff first: Main ends in front
        staff_term, main_term = self.calls("terminal create")
        self.assertEqual(staff_term[staff_term.index("--worktree") + 1], f"id:{self.ws_id}")
        self.assertEqual(main_term[main_term.index("--worktree") + 1], f"id:repo-acme::{self.repo}")
        self.fake(listed=True, terminals=[{"handle": "t-staff", "title": "staff-acme"}])
        res = self.orche("wake", "main-acme")
        self.assertEqual(res.stdout.splitlines(), ["main-acme pane opened"])
        self.assertEqual(self.creates(), ["staff-acme", "main-acme", "main-acme"])   # never a second Staff
        self.assertEqual(len(self.calls("worktree create")), 1)
        self.assertEqual(self.calls("terminal switch"), [])

    def test_asleep_stays_off_until_woken_in_words(self):
        self.fake(listed=True)
        self.assertEqual(self.orche("sleep", "staff-acme").stdout.strip(), "staff-acme pane is not open")
        self.assertEqual(self.reg()["projects"]["acme"]["staff"], {"status": "asleep"})
        self.assertEqual(self.orche("wake", "main-acme").stdout.splitlines(), ["main-acme pane opened"])
        self.assertEqual(self.creates(), ["main-acme"])
        self.assertEqual(self.orche("wake", "staff-acme").stdout.strip(), "staff-acme pane opened")
        self.assertEqual(self.reg()["projects"]["acme"]["staff"], {"status": "awake"})
        self.assertEqual(self.creates(), ["main-acme", "staff-acme"])

    def test_a_dry_run_prints_what_it_would_open(self):
        self.fake()
        res = self.orche("wake", "main-acme", "--dry-run")
        self.assertEqual(res.stdout.splitlines(), ["would open main-acme pane (dry-run)",
                                                   "would open staff-acme pane (dry-run)"], res.stderr)
        self.assertIn("[dry-run] orca worktree create --repo id:repo-acme --name staff --parent-worktree "
                      f"id:repo-acme::{self.repo} --json", res.stderr)
        self.assertEqual(self.creates(), [])
        self.orche("sleep", "staff-acme", "--dry-run")
        self.assertNotIn("staff", self.reg()["projects"]["acme"])          # a dry run records nothing

    def test_auto_open_never_raises_and_answers_once_per_project(self):
        proj = self.reg()["projects"]["acme"]
        line = staff.auto_open("acme", proj)   # in process the PATH's failing `orca` answers: Orca is down
        self.assertTrue(line.startswith("staff-acme pane not opened ("), line)
        self.assertIs(staff.auto_open("acme", proj), line)
        self.assertIsNone(staff.auto_open("other", {**proj, "staff": {"status": "asleep"}}))

    def test_install_and_update_open_staff_only_beside_an_open_main(self):
        """`staff.auto_open_all` (orche install, orche update): the Staff of each project whose Main pane is
        open, never one for a project without an open Main, never an asleep one."""
        self.add_beta()
        self.fake(listed=True, terminals=[{"handle": "t-main", "title": "main-acme"}])
        with mock.patch.dict(os.environ, {"ORCHE_FAKE_ORCA": "1"}):
            self.assertEqual(staff.auto_open_all(dry_run=True), ["would open staff-acme pane (dry-run)"])
            self.assertEqual(self.creates(), [])
            staff._AUTO.clear()
            self.assertEqual(staff.auto_open_all(), ["staff-acme pane opened"])
            self.assertEqual(self.creates(), ["staff-acme"])                # none for beta: its Main is not open
            staff._AUTO.clear()
            staff.set_status("acme", "asleep")
            self.assertEqual(staff.auto_open_all(), [])
        self.assertEqual(self.creates(), ["staff-acme"])

    def test_the_registry_entry_is_checked_and_the_old_shared_one_ignored(self):
        reg = self.reg()
        acme = reg["projects"]["acme"]
        with_staff = lambda value: {**reg, "projects": {"acme": {**acme, "staff": value}}}
        self.assertEqual(registry.validate(with_staff({"status": "asleep"})), [])
        self.assertTrue(registry.validate(with_staff({"status": "gone"})))
        self.assertTrue(registry.validate(with_staff("asleep")))
        old = {**reg, "staff": {"status": "asleep"}}   # the shared Staff's record of 0.19.x: kept, ignored
        self.assertEqual(registry.validate(old), [])
        registry.save(old)
        self.assertEqual(registry.load()["staff"], {"status": "asleep"})
        self.assertFalse(staff.asleep(registry.load()["projects"]["acme"]))


class GreetingTest(StaffCase):
    def prompts(self, *texts):
        base = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
        self.main_transcript(self.named("main-acme", *(self.turn((base + dt.timedelta(minutes=i)).isoformat(), t,
                                                                 "ok", f"g{i}") for i, t in enumerate(texts))))

    def test_both_texts_are_pinned_short_and_plain(self):
        self.assertEqual(staff.greeting("acme", "en"), GREETING_EN)
        raw = Path(staff.GREETING.format(language="ko")).read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), GREETING_KO_SHA256)
        ko = staff.greeting("acme", "ko")
        self.assertTrue(staff.is_korean(ko))
        self.assertIn("acme", ko)
        self.assertIn('"Main', ko)
        for text in (GREETING_EN, ko):
            self.assertLessEqual(len(text.splitlines()), 6)
            self.assertNotRegex(text, "[\u2013\u2014]")
            self.assertNotIn("{project}", text)
            self.assertIn("Main", text)

    def test_the_language_follows_the_owners_prompts(self):
        self.assertTrue(staff.is_korean(KO_PROMPT + " PIKIT build PR"))
        self.assertFalse(staff.is_korean("check the build PR for " + KO_PROMPT[:2]))
        rows = lambda *ps: [{"prompt": p} for p in ps] + [{"prompt": None}]
        self.assertEqual(staff.language(rows(KO_PROMPT, KO_PROMPT, "ok ship it")), "ko")
        self.assertEqual(staff.language(rows(KO_PROMPT, "ship it", "and the docs")), "en")
        self.assertFalse((self.home / support.DEFAULTS_CALLS).exists())   # prompts decide: no locale read

    def test_no_prompts_fall_back_to_the_macos_locale(self):
        self.assertEqual(staff.language([{"prompt": None}]), "en")         # no locale set: English
        (self.home / "fake-locale").write_text("ko_KR\n")
        self.assertEqual(staff.language([]), "ko")
        self.assertEqual((self.home / support.DEFAULTS_CALLS).read_text().splitlines()[-1], "read -g AppleLocale")
        (self.home / "fake-locale").write_text("en_KR\n")
        self.assertEqual(staff.language([]), "en")

    def test_a_fresh_session_shows_the_greeting_a_resume_does_not(self):
        self.prompts(KO_PROMPT, KO_PROMPT)
        res = self.staff_hook("session_start.py", "session_start_clear.json", source="startup")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["systemMessage"], staff.greeting("acme", "ko"))
        context = out["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "SessionStart")
        self.assertIn("session: staff-acme\nproject: acme", context["additionalContext"])
        self.assertIn(KO_PROMPT, context["additionalContext"])
        for source in ("resume", "compact"):
            res = self.staff_hook("session_start.py", "session_start_clear.json", source=source)
            self.assertIn("session: staff-acme", res.stdout)
            self.assertNotIn("systemMessage", res.stdout)

    def test_an_english_owner_gets_english(self):
        self.prompts("ship the pricing page", "what about the docs?")
        res = self.staff_hook("session_start.py", "session_start_clear.json", source="startup")
        self.assertEqual(json.loads(res.stdout)["systemMessage"], GREETING_EN)


class HangulTest(StaffCase):
    def test_only_the_korean_text_files_carry_hangul(self):
        """Hangul only in the per-language text files (`templates/**/*.ko.*`): never in code, tests or docs."""
        tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=support.ROOT, capture_output=True, text=True,
                                 check=True).stdout.split("\0")
        found = []
        for rel in filter(None, tracked):
            try:
                text = (support.ROOT / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(staff._hangul(c) for c in text):
                found.append(rel)
        self.assertIn("templates/staff/greeting.ko.txt", found)
        self.assertEqual([rel for rel in found if not (rel.startswith("templates/") and ".ko." in rel.rsplit("/", 1)[-1])],
                         [])


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
        self.main_transcript(self.named("staff-acme", self.turn("2026-10-29T09:00:00Z", "mine", "not Main", "m9")),
                             "s2.jsonl")
        self.assertEqual([r["prompt"] for r in staff.recent(self.reg(), "acme")],
                         ["what about the docs?", None, "ship the pricing page"])

    def test_session_start_fits_the_cap_newest_first(self):
        path = self.main_transcript()
        base = dt.datetime(2026, 10, 29, 6, tzinfo=dt.timezone.utc)
        for i in range(staff.PER_PROJECT + 3):
            self.append(path, *self.turn((base + dt.timedelta(minutes=i)).isoformat(), f"q{i} " + "x" * 600,
                                         f"a{i} " + "y" * 900, f"m{i}"))
        text = staff.start_text(self.reg(), "acme", 6000)
        self.assertLessEqual(len(text), 6000)
        self.assertIn("CEO Staff", text)
        self.assertIn(f"session: staff-acme\nproject: acme\nmain checkout: {self.repo}\n", text)
        self.assertIn("## Project\nacme: needs you 0 · running 0 · finished 0", text)
        self.assertIn(staff.TRIMMED, text)
        newest = f"q{staff.PER_PROJECT + 2} "
        self.assertIn(newest, text)
        self.assertNotIn("ship the pricing page", text)        # older than the newest PER_PROJECT
        self.assertLess(text.index(newest), text.index(f"q{staff.PER_PROJECT + 1} "))

    def test_each_prompt_brings_only_what_is_new(self):
        path = self.main_transcript()
        self.assertIn("what about the docs?", staff.start_text(self.reg(), "acme", 6000))
        self.assertEqual(staff.prompt_text(self.reg(), "acme", inbox.BUDGET), "")
        self.append(path, {"type": "assistant", "timestamp": "2026-10-29T05:00:30Z",
                           "message": {"id": "m5", "role": "assistant", "content": [{"type": "text",
                                                                                     "text": "Blog after docs."}]}})
        text = staff.prompt_text(self.reg(), "acme", inbox.BUDGET)
        self.assertIn("owner: and the blog?", text)
        self.assertIn("main: Blog after docs.", text)
        self.assertNotIn("docs?", text.replace("and the blog?", ""))
        self.assertEqual(staff.prompt_text(self.reg(), "acme", inbox.BUDGET), "")
        cursor = json.loads((self.home / "inbox" / "staff-acme" / staff.CURSOR).read_text())
        self.assertEqual(list(cursor), ["at"])

    def test_the_prompt_budget_holds(self):
        path = self.main_transcript()
        staff.start_text(self.reg(), "acme", 6000)
        base = dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc)
        for i in range(30):
            self.append(path, *self.turn((base + dt.timedelta(minutes=i)).isoformat(), "p" * 700, "r" * 700, f"n{i}"))
        text = staff.prompt_text(self.reg(), "acme", inbox.BUDGET)
        self.assertLessEqual(len(text), inbox.BUDGET)
        self.assertTrue(text.endswith(staff.TRIMMED))
        self.assertEqual(staff.prompt_text(self.reg(), "acme", inbox.BUDGET), "")

    def test_a_staff_knows_only_its_own_projects_main(self):
        self.main_transcript()
        self.add_beta()
        acme, beta = staff.start_text(self.reg(), "acme", 6000), staff.start_text(self.reg(), "beta", 6000)
        self.assertIn("what about the docs?", acme)
        self.assertNotIn("beta", acme)
        self.assertIn("owner: beta question", beta)
        self.assertIn("## Project\nbeta: ", beta)
        for gone in ("what about the docs?", "acme"):
            self.assertNotIn(gone, beta)
        self.assertTrue((self.home / "inbox" / "staff-acme" / staff.CURSOR).is_file())   # one cursor per Staff
        self.assertTrue((self.home / "inbox" / "staff-beta" / staff.CURSOR).is_file())
        self.append(self.folder / "s1.jsonl", *self.turn("2026-10-30T09:00:00Z", "acme later", "ok", "m7"))
        self.assertEqual(staff.prompt_text(self.reg(), "beta", inbox.BUDGET), "")
        self.assertIn("owner: acme later", staff.prompt_text(self.reg(), "acme", inbox.BUDGET))

    def test_the_hooks_inject_it_and_skip_the_inbox(self):
        self.main_transcript()
        res = self.staff_hook("session_start.py", "session_start_clear.json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("session: staff-acme", res.stdout)
        self.assertIn("owner: what about the docs?", res.stdout)
        self.assertLessEqual(len(res.stdout), 6001)
        res = self.staff_hook("report_inject.py", "prompt.json")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertFalse((self.home / "sessions").exists())        # no standing orders, no inbox turn
        self.assertEqual(self.orca_calls(), [])
        res = self.staff_hook("report_inject.py", "prompt.json", session="staff-gone")   # no registered project
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)


class GateTest(StaffCase):
    def decision(self, hook, tool, tool_input, session="staff-acme"):
        res = self.run_hook(hook, self.payload("pretool_bash.json", cwd=str(self.ws), tool_name=tool,
                                               tool_input=tool_input), ORCHE_SESSION=session)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if res.stdout else None

    def test_writes_are_refused(self):
        for session in ("staff-acme", "staff-gone"):   # any Staff, its project still registered or not
            for command in ("orche dispatch < brief.txt", "orche queue approve acme --all", "orche queue add u1",
                            "orche hr add acme design", "orche hr audit --apply", "orche report done --summary x",
                            "orche wake main-acme", "orche sleep staff-acme", "git commit -m x",
                            "cd x && git push origin HEAD", "gh pr merge 12", "gh pr create --fill",
                            'bash -c "orche dispatch"', "python3 bin/orche.py dispatch",
                            "env FOO=1 orche --json dispatch"):
                self.assertEqual(self.decision("env_read_gate.py", "Bash", {"command": command}, session), staff.DENY,
                                 (session, command))
            for tool in ("Edit", "Write", "NotebookEdit"):
                self.assertEqual(self.decision("scope_gate.py", tool, {"file_path": "/tmp/x.md",
                                                                       "notebook_path": "/tmp/x.ipynb"}, session),
                                 staff.DENY, (session, tool))

    def test_reads_are_allowed(self):
        for command in ("orche status", "orche status acme", "orche card acme", 'orche ask acme "why?"',
                        "orche metrics", "orche doctor", "orche --json queue acme", "orche hr log",
                        'orche report info --summary "tell Main: x"', "git log --oneline | head",
                        "cat README.md"):
            self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": command}), command)
        self.assertIsNone(self.decision("env_read_gate.py", "Read", {"file_path": str(support.ROOT / "README.md")}))

    def test_other_sessions_are_not_touched(self):
        self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": "orche dispatch"}, "main-acme"))
        self.assertIsNone(self.decision("scope_gate.py", "Edit", {"file_path": str(self.repo / "x.md")}, "main-acme"))


class NoteTest(StaffCase):
    def report(self, *argv, session="staff-acme"):
        return subprocess.run([sys.executable, str(support.ROOT / "bin" / "orche.py"), "report", *argv],
                              env=child_env(self.home, self.claude, session), cwd=str(self.ws),
                              capture_output=True, text=True, timeout=30)

    def test_tell_main_leaves_a_held_note_that_does_not_wake(self):
        (self.home / "config.json").write_text(json.dumps({"inbox": {"wake": True, "quiet_s": 0}}))
        res = self.report("info", "--summary", f"tell Main: ship it ({CANARY})")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("note left for main-acme", res.stdout)
        [path] = self.inbox("main-acme")
        ptr = json.loads(path.read_text())
        self.assertEqual((ptr["from"], ptr["status"], ptr["urgent"]), ("staff-acme", "info", False))
        self.assertTrue(ptr["unit"].startswith("adhoc-staff-acme-"))
        self.assertNotIn(CANARY, ptr["summary"])
        self.assertTrue(inbox.held(ptr))
        self.assertFalse(inbox.wakes(ptr))
        self.assertFalse((self.home / "logs" / "nudge.log").exists())
        self.assertEqual(self.report("info", "--to", "shop", "--summary", "x").returncode, 0)   # its own, by alias
        self.assertEqual(len(self.inbox("main-acme")), 2)

    def test_only_info_notes_to_its_own_projects_main(self):
        self.add_beta()
        for argv in (("done", "--summary", "x"), ("info", "--urgent", "--summary", "x"),
                     ("info", "--to", "div-acme-build", "--summary", "x"), ("info", "--to", "beta", "--summary", "x"),
                     ("info", "--to", "main-beta", "--summary", "x")):
            res = self.report(*argv)
            self.assertEqual(res.returncode, 1, argv)
        res = self.report("info", "--summary", "x", session="staff-gone")
        self.assertEqual(res.returncode, 1)
        for session in ("main-acme", "div-acme-build", "main-beta"):
            self.assertEqual(self.inbox(session), [], session)


if __name__ == "__main__":
    import unittest
    unittest.main()
