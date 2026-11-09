import importlib.util
import io
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import support
import orche

SCRIPT = support.ROOT / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("orche_release_script", SCRIPT)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

BULLET = re.compile(r"^- \*\*([^*]+)\*\* \((\d{4}-\d{2}-\d{2})\): ")
PINNED = ["lib/orche/__init__.py", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
          "tests/test_registry.py", "tests/test_pyguard.py"]


def heading_dates(changelog_text):
    return dict(re.findall(r"^## (\S+) — (\d{4}-\d{2}-\d{2})$", changelog_text, re.M))


class ReadmeWhatsNewTest(unittest.TestCase):
    def test_readme_lists_the_current_version_first(self):
        _, bullets, _ = release.whats_new((support.ROOT / "README.md").read_text())
        self.assertTrue(1 <= len(bullets) <= 3, bullets)
        parsed = [BULLET.match(b) for b in bullets]
        self.assertTrue(all(parsed), bullets)
        dates = heading_dates((support.ROOT / "CHANGELOG.md").read_text())
        first_version, first_date = parsed[0].groups()
        self.assertEqual(first_version, orche.__version__,
                         "README's what's new must lead with the shipped version")
        self.assertEqual(first_date, dates.get(first_version))
        for m in parsed:
            self.assertIn(m.group(1), dates)


class BumpTest(unittest.TestCase):
    def test_bump_on_a_copy(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            for rel in PINNED + ["README.md"]:
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(support.ROOT / rel, tmp / rel)
            chlog = (support.ROOT / "CHANGELOG.md").read_text()
            if "## Unreleased" not in chlog:
                first = chlog.index("\n## ") + 1
                chlog = chlog[:first] + "## Unreleased\n\n### Added\n\n- a change\n\n" + chlog[first:]
            (tmp / "CHANGELOG.md").write_text(chlog)
            with redirect_stdout(io.StringIO()):
                release.bump(tmp, "9.9.9", "a summary", date(2030, 1, 2))
            self.assertEqual(release.current_version(tmp), "9.9.9")
            for rel in PINNED:
                self.assertIn("9.9.9", (tmp / rel).read_text(), rel)
            text = (tmp / "CHANGELOG.md").read_text()
            self.assertIn("\n## 9.9.9 — 2030-01-02\n", text)
            self.assertNotIn("## Unreleased", text)
            _, bullets, _ = release.whats_new((tmp / "README.md").read_text())
            self.assertTrue(bullets[0].startswith("- **9.9.9** (2030-01-02): a summary. [Notes]("), bullets[0])
            self.assertLessEqual(len(bullets), 3)


class NotesTest(unittest.TestCase):
    def test_notes_of_the_current_version(self):
        body = release.notes(support.ROOT, orche.__version__)
        self.assertTrue(body.strip())
        self.assertFalse(any(line.startswith("## ") for line in body.splitlines()))
        self.assertEqual(body, body.strip("\n"))

    def test_unknown_version_exits(self):
        with self.assertRaises(SystemExit):
            release.notes(support.ROOT, "0.0.0-none")


if __name__ == "__main__":
    unittest.main()
