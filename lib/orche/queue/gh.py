"""The integrator's GitHub calls (design §5.3, §5.5) through `gh`, run in the integrator's checkout
(gh finds the repository from it). It views a PR and merges one pinned to its head; nothing else."""
import json
import subprocess

from . import repo

VIEW_FIELDS = "state,headRefOid,baseRefName,isDraft,title,reviewDecision"
VIEW_S, MERGE_S = 30, 60


def _gh(where, argv, timeout):
    try:
        res = subprocess.run(["gh", *argv], cwd=str(where), capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", f"gh {' '.join(argv[:2])}: no answer within {timeout} s"
    except OSError as exc:
        return 127, "", f"gh: {exc}"
    return res.returncode, res.stdout or "", res.stderr or ""


def first_line(text, default="") -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), default)[:200]


def view(where, pr) -> dict:
    """`gh pr view N --json state,headRefOid,baseRefName,isDraft,title,reviewDecision` (30 s). Raises GhError."""
    code, out, err = _gh(where, ["pr", "view", str(pr), "--json", VIEW_FIELDS], VIEW_S)
    if code != 0:
        raise repo.GhError(f"gh: {first_line(err or out, f'exit {code}')}")
    try:
        data = json.loads(out or "null")
    except ValueError:
        raise repo.GhError("gh: unreadable JSON") from None
    if not isinstance(data, dict):
        raise repo.GhError("gh: no PR object")
    return data


def merge(where, pr, head, method="merge") -> tuple:
    """`gh pr merge N --merge|--squash --match-head-commit <head>` (60 s): (True, "") or (False, the
    first line GitHub answered). `--rebase` is never offered (design §5.5)."""
    flag = "--squash" if method == "squash" else "--merge"
    code, out, err = _gh(where, ["pr", "merge", str(pr), flag, "--match-head-commit", str(head)], MERGE_S)
    return (True, "") if code == 0 else (False, first_line(err or out, f"exit {code}"))
