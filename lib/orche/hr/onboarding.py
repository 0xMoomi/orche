"""Shared onboarding for divisions (`orche init`) and feature agents (`orche feat new`).

Steps: Orca worktree -> unit memory files (`.orche/<div|feat>/<name>/`) -> worktree
`.claude/settings.local.json` (merged) -> persona `.claude/agents/<session>.md` -> optional pane.
The caller writes the registry entry. Under dry-run every step prints its argv / target and
nothing is written; worktree paths are shown as `<name worktree>` placeholders.
"""
import json
import os
from pathlib import Path

from .. import cmd_sleep_wake, log, orca, shell, templates, units, util
from . import dry

DIVISION_MODELS = {
    "rnd": ("claude-opus-5-5", "high"),
    "research": ("claude-sonnet-5-5", "medium"),
    "build": ("claude-opus-5-5", "high"),
    "gtm": ("claude-opus-5-5", "high"),
    "ops": ("claude-opus-5-5", "high"),
}
FEATURE_MODEL = ("claude-opus-5-5", "high")
PERSONAS = templates.TEMPLATES_ROOT / "personas"
SETTINGS_TMPL = templates.TEMPLATES_ROOT / "project" / ".claude" / "settings.local.json.tmpl"
ROLE_TEXT = {
    "division": "You run the {name} division of {project} as a long-lived owner of its charter.",
    "feature": "You own the {name} feature of {project}, inside its scope globs only.",
    "liaison": "You answer other projects' questions about {project} from PROJECT_CARD.md and the repo.",
}


EXCLUDE_LINE = ".claude/settings.local.json"
UNITS_EXCLUDE = ".orche/units/"   # briefs/reports: never tracked, never keep the main checkout dirty
EXCLUDE_LINES = (EXCLUDE_LINE, UNITS_EXCLUDE)


class UnsafeTarget(Exception):
    """A write would land outside its worktree or inside the main checkout (exit 3)."""


def guard(target_dir, base, main_root=None) -> None:
    """Refuse when `target_dir` really resolves outside `base` (e.g. a `.claude` symlink).

    A target inside `base` is fine even when `base` is nested under `main_root`; one that
    resolves into the main checkout outside `base` is named as such in the refusal.
    """
    real = os.path.realpath(target_dir)
    if units.is_within(real, base):
        return
    if main_root and units.is_within(real, main_root):
        raise UnsafeTarget(f"{target_dir} resolves into the main checkout ({real}); refusing to write")
    raise UnsafeTarget(f"{target_dir} resolves to {real}, outside {base}; refusing to write")


def exclude_settings(worktree_dir, *, dry_run=False):
    """Add `.claude/settings.local.json` and `.orche/units/` to the repository's shared
    `info/exclude` (each once).

    Returns the exclude file path, or None when the worktree is not a git checkout.
    """
    return exclude_lines(worktree_dir, EXCLUDE_LINES, dry_run=dry_run)


def exclude_lines(worktree_dir, lines, *, dry_run=False):
    """Add each of `lines` (once) to the shared `info/exclude` of the checkout holding `worktree_dir`.

    Returns the exclude file path, or None when it is not a git checkout.
    """
    if dry_run:
        dry(f"append {' '.join(lines)} to the info/exclude of {worktree_dir} (once)")
        return None
    res = shell.run(["git", "-C", str(worktree_dir), "rev-parse", "--git-path", "info/exclude"], timeout=30)
    if not res.ok or not res.out.strip():
        log.log("hr", f"info/exclude not updated: {worktree_dir} is not a git checkout")
        return None
    path = Path(res.out.strip())
    path = path if path.is_absolute() else Path(worktree_dir) / path
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = {ln.strip() for ln in text.splitlines()}
    missing = [ln for ln in lines if ln not in have]
    if not missing:
        return path
    sep = "" if not text or text.endswith("\n") else "\n"
    util.atomic_write(path, text + sep + "".join(f"{ln}\n" for ln in missing))
    return path


def parse_worktree(result, repo_id, name, fallback_branch):
    """(worktree id `<repoId>::<path>`, path, branch) from an `orca worktree create` result."""
    item = result.get("worktree", result) if isinstance(result, dict) else {}
    item = item if isinstance(item, dict) else {}
    path = item.get("path") or (item.get("git") or {}).get("path")
    if not path:
        raise orca.OrcaError(f"worktree create for {name} returned no path", "bad_output")
    wt_id = item.get("id") if "::" in str(item.get("id", "")) else f"{repo_id}::{path}"
    branch = str(item.get("branch") or (item.get("git") or {}).get("branch") or "")
    branch = branch.removeprefix("refs/heads/") or fallback_branch
    return wt_id, path, branch


def find_worktree(repo_id, name):
    """(id, path, branch) of an Orca worktree of `repo_id` already named `name`, else None."""
    try:
        result = orca.worktree("list")
    except orca.OrcaError as exc:
        log.log("hr", f"worktree list failed, creating {name} without the duplicate check: {exc}")
        return None
    items = result.get("worktrees") if isinstance(result, dict) else result
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        same_repo = item.get("repoId") == repo_id or str(item.get("id", "")).startswith(f"{repo_id}::")
        if same_repo and name in (item.get("displayName"), os.path.basename(str(item["path"]).rstrip("/"))):
            return parse_worktree(item, repo_id, name, "")
    return None


def create_worktree(repo_id, name, fallback_branch, *, parent=None, dry_run=False):
    argv = ["--repo", f"id:{repo_id}", "--name", name]
    argv += ["--parent-worktree", f"id:{parent}"] if parent else ["--no-parent"]
    if dry_run:
        orca.worktree("create", *argv, dry_run=True)
        path = f"<{name} worktree>"
        return f"{repo_id}::{path}", path, fallback_branch
    return parse_worktree(orca.worktree("create", *argv), repo_id, name, fallback_branch)


def write_settings(worktree_dir, agent, *, main_root=None, dry_run=False) -> Path:
    """Merge `{"agent", "crossSessionInbound"}` into the worktree's settings.local.json."""
    target = Path(worktree_dir) / ".claude" / "settings.local.json"
    if dry_run:
        dry(f"write {target} agent={agent}")
        return target
    guard(target.parent, worktree_dir, main_root)
    rendered = json.loads(templates.render(SETTINGS_TMPL.read_text(encoding="utf-8"), SESSION=agent))
    current = util.read_json(target, default={}) if target.exists() else {}
    if not isinstance(current, dict):
        raise util.JsonError(target, "not a JSON object")
    util.write_json(target, {**current, **rendered})
    return target


def persona_template(role, name):
    """Most specific persona template that exists, else None."""
    names = {"division": [f"div-{name}.md", "division.md"], "feature": ["feature.md"],
             "liaison": ["liaison.md"]}[role]
    return next((PERSONAS / n for n in names if (PERSONAS / n).is_file()), None)


def persona_text(role, *, project, name, session, model, effort):
    tmpl = persona_template(role, name)
    if tmpl is not None:
        return templates.render(tmpl.read_text(encoding="utf-8"), PROJECT=project, NAME=name,
                                SESSION=session, MODEL=model, EFFORT=effort), True
    agent = "liaison" if role == "liaison" else session
    text = (f"---\nname: {agent}\ndescription: ORCHE {role} agent for {project} ({name})\nmodel: {model}\n---\n"
            f"{ROLE_TEXT[role].format(name=name, project=project)}\n"
            "Read .orche/STANDING.md first; keep your HANDOFF.md current before any pause.\n")
    return text, False


def write_persona(dirs, agent, role, *, project, name, session, model, effort, main_root=None,
                  dry_run=False) -> list:
    """Write `<dir>/.claude/agents/<agent>.md` into each of `dirs` (never overwrite). Returns written paths.

    Each target must resolve inside its own dir and, for dirs other than `main_root`, outside it.
    """
    text, from_template = persona_text(role, project=project, name=name, session=session, model=model,
                                       effort=effort)
    if not from_template and not dry_run:
        units.hr_log(f"persona template missing for {role} {name}; wrote a minimal persona for {agent}")
    written = []
    for d in dirs:
        target = Path(d) / ".claude" / "agents" / f"{agent}.md"
        if dry_run:
            dry(f"write {target}" + ("" if from_template else " (minimal persona)"))
            continue
        own_main = main_root and os.path.realpath(d) == os.path.realpath(main_root)
        guard(target.parent, d, None if own_main else main_root)
        if target.exists():
            continue
        util.atomic_write(target, text)
        written.append(target)
    return written


def open_pane(worktree_id, session, command, *, dry_run=False):
    return orca.terminal("create", "--worktree", f"id:{worktree_id}", "--title", session,
                         "--command", command, dry_run=dry_run)


def onboard(*, project, repo_root, repo_id, kind, name, session, worktree_name, branch, model, effort,
            wake, parent=None, existing=None, on_created=None, dry_run=False) -> dict:
    """Create one unit's worktree, files and (if `wake`) pane. Returns the partial registry entry.

    `existing` (an `onboarding` registry entry) resumes: only the file steps and the pane run.
    Otherwise an Orca worktree already named `worktree_name` is adopted, else one is created,
    and `on_created({"worktree", "branch"})` lets the caller save a provisional entry at once.
    """
    if existing:
        wt_id, branch = existing["worktree"], existing["branch"]
        wt_path = units.worktree_path(wt_id)
    else:
        found = None if dry_run else find_worktree(repo_id, worktree_name)
        if found:
            wt_id, wt_path, branch = found[0], found[1], found[2] or branch
        else:
            wt_id, wt_path, branch = create_worktree(repo_id, worktree_name, branch, parent=parent,
                                                     dry_run=dry_run)
    if not dry_run and units.is_within(repo_root, wt_path):
        raise orca.OrcaError(f"worktree for {name} resolved to the main checkout {wt_path}", "bad_output")
    if on_created and not existing and not dry_run:
        on_created({"worktree": wt_id, "branch": branch})
    unit_dir = Path(wt_path) / ".orche" / kind / name
    role = "division" if kind == "div" else "feature"
    if dry_run:
        dry(f"write {unit_dir}/{{{','.join(templates.UNIT_FILES)}}}")
    else:
        guard(unit_dir.parent, wt_path, repo_root)
        guard(unit_dir, wt_path, repo_root)
        templates.copy_unit(unit_dir, role, PROJECT=project, UNIT=name, SESSION=session)
    write_settings(wt_path, session, main_root=repo_root, dry_run=dry_run)
    exclude_settings(wt_path, dry_run=dry_run)
    write_persona([repo_root, wt_path], session, role, project=project, name=name, session=session,
                  model=model, effort=effort, main_root=repo_root, dry_run=dry_run)
    if wake:
        open_pane(wt_id, session, cmd_sleep_wake.pane_command(session, resume=False), dry_run=dry_run)
    now = util.now_iso()
    return {"status": "active" if wake else "dormant", "worktree": wt_id, "branch": branch,
            "session": session, "model": model, "effort": effort, "last_activity": now}
