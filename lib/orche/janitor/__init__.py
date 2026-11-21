"""Janitor: state-based cleanup (SPEC §11).

Only state retires things: registry `retired`, `docs/GRAVEYARD.md` pointer rows, a worktree gone
from Orca (confirmed with `--apply-removed`), `orche discard`, a worker whose work landed and that is
quiet `idle_days` (retired through hr; a retired worker's leftover worktree is removed through Orca).
Age only touches regenerable artifacts. Sessions are archived, never deleted; files go to a 30-day
trash, except git-ignored dependencies and build output, which are deleted outright with a row in the
global graveyard only. A large git-ignored folder that is not known to be cheap to recreate is never
deleted: one urgent `info` per project asks Main to put it to the owner (`asked` in state.json, once per
path). Every action writes its GRAVEYARD row (and a manifest for archive and trash) before anything is
removed.

Layout: `rules` decides (plan, no side effects), `discover` reads git/Orca/filesystem,
`archive` / `trash` act, `graveyard` records. This module runs one pass and renders the report.

The heartbeat (reconcile) starts `orche janitor run` detached when the last pass is DAILY_S old (the
report's mtime: every pass, dry or live, writes it), or older than the start of a brief day whose daily
brief is not built yet (flow/daily.py), and no pass holds `janitor/.pass.lock`.
"""
import contextlib
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .. import config, log, paths, registry, roster, units, util

DEFAULTS = {
    # Opt-in: set config `janitor.codex_tmp` to an absolute path, e.g.
    # "~/Library/Application Support/Orca/codex-runtime-home/home/.tmp".
    "codex_tmp": None,
    "scratch_root": f"/private/tmp/claude-{os.getuid()}",
    "claude_projects": "~/.claude/projects",
    "idle_days": 7,
    "trash_days": 30,
    # fnmatch patterns on a directory's own name: regenerable dependencies and build output
    "cache_names": [".next", ".next-*", "node_modules", "dist", "out", "build", "coverage", ".turbo"],
}
ACTOR = "janitor"
DAILY_S = 24 * 3600         # the heartbeat starts a pass when the last one is this old
LOCK_STALE_S = 2 * 3600     # a `.pass.lock` this old belongs to a dead pass


@dataclass
class Candidate:
    trigger: str            # retired | graveyard | orca-removed | landed | age:build-cache | age:costly | age:codex-tmp | age:scratch | age:trash
    action: str             # archive | trash | delete | purge | retire | remove | ask | skip
    target: str             # what the report shows
    reason: str
    size: int = 0
    kind: str = "path"      # path | sessions | purge | unit | worktree
    path: str = ""          # trash/delete/purge/remove/ask: absolute path
    root: str = ""          # trash: base for the relative path kept inside the trash; delete: the worktree
    unit: str = ""          # sessions/unit: session name
    project: str = ""
    files: list = field(default_factory=list)
    rows: list = field(default_factory=list)   # GRAVEYARD row hashes this candidate settles
    settle: bool = False    # a skip that still settles its rows (nothing left to do)
    result: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", [], False) or k in ("action", "size")}


def settings(claude_projects=None) -> dict:
    cfg = config.load().get("janitor") or {}
    out = {**DEFAULTS, **(cfg if isinstance(cfg, dict) else {})}
    if claude_projects:
        out["claude_projects"] = claude_projects
    return out


def configured_dir(value):
    """Resolved absolute path for a configured directory, or None (unset, empty, `.`, relative)."""
    from . import discover
    return discover.real(value)


def state_dir() -> Path:
    return paths.orche_home() / "janitor"


def enabled_flag() -> Path:
    return paths.orche_home() / "janitor.enabled"


def report_path() -> Path:
    return state_dir() / "last-report.md"


def lock_path() -> Path:
    return state_dir() / ".pass.lock"


def last_pass_age(now=None):
    """Seconds since the last pass (the report's mtime; `now` epoch seconds, default the clock), or None."""
    try:
        return (time.time() if now is None else now) - report_path().stat().st_mtime
    except FileNotFoundError:
        return None


def due(now=None) -> bool:
    """No pass yet, or the last one is DAILY_S old."""
    age = last_pass_age(now)
    return age is None or age >= DAILY_S


def running(now=None) -> bool:
    """A pass holds `.pass.lock` and the lock is younger than LOCK_STALE_S."""
    try:
        return (time.time() if now is None else now) - lock_path().stat().st_mtime < LOCK_STALE_S
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def pass_lock():
    """Hold `.pass.lock` (O_EXCL; a stale one is taken over) for one `orche janitor run`: yields True,
    or False when a live pass holds it; removed at the end."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    held = False
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            held = True
            break
        except FileExistsError:
            if running():
                break
            path.unlink(missing_ok=True)
    try:
        yield held
    finally:
        if held:
            path.unlink(missing_ok=True)


def ago(seconds) -> str:
    """`12m`, `3h`, `2d`."""
    s = max(0, int(seconds))
    return f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"


def last_pass_text(now=None) -> str:
    age = last_pass_age(now)
    return "never" if age is None else f"{ago(age)} ago"


def brief_due(now=None) -> bool:
    """A daily brief waits for this pass (flow.daily.wanted): a brief day began after the last pass."""
    from ..flow import daily
    try:
        reg = registry.load()
    except registry.RegistryError:
        return False
    try:
        at = report_path().stat().st_mtime
    except FileNotFoundError:
        at = None
    return daily.wanted(reg, now or units.now(), config.load(), at)


def heartbeat(dry_run) -> list:
    """The heartbeat's class `janitor` (reconcile): when due() or brief_due() and no pass is running,
    `orche janitor run` started detached, never inline. One row as review's, or none."""
    from .. import procs
    if running() or not (due() or brief_due()):
        return []
    when, failed = last_pass_text(), None
    if not dry_run:
        try:
            procs.spawn_orche(["janitor", "run"], paths.orche_home() / "logs" / "janitor.log")
        except Exception as exc:  # noqa: BLE001 - due again next pass
            failed = type(exc).__name__
            log.log("hooks", f"reconcile: janitor: {failed}: {exc}")
    return [{"action": "janitor", "class": "janitor", "project": None, "target": "janitor", "rev": None,
             "attempt": 1, "cap": 1, "failed": failed,
             "line": f"{'would start' if dry_run else 'start'} the janitor's daily pass (last pass {when})"}]


def load_state() -> dict:
    try:
        data = util.read_json(state_dir() / "state.json", default={}) or {}
    except util.JsonError as exc:
        log.log("janitor", f"state reset: {exc}")
        data = {}
    data.setdefault("processed", {})
    return data


def save_state(state: dict) -> None:
    util.write_json(state_dir() / "state.json", state)


def context(reg, *, apply_removed=False, claude_projects=None):
    from . import discover
    st = settings(claude_projects)
    recs = discover.unit_records(reg)
    retired = {u.session for u in recs if u.status == "retired"}
    every = set(roster.Roster(reg).sessions())
    return SimpleNamespace(
        reg=reg, st=st, now=units.now(), units=recs, retired=retired, all_sessions=every,
        worktrees=discover.orca_worktrees(), terminals=discover.orca_terminals(),
        state=load_state(), apply_removed=apply_removed, home=paths.orche_home(),
        claude_root=_opt_path(configured_dir(st["claude_projects"])), wip={}, silent_rows=[], leaving=set())


def _opt_path(value):
    return Path(value) if value else None


def untouchable_sessions(ctx) -> set:
    return ctx.all_sessions - ctx.retired


def run(*, dry_run, apply_removed=False, claude_projects=None) -> dict:
    """One pass. Live only when `janitor.enabled` exists and `dry_run` is false."""
    from . import rules
    paths.ensure_dirs()
    reg = registry.load()
    ctx = context(reg, apply_removed=apply_removed, claude_projects=claude_projects)
    cands = rules.plan(ctx)
    live = enabled_flag().exists() and not dry_run
    if live:
        execute(ctx, cands)
    mode = "live" if live else "dry-run"
    text = render_report(mode, ctx.now, cands)
    util.atomic_write(report_path(), text)
    log.log("janitor", f"run mode={mode} candidates={len(cands)} "
                       f"errors={sum(1 for c in cands if c.error)}")
    return {"mode": mode, "enabled": enabled_flag().exists(), "report": str(report_path()),
            "candidates": cands, "text": text}


def _project_root(ctx, key):
    proj = ctx.reg.get("projects", {}).get(key or "") or {}
    return proj.get("path") or None


def _settle(ctx, hashes, result):
    for h in hashes:
        ctx.state["processed"][h] = {"at": ctx.now.isoformat(timespec="seconds"), "result": result}


def _recorder(ctx, cand, what, recovery_fn):
    from . import graveyard

    def record(info):
        graveyard.record(_project_root(ctx, cand.project), what, f"{cand.trigger}: {cand.reason}",
                         recovery_fn(info), ACTOR, project=cand.project, date=ctx.now.date())
    return record


def _retire(ctx, cand):
    from .. import hr                      # function level: hr/retire.py imports the janitor's modules
    from . import archive, graveyard
    rec = next(u for u in ctx.units if u.session == cand.unit)
    unit = roster.Roster(ctx.reg).member(rec.session)
    if unit is None or unit.entry is not rec.entry:
        raise ValueError(f"no registered unit for {rec.session!r}")
    graveyard.record(_project_root(ctx, rec.project), f"unit {rec.session} (worktree {rec.raw_worktree})",
                     f"{cand.trigger}: {cand.reason}", f"session archive: {archive.unit_dir(rec.session)}",
                     ACTOR, project=rec.project, date=ctx.now.date())
    hr.retire_gone(ctx.reg, unit, by=ACTOR)
    return "retired"


def _retire_landed(ctx, cand):
    """A landed, quiet worker: `orche feat retire --yes` by the janitor (hr re-checks, with a fetch)."""
    from .. import hr                      # function level: hr/retire.py imports the janitor's modules
    unit = roster.Roster(ctx.reg).member(cand.unit)
    if unit is None or unit.kind != "feat":
        raise ValueError(f"no registered worker {cand.unit!r}")
    _data, line = hr.retire(ctx.reg, unit, yes=True, by=ACTOR)
    return line


def _delete(ctx, cand):
    """A regenerable directory: one row in the global graveyard (the project's file is tracked), then rmtree."""
    import shutil
    from . import graveyard
    path, root = cand.path, cand.root
    if not root or os.path.islink(path) or not os.path.isdir(path) \
            or not units.is_within(path, root) or units.real(path) == units.real(root):
        raise ValueError(f"not a directory strictly inside {root or '?'}: {path}")
    graveyard.record(None, f"deleted {cand.target}", f"{cand.trigger}: {cand.reason}",
                     "none: regenerable (reinstall or rebuild)", ACTOR, project=cand.project, date=ctx.now.date())
    shutil.rmtree(path)
    return "deleted"


def _remove(ctx, cand):
    """A retired worker's leftover worktree: GRAVEYARD row naming the branch, then `orca worktree rm`."""
    from .. import orca
    rec = next(u for u in ctx.units if u.session == cand.unit)
    branch, name = rec.entry.get("branch", ""), rec.entry.get("name", "")
    _recorder(ctx, cand, f"worktree {cand.path} of retired unit {cand.unit}",
              lambda _: f"branch {branch} was tagged at retirement: git tag -l 'archive/feat-{name}-*'")(None)
    orca.worktree("rm", "--worktree", f"id:{rec.raw_worktree}")
    return "removed"


ASK_SUMMARY = ("janitor: {n} large folders ({size}) in quiet worktrees were left alone because recreating them "
               "may be slow; ask the owner before removing them")


def _ask_owner(ctx, asks) -> None:
    """One urgent `info` to each project's Main listing its new `ask` folders (REPORT.md beside the state), then
    the paths go into `asked`. A paused project or one with no Main is not asked now: a later pass asks."""
    from .. import inbox
    from ..flow import guard
    asked = ctx.state.setdefault("asked", {})
    members = roster.Roster(ctx.reg)
    for key, cands in sorted(asks.items()):
        main = members.main(key)
        why = "no Main registered" if main is None else "project paused" if guard.paused(key) else None
        if why:
            for c in cands:
                c.result = f"not asked: {why}"
            continue
        try:
            report = state_dir() / "asked" / key / "REPORT.md"
            lines = [f"# Large folders left alone in {key}", "",
                     "Recreating them may be slow; the owner decides. Delete exactly these on a yes:", ""]
            lines += [f"- {c.path} ({human_size(c.size)}, unit {c.unit})" for c in cands]
            util.atomic_write(report, "\n".join(lines) + "\n")
            summary = ASK_SUMMARY.format(n=len(cands), size=human_size(sum(c.size for c in cands)))
            inbox.post(inbox.make("janitor", ACTOR, main.session, "info", brief_rev=0, urgent=True,
                                  summary=summary, report_path=str(report)))
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the pass; a later pass asks
            for c in cands:
                c.error = str(exc)
            log.log("janitor", f"FAILED ask {key}: {exc}")
            continue
        for c in cands:
            asked[c.path] = ctx.now.isoformat(timespec="seconds")
            c.result = "asked"
        log.log("janitor", f"ask {key}: {len(cands)} folders -> {main.session}")


def execute(ctx, cands) -> None:
    """Carry out every non-skip candidate in plan order; errors are kept on the candidate."""
    from . import archive, trash
    failed_units, asks = set(), {}
    for c in cands:
        if c.action == "skip":
            if c.settle:
                _settle(ctx, c.rows, f"skipped: {c.reason}")
            continue
        if c.action == "ask":
            at = (ctx.state.get("asked") or {}).get(c.path)
            if at:
                c.result = f"asked {str(at)[:10]}; left for the owner"
            else:
                asks.setdefault(c.project, []).append(c)
            continue
        if c.unit in failed_units:
            c.error = "skipped: retiring the unit failed"
            continue
        try:
            if c.action == "retire":
                c.result = _retire_landed(ctx, c) if c.trigger == "landed" else _retire(ctx, c)
            elif c.action == "delete":
                c.result = _delete(ctx, c)
            elif c.action == "remove":
                c.result = _remove(ctx, c)
            elif c.action == "archive":
                rec = _recorder(ctx, c, f"archived {len(c.files)} session files of {c.unit}",
                                lambda m: f"orche janitor restore {m['unit']}")
                c.result = archive.archive_sessions(c.unit, c.files, ctx.claude_root, trigger=c.trigger,
                                                    reason=c.reason, project=c.project, now=ctx.now,
                                                    record=rec)["archive"]
            elif c.action == "trash":
                rec = _recorder(ctx, c, f"trashed {c.target}", lambda m: f"orche janitor undo {m['id']}")
                c.result = trash.move(c.path, c.root, trigger=c.trigger, reason=c.reason, project=c.project,
                                      size=c.size, now=ctx.now, record=rec)["id"]
            elif c.action == "purge":
                rec = _recorder(ctx, c, f"purged {c.target}", lambda n: f"none: {n} trashed items removed")
                c.result = f"purged {trash.purge(c.path, record=rec)} items"
            _settle(ctx, c.rows, c.result)
            log.log("janitor", f"{c.action} {c.target} -> {c.result}")
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the pass
            c.error = str(exc)
            if c.action == "retire":
                failed_units.add(c.unit)
            log.log("janitor", f"FAILED {c.action} {c.target}: {exc}")
    _ask_owner(ctx, asks)
    _settle(ctx, ctx.silent_rows, "refused: protected path")
    save_state(ctx.state)


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_report(mode, now, cands) -> str:
    lines = [f"# Janitor report: {now.isoformat(timespec='seconds')} ({mode})", ""]
    if mode == "dry-run":
        lines += ["Nothing was changed. Enable with `orche janitor enable`; review this list first.", ""]
    if not cands:
        return "\n".join(lines + ["No candidates.", ""])
    lines += ["| action | trigger | size | target | reason | result |", "|---|---|---|---|---|---|"]
    for c in cands:
        result = f"ERROR: {c.error}" if c.error else c.result
        lines.append("| " + " | ".join(_cell(x) for x in (c.action, c.trigger, human_size(c.size), c.target,
                                                           c.reason, result)) + " |")
    return "\n".join(lines) + "\n"
