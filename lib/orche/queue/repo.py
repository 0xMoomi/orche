"""git and gh probes the queue's add checks and facts read (design §3.2, §4.3). Read-only."""
import json
import re
import subprocess

from .. import shell

SKIP_CI = re.compile(r"\[(?:skip ci|ci skip)\]", re.IGNORECASE)


class GhError(RuntimeError):
    pass


def git(where, *argv) -> str | None:
    """`git -C <where> …` stdout (stripped), None when git fails."""
    res = shell.run(["git", "-C", str(where), *argv], timeout=30)
    return res.out.strip() if res.ok else None


def gh(where, *argv):
    """`gh …` run in `where` (gh finds the repo from the checkout), parsed JSON. Raises GhError."""
    try:
        res = subprocess.run(["gh", *argv], cwd=str(where), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh: {exc}") from None
    if res.returncode != 0:
        line = next((ln.strip() for ln in (res.stderr or res.stdout).splitlines() if ln.strip()),
                    f"exit {res.returncode}")
        raise GhError(f"gh: {line[:160]}")
    try:
        return json.loads(res.stdout or "null")
    except ValueError:
        raise GhError("gh: unreadable JSON") from None


def head(where) -> tuple:
    """(HEAD sha, branch) of a worktree; (None, None) when git fails."""
    sha = git(where, "rev-parse", "HEAD")
    branch = git(where, "rev-parse", "--abbrev-ref", "HEAD")
    return sha, branch


def base_sha(where, base_branch) -> str | None:
    return git(where, "rev-parse", f"origin/{base_branch}") or git(where, "rev-parse", base_branch)


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


def open_prs(where, branch) -> list:
    """The open PRs whose head is `branch` (§3.2)."""
    return gh(where, "pr", "list", "--head", branch, "--state", "open", "--json",
              "number,baseRefName,headRefOid,isDraft", "--limit", "2") or []


def pr_view(where, number) -> dict:
    return gh(where, "pr", "view", str(number), "--json",
              "number,state,baseRefName,headRefName,headRefOid,isDraft,reviewDecision") or {}
