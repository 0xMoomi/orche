"""Template files under `<repo>/templates`: unit memory files, project docs, verify-skill, prompt texts.

`render` substitutes `{{VAR}}` placeholders; unknown placeholders are left as they are.
Copies never overwrite existing files and go through `util.atomic_write` (umask modes).
"""
import re
from pathlib import Path

from . import util

TEMPLATES_ROOT = Path(__file__).resolve().parent.parent.parent / "templates"
UNIT_KINDS = ("division", "feature", "main")
UNIT_FILES = ("CHARTER.md", "STATUS.md", "HANDOFF.md", "DECISIONS.tsv")
_VAR = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def render(text: str, **vars) -> str:
    """Replace `{{NAME}}` with `vars["NAME"]` for every NAME given."""
    return _VAR.sub(lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), text)


def load(path, **vars) -> str:
    """Template `path` (relative to TEMPLATES_ROOT, or absolute) rendered, without its last newline:
    a template file's last newline ends the file, not the text it holds."""
    return render((TEMPLATES_ROOT / path).read_text(encoding="utf-8").removesuffix("\n"), **vars)


def _copy_tree(src_root: Path, dest_root: Path, rels, vars) -> list:
    created = []
    for rel in rels:
        target = dest_root / rel
        if target.exists():
            continue
        util.atomic_write(target, render((src_root / rel).read_text(encoding="utf-8"), **vars))
        created.append(target)
    return created


def copy_unit(dest_dir, kind: str, **vars) -> list:
    """Copy CHARTER/STATUS/HANDOFF/DECISIONS into `dest_dir` (missing files only). Returns created paths.

    `kind` is division | feature | main. Defaults: UNIT = dest_dir's name, KIND = kind,
    SESSION = "(unassigned)"; pass PROJECT/UNIT/SESSION/... to override.
    """
    if kind not in UNIT_KINDS:
        raise ValueError(f"kind must be one of {'|'.join(UNIT_KINDS)}, got {kind!r}")
    dest = Path(dest_dir)
    vars = {"UNIT": dest.name, "KIND": kind, "SESSION": "(unassigned)", **vars}
    return _copy_tree(TEMPLATES_ROOT / "unit", dest, UNIT_FILES, vars)


def project_files() -> list:
    """The paths (relative, sorted) `copy_project` copies: `templates/project/**` but `*.tmpl` files."""
    src = TEMPLATES_ROOT / "project"
    return sorted(p.relative_to(src) for p in src.rglob("*")
                  if p.is_file() and p.name != ".DS_Store" and p.suffix != ".tmpl")


def copy_project(repo_root, **vars) -> list:
    """Copy `templates/project/**` into `repo_root`, skipping files that already exist.

    `*.tmpl` files are not copied: onboarding renders them per worktree.
    """
    src = TEMPLATES_ROOT / "project"
    rels = project_files()
    root = Path(repo_root)
    vars = {"PROJECT": root.name, **vars}
    return _copy_tree(src, root, rels, vars)
