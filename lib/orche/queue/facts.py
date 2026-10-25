"""The A.7 facts block: six lines a reviewer and the integrator read about a queued head."""
import re

from .. import ledger, units
from . import repo, store


def scope_globs(scope_text) -> list:
    """The SCOPE field's path globs: its backticked items, else the tokens that look like paths."""
    text = str(scope_text or "")
    found = re.findall(r"`([^`]+)`", text)
    if not found:
        found = [t for t in re.split(r"[\s,]+", text) if "/" in t or "*" in t]
    return [g.strip() for g in found if g.strip()]


def is_docs(rel, docs_paths) -> bool:
    return any(units.scope_match(rel, g) for g in docs_paths)


def skip_ci_commits(where, base_ref, head, docs_paths) -> list:
    """Short shas of the commits base..head whose message carries [skip ci] and that change code."""
    out = []
    for sha in repo.commits(where, base_ref, head):
        if repo.SKIP_CI.search(repo.message(where, sha)) and any(
                not is_docs(p, docs_paths) for p in repo.commit_paths(where, sha)):
            out.append(sha[:7])
    return out


def gather(where, *, project, pr, head, base, tier="standard", scope="", docs_paths=(), entry_id=None) -> dict:
    """Read the facts of `head` (PR `pr`) against `base` in the checkout `where`."""
    base_ref = f"origin/{base}" if repo.git(where, "rev-parse", "--verify", "-q", f"origin/{base}") else base
    base_sha = repo.git(where, "rev-parse", base_ref) or ""
    files = repo.changed(where, base_ref, head)
    paths_ = [f[0] for f in files]
    globs = scope_globs(scope)
    outside = [p for p in paths_ if globs and not any(units.scope_match(p, g) for g in globs)]
    overlap = []
    for other in store.read(project)["entries"]:
        if other.get("pr") == pr or other.get("id") == entry_id:
            continue
        theirs = {f[0] for f in repo.changed(where, base_ref, other["head"])}
        shared = sorted(theirs & set(paths_))
        if shared:
            overlap.append({"pr": other["pr"], "paths": shared})
    row = ledger.row_for_sha(project, head)
    pre = ledger.review_for(project, pr, head, "pre")
    return {"pr": pr, "head": head, "base": base, "base_sha": base_sha, "tier": tier,
            "files": len(files), "added": sum(f[1] for f in files), "deleted": sum(f[2] for f in files),
            "scope_outside": outside, "scope_declared": bool(globs),
            "skip_ci": skip_ci_commits(where, base_ref, head, list(docs_paths)),
            "overlap": overlap, "ledger": row, "pre_review": pre}


def lines(f) -> list:
    """The six lines (A.7)."""
    out = [f"pr #{f['pr']} · head {f['head'][:7]} · base {f['base']} @ {f['base_sha'][:7] or '?'} · "
           f"tier {f['tier']} · files {f['files']} (+{f['added']} −{f['deleted']})"]
    if not f["scope_declared"]:
        out.append("scope: none declared")
    else:
        out.append("scope: ok" if not f["scope_outside"] else f"scope: outside {', '.join(f['scope_outside'][:5])}")
    sk = f["skip_ci"]
    out.append("skip-ci: none" if not sk else f"skip-ci: {len(sk)} code commits ({', '.join(sk)})")
    ov = f["overlap"]
    out.append("overlap: none" if not ov else "overlap: " + " · ".join(
        f"PR #{o['pr']} ({', '.join(o['paths'][:3])})" for o in ov))
    row = f["ledger"]
    out.append(f"ledger: {row['verdict']} by {row['evidence']}" if row else "ledger: none")
    pre = f["pre_review"]
    out.append(f"pre-review: {pre['verdict'].upper()} round {pre['round']} by {pre['model']} ({pre['evidence']})"
               if pre else "pre-review: none")
    return out
