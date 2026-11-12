"""The CEO Staff (lib/orche/staff.py), one per project: its pane in its own workspace under Main, what it
knows of that project's Main, its gates and its notes."""
import datetime as dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import support
import test_hooks
from orche import errand, inbox, mask, orca, registry, runs, staff, watch
from orche.flow import lineage
from orche.janitor import discover

CANARY = "sk-" + "CANARY" + "abcdefghijklmnop" + "0123456789"
FIXTURE = support.FIXTURES / "transcripts" / "staff_main.jsonl"
GREETING_EN = (
    "Hi, this is acme's Staff, not its Main: a window under Main for your everyday questions.\n"
    "I know everything you've told acme's Main and what it answered, and I catch up again with every question.\n"
    "I don't change anything myself: no code, no pushes, no merges.\n"
    "I run small errands in the background (look-ups, drafts, status checks) and hand project work to Main for "
    "you; the answers come back here.\n"
    "I run at a lighter effort than Main, so ask away, even about small things.")
# The Korean greeting lives only in templates/staff/greeting.ko.txt (Hangul only in such per-language
# text files, HangulTest): pinned by its hash here, its shape below.
GREETING_KO_SHA256 = "fbeb2ce7619811b3f4fa8bcd02e042f967816f5ef9b66937c1c1b7a99924889e"
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
        self.assertIn("Main", ko.splitlines()[0])      # line 1 says plainly: this is Staff, not Main
        self.assertIn("not its Main", GREETING_EN.splitlines()[0])
        for text in (GREETING_EN, ko):
            self.assertLessEqual(len(text.splitlines()), 5)
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

    def test_the_hooks_inject_the_inbox_first_then_the_exchanges(self):
        path = self.main_transcript()
        res = self.staff_hook("session_start.py", "session_start_clear.json", session_id="sess-staff-0001")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("session: staff-acme", res.stdout)
        self.assertIn("owner: what about the docs?", res.stdout)
        self.assertLessEqual(len(res.stdout), 6000)
        res = self.staff_hook("report_inject.py", "prompt.json", session_id="sess-staff-0001")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)    # nothing new, rules already given
        unit = self.root / "acme" / ".orche" / "units" / "acme-x-20261112-1"
        unit.mkdir(parents=True)
        (unit / "REPORT.md").write_text("Two PRs touch the payment page: #12 and #14.\n")
        inbox.write(inbox.make("acme-x-20261112-1", "staff-acme", "staff-acme", "done", via="runner",
                               report_path=str(unit / "REPORT.md"), summary="two PRs"))
        self.append(path, *self.turn("2026-10-30T09:00:00Z", "and the blog?", "Blog after docs.", "m8"))
        res = self.staff_hook("report_inject.py", "prompt.json", session_id="sess-staff-0001")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertLess(out.index("## Inbox (1)"), out.index("#12 and #14"))
        self.assertLess(out.index("#12 and #14"), out.index(staff.OWNER_ITEMS_LINE))
        self.assertLess(out.index(staff.OWNER_ITEMS_LINE), out.index("owner: and the blog?"))
        self.assertNotIn(staff.RULES_HEAD, out)
        self.assertEqual(inbox.pending("staff-acme"), [])
        self.assertEqual(len(self.inbox("staff-acme", drained=True)), 1)
        self.assertIs(lineage.read_turn("staff-acme")["owner"], True)
        self.assertEqual(self.orca_calls(), [])
        res = self.staff_hook("report_inject.py", "prompt.json", session="staff-gone")   # no registered project
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)


class GateTest(StaffCase):
    def decision(self, hook, tool, tool_input, session="staff-acme", **env):
        res = self.run_hook(hook, self.payload("pretool_bash.json", cwd=str(self.ws), tool_name=tool,
                                               tool_input=tool_input), ORCHE_SESSION=session, **env)
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

    def test_every_working_tree_git_word_and_pr_write_is_refused(self):
        for word in ("commit", "push", "checkout", "switch", "reset", "stash", "restore", "clean", "rebase", "merge",
                     "pull", "cherry-pick", "revert", "am", "apply", "rm", "mv", "add", "fetch", "update-ref",
                     "config", "ci", "worktree", "tag", "branch", "remote", "gc", "notes", "replace"):
            self.assertTrue(staff._denied(f"git {word} x", 0), word)
            self.assertTrue(staff._denied(f"git -C /tmp/r -c a=b {word}", 0), word)
        for word in ("merge", "create", "review", "close", "reopen", "ready", "edit", "comment", "lock", "delete"):
            self.assertTrue(staff._denied(f"gh pr {word} 12", 0), word)
        for command in ("git checkout main", "git stash", "git reset --hard", "git add .", "gh pr review 3 -a",
                        "gh pr edit 3 --title x"):
            self.assertEqual(self.decision("env_read_gate.py", "Bash", {"command": command}), staff.DENY, command)
        for command in ("git log --grep merge", "git diff HEAD~1 -- add.py", "git diff main...HEAD",
                        "git branch --merged", "gh pr view 12", "gh pr checks 12", "gh issue view 3",
                        "gh pr view 12 --json state"):
            self.assertFalse(staff._denied(command, 0), command)

    def test_git_and_gh_are_allowlists(self):
        refused = ("git branch -D main", "git update-ref -d refs/heads/main", "git tag -d v1", "git tag v2",
                   "git worktree remove ../x", "git fetch origin main:main", "git -c alias.x=commit x -m y",
                   "git config alias.ci commit", "git ci", "git -C /tmp/r status", "git --git-dir .git log",
                   "git --work-tree . status", "git --exec-path=/x log", "git branch new", "git branch -m a b",
                   "git remote add o u", "git stash", "git stash drop", "git config --unset user.name",
                   "git config --get user.name --add x y", "git log --output=/tmp/x", "git ls-remote --upload-pack=x .",
                   "git grep -Ovim x", "git", "git --version",
                   "gh -R a/b pr merge 1", "gh pr --repo a/b merge 1", "gh api -X PUT repos/a/b/pulls/1/merge",
                   "gh api repos/a/b", "gh release create v9", "gh issue create", "gh repo clone a/b",
                   "gh run rerun 1", "gh auth login", "gh pr comment 3 -b x", "gh issue comment 3 -b x", "gh",
                   "gh alias set co 'pr checkout'", "gh co 3")
        for command in refused:
            self.assertTrue(staff._denied(command, 0), command)
        allowed = ("git status", "git --no-pager log -5", "git -P show HEAD", "git blame x.py", "git ls-files",
                   "git rev-parse HEAD", "git cat-file -p HEAD", "git for-each-ref", "git merge-base a b",
                   "git remote", "git remote -v", "git remote show origin", "git branch", "git branch -a -vv",
                   "git branch --show-current", "git branch --contains abc123", "git branch --list 'feat*'",
                   "git tag", "git tag -l 'v*'", "git stash list", "git worktree list", "git config --get user.name",
                   "git config --list", "git config --global --get-all alias.x",
                   "gh pr list", "gh pr diff 3", "gh pr status", "gh issue list", "gh issue status", "gh repo view",
                   "gh run list", "gh run view 5 --log", "gh release list", "gh release view v1",
                   "gh search prs --author x", "gh auth status", "gh status")
        for command in allowed:
            self.assertFalse(staff._denied(command, 0), command)
        with mock.patch.dict(os.environ, {"ORCHE_RUNNER": "1"}):    # the owner's dictated comment, in an errand
            for command in ("gh pr comment 3 -b x", "gh issue comment 3 -b x"):
                self.assertFalse(staff._denied(command, 0), command)
            self.assertTrue(staff._denied("gh pr merge 3", 0))
        self.assertIn("read-only git and gh commands", staff.DENY)

    def test_an_errand_is_started_from_the_pane_never_under_the_runner(self):
        for command in ('orche errand --goal "list open PRs"', "orche errand acme-x-20261112-1 --stop",
                        "orche --dry-run errand --main --goal x"):
            self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": command}), command)
            self.assertEqual(self.decision("env_read_gate.py", "Bash", {"command": command}, ORCHE_RUNNER="1"),
                             staff.DENY, command)
        self.assertIn("orche errand --main", staff.DENY)

    def test_reads_are_allowed(self):
        for command in ("orche status", "orche status acme", "orche card acme", 'orche ask acme "why?"',
                        "orche metrics", "orche doctor", "orche --json queue acme", "orche hr log",
                        'orche report info --summary "tell Main: x"', "git log --oneline | head",
                        "git status", "cat README.md"):
            self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": command}), command)
            self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": command}, ORCHE_RUNNER="1"),
                              command)
        self.assertIsNone(self.decision("env_read_gate.py", "Read", {"file_path": str(support.ROOT / "README.md")}))

    def test_other_sessions_are_not_touched(self):
        self.assertIsNone(self.decision("env_read_gate.py", "Bash", {"command": "orche dispatch"}, "main-acme"))
        self.assertIsNone(self.decision("scope_gate.py", "Edit", {"file_path": str(self.repo / "x.md")}, "main-acme"))


class ErrandCase(StaffCase):
    def start(self, goal="List open PRs", context="Owner's words: which PRs?", running=True):
        """An errand of acme (brief written, the run not started); `running`: a live run lock for it."""
        with mock.patch.object(runs, "start", return_value=True):
            unit = errand.start(registry.load(), "staff-acme", goal, context)
        if running:
            lock = runs.run_dir("acme") / f"{unit}.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({"pid": os.getpid(), "unit": unit}))
        return unit


class ErrandScopeTest(ErrandCase):
    """The scope gate: an errand (ORCHE_RUNNER) writes only in the unit folder of the unit it runs (its
    ORCHE_RUN_ID, else its target lock), the scratch root and new files in a safe folder its brief names;
    the pane writes nowhere. HOME is the test's temp root, never the real home."""

    def setUp(self):
        super().setUp()
        self.unit = None

    def write(self, path, tool="Write", runner=True, run_id=True, home=None):
        payload = self.payload("pretool_bash.json", cwd=str(self.repo), tool_name=tool,
                               tool_input={"file_path": str(path), "content": "x"})
        env = {"HOME": str(home or self.root)}
        if runner:
            env["ORCHE_RUNNER"] = "1"
        if runner and run_id and self.unit:
            env["ORCHE_RUN_ID"] = f"{self.unit}@1700000000000000000"
        res = self.run_hook("scope_gate.py", payload, ORCHE_SESSION="staff-acme", **env)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if res.stdout else None

    def start(self, *args, **kw):
        self.unit = super().start(*args, **kw)
        return self.unit

    def test_an_errand_writes_in_its_own_unit_folder_only(self):
        unit = self.start()
        folder = self.units_dir() / unit
        self.assertIsNone(self.write(folder / "draft.md"))
        self.assertIsNone(self.write(folder / "sub" / "notes.md", tool="Edit"))
        self.assertIsNone(self.write(self.root / "scratch" / "s1" / "x.md"))
        for name in ("BRIEF.md", "RUN.json", "BRIEF.rev2.md", "PROGRESS.json", "REPORT.md", "REPLY.md", "report.md",
                     "JOINED"):
            self.assertEqual(self.write(folder / name), staff.DENY, name)
        for path in (self.repo / "README", self.repo / "web" / "pay.tsx", self.home / "registry.json",
                     self.root / "acme-build" / "x"):
            self.assertEqual(self.write(path), staff.DENY, path)
        for path in (folder / "draft.md", self.root / "scratch" / "s1" / "x.md"):
            self.assertEqual(self.write(path, runner=False), staff.DENY, path)    # the pane writes nowhere

    def test_never_another_errands_folder(self):
        other = self.start("Another errand", running=False)
        unit = self.start()
        for name in ("REPORT.md", "REPLY.md", "draft.md"):
            self.assertEqual(self.write(self.units_dir() / other / name), staff.DENY, name)
        self.assertIsNone(self.write(self.units_dir() / unit / "draft.md"))

    def test_the_unit_comes_from_the_target_lock_or_nowhere(self):
        unit = self.start()
        draft = self.units_dir() / unit / "draft.md"
        self.assertEqual(self.write(draft, run_id=False), staff.DENY)          # no ORCHE_RUN_ID, no target lock
        lock = runs.target_lock("acme", "staff-acme")
        lock.write_text(json.dumps({"pid": os.getpid(), "unit": unit}))
        self.assertIsNone(self.write(draft, run_id=False))
        lock.write_text(json.dumps({"pid": 999999999, "unit": unit}))           # a dead run names nothing
        self.assertEqual(self.write(draft, run_id=False), staff.DENY)

    def test_new_files_only_in_a_folder_its_brief_names(self):
        out = self.root / "elsewhere" / "exports"
        out.mkdir()
        (out / "old.png").write_text("x")
        self.start(goal="Re-export the logos as PNG", context=f"Owner's words: put them in {out}.")
        self.assertIsNone(self.write(out / "logo.png"))
        self.assertIsNone(self.write(out / "new" / "logo.png"))
        self.assertEqual(self.write(out / "old.png"), staff.DENY)                 # never overwrite
        self.assertEqual(self.write(out / "logo.png", tool="Edit"), staff.DENY)
        self.assertEqual(self.write(out / ".hidden" / "x.png"), staff.DENY)       # no dot folder below it
        self.assertEqual(self.write(self.root / "elsewhere" / "other.png"), staff.DENY)   # not the named folder
        self.assertEqual(self.write(out / "logo.png", runner=False), staff.DENY)

    def test_a_named_folder_counts_only_for_its_own_errand(self):
        out = self.root / "elsewhere"
        named = self.start(goal=f"Draft the notes into {out}", running=False)
        self.start(goal="Something else")
        self.assertEqual(self.write(out / "notes.md"), staff.DENY)
        self.unit = named
        self.assertIsNone(self.write(out / "notes.md"))

    def test_a_folder_that_holds_the_project_is_never_named(self):
        self.start(goal=f"Write the notes into {self.root}")
        self.assertEqual(self.write(self.root / "notes.md"), staff.DENY)

    def test_unsafe_named_folders_are_refused(self):
        patcher = mock.patch.object(errand, "OPEN_MAX", 99)     # one errand per folder below
        patcher.start()
        self.addCleanup(patcher.stop)
        home = self.root
        (home / "Downloads" / "out").mkdir(parents=True)
        beta = home / "work" / "beta"
        beta.mkdir(parents=True)
        registry.update(lambda r: r["projects"].__setitem__("beta", {
            "repo_id": "repo-beta", "path": str(beta), "stage": "building", "aliases": [], "main_session": "main-beta",
            "card": "x", "standing": ".orche/STANDING.md", "divisions": {}}))
        cases = {"~/.claude/skills/x": "SKILL.md", "~/.config/app": "x.conf", "~/.ssh": "id_new",
                 "~/Library/LaunchAgents": "zz.plist", "~/work": "notes.md", f"{beta}/docs": "notes.md",
                 str(self.home): "x.md", str(support.ROOT / "hooks"): "zz.py", "~/Downloads/../.ssh": "id_new",
                 "/tmp/outside-c123f": "x.md"}
        for folder, name in cases.items():
            self.start(goal=f"Write the file into {folder}", running=False)
            path = os.path.join(os.path.realpath(os.path.expanduser(folder.replace("~", str(home), 1))), name)
            self.assertEqual(self.write(path, home=home), staff.DENY, folder)
        self.start(goal="Re-export the logo into ~/Downloads/out", running=False)
        self.assertIsNone(self.write(home / "Downloads" / "out" / "logo.png", home=home))
        self.start(goal="Write the notes into ~", running=False)                # the home itself
        self.assertEqual(self.write(home / "notes.md", home=home), staff.DENY)


class SafeFolderTest(StaffCase):
    """scope_gate.safe_folder, each rule alone, under a temp HOME that holds every place it names."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("scope_gate_c123f", support.ROOT / "hooks" / "scope_gate.py")
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.h = self.root / "home"
        for d in ("Downloads/out", ".claude/skills", "x/.cache/y", "Library/LaunchAgents", "work/beta/docs",
                  "plugin/hooks", "ohome/inbox", "Documents"):
            (self.h / d).mkdir(parents=True)
        (self.h / "Documents" / "link").symlink_to(self.h / ".claude")
        patches = [mock.patch.dict(os.environ, {"HOME": str(self.h), "ORCHE_HOME": str(self.h / "ohome")}),
                   mock.patch("orche.paths.PLUGIN_ROOT", self.h / "plugin")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.reg = {"projects": {"beta": {"path": str(self.h / "work" / "beta")}}}

    def test_each_rule(self):
        h = self.h
        refused = {h: "the home itself", h / ".claude" / "skills": "a dot folder",
                   h / "x" / ".cache" / "y": "dot below", h / "Library" / "LaunchAgents": "Library",
                   h / "work" / "beta" / "docs": "inside a project", h / "work": "holds a project",
                   h / "plugin" / "hooks": "inside the plugin", h / "plugin": "the plugin",
                   h / "ohome" / "inbox": "inside ORCHE_HOME", h / "Documents" / "link": "a symlink to a dot folder",
                   h / "Downloads" / ".." / ".claude": "dot dot", self.root / "elsewhere": "outside the home"}
        for folder, why in refused.items():
            self.assertFalse(self.gate.safe_folder(self.reg, str(folder)), why)
        self.assertTrue(self.gate.safe_folder(self.reg, str(h / "Downloads" / "out")))
        self.assertTrue(self.gate.safe_folder(self.reg, str(h / "Documents")))
        self.assertFalse(self.gate.safe_folder({"projects": {"beta": {"path": str(h / "Downloads")}}},
                                               str(h / "Downloads" / "out")))      # any registered project


class WatchTest(StaffCase):
    """Staff listens on its wake file: SessionStart arms it for the pane's own session, SessionEnd disarms it."""

    def start(self, source="resume", session_id="sess-staff-0001", **env):
        payload = self.payload("session_start_clear.json", cwd=str(self.ws), source=source, session_id=session_id)
        base = {"ORCHE_SESSION": "staff-acme", "CLAUDE_CONFIG_DIR": str(self.claude)}
        res = self.run_hook("session_start.py", payload, **{**base, **env})
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def test_startup_and_resume_arm_the_wake_file(self):
        wake = str(self.home / "inbox" / "staff-acme" / watch.WAKE)
        res = self.start("resume")
        self.assertEqual(res.watch, [wake])
        self.assertIn("session: staff-acme", res.stdout)
        self.assertEqual(watch.marker("staff-acme")["session_id"], "sess-staff-0001")
        res = self.start("startup", session_id="sess-staff-0002")
        out = json.loads(res.raw)
        self.assertEqual(out["hookSpecificOutput"]["watchPaths"], [wake])
        self.assertIn("session: staff-acme", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Staff", out["systemMessage"])
        self.assertEqual(watch.marker("staff-acme")["session_id"], "sess-staff-0002")

    def test_not_unhooked_not_under_the_runner_not_another_session(self):
        bare = self.home / "bare-claude"
        bare.mkdir()
        res = self.start("resume", CLAUDE_CONFIG_DIR=str(bare))
        out = json.loads(res.raw)["hookSpecificOutput"]
        self.assertNotIn("watchPaths", out)
        self.assertIn("session: staff-acme", out["additionalContext"])
        res = self.start("resume", ORCHE_RUNNER="1")
        self.assertEqual(res.raw, "")
        self.assertIsNone(watch.marker("staff-acme"))
        payload = self.payload("session_start_clear.json", cwd=str(self.ws), source="resume")
        res = self.run_hook("session_start.py", payload, CLAUDE_CONFIG_DIR=str(self.claude))   # no ORCHE_SESSION
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIsNone(watch.marker("staff-acme"))

    def test_session_end_disarms_only_its_own_record(self):
        self.start("resume")
        end = self.payload("session_end.json", cwd=str(self.ws), session_id="sess-other-0001")
        self.assertEqual(self.run_hook("session_end.py", end, ORCHE_SESSION="staff-acme").returncode, 0)
        self.assertIsNotNone(watch.marker("staff-acme"))
        end["session_id"] = "sess-staff-0001"
        self.assertEqual(self.run_hook("session_end.py", end, ORCHE_SESSION="staff-acme").returncode, 0)
        self.assertIsNone(watch.marker("staff-acme"))


class PromptTest(StaffCase):
    """The prompt hook in the Staff pane: the turn record, the pending items with Orche's line, the drain."""

    WAKE_PROMPT = "<task>\n[orche] inbox: new reports\n</task>"

    def setUp(self):
        super().setUp()
        staff.rules_given("acme", "sess-staff-0001")

    def prompt(self, prompt, prompt_id, session_id="sess-staff-0001"):
        res = self.staff_hook("report_inject.py", "prompt.json", prompt=prompt, prompt_id=prompt_id,
                              session_id=session_id)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def item(self, unit="acme-x-20261112-1", status="done", summary="the answer", **extra):
        return inbox.write(inbox.make(unit, "staff-acme", "staff-acme", status, via="runner", summary=summary,
                                      **extra))

    def test_a_typed_prompt_is_the_owners_turn(self):
        nudged = self.home / "inbox" / "staff-acme" / inbox.NUDGED
        nudged.parent.mkdir(parents=True, exist_ok=True)
        nudged.write_text("x")
        self.item()
        out = self.prompt("what did the errand find?", "p1")
        self.assertIn("## Inbox (1)", out)
        self.assertIn("summary: the answer", out)
        self.assertTrue(out.rstrip().endswith(staff.OWNER_ITEMS_LINE), out)
        turn = lineage.read_turn("staff-acme")
        self.assertEqual((turn["owner"], turn["lineages"]), (True, []))
        self.assertFalse(nudged.exists())
        self.assertEqual((len(inbox.pending("staff-acme")), len(self.inbox("staff-acme", drained=True))), (0, 1))
        self.assertEqual(self.prompt("thanks", "p2"), "")                 # drained: never shown twice

    def test_the_wake_line_is_orches_turn(self):
        self.item(status="blocked", owner="Which logo? Recommend the blue one; default blue.")
        out = self.prompt(self.WAKE_PROMPT, "p1")
        self.assertIn("OWNER STEP: Which logo?", out)
        self.assertIn(staff.ORCHE_ITEMS_LINE, out)
        self.assertIs(lineage.read_turn("staff-acme")["owner"], False)
        self.prompt("ok, the blue one", "p2")
        self.assertIs(lineage.read_turn("staff-acme")["owner"], True)

    def test_a_wake_delivered_inside_the_running_turn_keeps_its_record(self):
        self.prompt("check the PRs", "p1")
        self.item()
        out = self.prompt(self.WAKE_PROMPT, "p1")       # same prompt id: Claude Code delivered it mid-turn
        self.assertIn(staff.OWNER_ITEMS_LINE, out)
        self.assertIs(lineage.read_turn("staff-acme")["owner"], True)
        self.prompt(self.WAKE_PROMPT, "p4")
        self.prompt("typed while Orche's turn runs", "p4")
        self.assertIs(lineage.read_turn("staff-acme")["owner"], False)

    def test_the_inbox_leaves_room_for_the_exchanges(self):
        path = self.main_transcript()
        staff.start_text(self.reg(), "acme", 6000)
        for i in range(12):
            self.item(unit=f"acme-x-20261112-{i + 1}", summary="\n".join(["z" * 300] * 5))
        self.append(path, *self.turn("2026-10-30T09:00:00Z", "and the blog?", "Blog after docs.", "m8"))
        out = self.prompt("news?", "p1")
        self.assertLessEqual(len(out), inbox.BUDGET)
        self.assertIn("owner: and the blog?", out)
        self.assertIn("left pending for size", out)
        self.assertLess(out.index("## Inbox"), out.index("owner: and the blog?"))
        self.assertTrue(inbox.pending("staff-acme"))       # the rest waits for the next prompt

    def test_an_item_larger_than_the_room_is_cut_never_stuck(self):
        self.item(summary="\n".join(["w" * 3000] * 5))
        out = staff.prompt_text(self.reg(), "acme", inbox.BUDGET, {"session_id": "sess-staff-0001", "prompt": "x"})
        self.assertLessEqual(len(out), inbox.BUDGET)
        self.assertIn(staff.CUT_LINE, out)
        self.assertTrue(out.endswith(staff.OWNER_ITEMS_LINE))
        self.assertEqual(inbox.pending("staff-acme"), [])


class RulesTest(StaffCase):
    """A Staff session that was given other rules (a pane started before an update) gets the current ones once."""

    def payload_of(self, session_id, prompt="hi", prompt_id="p1"):
        return {"session_id": session_id, "prompt": prompt, "prompt_id": prompt_id}

    def test_an_old_session_gets_the_rules_once(self):
        text = staff.prompt_text(self.reg(), "acme", inbox.BUDGET, self.payload_of("sess-old-0001"))
        self.assertTrue(text.startswith(staff.RULES_HEAD + "\n# CEO Staff"), text[:200])
        self.assertIn("orche errand --main", text)
        self.assertEqual(staff.prompt_text(self.reg(), "acme", inbox.BUDGET,
                                           self.payload_of("sess-old-0001", "x", "p2")), "")
        seen = json.loads((self.home / "inbox" / "staff-acme" / staff.RULES).read_text())
        self.assertEqual(seen, {"sess-old-0001": staff.rules_hash()})

    def test_a_session_started_on_this_version_gets_nothing_extra(self):
        res = self.staff_hook("session_start.py", "session_start_clear.json", session_id="sess-new-0001")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(staff.prompt_text(self.reg(), "acme", inbox.BUDGET, self.payload_of("sess-new-0001")), "")
        res = self.staff_hook("report_inject.py", "prompt.json", session_id="sess-new-0001")
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_changed_rules_reach_a_running_session_first(self):
        path = self.home / "inbox" / "staff-acme" / staff.RULES
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sess-a-0001": "0" * 12}))
        inbox.write(inbox.make("acme-x-20261112-1", "staff-acme", "staff-acme", "done", via="runner",
                               summary="the answer"))
        self.main_transcript()
        out = staff.prompt_text(self.reg(), "acme", inbox.BUDGET, self.payload_of("sess-a-0001"))
        self.assertLessEqual(len(out), inbox.BUDGET)
        self.assertLess(out.index(staff.RULES_HEAD), out.index("## Inbox (1)"))
        self.assertLess(out.index("## Inbox (1)"), out.index("## Owner and Main"))
        for n in range(staff.RULES_KEEP + 5):
            staff.rules_given("acme", f"sess-many-{n:04d}")
        self.assertEqual(len(json.loads(path.read_text())), staff.RULES_KEEP)


class NoteTest(ErrandCase):
    def report(self, *argv, session="staff-acme"):
        return subprocess.run([sys.executable, str(support.ROOT / "bin" / "orche.py"), "report", *argv],
                              env=child_env(self.home, self.claude, session), cwd=str(self.ws),
                              capture_output=True, text=True, timeout=30)

    def test_a_note_is_quiet_and_shown_at_mains_next_prompt(self):
        (self.home / "config.json").write_text(json.dumps({"inbox": {"wake": True, "quiet_s": 0}}))
        res = self.report("info", "--summary", f"FYI: the owner prefers blue ({CANARY})")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("note left for main-acme", res.stdout)
        [path] = self.inbox("main-acme")
        ptr = json.loads(path.read_text())
        self.assertEqual((ptr["from"], ptr["status"], ptr["urgent"], ptr["quiet"], ptr["via"]),
                         ("staff-acme", "info", False, True, "staff"))
        self.assertTrue(ptr["unit"].startswith("adhoc-staff-acme-"))
        self.assertNotIn(CANARY, ptr["summary"])
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
        self.assertIn("hand it over: `orche errand --main --goal ...`",
                      self.report("info", "--urgent", "--summary", "x").stderr)
        res = self.report("info", "--summary", "x", session="staff-gone")
        self.assertEqual(res.returncode, 1)
        for session in ("main-acme", "div-acme-build", "main-beta"):
            self.assertEqual(self.inbox(session), [], session)

    def test_a_report_on_an_errand_unit_is_refused_for_everyone(self):
        unit = self.start(running=False)
        for session in ("main-acme", "div-acme-build"):
            res = self.report("info", "--unit", unit, "--summary", "x", session=session)
            self.assertEqual(res.returncode, 1, (session, res.stderr))
            self.assertIn(f"{unit} is the owner's errand: your reply in this turn reaches Staff by itself",
                          res.stderr)
        self.assertEqual(inbox.pending("staff-acme"), [])
        self.assertEqual(self.inbox("main-acme"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
