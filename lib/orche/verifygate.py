"""The merge queue's check script, `scripts/verify/ci.sh`: rendered from `templates/verify/ci.sh` with the
repository's own commands (detect.toolchain), part of the first commit of a repository `orche init --new` makes,
and offered to an existing repository as one pull request by `orche init` and `orche init --refresh`, so
`orche update` (`ensure`).

`ensure` never asks and never fails init: it answers a state for the setup report's "Running now" line,
  {"state": "on"}                        the script is on origin/<base> (fetched first)
  {"state": "pr", "number", "url"}       an open PR from BRANCH adds it (found, or opened now)
  {"state": "waiting", "reason"}         nothing could be pushed (no origin, no base, gh, the push)
  {"state": "word", "project"}           neither, and `open_pr` is false: a team repository's PR waits for the
                                         owner's word through Main (R-TEAM-5)
It opens at most one PR per project: an open PR with head BRANCH, or BRANCH on origin, means it is done.
The branch is cut from origin/<base> in a temporary worktree, so the owner's checkout is never touched.
init keeps an open PR's number and URL on the project's registry entry (`verify_pr`), so the doctor and the
setup report name it without a gh call (`awaiting`), while the pushed branch says it is open (`pending`).
"""
import re
import shutil
import tempfile
from pathlib import Path

from . import detect, github, mask, publish, shell, templates

BRANCH = "orche/verify-gate"
SCRIPT = "scripts/verify/ci.sh"
COMMIT = "chore(verify): the merge queue's check script"
TITLE = "Add the merge queue's check script"
_PR = re.compile(r"/pull/(\d+)")


def script(root) -> str:
    """The check script for the repository at `root`: its install and check commands, the verdict they earn."""
    install, check = detect.toolchain(root)
    return templates.load(
        "verify/ci.sh", INSTALL=install or "# install: nothing to install",
        CHECK=check or 'echo "no test command found in package.json, pyproject.toml, go.mod or Cargo.toml"',
        VERDICT="unit-test-verified" if check else "type-check-only") + "\n"


def write(root) -> Path:
    """`<root>/scripts/verify/ci.sh` (executable), unless the repository already has one."""
    path = Path(root) / SCRIPT
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script(root), encoding="utf-8")
        path.chmod(0o755)
    return path


def body(check) -> str:
    runs = f"`{check}`" if check else "no tests yet (no test command was found), so it only reports type-check-only"
    return (f"Adds {SCRIPT}, the check ORCHE's merge queue runs on each approved pull request: {runs}.\n"
            "The queue lands pull requests only on the owner's word; merging this one turns it on.\n")


def pending(root) -> bool:
    """Whether this checkout has pushed BRANCH (its remote-tracking ref): a PR adding the script waits."""
    return shell.git(root, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{BRANCH}", timeout=10).ok


def awaiting(proj) -> str:
    """The PR adding the script as init last recorded it (`verify_pr`): `PR #<n> (<url>)`."""
    pr = (proj or {}).get("verify_pr") or {}
    return f"PR #{pr['number']} ({pr['url']})" if pr.get("number") else f"the PR from {BRANCH}"


_FAILING = re.compile(r"error|fail|refused|rejected", re.IGNORECASE)


def reason(res) -> str:
    """Why a push failed: the first stderr line naming an error, a failure, a refusal or a rejection, else
    the last non-empty one (a pre-push hook prints progress first), else `exit N`."""
    lines = [ln.strip() for ln in str(res.err or res.out or "").splitlines() if ln.strip()]
    return next((ln for ln in lines if _FAILING.search(ln)), lines[-1] if lines else f"exit {res.code}")[:160]


def _waiting(why) -> dict:
    return {"state": "waiting", "reason": mask.redact(str(why).removeprefix("gh: "))[0]}


def ensure(root, key, cfg=None, open_pr=True) -> dict:
    """The merge queue's script on origin/<base>, or one PR adding it (see the module docstring)."""
    if not shell.git(root, "remote", "get-url", "origin", timeout=10).ok:
        return _waiting("the repository has no origin remote")
    base = publish.base(root, cfg, project=key)
    if not base:
        return _waiting("no base branch: origin/HEAD is not set")
    shell.git(root, "fetch", "-q", "--no-tags", "origin", base, timeout=github.NET_S)
    if publish.gate(root, cfg, key)[1]:
        return {"state": "on"}
    try:
        prs = github.pr_for_branch(root, BRANCH)
    except github.GhError as exc:
        return _waiting(exc)
    if prs:
        return {"state": "pr", "number": prs[0].get("number"), "url": prs[0].get("url")}
    remote = shell.git(root, "ls-remote", "--heads", "origin", BRANCH, timeout=github.NET_S)
    if not remote.ok or remote.out.strip():
        return _waiting(f"the branch {BRANCH} is already on origin" if remote.ok else github.first_line(remote))
    if not open_pr:
        return {"state": "word", "project": key}
    tmp = Path(tempfile.mkdtemp(prefix="orche-verify-"))
    wt = tmp / "checkout"
    try:
        res = shell.git(root, "worktree", "add", "-q", "--detach", str(wt), f"origin/{base}", timeout=60)
        if res.ok:
            write(wt)
            res = shell.git(wt, "add", SCRIPT, timeout=60)
        if res.ok:
            res = shell.git(wt, "commit", "-q", "-m", COMMIT, timeout=60)
        if res.ok:
            res = github.push(wt, BRANCH)
        if not res.ok:
            return _waiting(reason(res))
        url = github.create(root, base=base, head=BRANCH, title=TITLE, body=body(detect.verify_command(wt))).strip()
    except github.GhError as exc:
        return _waiting(exc)
    finally:
        shell.git(root, "worktree", "remove", "--force", str(wt), timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
    found = _PR.search(url)
    return {"state": "pr", "number": int(found.group(1)) if found else None, "url": url.splitlines()[-1] if url else None}


def running(state) -> str:
    """The setup report's merge-queue state for `ensure`'s answer."""
    if state["state"] == "on":
        return "on"
    if state["state"] == "pr":
        return f"waiting for PR #{state['number']} ({state['url']}) to be merged"
    if state["state"] == "word":
        return f'waits for the owner\'s word (say "set up the merge queue for {state["project"]}")'
    return f"waiting; the check script could not be pushed ({state['reason']})"
