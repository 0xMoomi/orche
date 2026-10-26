"""Template files and the templates.py copy/render helpers."""
import re
import unittest

import support
from orche import templates


class TemplatesTest(support.TempHomeCase):
    def test_root_and_render(self):
        self.assertEqual(templates.TEMPLATES_ROOT, support.ROOT / "templates")
        self.assertEqual(templates.render("# {{PROJECT}} / {{OTHER}}", PROJECT="acme"), "# acme / {{OTHER}}")

    def test_copy_unit_creates_four_files(self):
        dest = self.home / ".orche" / "feat" / "checkout"
        created = templates.copy_unit(dest, "feature", SESSION="feat-acme-checkout")
        self.assertEqual(sorted(p.name for p in created),
                         ["CHARTER.md", "DECISIONS.tsv", "HANDOFF.md", "STATUS.md"])
        charter = (dest / "CHARTER.md").read_text()
        self.assertIn("# CHARTER: checkout (feature)", charter)
        self.assertIn("feat-acme-checkout", charter)
        for section in ("## Scope (globs)", "## Interfaces", "## Forbidden", "## Owner session"):
            self.assertIn(section, charter)
        handoff = (dest / "HANDOFF.md").read_text()
        self.assertEqual(re.findall(r"^## .*$", handoff, re.M),
                         ["## Intent", "## Done & verified", "## In flight", "## Next", "## Files",
                          "## Gotchas", "## Resume order"])
        status = (dest / "STATUS.md").read_text().splitlines()
        self.assertEqual([l.split(":")[0] for l in status],
                         ["State", "Current unit", "Last verified", "Blockers", "Next"])
        self.assertEqual((dest / "DECISIONS.tsv").read_text(), "ts\tphase\tdecision\twhy\tevidence\tresult\n")
        (dest / "STATUS.md").write_text("State: busy\n")
        self.assertEqual(templates.copy_unit(dest, "feature"), [])
        self.assertEqual((dest / "STATUS.md").read_text(), "State: busy\n")
        with self.assertRaises(ValueError):
            templates.copy_unit(dest, "squad")

    def test_copy_project_skips_existing(self):
        repo = self.home / "acme"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "RUBRIC.md").write_text("mine\n")
        created = templates.copy_project(repo, PROJECT="acme")
        self.assertEqual(sorted(str(p.relative_to(repo)) for p in created),
                         [".env.example", ".orche/STANDING.md", "PROJECT_CARD.md", "docs/GRAVEYARD.md",
                          "docs/LEDGER.tsv"])
        self.assertFalse((repo / ".claude").exists())  # *.tmpl files are rendered per worktree, not copied
        self.assertEqual((repo / "docs" / "RUBRIC.md").read_text(), "mine\n")
        self.assertEqual((repo / "docs" / "LEDGER.tsv").read_text(), "pr\tsha\tverdict\tevidence\tverifier\tts\n")
        graveyard = (repo / "docs" / "GRAVEYARD.md").read_text()
        self.assertIn("# GRAVEYARD: acme", graveyard)
        self.assertIn("| date | what | why | recovery | actor |", graveyard)
        self.assertEqual(templates.copy_project(repo), [])

    def test_rubric_and_verify_skill_templates(self):
        root = templates.TEMPLATES_ROOT
        rubric = (root / "project" / "docs" / "RUBRIC.md").read_text()
        self.assertEqual(len(re.findall(r"^- \[ \] ", rubric, re.M)), 5)
        skill = (root / "verify-skill" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: verify-{{APP}}\ndescription: "))
        for section in ("## Launch", "## Doctor", "## Drive", "## Evidence", "## Cleanup"):
            self.assertIn(f"\n{section}\n", skill)
        for rule in ("Never edit product code.", "Cleanup never deletes Evidence.",
                     "At most one PR per maintenance run."):
            self.assertIn(rule, skill)
        features = (root / "verify-skill" / "features" / "README.md").read_text()
        for section in ("## Entry", "## Steps", "## Expected", "## Evidence"):
            self.assertIn(section, features)
        self.assertTrue((root / "verify-skill" / "references" / "rubric.example.md").is_file())
        for path in root.rglob("*"):
            if path.is_file():
                self.assertNotIn("/" + "Users/", path.read_text(), str(path))


class EnvExampleTemplateTest(unittest.TestCase):
    def test_ships_without_seeded_secret_names(self):
        text = (support.ROOT / "templates" / "project" / ".env.example").read_text()
        names = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(names, [])
        for seeded in ("DATABASE_URL=", "STRIPE_SECRET_KEY=\n", "GOOGLE_API_KEY="):
            self.assertNotIn("\n" + seeded, text)


class PromptTemplatesTest(unittest.TestCase):
    """N12: the prompt texts runs/job.py, ladder.py and queue/integrator.py send live in templates/run and
    templates/queue; a two-way text is one template plus its two small variants."""
    PROMPTS = {"run/footer.md": {"WORKTREE", "COMMIT"}, "run/commit.md": set(), "run/commit-gated.md": set(),
               "run/codex-sandbox.md": {"WORKTREE", "UNITS", "HOME"},
               "run/escalation.md": {"MODEL", "EFFORT", "OUTCOME", "TAIL"},
               "queue/conflict.md": {"BASE", "FILES"}, "queue/red.md": {"BASE", "STEP", "LOG", "TAIL", "LAST"},
               "queue/red-last.md": set(), "queue/timeout-last.md": set()}

    def test_each_prompt_template_names_exactly_the_variables_its_code_passes(self):
        """render() leaves an unknown placeholder as it is, so a misspelt one would reach the model."""
        for rel, names in self.PROMPTS.items():
            text = (templates.TEMPLATES_ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(set(re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}", text)), names, rel)
            self.assertNotIn("{{", templates.load(rel, **{n: "v" for n in names}), rel)

    def test_load_renders_a_template_without_its_last_newline(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.md"
            path.write_text("a {{X}} {{Y}}\n\n", encoding="utf-8")
            self.assertEqual(templates.load(path, X="{{Y}}"), "a {{Y}} {{Y}}\n")
