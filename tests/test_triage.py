"""Triage gate (lib/orche/triage.py): tags, bypasses, heuristics, classifier, decision rule, action, records.

Offline: the classifier is a stub, a fake `claude` (support.write_fake_claude, via config `claude_bin`)
or a fake Jev opener; the runner spawn is monkeypatched or runs the fake claude end to end.
"""
import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import support
from orche import brief, config, dispatch, inbox, jev, paths, procs, registry, roster, triage, units
from test_runner import REAL_POPEN, SPAWN, RunnerCase, alive

SECRET = "sk-" + "b" * 30
DIRECT = {"route": "direct", "division": "build", "tier": "mechanical", "confidence": 0.95,
          "goal": "Add an install section to README.md", "accept": "README.md has an Install section",
          "verify": "grep -q Install README.md", "scope": ["README.md"]}


def stub(result, backend="claude-haiku", secs=0.0, calls=None):
    def classifier(prompt, divisions, cfg):
        if calls is not None:
            calls.append((prompt, list(divisions)))
        return (triage.normalize(result, divisions) if result else None), backend, secs, None
    return classifier


def haiku_reply(result, cost=0.0012, is_error=False):
    inner = result if isinstance(result, str) else json.dumps(result)
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "result": inner,
                       "total_cost_usd": cost})


class TriageCase(RunnerCase):
    """Registry acme (main-acme; divisions rnd + build with sessions), runner delivery, fake claude."""

    def setUp(self):
        super().setUp()
        os.environ["ORCHE_FAKE_ORCA"] = "1"
        self.spawned = []
        self.enable()

    def enable(self, **over):
        cfg = {"claude_bin": str(self.claude), "delivery": "runner",
               "triage": {"enabled": True, "backend": "claude-haiku", **over}}
        self.config(cfg)
        return cfg

    def cfg(self):
        return config.load()

    def decide(self, prompt, classifier=None, transcript=None, divisions=("rnd", "build")):
        return triage.decide(prompt, list(divisions), self.cfg(), transcript, classifier or stub(None))

    def handle(self, prompt, classifier=None, popen=None, session="main-acme", transcript=None):
        def fake_popen(argv, **kw):
            if argv[:3] != SPAWN:
                return REAL_POPEN(argv, **kw)
            self.spawned.append((argv, kw))
            if popen is not None:
                return popen(argv, **kw)
            return mock.Mock(pid=4242)
        payload = {"session_id": "sess-main-0001", "prompt": prompt, "cwd": str(self.repo),
                   "transcript_path": transcript or str(self.root / "none.jsonl")}
        env = {"ORCHE_SESSION": session} if session else {}
        with mock.patch.dict(os.environ, env), mock.patch.object(procs.subprocess, "Popen", fake_popen), \
                mock.patch.object(triage, "classify", classifier or stub(None)):
            r = roster.Roster(registry.load())
            return triage.handle(payload, time.monotonic(), r, r.identify(payload["cwd"]))

    def samples(self):
        path = self.home / "triage" / "samples.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assistant_transcript(self, text, name="main.jsonl", pad=0):
        lines = [{"type": "user", "message": {"role": "user", "content": "x" * pad}}] if pad else []
        lines += [{"type": "user", "message": {"role": "user", "content": "go"}},
                  {"type": "assistant", "message": {"id": "m1", "role": "assistant",
                                                    "content": [{"type": "text", "text": text}]}},
                  {"type": "assistant", "message": {"id": "m1", "role": "assistant",
                                                    "content": [{"type": "tool_use", "name": "Read"}]}}]
        path = self.root / name
        path.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))
        return str(path)


class TagTest(TriageCase):
    def test_parse_tag_strips_the_first_token_case_insensitively(self):
        self.assertEqual(triage.parse_tag("[MAIN] add a.md"), ("main", "add a.md"))
        self.assertEqual(triage.parse_tag("  [Build]   fix lib/x.py"), ("build", "fix lib/x.py"))
        self.assertEqual(triage.parse_tag("[drain]"), ("drain", ""))
        self.assertEqual(triage.parse_tag("/clear"), ("bypass", "/clear"))
        self.assertEqual(triage.parse_tag("!ls -la"), ("bypass", "!ls -la"))
        self.assertEqual(triage.parse_tag("[foo] add a.md"), (None, "[foo] add a.md"))   # unknown tag
        self.assertEqual(triage.parse_tag("[build]"), ("build", ""))

    def test_a_tag_is_the_whole_first_token(self):
        """M1: `[build]s are failing` is no tag; it goes through the usual stages."""
        for p in ("[build]s are failing again, see ci.yml", "[main]tain README.md", "[build]: fix a.md"):
            self.assertEqual(triage.parse_tag(p), (None, p), p)
        calls = []
        d = self.decide("[build]s are failing again, see ci.yml", stub(DIRECT, calls=calls))
        self.assertEqual((d.decision, d.stage), ("main", 3))
        self.assertEqual(calls, [])

    def test_main_tag_and_bypass_tags(self):
        calls = []
        self.assertEqual(self.decide("[main] add a section to README.md", stub(DIRECT, calls=calls)).decision, "tag")
        for p in ("[drain]", "/orche-status", "!git status"):
            d = self.decide(p, stub(DIRECT, calls=calls))
            self.assertEqual((d.decision, d.stage), ("bypass", 1), p)
        self.assertEqual(calls, [])

    def test_division_tag_with_a_path_or_imperative_needs_no_classifier(self):
        calls = []
        for p in ("[build] lib/x.py header comment", "[build] 헤더 정리"):
            d = self.decide(p, stub(DIRECT, calls=calls))
            self.assertEqual((d.decision, d.stage, d.division, d.tier, d.backend), ("direct", 1, "build", "standard",
                                                                                   "none"), p)
        self.assertEqual(calls, [])

    def test_division_tag_without_target_asks_the_classifier_for_tier_only(self):
        calls = []
        d = self.decide("[rnd] the onboarding numbers from last week", stub(DIRECT, calls=calls))
        self.assertEqual((d.decision, d.division, d.tier), ("direct", "rnd", "mechanical"))   # tag wins division
        self.assertEqual(len(calls), 1)
        hard = dict(DIRECT, tier="hard")
        self.assertEqual(self.decide("[rnd] the onboarding numbers", stub(hard)).tier, "standard")
        low = dict(DIRECT, route="main", confidence=0.1)
        self.assertEqual(self.decide("[rnd] the onboarding numbers", stub(low)).decision, "direct")  # tags force

    def test_direct_tag_takes_the_classifier_division_else_build(self):
        d = self.decide("[direct] the onboarding numbers", stub(dict(DIRECT, division="rnd")))
        self.assertEqual((d.decision, d.division), ("direct", "rnd"))
        d = self.decide("[direct] the onboarding numbers", stub(None))
        self.assertEqual((d.decision, d.division, d.tier), ("direct", "build", "standard"))
        self.assertEqual(self.decide("[direct]").decision, "bypass")      # nothing left to route


class BypassTest(TriageCase):
    def test_short_and_question_prompts(self):
        for p, why in (("fix a.md", "short"), ("what changed in docs/a.md", "question"),
                       ("Should I add tests to a.py", "question"), ("README.md 수정한 거 맞아?", "question"),
                       ("README.md 수정할까요", "question"), ("왜 README.md 를 수정했지", "question"),
                       ("어떻게 README.md 정리하지", "question"), ("Add a section to README.md？", "question")):
            d = self.decide(p, stub(DIRECT))
            self.assertEqual((d.decision, d.stage, d.reason), ("bypass", 2, why), p)

    def test_a_reply_to_a_question_main_asked_reaches_main(self):
        prompt = "Add an install section to README.md"
        for text in ("I can do it two ways. Which one do you want?", "계획은 이렇습니다.\n\n진행할까요",
                     "Done with step 1. Shall I proceed with step 2.", "어느 쪽으로 할지 **확인해 주세요**"):
            d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(text))
            self.assertEqual((d.decision, d.reason), ("bypass", "reply-to-main"), text)
        d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript("Shipped. Report filed."))
        self.assertEqual(d.decision, "direct")
        self.assertEqual(self.decide(prompt, stub(DIRECT), transcript=str(self.root / "missing.jsonl")).decision,
                         "direct")

    def test_a_question_followed_by_a_dispatch_block_or_status_line_reaches_main(self):
        """I1: Main's final reply ends with its ```dispatch block (and maybe a STATUS line)."""
        prompt = "옵션 B로 README.md 수정해줘"
        dispatch_block = "```dispatch\nTO build\nGOAL x\n  more\nTIER standard\n```"
        asked = ("Which option do you prefer?\n\n" + dispatch_block,
                 "A와 B 중 어느 쪽으로 할까요\n\n" + dispatch_block + "\n\nSTATUS: waiting\n",
                 "Plan below.\n\nShall I proceed with option B, or keep A\n" + dispatch_block,
                 "Which one?\n```\ncode\n```\n```dispatch\nTO build\n```")
        for text in asked:
            d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(text))
            self.assertEqual((d.decision, d.reason), ("bypass", "reply-to-main"), text)
        told = ("Dispatched the README change.\n\n" + dispatch_block,
                "Is it fixed? Yes: the tests pass.\n" + dispatch_block,
                "Which one?\n\nI picked B.\n" + dispatch_block)
        for text in told:
            d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(text))
            self.assertEqual(d.decision, "direct", text)
        self.assertEqual(triage.strip_trailer("Ok?\n```dispatch\nTO build"), "Ok?\n```dispatch\nTO build")
        self.assertEqual(triage.strip_trailer("Ok?\n\nSTATUS: done\n```dispatch\nTO b\n```\n\n"), "Ok?")

    def test_a_dispatch_block_with_a_nested_fenced_snippet_is_stripped_whole(self):
        """m1: the strip starts at the opener of the last ```dispatch/```amend/```stop block, so an
        indented snippet inside its continuation lines no longer cuts the block short."""
        prompt = "옵션 B로 README.md 수정해줘"
        nested = ("```dispatch\nTO build\nGOAL fix the parser\nCONTEXT the failing call:\n  ```py\n"
                  "  parse(x)\n  ```\n  and its bare twin:\n  ```\n  y = 1\n  ```\nTIER standard\n```")
        for opener in ("dispatch", "amend", "stop"):
            block = nested.replace("```dispatch", "```" + opener, 1)
            text = "Which option?\n\n" + block
            self.assertEqual(triage.strip_trailer(text), "Which option?", opener)
            self.assertEqual(triage.strip_trailer(text + "\n\nSTATUS: waiting\n"), "Which option?", opener)
            d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(text))
            self.assertEqual((d.decision, d.reason), ("bypass", "reply-to-main"), opener)
        told = "I picked B.\n\n" + nested
        self.assertEqual(triage.strip_trailer(told), "I picked B.")
        self.assertEqual(self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(told)).decision,
                         "direct")
        # A question after an earlier block stays; a plain trailing code block is still stripped.
        self.assertEqual(triage.strip_trailer(nested + "\nShall I go?\n```\ncode\n```"), nested + "\nShall I go?")

    def test_tilde_fences_mirror_the_backtick_tests(self):
        """`~~~` fences are stripped like ``` fences: same character, at least the opener's length."""
        prompt = "옵션 B로 README.md 수정해줘"
        tilde = "~~~dispatch\nTO build\nGOAL x\n  more\nTIER standard\n~~~"
        asked = ("Which option do you prefer?\n\n" + tilde,
                 "A와 B 중 어느 쪽으로 할까요\n\n" + tilde + "\n\nSTATUS: waiting\n",
                 "Which one?\n~~~\ncode\n~~~\n" + tilde,
                 "Which one?\n```\ncode\n```\n" + tilde,
                 "Which one?\n~~~~dispatch\nTO build\n~~~\n```\n~~~~")
        for text in asked:
            d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(text))
            self.assertEqual((d.decision, d.reason), ("bypass", "reply-to-main"), text)
        for text in ("Dispatched the README change.\n\n" + tilde, "Which one?\n\nI picked B.\n" + tilde):
            d = self.decide(prompt, stub(DIRECT), transcript=self.assistant_transcript(text))
            self.assertEqual(d.decision, "direct", text)
        self.assertEqual(triage.strip_trailer("Ok?\n~~~dispatch\nTO build"), "Ok?\n~~~dispatch\nTO build")
        self.assertEqual(triage.strip_trailer("Ok?\n\nSTATUS: done\n~~~dispatch\nTO b\n~~~\n\n"), "Ok?")
        # A closer of the other character, or a shorter one, does not close the block.
        self.assertEqual(triage.strip_trailer("Ok?\n~~~dispatch\nTO b\n```"), "Ok?\n~~~dispatch\nTO b\n```")
        self.assertEqual(triage.strip_trailer("Ok?\n````dispatch\nTO b\n```"), "Ok?\n````dispatch\nTO b\n```")
        nested = ("~~~dispatch\nTO build\nGOAL fix the parser\nCONTEXT the failing call:\n  ~~~py\n"
                  "  parse(x)\n  ~~~\n  and a backtick twin:\n  ```\n  y = 1\n  ```\nTIER standard\n~~~")
        for opener in ("dispatch", "amend", "stop"):
            block = nested.replace("~~~dispatch", "~~~" + opener, 1)
            self.assertEqual(triage.strip_trailer("Which option?\n\n" + block), "Which option?", opener)
            self.assertEqual(triage.strip_trailer("Which option?\n\n" + block + "\n\nSTATUS: waiting\n"),
                             "Which option?", opener)
        self.assertEqual(triage.strip_trailer("I picked B.\n\n" + nested), "I picked B.")
        self.assertEqual(triage.strip_trailer(nested + "\nShall I go?\n~~~\ncode\n~~~"), nested + "\nShall I go?")

    def test_an_acknowledgement_or_continuation_word_first_reaches_main(self):
        """I6: the prompt continues the conversation; no classifier call, whatever Main said last."""
        calls = []
        statement = self.assistant_transcript("Option A changes README.md, option B changes docs/a.md.")
        yes = ("응, 그걸로 해줘. README.md도 같이 수정해줘", "네 README.md 수정해 주세요", "예, docs/a.md 정리해줘",
               "그래 README.md에 추가해줘", "좋아요. README.md 고쳐줘", "아니, docs/a.md만 수정해줘",
               "그걸로 README.md 수정해줘", "그렇게 docs/a.md 정리해", "그럼 README.md 바꿔줘",
               "yes, and add it to README.md too", "Yeah add a line to README.md", "ok add it to README.md",
               "Okay. Update CHANGELOG.md as well", "sure, fix docs/a.md", "No, add it to docs/a.md instead",
               "nope - write it in docs/b.md", "go ahead and add the section to README.md",
               "Do it: update README.md", "that one, add it to README.md")
        for p in yes:
            d = self.decide(p, stub(DIRECT, calls=calls), transcript=statement)
            self.assertEqual((d.decision, d.stage, d.reason), ("bypass", 2, "continuation"), p)
        self.assertEqual(calls, [])
        for p in ("예시 파일 example.md 추가해줘", "네이버 로그인 버튼 src/a.tsx 추가해줘", "아니면 README.md 정리해줘",
                  "Nobody reads README.md; add a summary", "Notes.md에 정리 추가해줘", "Add a yes/no toggle to a.py",
                  "Nope.md add a heading line", "ok.py 고쳐줘 please now"):
            self.assertNotEqual(self.decide(p, stub(DIRECT)).reason, "continuation", p)

    def test_transcript_tail_read_is_cheap_on_a_big_transcript(self):
        path = self.assistant_transcript("Which one?", pad=5_000_000)
        triage.main_asked(path)
        started = time.perf_counter()
        self.assertTrue(triage.main_asked(path))
        self.assertLess(time.perf_counter() - started, 0.010)

    def test_last_assistant_text_skips_tool_only_entries_and_huge_tails(self):
        path = self.root / "t.jsonl"
        big = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "y" * 700_000}]}})
        path.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Ok?"}]}})
                        + "\n" + big + "\n")
        self.assertEqual(triage.last_assistant_text(str(path)), "")       # beyond the bounded tail: not read
        self.assertEqual(triage.last_assistant_text(None), "")

    def test_disabled_or_non_main_session_never_triages(self):
        self.config({"claude_bin": str(self.claude), "delivery": "runner"})     # triage.enabled default false
        out = self.handle("[build] add a line to README.md")
        self.assertEqual((out.block, out.note, self.spawned, self.samples()), (None, "", [], []))
        self.enable()
        for session in ("div-acme-build", "feat-acme-checkout", "main-nope", None):
            out = self.handle("[build] add a line to README.md", session=session)
            self.assertEqual((out.block, self.spawned), (None, []), session)
        self.assertEqual(self.samples(), [])
        self.assertNotIn("triage ", self.events())

    def test_pane_delivery_leaves_triage_inert(self):
        self.config({"claude_bin": str(self.claude), "delivery": "pane", "triage": {"enabled": True}})
        self.assertIsNone(self.handle("[build] add a line to README.md").block)
        self.assertEqual(self.spawned, [])

    def test_wake_line_is_not_triaged(self):
        """C9 L4: ORCHE's own wake line goes to Main untouched: no sample row, no `triage` events line."""
        for prompt in (inbox.WAKE_TEXT, "[orche] inbox: acme-a-20261016-1 rev 2"):
            out = self.handle(prompt, classifier=stub(DIRECT))
            self.assertEqual((out.block, out.note, self.spawned), (None, "", []), prompt)
        self.assertEqual(self.samples(), [])
        self.assertNotIn("triage ", self.events())


class HeuristicTest(TriageCase):
    def test_imperative_and_target_make_a_candidate(self):
        yes = ("README.md에 설치 방법 추가해줘", "docs/guide.md 오타 수정", "Add a usage section to docs/guide.md",
               "please fix the typo in src/app.ts", "Rename `fooBar` to `foo_bar` in utils",
               "lib/x.py 헤더 주석 작성해", "Update CHANGELOG.md for 0.3.0", "config.yaml 값 바꿔줘")
        no = ("fix the login bug", "README.md looks great today", "새 기능 기획을 같이 해보자",
              "Let's think about pricing for docs/pricing.md", "https://example.com/a.html 추가해줘")
        for p in yes:
            self.assertTrue(triage.is_candidate(p), p)
        for p in no:
            self.assertFalse(triage.is_candidate(p), p)

    def test_a_negation_is_never_a_candidate(self):
        """M5: "수정하지 말고", "don't change" name a target but ask for no change."""
        for p in ("README.md는 수정하지 말고 읽어보고 의견 줘", "docs/a.md 삭제하지 마", "please don't change src/App.jsx",
                  "Update nothing; do not touch README.md", "Never rename `fooBar` in utils"):
            self.assertFalse(triage.is_candidate(p), p)

    # M5 table: (prompt, is a candidate). Korean verb forms count, a bare verbal noun in the middle
    # ("수정 이력") does not; a Korean question ending never makes a candidate, with or without `?`.
    KO_TABLE = [
        ("README.md에 설치 섹션 추가해줘", True), ("docs/a.md 오타 수정해 주세요", True), ("src/app.py 정리하자", True),
        ("CHANGELOG.md 업데이트해줘", True), ("config.yaml에서 debug 항목 삭제해", True), ("utils.py 이름 바꿔줘", True),
        ("README.md 고쳐 주세요", True), ("notes.txt 지워줘", True), ("docs/guide.md에 예시 넣어줘", True),
        ("README.md 수정 부탁해", True), ("docs/guide.md 오타 수정", True), ("lib/x.py 헤더 주석 작성해", True),
        ("package.json 버전 올려줘", True), ("README.md 링크 고치자", True), ("src/App.tsx 버튼 문구 바꾸세요", True),
        ("README.md는 수정하지 말고 읽어보고 의견 줘", False), ("README.md 수정 이력 좀 봐줘", False),
        ("README.md 수정했니", False), ("docs/a.md 정리해야 하나", False), ("README.md 고칠까", False),
        ("src/App.jsx 고치고 나서 배포 계획도 세워줘", False), ("src/App.jsx 수정하고 배포 계획도 세워줘", False),
        ("README.md 추가된 부분 어때", False), ("이거 왜 이래 README.md 보면 이상한데", False),
        ("README.md 내용이 맞는지", False), ("docs/a.md 건드리지 마세요", False), ("README.md 수정해줄래", False),
        ("config.yaml 변경 사항 뭐야", False), ("README.md 바꾼 게 언제냐", False), ("src/app.py 수정된 건가요", False)]
    EN_TABLE = [
        ("Add a FAQ section to README.md", True), ("Please fix the typo in docs/guide.md", True),
        ("Let's rename utils.py to helpers.py", True), ("update CHANGELOG.md with the 0.3.3 entry", True),
        ("remove the unused import in src/app.ts", True), ("Delete old.txt please", True),
        ("Replace foo with bar in config.yaml", True), ("Move notes.md into docs/notes.md", True),
        ("bump the version in package.json", True), ("Implement the parse function in lib/parse.py", True),
        ("Just set debug to false in config.yaml", True), ("I need you to clean up src/util.py", True),
        ("go and reword the intro of README.md", True), ("Kindly translate the intro of docs/ko.md", True),
        ("please don't change src/App.jsx", False), ("Do not touch README.md, just read it", False),
        ("never edit .github/ci.yml", False), ("what does README.md say?", False),
        ("Is CHANGELOG.md up to date", False), ("Can you fix README.md", False),
        ("README.md looks fine to me", False), ("I fixed docs/a.md yesterday", False), ("add more tests", False),
        ("Should we rename utils.py", False), ("Do the rename of foo.py to bar.py", False),
        ("Has anyone updated package.json", False), ("Fix README.md, or should we leave it?", False),
        ("no need to update CHANGELOG.md", False)]

    def test_m5_table_korean_and_english(self):
        for table in (self.KO_TABLE, self.EN_TABLE):
            self.assertGreaterEqual(len(table), 20)
            self.assertEqual(sum(1 for _p, yes in table if yes), len(table) // 2)
            for prompt, yes in table:
                self.assertEqual(triage.is_candidate(prompt), yes, prompt)
                d = self.decide(prompt, stub(DIRECT))
                if yes:
                    self.assertEqual((d.decision, d.stage), ("direct", 4), prompt)
                else:
                    self.assertNotEqual(d.decision, "direct", prompt)
                    self.assertIn(d.stage, (2, 3), prompt)

    def test_korean_question_endings_bypass_without_a_question_mark(self):
        for p in ("README.md 수정했니", "docs/a.md 정리해야 하나", "README.md 고칠까", "README.md 내용이 맞는지",
                  "README.md 추가된 부분 어때", "README.md 바꾼 게 언제냐", "src/app.py 수정된 건가요",
                  "README.md 수정해줄래", "config.yaml 변경 사항 뭐야"):
            d = self.decide(p, stub(DIRECT))
            self.assertEqual((d.decision, d.stage, d.reason), ("bypass", 2, "question"), p)

    def test_find_paths_keeps_paths_and_file_names_in_order(self):
        self.assertEqual(triage.find_paths("Refactor src/a/b.ts and src/c.py, then notes.md."),
                         ["src/a/b.ts", "src/c.py", "notes.md"])
        self.assertEqual(triage.find_paths("README.md에 docs/a.md를 링크"), ["README.md", "docs/a.md"])
        self.assertEqual(triage.find_paths("see https://x.com/a.html"), [])

    def test_non_candidates_go_to_main_without_a_model(self):
        calls = []
        d = self.decide("Let's plan the Q4 roadmap together", stub(DIRECT, calls=calls))
        self.assertEqual((d.decision, d.stage, d.backend), ("main", 3, "none"))
        self.assertEqual(calls, [])

    def test_stages_one_to_three_take_under_five_ms(self):
        prompt = "Add an install section to README.md and link docs/guide.md from it " * 20
        triage.is_candidate(prompt)
        started = time.perf_counter()
        for _ in range(20):
            triage.parse_tag(prompt), triage.bypass_reason(prompt), triage.is_candidate(prompt)
        self.assertLess((time.perf_counter() - started) / 20, 0.005)


class ClassifierTest(TriageCase):
    def test_parse_valid_fenced_and_embedded_json(self):
        divs = ["build", "rnd"]
        self.assertEqual(triage.normalize(triage.parse_json_object(json.dumps(DIRECT)), divs)["route"], "direct")
        fenced = "```json\n" + json.dumps(DIRECT) + "\n```"
        self.assertEqual(triage.normalize(triage.parse_json_object(fenced), divs)["division"], "build")
        prose = "Sure. " + json.dumps(DIRECT) + " Hope that helps."
        self.assertEqual(triage.normalize(triage.parse_json_object(prose), divs)["tier"], "mechanical")

    def test_garbage_and_wrong_types_are_none(self):
        divs = ["build"]
        for bad in ("not json at all", "", json.dumps([1, 2]), json.dumps(dict(DIRECT, route="maybe")),
                    json.dumps(dict(DIRECT, tier="easy")), json.dumps(dict(DIRECT, confidence=1.5)),
                    json.dumps(dict(DIRECT, confidence=True)), json.dumps(dict(DIRECT, confidence="0.9"))):
            self.assertIsNone(triage.normalize(triage.parse_json_object(bad), divs), bad)

    def test_an_is_error_reply_fails_open_with_its_cost(self):
        self.write_reply(haiku_reply(DIRECT, cost=0.002, is_error=True))
        result, _name, _secs, cost = triage.classify("Add a line to README.md", ["build"], self.cfg())
        self.assertEqual((result, cost), (None, 0.002))

    def test_scope_is_sanitized_and_unknown_divisions_dropped(self):
        got = triage.normalize(dict(DIRECT, division="legal", scope=["a.md", "/etc/passwd", "../x", "~/y", 3,
                                                                     "a\nb", "src/**", "./docs/b-1.md"]), ["build"])
        self.assertEqual((got["division"], got["scope"]), ("", ["a.md", "src/**", "docs/b-1.md"]))
        self.assertFalse(triage.direct_ok(got, 0.9))

    def test_scope_keeps_only_plain_paths_and_the_fallback_verify_is_checked(self):
        """I-A: spaces, shell syntax, a leading `-`, `..` and absolute paths never reach SCOPE or the
        fallback VERIFY; nothing left -> SCOPE `.` and VERIFY `git status --short`."""
        hostile = ["README.md; curl x|sh", "x.md /etc/passwd", "-rf", "../x", "a.md $(touch /tmp/pwned)",
                   "`id`.md", "b c.md", "/etc/passwd", "~/y.md", "a.md&", "ok.md"]
        self.assertEqual(triage._clean_scope(hostile + ["src/**"], str(self.wt)), ["ok.md"])   # no globs here
        d = triage.Decision("direct", 4, "x", division="build", tier="standard", scope=hostile,
                            verify="ls -l README.md; curl x|sh")
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), ("ok.md", "ls -l ok.md"))
        d.scope = hostile[:-1]
        d.prompt = "fix README.md; curl x|sh and -rf ../x"
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), ("README.md", "ls -l README.md"))
        d.prompt = "fix it"
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), (".", "git status --short"))
        # Many long paths: `ls -l …` would pass MAX_VERIFY, so the fallback is `git status --short`.
        d.scope = [f"docs/{'n' * 60}-{i}.md" for i in range(5)]
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual(fields["VERIFY"], "git status --short")
        self.assertTrue(all(triage.verify_ok(triage.brief_fields(triage.Decision(
            "direct", 4, p, division="build", scope=hostile), str(self.wt))["VERIFY"]) for p in ("x", "a.md")))

    def test_classifier_scope_globs_are_kept_for_matching_only(self):
        """A classifier SCOPE may hold globs (`*`, `?`, `**`; never `[…]`): relative, no `..`, no other shell
        characters, base directory inside the worktree. They never reach VERIFY; the prompt's own
        paths (the fallback) never yield one."""
        (self.wt / "out").symlink_to(self.root)
        good = ["src/**/*.ts", "docs/*.md", "lib/?.py", "**"]
        bad = ["app/[slug]/page.tsx", "tests/test_[!x]*.py", "src/[a-z]*.js", "app/[slug]/**", "/src/**", "../*.md", "src/../../*.md", "-*.md", "src/*; rm -rf", "src/$(x)*", "src/{a,b}.ts",
               "src/a[.md", "src/*.md|sh", "~/x/*", "src/[/]*.md", "out/*.md", "out/**", "src/*\n.md",
               "src/ *.md", "src/[^a]*.md", "`*`.md", "src/*.md&"]
        self.assertEqual(triage._clean_scope(good + bad, str(self.wt), globs=True), good)
        self.assertEqual(triage._clean_scope(good, str(self.wt)), [])
        self.assertEqual(triage.normalize(dict(DIRECT, scope=good + bad), ["build"])["scope"][:2],
                         ["src/**/*.ts", "docs/*.md"])
        d = triage.Decision("direct", 4, "x", division="build", tier="standard", scope=["src/**/*.ts", "README.md"])
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), ("src/**/*.ts README.md", "ls -l README.md"))
        self.assertIn("never `-A`, `-u`, `.` or a glob", fields["ACCEPT"])
        d.scope = ["docs/*.md"]
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), ("docs/*.md", "git status --short"))
        self.assertFalse(triage.verify_ok("ls -l docs/*.md"))
        d.scope, d.prompt = [], "fix src/*.ts and README.md"
        self.assertEqual(triage.brief_fields(d, str(self.wt))["SCOPE"], "README.md")
        # The scope-gate grammar (units.scope_match) the globs mean.
        # `[` is literal (0.3.2 again): a feature scope `app/[slug]/**` admits only that directory.
        match = [("src/a/b.ts", "src/**/*.ts"), ("src/b.ts", "src/**/*.ts"), ("lib/a.py", "lib/?.py"),
                 ("app/[slug]/page.tsx", "app/[slug]/page.tsx"), ("app/[slug]/x/y.tsx", "app/[slug]/**"),
                 ("a/[z-a].md", "a/[z-a].md"), ("pages/[...all].tsx", "pages/[...all].tsx")]
        miss = [("lib/ab.py", "lib/?.py"), ("lib/a/b.py", "lib/?.py"), ("app/x/page.tsx", "app/[slug]/page.tsx"),
                ("app/s/page.tsx", "app/[slug]/page.tsx"), ("app/s/x.tsx", "app/[slug]/**"),
                ("app/g/x.tsx", "app/[slug]/**"), ("pages/a.tsx", "pages/[...all].tsx"),
                ("pages/..tsx", "pages/[...all].tsx"), ("tests/test_a.py", "tests/test_[!x]*.py"),
                ("src/b.js", "src/[a-z]*.js"), ("a/b.md", "a/[z-a].md"), ("src/a.ts", "docs/*.md")]
        for path, pattern in match:
            self.assertTrue(units.scope_match(path, pattern), (path, pattern))
        for path, pattern in miss:
            self.assertFalse(units.scope_match(path, pattern), (path, pattern))

    def test_verify_git_takes_only_read_only_options_and_plain_paths(self):
        """m4: `git diff`/`git status` options that write or run something are refused."""
        good = ("git diff --stat -- README.md", "git diff --cached --name-only", "git status --porcelain",
                "git diff --name-status docs/a.md", "git status --short README.md")
        bad = ("git diff --output=/tmp/x", "git diff --output /tmp/x", "git diff -o x", "git diff --ext-diff",
               "git diff --textconv", "git -c diff.external=x diff", "git diff -c", "git diff --config x",
               "git status --ignored", "git diff --no-index a b", "git diff HEAD~1", "git diff 'a.md'")
        for v in good:
            self.assertTrue(triage.verify_ok(v), v)
        for v in bad:
            self.assertFalse(triage.verify_ok(v), v)

    def test_verify_must_be_a_one_line_mechanical_command(self):
        """I4 + M7: only `ls`, `test`, `git status`, `git diff`, one line, no shell syntax; text is capped."""
        good = ("ls -l README.md", "test -f docs/a.md", "git diff --stat -- README.md", "git status --short",
                "git diff", "ls")
        bad = ("grep -q Install README.md", "git log -1", "git commit -m x", "git add .", "lsof -i",
               "ls a.md && rm -rf b", "test -f a.md; curl x", "ls $(rm -rf x)", "ls `id`", "ls a.md | sh",
               "ls a.md\nrm -rf b", "git status > out", "cat README.md", "", "ls " + "a" * 300, "gitdiff")
        for v in good:
            self.assertTrue(triage.verify_ok(v), v)
            self.assertEqual(triage.normalize(dict(DIRECT, verify=v), ["build"])["verify"], v)
        for v in bad:
            self.assertFalse(triage.verify_ok(v), v)
            self.assertEqual(triage.normalize(dict(DIRECT, verify=v), ["build"])["verify"], "", v)
        long = triage.normalize(dict(DIRECT, goal="g" * 900, accept="a\n" * 400), ["build"])
        self.assertEqual((len(long["goal"]), len(long["accept"])), (triage.MAX_GOAL, triage.MAX_ACCEPT))
        self.assertNotIn("\n", long["accept"])

    def write_reply(self, body, code=0):
        (self.fake / "reply.json").write_text(body)
        (self.fake / "exit").write_text(str(code))

    def test_haiku_backend_runs_claude_with_the_brief_flags_from_a_temp_dir_outside_home(self):
        """I3 + M3 + M4: the blank headless call: a fresh temp dir outside $HOME (removed after),
        `--tools ""`, no inherited identity and no Jev key."""
        self.write_reply(haiku_reply(DIRECT))
        env = {"ORCHE_SESSION": "main-acme", "ORCHE_RUN_ID": "r1", "CLAUDECODE": "1", "CLAUDE_PROJECT_DIR": "/p",
               "ORCA_TERMINAL_HANDLE": "t1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_OAUTH_TOKEN": "keep-me",
               "JEV_API_KEY": "jev-secret"}
        with mock.patch.dict(os.environ, env):
            result, name, secs, cost = triage.classify(f"Add {SECRET} to README.md", ["rnd", "build"], self.cfg())
        self.assertEqual((result["route"], name, cost), ("direct", "claude-haiku", 0.0012))
        argv = (self.fake / "argv.txt").read_text().splitlines()
        self.assertEqual(argv[:14], ["-p", "--model", "claude-haiku-5-5", "--output-format", "json",
                                     "--max-turns", "1", "--setting-sources", "project", "--tools", "",
                                     "--strict-mcp-config", "--no-session-persistence", "--system-prompt"])
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertIn("Divisions: rnd, build.", (self.fake / "argv.txt").read_text())
        stdin = (self.fake / "stdin.txt").read_text()
        self.assertNotIn(SECRET, stdin)
        self.assertIn("[REDACTED]", stdin)
        cwd = (self.fake / "cwd.txt").read_text().strip()
        self.assertTrue(cwd.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep), cwd)
        self.assertTrue(os.path.basename(cwd).startswith("orche-blank-"), cwd)
        self.assertFalse(units.is_within(cwd, Path.home()), cwd)
        self.assertFalse(os.path.exists(cwd))                                       # removed afterwards
        self.assertEqual((self.fake / "session.txt").read_text().strip(), "")   # ORCHE_SESSION dropped
        seen = dict(line.split("=", 1) for line in (self.fake / "env.txt").read_text().splitlines() if "=" in line)
        for key in ("ORCHE_RUN_ID", "CLAUDECODE", "CLAUDE_PROJECT_DIR", "ORCA_TERMINAL_HANDLE",
                    "CLAUDE_CODE_ENTRYPOINT"):
            self.assertNotIn(key, seen)
        self.assertNotIn("ORCHE_SESSION", seen)                                     # no caller identity
        self.assertNotIn("JEV_API_KEY", seen)                                       # the Jev key never passes
        self.assertEqual(seen.get("CLAUDE_CODE_OAUTH_TOKEN"), "keep-me")              # auth stays
        self.assertTrue(seen.get("PATH", "").startswith(str(paths.PLUGIN_ROOT / "bin")), seen.get("PATH"))

    def test_timeout_kills_the_classifier_process_group(self):
        """M3: a wrapper's child (here a background sleep) dies with it; nothing is left behind."""
        latency = 2.0
        self.enable(max_latency_s=latency)
        self.write_reply(haiku_reply(DIRECT))
        (self.fake / "action.sh").write_text(f'sleep 30 &\necho $! > "{self.fake}/child"\n')
        (self.fake / "sleep").write_text("30")
        started = time.monotonic()
        self.assertIsNone(triage.classify("Add a line to README.md", ["build"], self.cfg())[0])
        self.assertLess(time.monotonic() - started, latency + 2.5)
        self.assertIn("triage: claude-haiku timeout after", self.hooks_log())   # the classifier ran, and was cut
        pids = [(self.fake / "pid").read_text().strip(), (self.fake / "child").read_text().strip()]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(alive(p) for p in pids):
            time.sleep(0.05)
        self.assertEqual([p for p in pids if alive(p)], [])
        self.assertFalse(os.path.exists((self.fake / "cwd.txt").read_text().strip()))

    def hooks_log(self):
        path = self.home / "logs" / "hooks.log"
        return path.read_text() if path.exists() else ""

    def test_haiku_timeout_and_failure_go_to_main(self):
        latency = 2.0
        self.enable(max_latency_s=latency)
        self.write_reply(haiku_reply(DIRECT))
        (self.fake / "sleep").write_text("5")
        started = time.monotonic()
        self.assertIsNone(triage.classify("Add a line to README.md", ["build"], self.cfg())[0])
        self.assertLess(time.monotonic() - started, latency + 2.5)
        self.assertIn("triage: claude-haiku timeout after", self.hooks_log())
        (self.fake / "sleep").unlink()
        self.write_reply(haiku_reply(DIRECT), code=1)
        self.assertIsNone(triage.classify("Add a line to README.md", ["build"], self.cfg())[0])
        self.write_reply("I think you should ask Main.")
        d = triage.decide("Add a line to README.md", ["build"], self.cfg())
        self.assertEqual((d.decision, d.stage, d.reason), ("main", 4, "classifier unavailable"))

    def test_backend_resolution(self):
        cfg = self.enable(backend="auto")
        self.assertEqual(triage.backend(self.cfg())[0], "claude-haiku")
        cfg["jev"] = {"enabled": True, "base_url": "https://jev.test"}
        self.config(cfg)
        with mock.patch.dict(os.environ, {"JEV_API_KEY": "k"}):
            self.assertEqual(triage.backend(self.cfg())[0], "jev")
        cfg["triage"]["backend"] = "jev"
        cfg["jev"] = {"enabled": False, "base_url": ""}
        self.config(cfg)
        self.assertEqual(triage.backend(self.cfg()), ("jev", None))
        # M10: an unconfigured Jev makes no call, so the row says backend=none (no classifier call counted).
        self.assertEqual(triage.classify("Add a line to README.md", ["build"], self.cfg())[:2], (None, "none"))

    def test_jev_backend_maps_choices_to_a_decision(self):
        cfg = self.enable(backend="jev")
        cfg["jev"] = {"base_url": "https://jev.test"}
        self.config(cfg)
        sent = []

        def opener(req, timeout):
            sent.append((json.loads(req.data), timeout))
            return json.dumps({"model": "jev-1.13.0", "answers": {
                "route": {"type": "choice", "choice": "direct", "probabilities": {}, "confidence": 0.97},
                "division": {"type": "choice", "choice": "build", "probabilities": {}, "confidence": 0.93},
                "tier": {"type": "choice", "choice": "mechanical", "probabilities": {}, "confidence": 0.91}}}).encode()
        with mock.patch.dict(os.environ, {"JEV_API_KEY": "k"}), mock.patch.object(jev, "OPENER", opener):
            result, name, _s, _c = triage.classify(f"Add {SECRET} to README.md", ["rnd", "build"], self.cfg())
        self.assertEqual(name, "jev")
        # No probabilities: the route answer's own confidence; division and tier never lower it.
        self.assertEqual((result["route"], result["division"], result["tier"], result["confidence"]),
                         ("direct", "build", "mechanical", 0.97))
        [(body, timeout)] = sent
        self.assertEqual(list(body["questions"]), ["route", "division", "tier"])
        self.assertEqual(body["questions"]["tier"]["criteria"], triage.TIER_HINTS)
        self.assertEqual(body["questions"]["route"]["criteria"], triage.ROUTE_HINTS)
        self.assertEqual(body["questions"]["division"]["criteria"],
                         {"rnd": "experiments and prototypes", "build": "code and doc changes"})
        self.assertNotIn(SECRET, body["state"])
        self.assertLessEqual(timeout, 1.0)


    def jev_answers(self, route_probs, divisions=("rnd", "build"), choice="direct", route=None):
        cfg = self.enable(backend="jev")
        cfg["jev"] = {"base_url": "https://jev.test"}
        self.config(cfg)
        sent = []
        if route is None:
            route = {"type": "choice", "choice": choice, "confidence": 0.86}
            if route_probs is not None:
                route["probabilities"] = route_probs

        def opener(req, timeout):
            sent.append(json.loads(req.data))
            return json.dumps({"model": "jev-1.13.0", "answers": {
                "route": route,
                "division": {"type": "choice", "choice": "build", "confidence": 0.2},
                "tier": {"type": "choice", "choice": "mechanical", "confidence": 0.4}}}).encode()
        with mock.patch.dict(os.environ, {"JEV_API_KEY": "k"}), mock.patch.object(jev, "OPENER", opener):
            result = triage.classify("Add a line to README.md", list(divisions), self.cfg())[0]
        return result, sent[0]

    def test_jev_confidence_is_the_route_probability_of_direct(self):
        result, body = self.jev_answers({"direct": 0.93, "main": 0.07})
        self.assertEqual((result["division"], result["confidence"]), ("build", 0.93))
        self.assertTrue(triage.direct_ok(result, 0.9))
        self.assertIn("code and doc changes", json.dumps(body["questions"]["division"]))

    def test_jev_confidence_falls_back_to_the_route_confidence(self):
        result, _body = self.jev_answers(None)
        self.assertEqual(result["confidence"], 0.86)
        self.assertFalse(triage.direct_ok(result, 0.9))

    def test_jev_confidence_is_the_probability_of_the_chosen_route(self):
        result, _body = self.jev_answers({"direct": 0.03, "main": 0.97}, choice="main")
        self.assertEqual((result["route"], result["confidence"]), ("main", 0.97))
        self.assertFalse(triage.direct_ok(result, 0.8))

    def test_jev_probabilities_without_the_chosen_route_fall_back(self):
        result, _body = self.jev_answers({"main": 0.2})
        self.assertEqual(result["confidence"], 0.86)

    def test_jev_garbage_route_probability_falls_back(self):
        for bad in ("0.99", 1.5):
            result, _body = self.jev_answers({"direct": bad, "main": 0.1})
            self.assertEqual(result["confidence"], 0.86, bad)

    def test_jev_without_a_usable_route_answer_goes_to_main(self):
        for route in ({"type": "choice", "choice": "maybe", "confidence": 0.99},
                      {"type": "choice", "choice": "direct"}, "direct"):
            result, _body = self.jev_answers(None, route=route)
            self.assertIsNone(result, route)

    def test_jev_custom_division_described_by_its_name(self):
        _result, body = self.jev_answers(None, divisions=("build", "legal"))
        self.assertEqual(body["questions"]["division"]["criteria"]["legal"], "legal")

    def test_system_prompt_carries_every_division_hint(self):
        for name, hint in triage.DIVISION_HINTS.items():
            self.assertIn(f"{name} = {hint}", triage.SYSTEM_PROMPT)


class DecisionRuleTest(TriageCase):
    def test_direct_needs_route_confidence_tier_and_division(self):
        ok = triage.normalize(DIRECT, ["build"])
        self.assertTrue(triage.direct_ok(ok, 0.9))
        self.assertFalse(triage.direct_ok(dict(ok, confidence=0.89), 0.9))
        self.assertFalse(triage.direct_ok(dict(ok, tier="hard"), 0.9))
        self.assertFalse(triage.direct_ok(dict(ok, route="main"), 0.9))
        self.assertFalse(triage.direct_ok(dict(ok, division=""), 0.9))
        self.assertFalse(triage.direct_ok(None, 0.9))

    def test_min_confidence_comes_from_config(self):
        prompt = "Add an install section to README.md"
        self.assertEqual(self.decide(prompt, stub(dict(DIRECT, confidence=0.79))).decision, "main")
        self.enable(min_confidence=0.75)
        d = self.decide(prompt, stub(dict(DIRECT, confidence=0.8)))
        self.assertEqual((d.decision, d.stage, d.confidence), ("direct", 4, 0.8))
        d = self.decide(prompt, stub(dict(DIRECT, tier="hard")))
        self.assertEqual((d.decision, d.reason), ("main", "classifier said main"))


class ActionTest(TriageCase):
    PROMPT = "Add an install section to README.md"

    def test_direct_writes_the_brief_spawns_the_runner_and_blocks_the_prompt(self):
        out = self.handle(self.PROMPT, stub(DIRECT))
        unit = self.only_unit()
        self.assertEqual(out.block, {"decision": "block", "reason": (
            f"[orche] routed to div-acme-build as {unit} (runner-started, TIER mechanical): \"Add an install "
            f"section to README.md\". Results arrive in this inbox; prefix the prompt with [main] to talk to "
            f"Main instead (to cancel: [main] STOP {unit}).")})
        fields = brief.find(registry.load(), unit).fields
        # The classifier's `grep -q …` cannot run at TIER mechanical (I4): the fallback replaces it.
        # GOAL is the classifier's sentence, so CONTEXT quotes the prompt itself (M8).
        self.assertEqual(fields, {"TO": "div-acme-build", "GOAL": DIRECT["goal"], "SCOPE": "README.md",
                                  "CONTEXT": triage.CONTEXT_QUOTED + "\n> " + self.PROMPT,
                                  "ACCEPT": DIRECT["accept"] + "; `git add` only the files you changed, by name "
                                                               "(never `-A`, `-u`, `.` or a glob); exactly one "
                                                               "commit with a conventional message",
                                  "VERIFY": "ls -l README.md",
                                  "TIMEBOX": "30m", "FORBID": "push, amend, --no-verify, files outside SCOPE",
                                  "REPORT": "standard", "TIER": "mechanical"})
        [(argv, kw)] = self.spawned
        self.assertEqual(argv, SPAWN + [unit])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(self.pointers("div-acme-build"), [])     # C9 ruling 6: a runner unit gets no target pointer
        self.assertEqual([p["summary"] for p in self.pointers("main-acme")],
                         [f"routed {unit} → div-acme-build (runner-started)"])
        self.assertIn(f"unit={unit} rev=1 to=div-acme-build by=triage delivery=runner-started", self.events())
        self.assertRegex(self.events(), rf"triage session=main-acme decision=direct stage=4 conf=0.95 "
                                        rf"backend=claude-haiku ms=\d+ unit={unit}")

    def test_fallbacks_when_the_classifier_gave_no_goal_or_scope(self):
        out = self.handle("[build] lib/x.py 헤더 주석 작성해")
        self.assertIsNotNone(out.block)
        fields = brief.find(registry.load(), self.only_unit()).fields
        self.assertEqual((fields["GOAL"], fields["SCOPE"], fields["VERIFY"], fields["TIER"], fields["ACCEPT"]),
                         ("lib/x.py 헤더 주석 작성해", "lib/x.py", "ls -l lib/x.py", "standard",
                          triage.DEFAULT_ACCEPT + "; " + triage.COMMIT_ACCEPT))

    def test_a_valid_classifier_verify_is_kept(self):
        self.handle(self.PROMPT, stub(dict(DIRECT, verify="git diff --stat -- README.md")))
        self.assertEqual(brief.find(registry.load(), self.only_unit()).fields["VERIFY"],
                         "git diff --stat -- README.md")

    def test_scope_paths_stay_relative_and_inside_the_worktree(self):
        """M2: absolute, `~`, `..` and escaping paths are dropped from SCOPE and VERIFY; nothing left -> `.`."""
        (self.wt / "out").symlink_to(self.root)
        d = triage.Decision("direct", 4, "fix this: Error: Cannot find module './utils/helpers.js' at "
                                         "/Users/x/p/node_modules/a/index.js:12 and ../other/b.py, ~/c.md, "
                                         "src/../../d.py, out/e.py", division="build", tier="mechanical")
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), ("utils/helpers.js", "ls -l utils/helpers.js"))
        d.scope = ["/etc/passwd", "../x.md", "out/f.md"]          # classifier scope all bad: the prompt's paths
        self.assertEqual(triage.brief_fields(d, str(self.wt))["SCOPE"], "utils/helpers.js")
        d.scope, d.prompt = ["docs/a.md", "./b.md"], "x"
        self.assertEqual(triage.brief_fields(d, str(self.wt))["SCOPE"], "docs/a.md b.md")
        d.scope, d.prompt = [], "Add /etc/hosts and ../z.md to the list"
        fields = triage.brief_fields(d, str(self.wt))
        self.assertEqual((fields["SCOPE"], fields["VERIFY"]), (".", "git status --short"))
        self.assertEqual(triage.target_root(self.reg, "acme", "build"), str(self.wt))

    def test_a_failing_record_never_unblocks_a_started_run(self):
        """I2: once the runner started, the prompt is blocked even if recording the decision raises."""
        with mock.patch.object(triage, "record", side_effect=TypeError("bad row")):
            out = self.handle(self.PROMPT, stub(DIRECT))
        self.assertIsNotNone(out.block)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("triage: cannot record the decision: TypeError: bad row",
                      (self.home / "logs" / "hooks.log").read_text())

    def test_a_failing_feedback_pointer_never_unblocks_a_started_run(self):
        """I2: the sender's feedback pointer is written after the spawn; an OSError there is logged only."""
        real = dispatch.inbox.write

        def write(ptr, *a, **kw):
            if ptr.get("from") == "orche":
                raise OSError(28, "No space left on device")
            return real(ptr, *a, **kw)
        with mock.patch.object(dispatch.inbox, "write", side_effect=write):
            out = self.handle(self.PROMPT, stub(DIRECT))
        self.assertIsNotNone(out.block)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("dispatch: cannot file feedback for 'main-acme'", (self.home / "logs" / "hooks.log").read_text())

    def test_a_multiline_prompt_with_hash_lines_survives_as_the_goal(self):
        self.handle("[build] README.md 정리해\n# Install\n  keep this indented")
        goal = brief.find(registry.load(), self.only_unit()).fields["GOAL"]
        self.assertEqual(goal.splitlines()[:2], ["README.md 정리해", "# Install"])

    def test_a_long_prompt_gives_a_bounded_goal_and_the_whole_prompt_in_context(self):
        """M8: GOAL is an excerpt of at most GOAL_EXCERPT characters plus the pointer; CONTEXT quotes
        every line of the prompt (blank lines too), so the brief loses nothing."""
        body = " ".join(f"step{i} README.md 정리해" for i in range(40))
        prompt = f"[build] {body}\n\n# keep this heading\n  {SECRET} stays where it was typed"
        out = self.handle(prompt)
        unit = self.only_unit()
        fields = brief.find(registry.load(), unit).fields
        rest = prompt[len("[build] "):]
        excerpt, pointer = fields["GOAL"].rsplit(" (", 1)
        self.assertEqual(pointer, triage.GOAL_POINTER + ")")
        self.assertLessEqual(len(excerpt), triage.GOAL_EXCERPT)
        self.assertTrue(excerpt.endswith("…") and body.startswith(excerpt[:-1]), excerpt)
        self.assertEqual(fields["CONTEXT"].split("\n"),
                         [triage.CONTEXT_QUOTED] + [f"> {ln}" if ln.strip() else ">" for ln in rest.splitlines()])
        # The block reason shows the GOAL's first REASON_GOAL characters, masked.
        reason = out.block["reason"]
        self.assertIn(f'(runner-started, TIER standard): "{triage.excerpt(fields["GOAL"], triage.REASON_GOAL)}"',
                      reason)
        self.assertNotIn(SECRET, reason)

    def test_goal_bound_is_inclusive_and_short_prompts_stay_verbatim(self):
        d = triage.Decision("direct", 1, "x" * triage.GOAL_CHARS, division="build", tier="standard")
        self.assertEqual(triage.goal_and_context(d), (d.prompt, triage.CONTEXT))
        d.prompt = "add README.md\n# Install\n  keep"                     # multi-line but short: verbatim
        self.assertEqual(triage.goal_and_context(d), (d.prompt, triage.CONTEXT))
        d.prompt = "y" * (triage.GOAL_CHARS + 1)
        goal, context = triage.goal_and_context(d)
        self.assertEqual(goal, "y" * (triage.GOAL_EXCERPT - 1) + f"… ({triage.GOAL_POINTER})")
        self.assertEqual(context, f"{triage.CONTEXT_QUOTED}\n> {d.prompt}")
        d.goal = "Tidy the install section"
        self.assertEqual(triage.goal_and_context(d), (d.goal, f"{triage.CONTEXT_QUOTED}\n> {d.prompt}"))
        self.assertEqual(triage.block_reason("div-a-build", "u-1", "runner-started", "standard"),
                         "[orche] routed to div-a-build as u-1 (runner-started, TIER standard). Results arrive in "
                         "this inbox; prefix the prompt with [main] to talk to Main instead (to cancel: "
                         "[main] STOP u-1).")
        masked = triage.block_reason("div-a-build", "u-1", "runner-started", "standard", f"use {SECRET} in a.md")
        self.assertNotIn(SECRET, masked)
        self.assertIn('(runner-started, TIER standard): "use ', masked)

    def test_a_blocked_prompt_touches_mains_activity(self):
        """M10: a blocked prompt never reaches inbox.handle_prompt, so triage touches Main's activity."""
        path = units.activity_file("main-acme")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (1_000_000, 1_000_000))
        self.assertIsNotNone(self.handle(self.PROMPT, stub(DIRECT)).block)
        self.assertGreater(path.stat().st_mtime, time.time() - 60)
        # Not blocked: triage leaves it to the inbox path (which touches it on every prompt).
        os.utime(path, (1_000_000, 1_000_000))
        self.assertIsNone(self.handle(self.PROMPT, stub(DIRECT), popen=mock.Mock(side_effect=OSError("x"))).block)
        self.assertEqual(path.stat().st_mtime, 1_000_000)

    def test_spawn_failure_does_not_block_and_injects_one_line(self):
        out = self.handle(self.PROMPT, stub(DIRECT), popen=mock.Mock(side_effect=OSError("no exec")))
        unit = self.only_unit()
        self.assertIsNone(out.block)
        self.assertEqual(out.note, f"[orche] triage could not route this prompt: {unit} was filed for "
                                   "div-acme-build but the runner did not start (runner-failed)")
        self.assertIn("triage session=main-acme decision=main stage=5", self.events())

    def test_refused_relay_does_not_block(self):
        out = self.handle("[gtm] add a tagline to landing.md")     # acme has no gtm division
        self.assertIsNone(out.block)
        self.assertTrue(out.note.startswith("[orche] triage could not route this prompt: unknown TO 'gtm'"),
                        out.note)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.samples()[-1]["decision"], "main")

    def test_errors_inside_triage_fail_open(self):
        with mock.patch.object(triage, "decide", side_effect=RuntimeError("boom")):
            out = self.handle(self.PROMPT, stub(DIRECT))
        self.assertEqual((out.block, out.note), (None, ""))
        self.assertIn("triage: RuntimeError: boom", (self.home / "logs" / "hooks.log").read_text())

    def test_no_classifier_path_adds_under_20_ms(self):
        self.handle("Let's plan the Q4 roadmap together")          # warm imports and files
        for prompt in ("Let's plan the Q4 roadmap together", "what changed?", "[main] add a.md", "/clear"):
            started = time.perf_counter()
            self.handle(prompt)
            self.assertLess(time.perf_counter() - started, 0.020, prompt)


class RecordTest(TriageCase):
    def test_every_decision_logs_one_events_line_and_a_masked_sample(self):
        prompts = ["[main] add a.md", "/clear", "what changed?", "Let's plan the Q4 roadmap together",
                   f"Add {SECRET} to README.md"]
        for p in prompts:
            self.handle(p, stub(dict(DIRECT, confidence=0.5)))
        lines = [l for l in self.events().splitlines() if "\ttriage session=" in l]
        self.assertEqual([l.split("decision=")[1].split()[0] for l in lines], ["tag", "bypass", "bypass", "main", "main"])
        rows = self.samples()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["confidence"], 0.5)
        raw = (self.home / "triage" / "samples.jsonl").read_text()
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("README", raw)                       # no prompt text at all by default
        self.assertNotIn("prompt", rows[-1])
        self.assertEqual(len(rows[-1]["prompt_sha"]), 16)
        self.assertEqual(oct((self.home / "triage" / "samples.jsonl").stat().st_mode & 0o777), "0o600")

    def test_keep_prompts_stores_the_masked_prompt(self):
        self.enable(keep_prompts=True)
        self.handle(f"Add {SECRET} to README.md", stub(None))
        row = self.samples()[-1]
        self.assertEqual(row["prompt"], "Add [REDACTED] to README.md")


class HookTest(TriageCase):
    """The UserPromptSubmit hook as a subprocess: block JSON, or the inbox text plus the triage note."""

    def prompt(self, text, **env):
        payload = {"session_id": "sess-main-0001", "transcript_path": str(self.root / "none.jsonl"),
                   "cwd": str(self.repo), "hook_event_name": "UserPromptSubmit", "prompt": text}
        return self.run_hook("report_inject.py", payload, ORCHE_SESSION="main-acme", **env)

    def test_tagged_prompt_is_blocked_and_the_run_completes(self):
        self.reply("STATUS: done\nadded the line")
        self.commit_action(verdict=None)        # the worker commits, as the brief's ACCEPT asks
        started = time.monotonic()
        res = self.prompt("[build] add a line to README")
        self.assertLess(time.monotonic() - started, 3.0 + 1.5)
        self.assertEqual(res.returncode, 0, res.stderr)
        unit = self.only_unit()
        out = json.loads(res.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn(f"routed to div-acme-build as {unit} (runner-started, TIER standard)", out["reason"])
        record = self.units_dir() / unit / "RUN.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (record.exists() and not list(
                (self.home / "runs" / "acme").glob("*.lock"))):
            time.sleep(0.1)
        # Build is gated: the committed `done` stays `info` until a verifier records the ledger row
        # (writer != judge); the worktree is clean for the next Build unit.
        head = self.rev(self.wt)
        self.assertNotEqual(head, self.head)
        self.assertEqual(json.loads(record.read_text())["status"], "info")
        [summary] = [p["summary"] for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        self.assertIn(f"unverified: STATUS: done (no ledger row for {head[:7]})", summary)
        got = brief.find(registry.load(), unit).fields
        self.assertEqual(got["FORBID"], "push, amend, --no-verify, files outside SCOPE")
        self.assertTrue(got["ACCEPT"].endswith("; " + triage.COMMIT_ACCEPT), got["ACCEPT"])

    def test_untriaged_prompt_still_gets_the_inbox(self):
        from orche import inbox
        inbox.write(inbox.make("acme-a-20261016-1", "div-acme-build", "main-acme", "failed", summary="broke"))
        res = self.prompt("Let's plan the Q4 roadmap together")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("## Inbox (1)", res.stdout)
        self.assertIn("triage session=main-acme decision=main stage=3", self.events())

    def test_a_routed_prompt_rearms_the_wake(self):
        """C9 L8: Main spends no turn on a routed prompt, so the hook re-arms the wake for reports that
        are pending; a prompt that reaches Main does not (its own prompt injects them)."""
        import importlib.util
        import io
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location("report_inject_under_test",
                                                      support.ROOT / "hooks" / "report_inject.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)

        def fake_popen(argv, **kw):
            return mock.Mock(pid=4242) if argv[:3] == SPAWN else REAL_POPEN(argv, **kw)

        def run(prompt):
            payload = {"session_id": "sess-main-0001", "transcript_path": str(self.root / "none.jsonl"),
                       "cwd": str(self.repo), "hook_event_name": "UserPromptSubmit", "prompt": prompt}
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"ORCHE_SESSION": "main-acme"}), \
                    mock.patch.object(procs.subprocess, "Popen", fake_popen), \
                    mock.patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch.object(inbox, "rearm") as rearm, redirect_stdout(out):
                self.assertEqual(hook.main(), 0)
            return out.getvalue(), rearm

        marker = inbox.box("main-acme") / inbox.NUDGED
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("x")
        out, rearm = run("[build] add a line to README")
        self.assertEqual(json.loads(out)["decision"], "block")
        rearm.assert_called_once_with("main-acme")
        self.assertFalse(marker.exists())            # every prompt removes the wake marker, a routed one too
        out, rearm = run("Let's plan the Q4 roadmap together")
        rearm.assert_not_called()

    def test_relay_failure_note_is_injected_with_the_inbox(self):
        res = self.prompt("[gtm] add a tagline to landing.md")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.startswith("[orche] triage could not route this prompt: unknown TO 'gtm'"),
                        res.stdout)


class ConfigDefaultsTest(support.TempHomeCase):
    def test_triage_defaults(self):
        self.assertEqual(config.load()["triage"], {"enabled": False, "backend": "auto", "min_confidence": 0.8,
                                                   "max_latency_s": 12, "keep_prompts": False})
        self.assertEqual(config.load()["jev"]["timeout_s"], 1.0)      # Jev keeps its own 1 s
        cfg = config.load()
        self.assertEqual((triage.min_confidence(cfg), triage.max_latency(cfg)), (0.8, 12.0))
        cfg["triage"].update(min_confidence="high", max_latency_s=-1)    # bad values fall back
        self.assertEqual((triage.min_confidence(cfg), triage.max_latency(cfg)), (0.8, 12.0))


LABELLED = [("Add an install section to README.md", "direct"), ("docs/a.md 오타 수정해줘", "direct"),
            ("Let's plan the Q4 roadmap together", "main"), ("what changed in docs/a.md?", "main"),
            ("Fix the flaky test in tests/test_x.py", "main"), ("Rename `fooBar` in utils", "direct")]


class CommandTest(TriageCase):
    def orche_ok(self, *argv):
        code, out, err = self.orche(*argv)
        self.assertEqual(code, 0, err)
        return out

    def test_enable_disable_and_dry_run(self):
        self.config({"claude_bin": str(self.claude)})
        self.assertEqual(self.orche_ok("--dry-run", "triage", "enable"), "would set triage.enabled=true\n")
        self.assertFalse(config.load()["triage"]["enabled"])
        self.assertEqual(self.orche_ok("triage", "enable"), "set triage.enabled=true\n")
        self.assertTrue(config.load()["triage"]["enabled"])
        self.assertEqual(json.loads((self.home / "config.json").read_text())["claude_bin"], str(self.claude))
        self.orche_ok("triage", "disable")
        self.assertFalse(config.load()["triage"]["enabled"])
        self.config({"delivery": "pane"})
        self.assertIn("stays inert", self.orche_ok("triage", "enable"))

    def test_status_prints_backend_latency_and_the_last_20_decisions(self):
        for i in range(22):
            self.handle(f"Add section {i} to README.md", stub(dict(DIRECT, confidence=0.5), secs=(i + 1) / 1000))
        self.handle("what changed?")
        data = json.loads(self.orche_ok("--json", "triage", "status"))
        self.assertEqual((data["enabled"], data["backend"], data["resolved_backend"]), (True, "claude-haiku",
                                                                                         "claude-haiku"))
        self.assertEqual((data["samples"], data["classifier_calls"], len(data["last"])), (23, 22, 20))
        self.assertEqual((data["p50_ms"], data["p95_ms"]), (11, 21))
        self.assertEqual(data["last"][-1]["decision"], "bypass")
        text = self.orche_ok("triage", "status")
        self.assertIn("triage: enabled", text)
        self.assertIn("latency: 22 classifier calls, p50 11 ms, p95 21 ms", text)
        self.assertIn("last 20 of 23 decisions:", text)

    def test_status_with_no_samples(self):
        self.assertIn("no decisions yet", self.orche_ok("triage", "status"))

    def labelled(self, rows=LABELLED, name="labels.jsonl"):
        path = self.root / name
        path.write_text("".join(json.dumps({"prompt": p, "label": l}, ensure_ascii=False) + "\n" for p, l in rows))
        return str(path)

    def test_eval_heuristics_precision_and_recall(self):
        data = json.loads(self.orche_ok("--json", "triage", "eval", self.labelled(), "--backend", "heuristics"))
        r = data["models"]["heuristics"]["direct"]
        # Candidates: README.md (tp), docs/a.md (tp), tests/test_x.py (fp), `fooBar` (tp).
        self.assertEqual((r["n"], r["tp"], r["fp"], r["fn"], r["tn"]), (6, 3, 1, 0, 2))
        self.assertEqual((r["precision"], r["recall"]), (0.75, 1.0))
        text = self.orche_ok("triage", "eval", self.labelled(), "--backend", "heuristics")
        self.assertIn("direct: n 6 precision 0.750 recall 1.000 tp 3 fp 1 fn 0 tn 2", text)

    def test_eval_with_the_haiku_backend_uses_the_classifier(self):
        (self.fake / "reply.json").write_text(haiku_reply(DIRECT))
        data = json.loads(self.orche_ok("--json", "triage", "eval", self.labelled()))
        r = data["models"]["claude-haiku"]["direct"]
        self.assertEqual((r["tp"], r["fp"], r["classifier_calls"]), (3, 1, 4))
        out = self.orche_ok("--dry-run", "triage", "eval", self.labelled())
        self.assertEqual(out, "would make 4 claude-haiku classifier calls for 6 labelled prompts\n")

    def test_status_shows_the_eval_on_file_and_when_it_last_ran(self):
        """M9: eval.json records each run's summary per backend (no prompts); status shows it."""
        data = json.loads(self.orche_ok("--json", "triage", "status"))
        self.assertEqual((data["eval"]["last_run"], data["eval"]["resolved_evaluated"]), (None, False))
        self.assertIn("eval: never run (no labelled eval set on file", self.orche_ok("triage", "status"))
        self.orche_ok("--dry-run", "triage", "eval", self.labelled(), "--backend", "heuristics")
        self.assertFalse((self.home / "triage" / "eval.json").exists())        # a dry run records nothing
        path = self.labelled()
        self.orche_ok("triage", "eval", path, "--backend", "heuristics")
        saved = json.loads((self.home / "triage" / "eval.json").read_text())
        self.assertEqual(saved["last"], "heuristics")
        run = saved["runs"]["heuristics"]
        self.assertEqual((run["file"], run["n"], run["precision"], run["recall"]),
                         (str(Path(path).resolve()), 6, 0.75, 1.0))
        self.assertNotIn("prompt", json.dumps(saved))
        text = self.orche_ok("triage", "status")
        self.assertIn(f"eval: last run {run['at']} (heuristics, n 6, precision 0.750 recall 1.000) on "
                      f"{run['file']} (present)", text)
        self.assertIn("eval: none on file for the claude-haiku backend", text)
        Path(path).unlink()
        self.assertIn(f"on {run['file']} (missing)", self.orche_ok("triage", "status"))
        (self.fake / "reply.json").write_text(haiku_reply(DIRECT))
        self.orche_ok("triage", "eval", self.labelled())
        data = json.loads(self.orche_ok("--json", "triage", "status"))["eval"]
        self.assertEqual((data["last_backend"], data["resolved_evaluated"], data["set_exists"]),
                         ("claude-haiku", True, True))
        self.assertEqual(set(json.loads((self.home / "triage" / "eval.json").read_text())["runs"]),
                         {"heuristics", "claude-haiku"})

    def test_partial_eval_file_neither_crashes_status_nor_hides_the_triage_line(self):
        """0.3.4 review M-10: a run entry without `file`/`at`/`n` is shown with `?`, not a KeyError."""
        (self.home / "triage").mkdir(parents=True, exist_ok=True)
        (self.home / "triage" / "eval.json").write_text(json.dumps(
            {"last": "heuristics", "runs": {"heuristics": {"precision": 0.5}, "jev": "garbage"}}))
        text = self.orche_ok("triage", "status")
        self.assertIn("eval: last run ? (heuristics, n ?, precision 0.500 recall -) on ? (missing)", text)
        self.assertTrue(any(l.startswith("triage: enabled") for l in self.orche_ok("status").splitlines()))

    def test_jev_without_an_eval_is_visible_in_status_enable_and_orche_status(self):
        """M9: triage on Jev with no Jev eval on file warns (SPEC §7.3 condition 5)."""
        self.assertEqual(self.orche_ok("status").splitlines()[-2], "triage: enabled, backend claude-haiku, "
                                                                   "no eval on file")
        cfg = self.enable(backend="auto")
        cfg["jev"] = {"enabled": True, "base_url": "https://jev.test"}
        self.config(cfg)
        warning = "warning: triage is enabled on the Jev backend with no Jev eval on file"
        with mock.patch.dict(os.environ, {"JEV_API_KEY": "k"}):
            self.assertIn(warning, self.orche_ok("triage", "status"))
            self.assertIn(warning, self.orche_ok("triage", "enable"))
            line = self.orche_ok("status").splitlines()[-2]
            self.assertTrue(line.startswith("triage: enabled, backend jev, no eval on file · " + warning), line)
            from orche import cmd_triage
            cmd_triage.save_eval(self.labelled(), "jev", cmd_triage.report(
                [{"label": True, "direct": True, "model_ms": 5, "called": True}], "jev"))
            self.assertNotIn(warning, self.orche_ok("triage", "status"))
            self.assertNotIn(warning, self.orche_ok("triage", "enable"))
            self.assertRegex(self.orche_ok("status").splitlines()[-2],
                             r"^triage: enabled, backend jev, last eval \S+ \(jev\)$")
        self.orche_ok("triage", "disable")
        self.assertFalse(any(l.startswith("triage:") for l in self.orche_ok("status").splitlines()))

    def test_eval_rejects_bad_files(self):
        bad = self.root / "bad.jsonl"
        bad.write_text('{"prompt": "x", "label": "maybe"}\n')
        code, _out, err = self.orche("triage", "eval", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("line 1:", err)
        self.assertEqual(self.orche("triage", "eval", str(self.root / "missing.jsonl"))[0], 1)
        self.assertEqual(self.orche("triage", "eval", self.labelled(), "--project", "nope")[0], 1)
        self.assertEqual(self.orche("triage", "eval", self.labelled(), "--backend", "jev")[0], 1)


class MetricsSectionTest(TriageCase):
    def test_summary_counts_and_the_rendered_section(self):
        from orche import metrics
        self.handle("Add an install section to README.md", stub(DIRECT, secs=0.004))
        self.handle("[main] add a.md")
        self.handle("what changed?")
        self.handle("Let's plan the Q4 roadmap together")
        self.handle("Update CHANGELOG.md for 0.3.0", stub(dict(DIRECT, confidence=0.2), secs=0.002))
        s = metrics.triage_summary(triage.samples_path(), days=30)
        self.assertEqual({k: s[k] for k in ("prompts", "direct", "main", "bypass", "classifier_calls",
                                             "mean_classifier_ms", "main_turns_saved")},
                         {"prompts": 5, "direct": 1, "main": 3, "bypass": 1, "classifier_calls": 2,
                          "mean_classifier_ms": 3, "main_turns_saved": 1})
        old = metrics.triage_summary(triage.samples_path(), days=30,
                                     now=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(old["prompts"], 0)
        out = self.orche_ok_metrics()
        self.assertIn("Triage (Main prompts, triage/samples.jsonl):", out)
        self.assertRegex(out, r"  prompts 5, direct 1, main 3, bypass 1, mean latency \d+ ms \(2 classifier "
                              r"calls, mean 3 ms\), Main turns saved ≈ 1")

    def orche_ok_metrics(self):
        code, out, err = self.orche("metrics", "--root", str(self.root / "no-transcripts"))
        self.assertEqual(code, 0, err)
        return out
