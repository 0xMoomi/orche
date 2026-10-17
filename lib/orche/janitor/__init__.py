"""Janitor: state-based cleanup (SPEC §11).

Only state retires things: registry `retired`, `docs/GRAVEYARD.md` pointer rows, a worktree gone
from Orca (confirmed with `--apply-removed`), `orche discard`. Age only touches regenerable
artifacts. Sessions are archived, never deleted; files go to a 30-day trash. Every action writes
a manifest and a GRAVEYARD row before anything is removed.

Layout: `rules` decides (plan, no side effects), `discover` reads git/Orca/filesystem,
`archive` / `trash` act, `graveyard` records. This module runs one pass and renders the report.
"""
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

from .. import config, log, paths, registry, units, util

DEFAULTS = {
    # Opt-in: set config `janitor.codex_tmp` to an absolute path, e.g.
    # "~/Library/Application Support/Orca/codex-runtime-home/home/.tmp".
    "codex_tmp": None,
    "scratch_root": f"/private/tmp/claude-{os.getuid()}",
    "claude_projects": "~/.claude/projects",
    "idle_days": 7,
    "trash_days": 30,
    "cache_names": [".next", "dist", "out", "build", "coverage", ".turbo"],
}
ACTOR = "janitor"


@dataclass
class Candidate:
    trigger: str            # retired | graveyard | orca-removed | age:build-cache | age:codex-tmp | age:scratch | age:trash
    action: str             # archive | trash | purge | retire | skip
    target: str             # what the report shows
    reason: str
    size: int = 0
    kind: str = "path"      # path | sessions | purge | unit
    path: str = ""          # trash/purge: absolute path
    root: str = ""          # trash: base for the relative path kept inside the trash
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
    every = set(units.all_sessions(reg))
    return SimpleNamespace(
        reg=reg, st=st, now=units.now(), units=recs, retired=retired, all_sessions=every,
        worktrees=discover.orca_worktrees(), terminals=discover.orca_terminals(),
        state=load_state(), apply_removed=apply_removed, home=paths.orche_home(),
        claude_root=_opt_path(configured_dir(st["claude_projects"])), wip={}, silent_rows=[])


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
    from . import archive, graveyard
    rec = next(u for u in ctx.units if u.session == cand.unit)
    graveyard.record(_project_root(ctx, rec.project), f"unit {rec.session} (worktree {rec.raw_worktree})",
                     f"{cand.trigger}: {cand.reason}", f"session archive: {archive.unit_dir(rec.session)}",
                     ACTOR, project=rec.project, date=ctx.now.date())
    rec.entry["status"] = "retired"
    registry.append_event(ctx.reg, type="retire", project=rec.project, unit=rec.event_unit, by=ACTOR)
    registry.save(ctx.reg)
    units.hr_log(f"retire {rec.session} by {ACTOR} (worktree gone from Orca)")
    return "retired"


def execute(ctx, cands) -> None:
    """Carry out every non-skip candidate in plan order; errors are kept on the candidate."""
    from . import archive, trash
    failed_units = set()
    for c in cands:
        if c.action == "skip":
            if c.settle:
                _settle(ctx, c.rows, f"skipped: {c.reason}")
            continue
        if c.unit in failed_units:
            c.error = "skipped: retiring the unit failed"
            continue
        try:
            if c.action == "retire":
                c.result = _retire(ctx, c)
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
    lines = [f"# Janitor report — {now.isoformat(timespec='seconds')} ({mode})", ""]
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
