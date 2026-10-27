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
"""
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
