"""A gated unit's verified `done` publishes its work (design §3.2, §5.11; R6, R7, R28, R29).

ORCHE, not the model, pushes the unit's branch and opens its PR: the runner calls `publish` after the
done gate passed (outside any Codex sandbox: the runner process holds the credentials), and
`orche report done` from a gated pane member calls it after `donegate.for_report`. The push is never
forced and never targets another ref; the PR is ready, never a draft, and never retargeted.

`sync` merges `origin/<base>` into a feature's clean, behind branch at a run's start; `guard_base`
resets a new feature branch that Orca forked from somewhere other than `origin/<base>`.

The base is config `publish.per_project.<project>.base`, else `publish.base`, else origin/HEAD of the project's checkout; neither → a Gap with
the OWNER step (`git remote set-head origin -a`), never a guess (R12); the queue and the Build review
read the same `base` and refuse with `no_base`. A Build HEAD that changes nothing against the fetched
origin/<base> (coordination) publishes nothing: Unchanged, and the done is filed with `pr: none` (R5);
a feature's is a Gap. Otherwise ORCHE publishes only the unit's own recorded branch: the worker's recorded
branch, Build's `div/build` (or the branch the registry records); a worktree on any other branch is a Gap.

Every gh call and the push go through github (tests replace PATH's `gh` and use a bare repo as origin).
`publish` returns a Published, an Unchanged or a Gap (its text and, when only the owner can fix it,
an OWNER step).

The worker's name carries the issue key (`issue_of`: `des-47-…` -> DES-47, `123-…` -> #123): the PR title
ends with ` (KEY)` and its body starts with `Fixes KEY`; the tracker's own GitHub automation moves the
issue (ORCHE never calls the tracker). The body names no writer (no AI-authorship marker: a squash merge can
copy it into history); its queue sentence appears only when the project's gate applies (`gate`).

In a team repository (registry.team) a gated done publishes nothing: it is filed with `ready_line` and
`pr: none`, and `orche queue add <unit>` (Main, on the owner's "push") calls `publish`.
"""
import re
from dataclasses import dataclass

from . import github, mask, shell

FETCH_TIMEOUT = 60
TITLE_MAX = 72
BODY_MAX = 4000
FOOTER_QUEUE = "Lands through the merge queue after its checks and the head review; please do not merge by hand."
DEFAULT_PREFIX = "feat"
_ISSUE = re.compile(r"([a-z]+)-(\d+)(?:-|$)|(\d+)-", re.IGNORECASE)
REJECTED = ("push rejected: origin/{branch} has commits this worktree lacks (someone pushed to it); merge them, "
            "then report again")
SYNC_NOTE = "## Branch sync: {files} conflict with origin/{base}; merge it yourself first"
OWNER_AUTH = "gh auth login (then orche run {unit} or report done again)"
OWNER_PERMISSION = "grant this machine's git/gh account push and PR rights on the repository"
OWNER_BASE = ("git -C {root} remote set-head origin -a (or set publish.base in ORCHE's config), then orche run "
              "{unit} or report done again")
_PR_URL = re.compile(r"https?://\S+/pull/(\d+)")


@dataclass(frozen=True)
class Published:
    pr: int
    url: str
    head: str

    def record(self) -> dict:
        """RUN.json `pr` and the pointer's `pr`."""
        return {"number": self.pr, "url": self.url, "head": self.head}


@dataclass(frozen=True)
class Unchanged:
    """A verified done whose HEAD changes nothing against origin/<base>: no push, no PR (R5)."""
    base: str

    def note(self) -> str:
        return f"PR: none (no change against origin/{self.base}; nothing published)"


@dataclass(frozen=True)
class Gap:
    text: str
    owner: str | None = None


def ready_line(unit, branch) -> str:
    """A gated done in team mode (registry.team): nothing is published; the owner's word publishes it."""
    return f'ready: say "push {unit}" ({branch})'


def _first(res) -> str:
    text = (res.err or res.out or "").strip()
    return mask.redact(text.splitlines()[0] if text else f"exit {res.code}")[0]


def settings(cfg, project) -> dict:
    """config `publish` with `publish.per_project.<project>` laid over it (cfg None: config.load())."""
    from . import config
    return config.for_project(config.load() if cfg is None else cfg, "publish", project)


def base(root, cfg=None, worktree=None, project=None) -> str | None:
    """The project's base branch: config `publish.per_project.<project>.base`, else `publish.base`, else
    origin/HEAD in the main checkout (then in `worktree`, the unit's: a git worktree shares it):
    `origin/main` -> `main`; None when nothing names one (R12: never a guess)."""
    configured = str(settings(cfg, project).get("base") or "").strip()
    if configured:
        return configured
    for where in dict.fromkeys(str(w) for w in (root, worktree) if w):
        res = shell.git(where, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        name = res.out.strip() if res.ok else ""
        if name.startswith("origin/") and name != "origin/":
            return name.split("/", 1)[1]
    return None


def base_ref(where, name) -> str:
    """`origin/<name>` when the checkout `where` has it (a commit), else `<name>`: the one ref every diff
    against the base uses (publish, the pre-review, the head review, the queue's facts and admission)."""
    ref = f"origin/{name}"
    return ref if shell.git(where, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").ok else name


def prefix(cfg, project) -> str:
    """The first path segment of a feature worker's branch: `publish.branch_prefix`, default `feat`."""
    return str(settings(cfg, project).get("branch_prefix") or "").strip() or DEFAULT_PREFIX


def issue_of(name) -> str:
    """The issue key a worker's name starts with: `des-47-x` or `des-47` -> `DES-47`, `123-x` -> `#123`,
    else "" (only the first token counts)."""
    m = _ISSUE.match(str(name or ""))
    if not m:
        return ""
    return f"{m.group(1).upper()}-{m.group(2)}" if m.group(1) else f"#{m.group(3)}"


def own_branch(member) -> str:
    """The branch a gated member publishes from: a feature's recorded branch, Build's recorded branch
    or `div/build`; "" when none is known."""
    if member is None:
        return ""
    if member.kind == "div":
        return member.branch or f"div/{member.name}"
    return member.branch or ""


def _current(worktree) -> str:
    res = shell.git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    return res.out.strip() if res.ok else ""


def branch(member, worktree):
    """(branch, None) or (None, Gap): the member's own branch (own_branch), which the worktree must be on (R12)."""
    current, recorded = _current(worktree), own_branch(member)
    if not recorded or current != recorded:
        return None, Gap(f"worktree is on {current or 'nothing'}, recorded {recorded or 'nothing'}: ORCHE publishes "
                         "only the unit's own branch")
    return recorded, None


def gate(root, cfg=None, project=None):
    """(base or None, whether `scripts/verify/ci.sh` is committed on origin/<base>): the merge queue's gate."""
    name = base(root, cfg, project=project)
    ok = bool(name) and shell.git(root, "cat-file", "-e", f"origin/{name}:scripts/verify/ci.sh", timeout=10).ok
    return name, ok


def no_base(root) -> str:
    """R12's refusal (the queue, the Build review) when nothing names the base: the gap and the OWNER step."""
    return (f"no base branch: publish.base is unset and origin/HEAD is not set in {root}; OWNER: git -C {root} "
            "remote set-head origin -a (or set publish.base in ORCHE's config), then try again")


def changes(worktree, base_name) -> bool:
    """Whether HEAD changes anything against origin/<base> (else <base>), fetched first (a failed fetch,
    bounded by FETCH_TIMEOUT, leaves the local ref); git's error counts as a change."""
    _fetch(worktree, base_name)
    return shell.git(worktree, "diff", "--quiet", f"{base_ref(worktree, base_name)}...HEAD").code != 0


def title_of(goal, issue="") -> str:
    """The GOAL's first line, masked, within TITLE_MAX; with an issue key it ends with ` (KEY)` (the GOAL
    part is cut, never the key)."""
    line = next((ln.strip() for ln in str(goal or "").splitlines() if ln.strip()), "ORCHE unit")
    line = mask.redact(line)[0]
    tail = f" ({issue})" if issue else ""
    room = TITLE_MAX - len(tail)
    return (line if len(line) <= room else line[:room - 1] + "…") + tail


def body_of(report, *, issue="", queued=False) -> str:
    """Appendix A.12: `Fixes KEY` first when the worker's name carries an issue key, then the unit's
    REPORT.md, masked and capped, then the queue sentence (only when the gate applies)."""
    text = mask.redact(str(report or "").strip())[0][:BODY_MAX]
    return (f"Fixes {issue}\n\n" if issue else "") + text + (f"\n\n{FOOTER_QUEUE}" if queued else "")


def pr_text(member, root, *, goal, report, cfg=None) -> tuple:
    """(title, body) of a gated member's PR: the issue key from the worker's name, the queue sentence when the
    project's gate applies (one `gate` call)."""
    project = member.project if member is not None else None
    key = issue_of(member.name) if member is not None and member.kind == "feat" else ""
    queued = gate(root, cfg, project)[1]
    return title_of(goal, key), body_of(report, issue=key, queued=queued)


def _gh_gap(exc, what, unit) -> Gap:
    res = exc.result
    if res is None or res.ok:       # gh answered, but nothing readable
        return Gap(f"{what}: unreadable output ({(res.out if res else '').strip()[:80]})")
    line = _first(res)
    if res.code == 127:
        return Gap(f"{what}: gh is not installed ({line})", "install gh (brew install gh), then gh auth login")
    if github.AUTH.search(res.err + res.out):
        return Gap(f"{what}: gh is not logged in ({line})", OWNER_AUTH.format(unit=unit))
    if github.PERMISSION.search(res.err + res.out):
        return Gap(f"{what}: {line}", OWNER_PERMISSION)
    return Gap(f"{what}: {line}")


def _push(wt, name, unit):
    res = github.push(wt, name)
    if res.ok:
        return None
    text = res.err + res.out
    if "non-fast-forward" in text or "[rejected]" in text or "fetch first" in text:
        return Gap(REJECTED.format(branch=name))
    if "does not appear to be a git repository" in text or "No such remote" in text:
        return Gap(f"push failed: no remote origin ({_first(res)})", "add the repository's origin remote "
                                                                    f"(git remote add origin <url>), then orche run {unit}")
    if github.AUTH.search(text) or github.PERMISSION.search(text):
        return Gap(f"push failed: {_first(res)}", OWNER_PERMISSION)
    return Gap(f"push failed: {_first(res)}")


def _ensure_pr(wt, name, base_name, head, title, body, unit):
    try:
        prs = github.pr_for_branch(wt, name)
    except github.GhError as exc:
        return _gh_gap(exc, "gh pr list", unit)
    if len(prs) > 1:
        return Gap(f"two open PRs for {name}: " + ", ".join(f"#{p.get('number')}" for p in prs) + "; Build decides")
    if prs:
        pr = prs[0]
        number = int(pr.get("number"))
        if pr.get("baseRefName") != base_name:
            return Gap(f"PR #{number} targets {pr.get('baseRefName')}, not {base_name}; ORCHE never retargets: "
                       "Build decides")
        if pr.get("isDraft"):
            try:
                github.ready(wt, number)
            except github.GhError as exc:
                return _gh_gap(exc, f"gh pr ready {number}", unit)
        return Published(number, pr.get("url") or "", head)
    try:
        out = github.create(wt, base=base_name, head=name, title=title, body=body)
    except github.GhError as exc:
        return _gh_gap(exc, "gh pr create", unit)
    m = _PR_URL.search(out)
    if not m:
        return Gap(f"gh pr create: no PR URL in its output ({out.strip()[:80]})")
    return Published(int(m.group(1)), m.group(0), head)


def publish(member, worktree, root, *, unit, title, body, cfg=None):
    """Push the unit's own branch (never forced) and ensure its ready PR to the base: Published, Unchanged
    (nothing against the base) or Gap."""
    base_name = base(root, cfg, worktree, member.project if member is not None else None)
    if not base_name:
        if not shell.git(worktree, "remote", "get-url", "origin").ok:
            return Gap("push failed: no remote origin", "add the repository's origin remote "
                                                        f"(git remote add origin <url>), then orche run {unit}")
        return Gap("no base branch: origin/HEAD is not set in the project's checkout",
                   OWNER_BASE.format(root=root, unit=unit))
    name, gap = branch(member, worktree)
    build = member is not None and member.kind == "div"
    if build and (gap is None or _current(worktree) == base_name) and not changes(worktree, base_name):
        return Unchanged(base_name)     # R5: Build coordinated (from its branch or the base): nothing to publish
    if gap:
        return gap
    if not name or name in ("HEAD", base_name):
        return Gap(f"nothing to publish from {name or 'a detached HEAD'}")
    if not build and not changes(worktree, base_name):
        return Gap(f"nothing to publish: HEAD changes nothing against origin/{base_name}; a feature's done "
                   "needs its commit")
    head = shell.git_head(worktree)
    if not head:
        return Gap("nothing to publish: no HEAD")
    gap = _push(worktree, name, unit)
    if gap:
        return gap
    return _ensure_pr(worktree, name, base_name, head, title, body, unit)


def _fetch(wt, base_name) -> bool:
    return shell.git(wt, "fetch", "origin", base_name, timeout=FETCH_TIMEOUT).ok


def _ancestor(wt, a, b) -> bool:
    return shell.git(wt, "merge-base", "--is-ancestor", a, b).ok


def sync(worktree, base_name):
    """R28, at a feature run's start: merge origin/<base> into a clean, behind branch. Returns the
    prompt note on a conflict (the merge aborted, the tree untouched), else None. A failed fetch or a
    dirty tree leaves the branch as it is; so does an unknown base (None)."""
    if not base_name or not _fetch(worktree, base_name):
        return None
    status = shell.git(worktree, "status", "--porcelain", "--untracked-files=no")
    if not status.ok or status.out.strip():
        return None
    upstream = f"origin/{base_name}"
    if _ancestor(worktree, upstream, "HEAD"):
        return None
    if shell.git(worktree, "merge", "--no-edit", upstream, timeout=FETCH_TIMEOUT).ok:
        return None
    files = shell.git(worktree, "diff", "--name-only", "--diff-filter=U").out.split()
    shell.git(worktree, "merge", "--abort")
    return SYNC_NOTE.format(files=", ".join(files) or "files", base=base_name)


def guard_base(worktree, base_name) -> bool:
    """R29, after Orca created a feature's worktree: when HEAD is not an ancestor of origin/<base>
    (Orca forked from Build's current branch), reset the new, empty branch to origin/<base>. True
    when it reset; an unknown base (None) never resets."""
    if not base_name or not _fetch(worktree, base_name):
        return False
    upstream = f"origin/{base_name}"
    if _ancestor(worktree, "HEAD", upstream):
        return False
    return shell.git(worktree, "reset", "--hard", upstream).ok
