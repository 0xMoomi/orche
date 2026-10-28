"""git probes the queue's add checks and facts read. Read-only."""
import re

from .. import shell

SKIP_CI = re.compile(r"\[(?:skip ci|ci skip)\]", re.IGNORECASE)


def git(where, *argv) -> str | None:
    """`git -C <where> …` stdout (stripped), None when git fails."""
    res = shell.git(where, *argv)
    return res.out.strip() if res.ok else None


def head(where) -> tuple:
    """(HEAD sha, branch) of a worktree; (None, None) when git fails."""
    sha = git(where, "rev-parse", "HEAD")
    branch = git(where, "rev-parse", "--abbrev-ref", "HEAD")
    return sha, branch


def tree_mode(where, sha, rel) -> str | None:
    """The file's mode in commit `sha` (e.g. 100755), None when it is not tracked there."""
    out = git(where, "ls-tree", sha, "--", rel)
    return out.split()[0] if out else None


def message(where, sha) -> str:
    return git(where, "show", "-s", "--format=%B", sha) or ""


def changed(where, frm, to) -> list:
    """[(path, added, deleted)] between the merge base of frm and to, and to."""
    out = git(where, "diff", "--numstat", f"{frm}...{to}") if frm else None
    rows = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem, rel = parts
            rows.append((rel, int(add) if add.isdigit() else 0, int(rem) if rem.isdigit() else 0))
    return rows


def commits(where, frm, to) -> list:
    out = git(where, "rev-list", "--reverse", f"{frm}..{to}") if frm else None
    return (out or "").split()


def commit_paths(where, sha) -> list:
    out = git(where, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return (out or "").splitlines()
