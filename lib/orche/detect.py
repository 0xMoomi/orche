"""A repository's team conventions, read at `orche init`, `orche init --refresh` and `orche update` (c73).

`conventions(root, project, cfg)` returns only the keys it is sure about, each from one read with a
timeout that fails silently to "nothing detected" (never a refusal, never a guess below a threshold):

  base           the branch recent merged PRs landed on (`gh pr list --state merged`): at least MIN_PRS of
                 them, one base holding at least two thirds, and not origin/HEAD (that one needs no key)
  branch_prefix  the first segment of the issue-keyed remote branches (`dev/des-47-x`): at least
                 MIN_BRANCHES of them share one prefix holding at least two thirds of them
  issues         the issue tracker's team key (`DES`) holding at least two thirds of those branches
  entire         True when the repository uses Entire (`.entire/settings.json`)

cmd_init writes base, branch_prefix and issues into `publish.per_project.<project>` where unset;
`entire` is read live from the repository wherever it matters (onboarding.link_entire, the doctor).

Onboarding (c74) reads three more facts, each local and read-only (a failure counts as "no sign"):
`stage(root)` (`build` or `operations`: the preset `orche hr choices` recommends), `tracked(root)` (an empty
repository skips the intake) and `toolchain(root)` (the install and check commands of the merge queue's
check script, `scripts/verify/ci.sh`).
"""
import json
import re
from collections import Counter
from pathlib import Path

from . import github, shell

TIMEOUT = 20
PR_LIMIT = 30
MIN_PRS = 20
MIN_BRANCHES = 3
SHARE = 2 / 3
ENTIRE = Path(".entire") / "settings.json"
CODE_FILES = 20     # more tracked files than this: the repository has code
EMPTY_FILES = 5     # fewer tracked files than this: an empty repository (no intake)
MANIFESTS = ("package.json", "pyproject.toml", "go.mod", "Cargo.toml")
DEPLOY_FILES = ("vercel.json", "fly.toml", "Dockerfile", "render.yaml", "netlify.toml")
_SHIPS = re.compile(r"deploy|release", re.IGNORECASE)
_KEYED = re.compile(r"^(?P<prefix>[a-z][a-z0-9-]*)/(?P<team>[a-z]+)-(?P<n>[0-9]+)(-|$)")


def uses_entire(root) -> bool:
    """Whether the repository at `root` uses Entire (its committed `.entire/settings.json`)."""
    return (Path(root) / ENTIRE).is_file()


def _majority(values, minimum):
    """The value holding at least SHARE of `values` when there are at least `minimum`, else None."""
    if len(values) < minimum:
        return None
    value, count = Counter(values).most_common(1)[0]
    return value if count >= SHARE * len(values) else None


def _base(root, project, cfg):
    from . import publish
    try:
        prs = github.data(root, "pr", "list", "--state", "merged", "--limit", str(PR_LIMIT), "--json",
                          "baseRefName", timeout=TIMEOUT)
    except github.GhError:
        return None
    names = [p.get("baseRefName") for p in prs if isinstance(p, dict) and p.get("baseRefName")] \
        if isinstance(prs, list) else []
    found = _majority(names, MIN_PRS)
    return found if found and found != publish.base(root, {"publish": {}}) else None


def _branches(root) -> dict:
    res = shell.git(root, "ls-remote", "--heads", "origin", timeout=TIMEOUT)
    if not res.ok:
        return {}
    keyed = [m for m in (_KEYED.match(ln.split("refs/heads/", 1)[-1]) for ln in res.out.splitlines()
                         if "refs/heads/" in ln) if m]
    prefix = _majority([m.group("prefix") for m in keyed], MIN_BRANCHES)
    if not prefix:
        return {}
    mine = [m.group("team") for m in keyed if m.group("prefix") == prefix]
    team = _majority(mine, 1)
    return {"branch_prefix": prefix, **({"issues": team.upper()} if team else {})}


def conventions(root, project, cfg=None) -> dict:
    """The conventions the repository at `root` shows, by key (see the module docstring); {} for a plain one."""
    found = {}
    base = _base(root, project, cfg)
    if base:
        found["base"] = base
    found.update(_branches(root))
    if uses_entire(root):
        found["entire"] = True
    return found


def toplevel(path) -> str | None:
    """The top of the git repository holding `path`, else None."""
    res = shell.git(path, "rev-parse", "--show-toplevel", timeout=TIMEOUT)
    return (res.out.strip() or None) if res.ok else None


def tracked(root) -> int:
    """How many files git tracks in `root` (0 when git cannot tell)."""
    res = shell.git(root, "ls-files", "-z", timeout=TIMEOUT)
    return len([p for p in res.out.split("\0") if p]) if res.ok else 0


def _ships(root) -> bool:
    """A sign of running in production: a deploy config, a workflow about deploying or releasing, or a tag."""
    root = Path(root)
    if any((root / name).is_file() for name in DEPLOY_FILES):
        return True
    for wf in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        try:
            if _SHIPS.search(wf.name) or _SHIPS.search(wf.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    res = shell.git(root, "for-each-ref", "--count=1", "--format=%(refname)", "refs/tags", timeout=TIMEOUT)
    return res.ok and bool(res.out.strip())


def stage(root) -> str:
    """`operations` when the repository has code (more than CODE_FILES tracked files, or a package manifest)
    and a sign of running in production (`_ships`; tags are the ones fetched from origin), else `build`."""
    code = tracked(root) > CODE_FILES or any((Path(root) / name).is_file() for name in MANIFESTS)
    return "operations" if code and _ships(root) else "build"


def toolchain(root) -> tuple:
    """(install command or None, check command or None) from the repository's manifest: package.json's
    `verify` script, else its `test` script (pnpm, yarn or npm by the lockfile); Python, Go or Rust tests."""
    root = Path(root)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            scripts = {}
        if (root / "pnpm-lock.yaml").is_file():
            install, run = "pnpm install --frozen-lockfile", "pnpm {}"
        elif (root / "yarn.lock").is_file():
            install, run = "yarn install --frozen-lockfile", "yarn {}"
        else:
            install, run = "npm ci" if (root / "package-lock.json").is_file() else "npm install", "npm run {}"
        name = next((n for n in ("verify", "test") if isinstance(scripts, dict) and n in scripts), None)
        if name:
            return install, run.format(name)
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        return ("pip install -r requirements.txt" if (root / "requirements.txt").is_file() else None,
                "python -m pytest")
    if (root / "go.mod").is_file():
        return None, "go test ./..."
    if (root / "Cargo.toml").is_file():
        return None, "cargo test"
    return None, None


def verify_command(root) -> str | None:
    """The project's own check command (toolchain), else None."""
    return toolchain(root)[1]
