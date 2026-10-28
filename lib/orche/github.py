"""Every GitHub step ORCHE takes: `gh` run in a checkout (gh finds the repository
from it) and the remote git steps of a PR (push its branch, fetch its head). One runner on shell.run, one
timeout (TIMEOUT; NET_S for a fetch; PUSH_S for a push: a repository's pre-push hook may run its whole
verification) and one error, GhError. Tests put a fake `gh` on PATH and use a bare repository as origin
(tests/support.fake_gh).

  pr_for_branch(where, branch)   the open PRs whose head is `branch` (publish, orche queue add)
  view(where, pr)                one PR (the queue, the head review)
  create / ready                 open a ready PR (publish)
  merge(where, pr, head)         GitHub's squash merge pinned to the tested head (the integrator)
  push(where, branch)            the unit's branch, never forced (publish)
  fetch_pr_head(where, pr, head) the PR's head commit into a clone (orche queue add --pr, the head review)
"""
import json
import os
import re
import tempfile

from . import shell

TIMEOUT = 60
NET_S = 120
PUSH_S = 1800
PR_FIELDS = "number,url,state,baseRefName,headRefName,headRefOid,isDraft,title,reviewDecision"
AUTH = re.compile(r"gh auth login|not logged in|authentication|auth token|HTTP 401", re.IGNORECASE)
PERMISSION = re.compile(r"permission|denied|HTTP 403|protected branch|GH006", re.IGNORECASE)


class GhError(RuntimeError):
    """A GitHub step that failed; `result` is its shell.Result (None when there is none)."""

    def __init__(self, text, result=None):
        super().__init__(text)
        self.result = result


def first_line(res) -> str:
    """The first non-blank line a failed command printed (stderr, else stdout), else `exit N`."""
    return next((ln.strip() for ln in (res.err or res.out or "").splitlines() if ln.strip()), f"exit {res.code}")[:160]


def _gh(where, argv, timeout):
    """`gh …` in the checkout `where` (stdin closed: gh never waits on a prompt); its Result. Raises GhError."""
    res = shell.run(["gh", *argv], timeout=timeout, cwd=where, input="")
    if not res.ok:
        raise GhError(f"gh: {first_line(res)}", res)
    return res


def run(where, *argv, timeout=TIMEOUT) -> str:
    """`gh …` in `where`; its stdout. Raises GhError."""
    return _gh(where, argv, timeout).out


def data(where, *argv, timeout=TIMEOUT):
    """`gh … --json …`, parsed. Raises GhError."""
    res = _gh(where, argv, timeout)
    try:
        return json.loads(res.out or "null")
    except ValueError:
        raise GhError("gh: unreadable JSON", res) from None


def pr_for_branch(where, branch) -> list:
    """The open PRs whose head is `branch` (two at most: a second one is the caller's refusal)."""
    return data(where, "pr", "list", "--head", branch, "--state", "open", "--json",
                "number,url,baseRefName,headRefOid,isDraft", "--limit", "2") or []


def view(where, pr) -> dict:
    """PR `pr` (PR_FIELDS). Raises GhError, also when gh answers no PR object."""
    out = data(where, "pr", "view", str(pr), "--json", PR_FIELDS)
    if not isinstance(out, dict):
        raise GhError("gh: no PR object")
    return out


def create(where, *, base, head, title, body) -> str:
    """`gh pr create` (ready, never a draft; the body through a file): its stdout, the PR's URL. Raises GhError."""
    fd, tmp = tempfile.mkstemp(prefix="orche-pr-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        return run(where, "pr", "create", "--base", base, "--head", head, "--title", title, "--body-file", tmp)
    finally:
        os.unlink(tmp)


def ready(where, pr) -> None:
    """A draft PR made ready. Raises GhError."""
    run(where, "pr", "ready", str(pr))


def merge(where, pr, head) -> tuple:
    """`gh pr merge N --squash --match-head-commit <head>`: (True, "") or (False, the first line GitHub
    answered). Always a squash."""
    try:
        run(where, "pr", "merge", str(pr), "--squash", "--match-head-commit", str(head))
    except GhError as exc:
        return False, str(exc).removeprefix("gh: ")
    return True, ""


def push(where, branch):
    """`git push -u origin HEAD:refs/heads/<branch>`, never forced: its shell.Result (publish reads the refusal)."""
    return shell.git(where, "push", "-u", "origin", f"HEAD:refs/heads/{branch}", timeout=PUSH_S)


def fetch_pr_head(where, pr, head, base="") -> None:
    """PR `pr`'s head commit into the clone `where`: fetched by its id (with origin/<base> when named), else
    GitHub's pull/<pr>/head; never FETCH_HEAD (an integrator may be reading its own fetch in the queue's
    clone). Raises GhError when the commit is still missing."""
    def have():
        return shell.git(where, "cat-file", "-e", f"{head}^{{commit}}").ok

    if have():
        return
    why = ""
    for argv in ([head, *([f"+refs/heads/{base}:refs/remotes/origin/{base}"] if base else [])], [f"pull/{pr}/head"]):
        res = shell.git(where, "fetch", "--no-tags", "--no-write-fetch-head", "origin", *argv, timeout=NET_S)
        if have():
            return
        why = why or first_line(res)
    raise GhError(f"cannot fetch PR #{pr}'s head {head[:7]} from origin: {why}")
