"""Agent definitions, persona templates, skills, workflow/CI templates and the SessionStart role card."""
import importlib.util
import re
import shutil
import subprocess
import unittest

import support
import test_hooks
from orche import templates

AGENTS = support.ROOT / "agents"
PERSONAS = support.ROOT / "templates" / "personas"
SKILLS = support.ROOT / "skills"
MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "{{MODEL}}"}
EFFORTS = {"low", "medium", "high", "max", "{{EFFORT}}"}
ALWAYS_PAUSE = ("force-push to a shared branch", "deploy", "data deletion", "customer messages",
                "external publication", "payments")
INJECT = re.compile(r"<!-- inject:start -->\n(.*?)\n<!-- inject:end -->", re.S)
VARS = dict(PROJECT="acme", NAME="build", SESSION="div-acme-build", MODEL="claude-opus-5-5", EFFORT="high")


def frontmatter(text):
    """{key: value} from a leading `---` block of single-line `key: value` pairs (None if absent)."""
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return None
    out = {}
    for line in lines[1:lines.index("---", 1)]:
        key, sep, value = line.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise ValueError(f"bad frontmatter line: {line!r}")
        out[key] = value.strip()
    return out


class AgentFilesTest(unittest.TestCase):
    def test_every_agent_has_valid_frontmatter(self):
        files = sorted(AGENTS.glob("*.md"))
        self.assertEqual([p.stem for p in files], ["deep-reasoner", "exec", "hr", "liaison", "runner"])
        for path in files:
            fm = frontmatter(path.read_text())
            self.assertIsNotNone(fm, path.name)
            for key in ("name", "description", "model", "effort"):
                self.assertTrue(fm.get(key), f"{path.name}: {key}")
            self.assertEqual(fm["name"], path.stem)
            self.assertIn(fm["model"], MODELS - {"{{MODEL}}"}, path.name)
            self.assertIn(fm["effort"], EFFORTS - {"{{EFFORT}}"}, path.name)
            self.assertIn(fm.get("memory", "project"), ("project", "user"), path.name)

    def test_agent_values(self):
        fm = {p.stem: frontmatter(p.read_text()) for p in AGENTS.glob("*.md")}
        self.assertEqual((fm["exec"]["model"], fm["exec"]["effort"], fm["exec"]["autoCompactWindow"],
                          fm["exec"]["maxTurns"]), ("claude-sonnet-5-5", "medium", "200000", "60"))
        self.assertNotIn("tools", fm["exec"])
        self.assertEqual((fm["liaison"]["effort"], fm["liaison"]["memory"]), ("low", "project"))
        self.assertEqual((fm["hr"]["model"], fm["hr"]["effort"], fm["hr"]["memory"]), ("claude-sonnet-5-5", "low", "user"))
        self.assertEqual((fm["deep-reasoner"]["model"], fm["deep-reasoner"]["effort"]), ("claude-opus-5-5", "max"))
        self.assertEqual((fm["runner"]["model"], fm["runner"]["effort"], fm["runner"]["tools"]),
                         ("claude-haiku-5-5", "low", "Bash, Read, Grep, Glob"))


class PersonaTemplatesTest(unittest.TestCase):
    def test_eight_personas_with_placeholder_frontmatter(self):
        names = sorted(p.name for p in PERSONAS.glob("*.md"))
        self.assertEqual(names, sorted(["main.md", "liaison.md", "div-rnd.md", "div-research.md", "div-build.md",
                                        "div-gtm.md", "div-ops.md", "feature.md"]))
        for name in names:
            fm = frontmatter((PERSONAS / name).read_text())
            self.assertEqual(fm["name"], "liaison" if name == "liaison.md" else "{{SESSION}}", name)
            self.assertIn(fm["model"], MODELS, name)
            self.assertIn(fm["effort"], EFFORTS, name)
            self.assertEqual((fm["model"], fm["effort"], fm["memory"]), ("{{MODEL}}", "{{EFFORT}}", "project"), name)
            self.assertTrue(fm["description"], name)

    def test_rendered_personas_carry_the_rules(self):
        for path in sorted(PERSONAS.glob("*.md")):
            text = templates.render(path.read_text(), **VARS)
            self.assertNotIn("{{", text, path.name)
            self.assertIn("acme", text, path.name)
            self.assertIn("## Always-pause", text, path.name)
            for item in ALWAYS_PAUSE:
                self.assertIn(item, text, f"{path.name}: {item}")
            self.assertIn("## Memory-save before done", text, path.name)
            self.assertRegex(text, r"Before claiming done, write STATUS\.md and DECISIONS\.tsv", path.name)
            self.assertIn("never `/clear`", text, path.name)
            self.assertIn("## Ten principles", text, path.name)
            self.assertIn("10 Encode lessons in structure", text, path.name)
            self.assertIn("## Report format", text, path.name)
            self.assertIn("## Dispatch and inbox", text, path.name)
            self.assertNotRegex(text, r"STATUS: [^`]*stopped|inbox pointer JSON in|orche/inbox/", path.name)
            if path.name != "main.md":
                self.assertIn("orche report ", text, path.name)
        unit = templates.render((PERSONAS / "feature.md").read_text(), **VARS)
        for part in (".orche/units/<unit>/BRIEF.md", "orche report stop", "Never hand-write pointer JSON",
                     "Never `/clear` mid-unit", "STATUS: done | blocked | failed | stop`"):
            self.assertIn(part, unit)
        liaison = templates.render((PERSONAS / "liaison.md").read_text(), **VARS)
        self.assertIn("`.orche/liaison/` folder plus PROJECT_CARD.md", liaison)
        self.assertIn("--file .orche/liaison/DECISIONS.tsv", liaison)
        main = templates.render((PERSONAS / "main.md").read_text(), **VARS)
        self.assertIn("Understood: source = <project>, output = <project>", main)

    def test_onboarding_picks_the_division_template(self):
        from orche.hr import onboarding
        text, from_template = onboarding.persona_text("division", project="acme", name="build",
                                                      session="div-acme-build", model="claude-opus-5-5",
                                                      effort="high")
        self.assertTrue(from_template)
        self.assertTrue(text.startswith("---\nname: div-acme-build\n"))
        self.assertIn("# Build division (build) — acme", text)


class SkillsTest(unittest.TestCase):
    def test_skills_have_frontmatter_and_a_short_inject_block(self):
        self.assertEqual(sorted(p.name for p in SKILLS.iterdir() if p.is_dir()), ["orche", "orche-build", "orche-main"])
        for skill in ("orche", "orche-main", "orche-build"):
            text = (SKILLS / skill / "SKILL.md").read_text()
            fm = frontmatter(text)
            self.assertEqual(fm["name"], skill)
            self.assertTrue(fm["description"].startswith(("ORCHE help. Use", "Main role rulebook. Use",
                                                         "Build division rulebook. Use")), skill)
            blocks = INJECT.findall(text)
            self.assertEqual(len(blocks), 1, skill)
            self.assertLessEqual(len(blocks[0]), 1500, skill)
            self.assertIn("Always-pause", blocks[0], skill)
            self.assertNotIn("stopped", text, skill)
            if skill != "orche-main":
                self.assertIn("orche report <status>", blocks[0], skill)

    def test_rulebooks_cover_their_topics(self):
        main = (SKILLS / "orche-main" / "SKILL.md").read_text()
        for heading in ("## Dispatch grammar", "## Drain discipline", "## Question discipline",
                        "## Ketchup report format", "## Echo-back", "## Candor"):
            self.assertIn(heading, main)
        build = (SKILLS / "orche-build" / "SKILL.md").read_text()
        self.assertIn("Tags, releases and deploys need user approval (STANDING 5); Main approves integration.",
                      INJECT.search(build).group(1))
        self.assertIn("filed with `orche report done`", INJECT.search(build).group(1))
        from orche import inbox
        self.assertIn("Pointer statuses are exactly: " + ", ".join(inbox.STATUSES) + ".", build)
        for heading in ("## Ledger rules", "## Integration worktree", "## Review in a fresh session", "## Verify skill"):
            self.assertIn(heading, build)
        help_ = (SKILLS / "orche" / "SKILL.md").read_text()
        for part in ("## Commands", "## Cross-project requests", "`[project]`", "`[drain]`"):
            self.assertIn(part, help_)


def load_session_start():
    spec = importlib.util.spec_from_file_location("session_start_hook", support.ROOT / "hooks" / "session_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoleCardTest(test_hooks.HookCase):
    def build_payload(self):
        return self.payload("session_start_clear.json", cwd=str(self.root / "acme-build"))

    def test_build_and_main_cards_under_cap(self):
        out = self.run_hook("session_start.py", self.build_payload()).stdout
        card = INJECT.search((SKILLS / "orche-build" / "SKILL.md").read_text()).group(1)
        self.assertIn("## Role rules\n" + card, out)
        self.assertLessEqual(len(out.rstrip("\n")), 6000)
        main = self.run_hook("session_start.py", self.payload("session_start_clear.json", cwd=str(self.repo)),
                             ORCHE_SESSION="main-acme").stdout
        card = INJECT.search((SKILLS / "orche-main" / "SKILL.md").read_text()).group(1)
        self.assertIn("## Role rules\n" + card, main)
        self.assertLessEqual(len(main.rstrip("\n")), 6000)

    def test_card_dropped_first_under_pressure(self):
        unit = self.root / "acme-build" / ".orche" / "div" / "build"
        unit.mkdir(parents=True)
        status = "STATUS-TOP\n" + "".join(f"status line {i:03d} with padding text here\n" for i in range(135))
        (unit / "STATUS.md").write_text(status)
        (unit / "HANDOFF.md").write_text("HANDOFF-TOP\nnext: integrate\n")
        out = self.run_hook("session_start.py", self.build_payload()).stdout
        self.assertLessEqual(len(out.rstrip("\n")), 6000)
        self.assertNotIn("ORCHE Build rules", out)
        self.assertNotIn("## Role rules", out)
        for kept in ("STATUS-TOP", "status line 134", "HANDOFF-TOP", "## ORCHE role\nsession: div-acme-build"):
            self.assertIn(kept, out)
        self.assertTrue(out.rstrip("\n").endswith("[orche] truncated to 6000 chars: dropped Role rules (load the role skill)"),
                        out[-200:])

    def test_missing_markers_mean_no_card_and_a_log_line(self):
        hook = load_session_start()
        fake = self.home / "plugin"
        (fake / "skills" / "orche").mkdir(parents=True)
        (fake / "skills" / "orche" / "SKILL.md").write_text("---\nname: orche\n---\nno markers\n")
        hook.ROOT = str(fake)
        self.assertEqual(hook._role_card("orche"), [])
        self.assertEqual(hook._role_card("orche-build"), [])
        log = (self.home / "logs" / "hooks.log").read_text()
        self.assertIn("no inject block in skills/orche/SKILL.md", log)
        self.assertIn("no inject block in skills/orche-build/SKILL.md", log)


class FileTemplatesTest(unittest.TestCase):
    def test_ci_yaml_is_well_formed(self):
        ci = support.ROOT / "templates" / "github" / "ci.yml"
        text = ci.read_text()
        for part in ("cancel-in-progress: true", "[skip ci]", "lint", "unit", "type-check", "build"):
            self.assertIn(part, text)
        self.assertNotIn("secrets.", text)
        self.assertEqual(re.findall(r"^\s+- run: (.+)$", text, re.M), ["./scripts/verify/ci.sh"])
        self.assertIn("lint -> unit tests -> type-check -> build", text)
        self.assertIn("contents: read", text)
        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("ruby not on PATH; YAML well-formedness not checked")
        res = subprocess.run([ruby, "-ryaml", "-e", "YAML.load_file(ARGV[0])", str(ci)],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_large_job_meta_is_a_pure_literal(self):
        text = (support.ROOT / "templates" / "workflow" / "large-job.js").read_text()
        self.assertTrue(text.startswith("export const meta = {"))
        meta = text[len("export const meta = "):text.index("\n}\n") + 2]
        self.assertNotIn("(", meta)
        self.assertNotIn("${", meta)
        self.assertNotIn("`", meta)
        bare = re.sub(r"'[^'\n]*'", "''", meta)
        bare = re.sub(r"\b[A-Za-z_]\w*\s*:", "", bare)
        self.assertIsNone(re.search(r"[A-Za-z_$]", bare), bare)
        for part in ("pipeline(", "parallel(", "agent(", "phase: 'Verify'", "schema:", "return {"):
            self.assertIn(part, text)

    def test_brief_night_standing(self):
        brief = (support.ROOT / "templates" / "brief.md").read_text()
        self.assertTrue(brief.startswith("rev: 1\n"))
        keys = re.findall(r"^([A-Z]+) {2,}", brief, re.M)
        self.assertEqual(keys, ["TO", "GOAL", "SCOPE", "CONTEXT", "ACCEPT", "VERIFY", "TIMEBOX", "FORBID",
                                "REPORT", "TIER", "ROLLBACK"])
        night = (support.ROOT / "templates" / "night.md").read_text()
        self.assertEqual(re.findall(r"^## (.+)$", night, re.M), ["GOAL", "FINISH-PREDICATE", "PERMISSIONS", "ESCAPE"])
        standing = (support.ROOT / "templates" / "project" / ".orche" / "STANDING.md").read_text()
        self.assertTrue(standing.startswith("# STANDING ORDERS — {{PROJECT}}"))
        self.assertEqual([int(n) for n in re.findall(r"^(\d+)\. ", standing, re.M)], [1, 2, 3, 4, 5, 6])
        principles = (support.ROOT / "docs" / "principles.md").read_text()
        self.assertEqual(len(re.findall(r"^### \d+\. ", principles, re.M)), 10)
        self.assertEqual(principles.count("- **Applies when:**"), 10)
        self.assertEqual(principles.count("- **What it changes:**"), 10)


if __name__ == "__main__":
    unittest.main()
