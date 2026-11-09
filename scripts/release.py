#!/usr/bin/env python3
"""Release helper: keeps the version, CHANGELOG.md and the README's what's new list in step.

Usage:
  python3 scripts/release.py bump <new> "<one-line summary>"
      Moves the version in the five pinned places, turns `## Unreleased` in CHANGELOG.md into the
      new version's heading, and puts the summary at the top of the README's what's new list
      (the three newest are kept).
  python3 scripts/release.py notes <version>
      Prints that version's CHANGELOG.md section, for the GitHub release body.
"""
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = "https://github.com/0xMoomi/orche/releases/tag/v"
START = "<!-- whats-new: scripts/release.py keeps the three newest -->"
END = "<!-- /whats-new -->"
KEEP = 3


def current_version(root):
    text = (root / "lib/orche/__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not m:
        raise SystemExit("no __version__ in lib/orche/__init__.py")
    return m.group(1)


def sites(old, new):
    return {
        "lib/orche/__init__.py": (f'__version__ = "{old}"', f'__version__ = "{new}"'),
        ".claude-plugin/plugin.json": (f'"version": "{old}"', f'"version": "{new}"'),
        ".claude-plugin/marketplace.json": (f'"version": "{old}"', f'"version": "{new}"'),
        "tests/test_registry.py": (f"orche {old}\\n", f"orche {new}\\n"),
        "tests/test_pyguard.py": (f"orche {old}\\n", f"orche {new}\\n"),
    }


def whats_new(readme_text):
    """Return (before, bullets, after) around the README's what's new block."""
    if readme_text.count(START) != 1 or readme_text.count(END) != 1:
        raise SystemExit("README.md: the what's new markers must appear exactly once")
    before, rest = readme_text.split(START, 1)
    body, after = rest.split(END, 1)
    bullets = [line for line in body.splitlines() if line.startswith("- ")]
    return before, bullets, after


def bump(root, new, summary, today):
    root = Path(root)
    old = current_version(root)
    edits = sites(old, new)
    for rel, (a, _) in edits.items():
        n = (root / rel).read_text().count(a)
        assert n == 1, (rel, n)
    chlog = root / "CHANGELOG.md"
    ctext = chlog.read_text()
    assert ctext.count("## Unreleased") == 1, ("CHANGELOG.md", ctext.count("## Unreleased"))
    readme = root / "README.md"
    before, bullets, after = whats_new(readme.read_text())

    for rel, (a, b) in edits.items():
        p = root / rel
        p.write_text(p.read_text().replace(a, b))
        print(f"bumped {rel}: {old} -> {new}")
    head = f"## {new} — {today.isoformat()}"
    chlog.write_text(ctext.replace("## Unreleased", head))
    print(f"CHANGELOG.md: {head}")
    line = f"- **{new}** ({today.isoformat()}): {summary.rstrip('.')}. [Notes]({RELEASES}{new})"
    bullets = [line] + bullets[: KEEP - 1]
    readme.write_text(before + START + "\n" + "\n".join(bullets) + "\n" + END + after)
    print(f"README.md: {line}")


def notes(root, version):
    lines = (Path(root) / "CHANGELOG.md").read_text().splitlines()
    prefix = f"## {version} "
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    except StopIteration:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip("\n")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "bump":
        bump(ROOT, argv[1], argv[2], datetime.date.today())
    elif len(argv) == 2 and argv[0] == "notes":
        print(notes(ROOT, argv[1]))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
