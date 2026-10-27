"""Retiring units: `orche feat retire` (retire) and the janitor's orca-removed rule (retire_gone).

retire: refuses an onboarding entry, uncommitted work (anything in `git status` except untracked
        ORCHE-owned files), a HANDOFF.md without `final:` unless the branch head is already in
        origin/<base> (landed), the main checkout, or an open pane
        titled with the session; lists git-ignored files the removal destroys; needs --yes.
        copy the unit memory files (+ other ORCHE-owned untracked files under `_worktree/`) to the
        archive dir -> tag `archive/feat-<name>-<yyyymmdd>` -> delete the ORCHE-owned untracked
        files from the worktree -> `orca worktree rm` -> archive the unit's transcripts ->
        GRAVEYARD row -> status `retired` + event + hr-log. A re-run reuses byte-identical kept
        files and a tag that already points at the branch head.
"""
import filecmp
import shlex
import shutil
import sys
from pathlib import Path

from .. import janitor, orca, roster, shell, units
from ..cli import EXIT_BLOCKED, EXIT_USER, dry, warn
from ..janitor import archive, discover, graveyard
from . import book

FINAL_LINES = 20


def has_final(handoff) -> bool:
    try:
        with open(handoff, encoding="utf-8") as fh:
            head = [next(fh, "") for _ in range(FINAL_LINES)]
    except OSError:
        return False
    return any(line.strip().lower().startswith("final:") for line in head)


WORKTREE_COPY = "_worktree"   # ORCHE files outside the unit dir, kept under <kept>/_worktree/<rel>


def _retire_problem(unit, proj, kept):
    """(problem, owned, why): a refusal text or None, the ORCHE-owned untracked paths of the worktree,
    and why it may retire: "HANDOFF final", or "landed" (its branch head is in origin/<base>).

    Untracked ORCHE-owned paths are expected (they are kept, then cleared before `worktree rm`);
    anything else — including a tracked ORCHE file with uncommitted changes — is uncommitted work.
    """
    wt = str(unit.worktree or Path("."))
    if not wt or units.is_within(proj["path"], wt) or discover.real(wt) == discover.real(proj["path"]):
        return f"{unit.session}'s worktree is the project's main checkout; refusing to remove it", [], None
    res = shell.git(wt, "status", "--porcelain", "--untracked-files=all", timeout=60)
    if not res.ok:
        return f"cannot read git status of {wt}", [], None
    lines = [ln for ln in res.out.splitlines() if ln.strip()]
    owned = [units.porcelain_path(ln) for ln in lines
             if ln.startswith("??") and units.orche_owned(units.porcelain_path(ln), unit.session)]
    foreign = [units.porcelain_path(ln) for ln in lines
               if not (ln.startswith("??") and units.orche_owned(units.porcelain_path(ln), unit.session))]
    if foreign:
        listed = ", ".join(foreign[:10]) + (" …" if len(foreign) > 10 else "")
        return (f"worktree has uncommitted work: {listed}; commit or stash it "
                f"(or `orche report stop`) before retiring"), owned, None
    # A re-run after a failed `worktree rm` finds the unit files already cleared: the kept HANDOFF counts.
    own = unit.memory / "HANDOFF.md"
    handoff = own if own.exists() else kept / "HANDOFF.md"
    if has_final(handoff):
        return None, owned, "HANDOFF final"
    if _landed(wt, unit.branch, proj, unit.project):
        return None, owned, "landed"
    return f"HANDOFF.md has no 'final:' line in its first {FINAL_LINES} lines: {own}", owned, None


def _landed(wt, branch, proj, project) -> bool:
    """The worker's branch head is already in origin/<base> (fetched first; a failed fetch uses what is
    there), or the merge queue logged it `merged` (a squash merge leaves no ancestor: id `pr<N>-<head7>`)."""
    from .. import publish, queue   # here, not at the top: hr stays free of the git/gh publish layer at import
    base = publish.base(proj["path"], worktree=wt, project=project)
    if not base or not branch:
        return False
    shell.git(wt, "fetch", "origin", base, timeout=publish.FETCH_TIMEOUT)
    if shell.git(wt, "merge-base", "--is-ancestor", branch, f"origin/{base}").ok:
        return True
    head = shell.git(wt, "rev-parse", branch, timeout=20)
    h7 = head.out.strip()[:7] if head.ok else ""
    return bool(h7) and h7 in queue.landed_heads(project)


def _keep_sources(unit, wt, owned) -> dict:
    """{path inside kept: source file} — the unit dir as is, other ORCHE-owned files under _worktree/."""
    unit_dir = unit.memory
    sources = {}
    if unit_dir.is_dir():
        sources.update({p.relative_to(unit_dir): p for p in sorted(unit_dir.rglob("*")) if p.is_file()})
    prefix = f".orche/{unit.kind}/{unit.name}/"
    for rel in owned:
        if not rel.startswith(prefix) and (Path(wt) / rel).is_file():
            sources[Path(WORKTREE_COPY) / rel] = Path(wt) / rel
    return sources


def _kept_matches(kept, sources) -> bool:
    """True when every unit-dir file already sits byte-identical in `kept` (a re-run of a retirement)."""
    return all((kept / rel).is_file() and filecmp.cmp(src, kept / rel, shallow=False)
               for rel, src in sources.items() if rel.parts[0] != WORKTREE_COPY)


def _clear_owned(wt, owned) -> None:
    """Delete the ORCHE-owned untracked files (already kept) and the directories they leave empty."""
    root = Path(wt)
    for rel in owned:
        path = root / rel
        if path.is_file() or path.is_symlink():
            path.unlink()
        parent = path.parent
        while parent != root and units.is_within(parent, root) and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def _tag_at_head(wt, tag, branch) -> bool:
    """True when `tag` already exists and points at the branch head (a re-run may reuse it)."""
    have = shell.git(wt, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", timeout=30)
    head = shell.git(wt, "rev-parse", "-q", "--verify", f"{branch}^{{commit}}", timeout=30)
    return have.ok and head.ok and have.out.strip() == head.out.strip() != ""


def _transcripts(unit, proj):
    root = janitor.configured_dir(janitor.settings()["claude_projects"])
    wt, main = discover.real(str(unit.worktree or Path("."))), discover.real(proj["path"])

    def owns(cwd):
        real = discover.real(cwd)
        return bool(real and wt and real != main and units.is_within(real, wt))
    files = discover.session_files(root, unit.session, owns)
    keep = {proj.get("main_session")}
    files = [f for f in files if not any(part.removesuffix(".jsonl") in keep for part in f.parts)]
    return root, files


def _pane_problem(unit, key, name):
    """Refusal text while a pane titled with the session is open (or Orca cannot be listed)."""
    try:
        term = orca.find_terminal_by_title(unit.session)
    except orca.OrcaError as exc:
        return f"cannot list Orca panes to check {unit.session} is closed: {exc}"
    if term is not None:
        return f"pane open — run `orche feat sleep {key} {name}` first"
    return None


def _ignored_files(wt) -> list:
    """Git-ignored files in the worktree (outside `.orche/`) that `orca worktree rm` destroys."""
    res = shell.git(wt, "ls-files", "--others", "--ignored", "--exclude-standard", timeout=60)
    if not res.ok:
        return []
    return [ln for ln in res.out.splitlines() if ln.strip() and not ln.startswith(".orche/")]


def retire(reg, unit, *, yes=False, dry_run=False):
    """Retire feature agent `unit` (a roster.Member of `reg`): -> (data, line) for the CLI.
    Raises book.Refused (code = the CLI exit code) where `orche feat retire` used to fail."""
    key, name, proj = unit.project, unit.name, reg["projects"][unit.project]
    if unit.entry["status"] == "retired":
        return {"feature": name, "status": "retired", "changed": False}, f"{unit.session} is already retired"
    if unit.entry["status"] == "onboarding":
        raise book.Refused(EXIT_BLOCKED, f"{unit.session} is still onboarding; finish it with "
                                         f"`orche feat new {key} {name}` (or remove the entry) before retiring")
    tag = f"archive/feat-{name}-{units.now().strftime('%Y%m%d')}"
    unit_dir, kept = unit.memory, archive.unit_dir(unit.session) / "unit-files" / tag
    problem, owned, why = _retire_problem(unit, proj, kept)
    if problem:
        raise book.Refused(EXIT_BLOCKED, problem)
    if dry_run:
        dry(f"check no Orca pane is titled {unit.session}")
    pane = _pane_problem(unit, key, name)
    if pane:
        raise book.Refused(EXIT_BLOCKED, pane)
    if not yes and not dry_run:
        raise book.Refused(EXIT_USER, f"retiring {unit.session} removes its worktree; re-run with --yes")
    wt, branch = str(unit.worktree or Path(".")), unit.entry["branch"]
    tag_argv = ["git", "-C", wt, "tag", tag, branch]
    claude_root, files = _transcripts(unit, proj)
    sources = _keep_sources(unit, wt, owned)
    reuse = kept.is_dir() and any(kept.iterdir())
    if reuse and not _kept_matches(kept, sources):
        raise book.Refused(EXIT_USER, f"{kept} already holds the unit files of an earlier retirement of {unit.session} "
                                      f"(tag {tag}); refusing to overwrite it; retry tomorrow or move it aside")
    doomed = _ignored_files(wt)
    if doomed:
        line = (f"{len(doomed)} git-ignored file(s) will be destroyed by worktree rm: "
                + ", ".join(doomed[:10]) + (" …" if len(doomed) > 10 else ""))
        (dry if dry_run else warn)(line)
    extra = sum(1 for rel in sources if rel.parts[0] == WORKTREE_COPY)
    if dry_run:
        dry(f"reuse {kept} (same unit files)" if reuse else f"copy {unit_dir} -> {kept}")
        if extra and not reuse:
            dry(f"copy {extra} other ORCHE file(s) -> {kept / WORKTREE_COPY}")
        dry(f"reuse tag {tag} (points at {branch})" if _tag_at_head(wt, tag, branch) else shlex.join(tag_argv))
        if owned:
            dry(f"delete {len(owned)} ORCHE-owned untracked file(s) from {wt}")
        orca.worktree("rm", "--worktree", f"id:{unit.entry['worktree']}", dry_run=True)
        dry(f"archive {len(files)} transcript file(s) of {unit.session}" if files
            else f"no transcripts found for {unit.session}")
        dry(f"append GRAVEYARD row in {graveyard.project_file(proj['path'])}")
        return ({"feature": name, "dry_run": True, "tag": tag, "transcripts": len(files)},
                f"would retire {unit.session} (dry-run)")
    try:  # an empty leftover dir is reused; on a re-run only the other ORCHE files are (re)copied
        for rel, src in sources.items():
            if not reuse or rel.parts[0] == WORKTREE_COPY:
                (kept / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, kept / rel)
    except (OSError, shutil.Error) as exc:
        raise book.Refused(EXIT_USER, f"could not keep the unit files of {unit.session} in {kept}: {exc}; "
                                      f"nothing removed") from None
    if reuse:
        print(f"orche: unit files of {unit.session} already kept in {kept}; reusing them", file=sys.stderr)
    else:
        print(f"orche: unit files of {unit.session} kept in {kept}", file=sys.stderr)
    if _tag_at_head(wt, tag, branch):
        print(f"orche: tag {tag} already points at {branch}; reusing it", file=sys.stderr)
    else:
        res = shell.run(tag_argv, timeout=30)
        if not res.ok:
            raise book.Refused(EXIT_USER, f"git tag {tag} failed: {res.err.strip()}")
    try:
        _clear_owned(wt, owned)
    except OSError as exc:
        raise book.Refused(EXIT_USER, f"could not clear ORCHE files from {wt}: {exc} (tag {tag} and unit files in "
                                      f"{kept} kept; nothing else removed)") from None
    try:
        orca.worktree("rm", "--worktree", f"id:{unit.entry['worktree']}")
    except orca.OrcaError as exc:
        raise book.Refused(EXIT_USER, f"orca worktree rm failed: {exc} (tag {tag} and unit files in {kept} kept; "
                                      f"re-run `orche feat retire {key} {name} --yes` once it can be removed)") from None
    by, now, archived = roster.actor(), units.now(), None
    if files:
        def record(m):
            graveyard.record(proj["path"], f"archived {m['count']} session files of {unit.session}",
                             "feature retired", f"orche janitor restore {unit.session}", by, project=key,
                             date=now.date())
        try:
            archived = archive.archive_sessions(unit.session, files, claude_root, trigger="retired",
                                                reason="feature retired", project=key, now=now,
                                                record=record)["archive"]
        except (archive.ArchiveError, OSError) as exc:
            warn(f"transcript archive failed: {exc}; the janitor will retry")
    else:
        book.log(f"retire {unit.session}: no transcripts to archive")
    try:
        graveyard.record(proj["path"], f"feature {name} (worktree {wt}, branch {branch})",
                         f"retired: {why}", f"git checkout -b {branch} {tag}; unit files: {kept}", by,
                         project=key,
                         date=now.date())
    except OSError as exc:
        gy = graveyard.project_file(proj["path"])
        book.log(f"retire {unit.session}: GRAVEYARD row failed ({exc}); add it by hand to {gy}")
        warn(f"could not write the GRAVEYARD row ({exc}); add by hand to {gy}: feature {name}, "
             f"recovery: git checkout -b {branch} {tag}; unit files: {kept}")
    book.record(reg, f"retire {unit.session} by {by} (tag {tag})", change=lambda r: _retired(r, unit),
                event={"type": "retire", "project": key, "unit": unit.event_unit, "by": by},
                note=f"Feature agent {unit.session} retired")
    return ({"feature": name, "status": "retired", "tag": tag, "archive": archived},
            f"{unit.session} retired (tag {tag})")


def retire_gone(reg, unit, *, by) -> None:
    """Retire a unit whose worktree Orca no longer lists (`orche janitor run --apply-removed`). The caller
    has written its GRAVEYARD row first (R-JAN-3); no Orca step, no notification."""
    book.record(reg, f"retire {unit.session} by {by} (worktree gone from Orca)", change=lambda r: _retired(r, unit),
                event={"type": "retire", "project": unit.project, "unit": unit.event_unit, "by": by})


def _retired(reg, unit) -> None:
    book.entry_in(reg, unit)["status"] = "retired"
