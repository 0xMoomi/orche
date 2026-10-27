"""`orche init [orca-repo-name|path]` — register a project and onboard its divisions.

With no argument the project is the git repository holding the current directory.

The divisions come from a setup (`setup_flow`): `--setup-file`, else `--divisions`/`--custom`/
`--stage`/`--aliases`, else — on a terminal — the interactive checklist, else the setup defaults
(every standard division but `ops`; a registered project keeps what it has). Re-running adds only
divisions that are new; nothing is ever removed. Re-running also re-renders the project's ORCHE-owned
files from the current templates (`hr.refresh_project`: personas in the main checkout and the worktrees,
worktree settings, the card link; an overwritten persona is first copied to
`ORCHE_HOME/backups/<key>/<time>/`), never the user's files, stamps `orche_version` in the registry
(saved only when it changes) and ends by naming the panes to restart. `--print-setup` prints the setup
JSON and exits. `--refresh` (a registered project only; what `orche update` runs per project) does just
that re-render — the card link, personas, worktree settings, the stamp (never a project template
file: one the user deleted stays deleted), the merge queue's check script (verifygate.ensure, as a full init) and
the setup report — and stops: no Orca, panes, automations or intake; setup flags are refused; a folder it may not read is `unreadable (privacy): <path>` (exit 3).

Order: resolve the repo (`orca repo list`, read-only even under --dry-run) -> setup -> project templates +
card symlink -> registry entry -> `.orche/project` marker (the project key, read by the status
line) -> one worktree/unit files/settings/persona per division (all
`dormant` unless `--wake`; `ops` always dormant and gated by stage) -> the `main-<proj>`
pane in the main checkout when missing -> the two weekly Orca automations
(printed as dry-run lines unless `--automations`) -> the project intake (`orche intake`, unless
`--no-intake`; a failure is one "intake skipped: …" line and Ctrl-C one "intake interrupted; …"
line, never a failed init) -> a switch to the `main-<proj>` tab (live only; a failure is a warning) -> on a
live run, the setup report `.orche/SETUP.md` (`write_report`: what was made and the "Any time" list of
`guide.lines`; in `info/exclude`) -> on a first registration, the setup note to Main (`_setup_note`, at most
`inbox.MAX_LINES` lines, pointing at the report) and, as the very last action, closing the one pane that ran
init (`_init_window`; never inside `orche setup` or apply-all).
`--base <branch>`, `--branch-prefix <prefix>` and `--issues <KEY>` (with or without `--refresh`) write the
project's `publish.per_project.<key>.base` / `.branch_prefix` / `.issues` (config.set_project): the branch its
PRs target and its worktrees start from, the first segment of a feature worker's branch and the issue
tracker's team key; the setup report then names them. Before the flags, each run (and `--refresh`, so
`orche update`) reads the repository's team conventions (`detect.conventions`: merged PRs' base, issue-keyed
remote branches, `.entire/`) and writes each one that is still unset (`_detect`; a flag on the same run wins). Each run (and `--refresh`) adds every ORCHE file to the repository's `info/exclude`
(onboarding.exclude_settings).
`--new [private|public|local]` (a folder without `.git` only, else exit 2) first makes the repository, each step
one line and the first failure the end (`_new_repo`): `git init -b main`, a README when the folder is empty, the
merge queue's check script (verifygate.write), the first commit, then for private/public `gh repo create <folder name> --<visibility> --source . --push` (a gh failure
is one line and the setup goes on as local; the setup report then says how to publish it), then the normal flow
on that folder, which adds it to Orca.
A setup's `preset` (`--setup-file`, setup_flow.PRESETS) is recorded on the project; `build` also appends Build's
specialist standing order once (`_apply_preset`, book.correct). A live run (and a live `--refresh`) then makes sure the
merge queue's check script reaches the base branch (`_merge_queue`: verifygate.ensure, one PR at most) before the intake, and the intake is skipped
in an empty repository (fewer than detect.EMPTY_FILES tracked files, stage build).
A path that is a git repository but not in `orca repo list` is added with `orca repo add` once the
setup is confirmed, right before the first write (an abort leaves Orca untouched; --dry-run never adds:
a placeholder repo id `pending` stands in). A linked git worktree is refused. The main checkout's
`.claude/settings.local.json` is never written: every session opened there shares it.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, config, github, hr, intake, mask, orca, paths, privacy, registry, setup_flow, shell, templates, units, util, vendors
from .cli import EXIT_BLOCKED, EXIT_NOT_FOUND, EXIT_OK, EXIT_USER, dry, fail, out, warn
from .hr import onboarding

EXIT_INTERRUPTED = 130   # a setup given wrongly exits EXIT_USER (1), like argparse usage errors here
PERSONAS_UPDATED = "personas updated for {names}: read at their next session start"
SETUP_FLAGS = ("--divisions", "--custom", "--setup-file", "--stage", "--aliases", "--wake")
SETUP_ASK = ('read the report at report: and tell the owner, in their language, what was set up and the "Any time" '
             "list as short lines: translate every line, keep the numbers and the quoted owner words; then ask for "
             "the first request")
SETUP_REPORT = "SETUP.md"
CLOSED = "the window that ran init was closed"
NOT_CLOSED = "could not close the window that ran init: {why}"
ORCHE_PANES = ("main-", "div-", "feat-")
BASE_NAME = re.compile(r"^(?!origin/)(?!.*\.\.)(?!.*//)(?!.*\.lock$)[A-Za-z0-9][A-Za-z0-9._/-]*(?<![./])$")
PREFIX_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
ISSUES_NAME = re.compile(r"^[A-Z][A-Z0-9]*$")
PUBLISH_KEYS = ("base", "branch_prefix", "issues")
VISIBILITY = ("private", "public", "local")
SPECIALISTS = ("Build's workers are specialists: front (screens, client code, the app's look and interaction), back "
               "(API, data, services, jobs), infra (deploy, CI, environments, secrets, cost). The head names each worker "
               "by its area (front-<topic>, back-<topic>, infra-<topic>), scopes it to that area, and splits a unit "
               "that spans two areas into two workers.")
PRESET_LINES = {"build": "- Preset: build-focused (Build's workers specialise in front, back and infra).",
                "operations": "- Preset: operations."}


def register(subparsers):
    p = subparsers.add_parser("init", help="register an Orca repo as a project and onboard its divisions")
    p.add_argument("repo", nargs="?", default=None,
                   help="registry project key or alias, or Orca repo display name, id, or path "
                        "(default: the repository holding the current directory)")
    p.add_argument("--stage", choices=registry.STAGES, default=None, help="default: building")
    p.add_argument("--aliases", default=None, help="comma-separated aliases")
    p.add_argument("--divisions", default=None,
                   help="comma-separated standard divisions (default: all but ops; a registered project "
                        "keeps its own)")
    p.add_argument("--custom", action="append", default=[], metavar="NAME=LABEL:CHARTER[:MODEL[:EFFORT]]",
                   help="add a custom division (repeatable; model default claude-opus-5-5, effort high)")
    p.add_argument("--setup-file", default=None, metavar="JSON",
                   help='{"divisions": [...], "custom": {name: {label, charter[, model, effort]}}, '
                        '"stage": ..., "aliases": [...]}')
    p.add_argument("--print-setup", action="store_true",
                   help="print the project's setup JSON (the defaults for an unregistered repo) and exit")
    p.add_argument("--wake", default="", help="comma-separated divisions to start now (default: none)")
    p.add_argument("--automations", action="store_true", help="create the Orca automations (default: print them)")
    p.add_argument("--no-intake", action="store_true",
                   help="skip the closing project intake (run it later with: orche intake <project>)")
    p.add_argument("--refresh", action="store_true",
                   help="a registered project only: re-render its templates, card link, personas and worktree "
                        "settings, then stop (no Orca, panes, automations or intake; what `orche update` runs)")
    p.add_argument("--base", default=None, metavar="BRANCH",
                   help="the branch this project's PRs target and its worktrees start from (e.g. develop)")
    p.add_argument("--branch-prefix", default=None, metavar="PREFIX",
                   help="the first segment of a feature worker's branch (default feat; e.g. dev)")
    p.add_argument("--issues", default=None, metavar="KEY",
                   help="the issue tracker's team key (e.g. DES: a worker named des-47-<topic> fixes DES-47)")
    p.add_argument("--new", nargs="?", const="private", choices=VISIBILITY, default=None,
                   help="the folder has no git repository yet: create it (git init, a first commit, GitHub "
                        "private by default or public, or local only), then set it up")
    p.set_defaults(func=cmd_init)


def _publish_error(args):
    """The refusal for a malformed --base / --branch-prefix, else None."""
    if args.base is not None and not BASE_NAME.match(args.base):
        return f"--base takes a branch name such as develop (no origin/), not {args.base!r}"
    if args.branch_prefix is not None and not PREFIX_NAME.match(args.branch_prefix):
        return (f"--branch-prefix takes lowercase letters, digits and dashes, starting with a letter (such as dev), "
                f"not {args.branch_prefix!r}")
    if args.issues is not None and not ISSUES_NAME.match(args.issues):
        return f"--issues takes an uppercase team key such as DES, not {args.issues!r}"
    return None


def _detect(args, key, root) -> list:
    """The repository's team conventions (detect.conventions) into `publish.per_project.<key>`, each key only
    while it is unset there and no flag of this run names it (never over a flag or an earlier detection). Returns
    every key whose value now equals the detected one (none a flag of this run names), in PUBLISH_KEYS order,
    plus `entire` when the repository uses Entire (read live, never stored). A dry run reads nothing."""
    from . import detect
    if args.dry_run:
        return []
    found = detect.conventions(root, key, config.load())
    have = ((config.load().get("publish") or {}).get("per_project") or {}).get(key) or {}
    values = {k: found[k] for k in PUBLISH_KEYS
              if k in found and have.get(k) in (None, "") and getattr(args, k, None) is None}
    if values:
        config.set_project("publish", key, **values)
    now = {**have, **values}
    same = [k for k in PUBLISH_KEYS if k in found and now.get(k) == found[k] and getattr(args, k, None) is None]
    return same + (["entire"] if found.get("entire") else [])


def _set_publish(args, key) -> None:
    """--base / --branch-prefix / --issues into `publish.per_project.<key>` (a dry run only prints)."""
    values = {k: getattr(args, k) for k in PUBLISH_KEYS if getattr(args, k) is not None}
    if not values:
        return
    if args.dry_run:
        dry(f"set publish.per_project.{key}: " + ", ".join(f"{k} {v}" for k, v in values.items()))
    else:
        config.set_project("publish", key, **values)


def _publish_facts(key) -> dict:
    """The project's {base, branch_prefix, issues} as config names them (None: not set)."""
    from . import publish
    s = publish.settings(config.load(), key)
    return {k: s.get(k) for k in PUBLISH_KEYS}


def _setup_flags(args) -> list:
    """The setup flags given on the command line, by name."""
    return [f for f, v in zip(SETUP_FLAGS, (args.divisions is not None, args.custom, args.setup_file,
                                            args.stage, args.aliases is not None, args.wake)) if v]


def _registered(needle, reg):
    """The registry key of `needle` (a key, an alias or the main checkout's path), else None."""
    real = os.path.realpath(os.path.expanduser(needle))
    for key, proj in reg["projects"].items():
        if needle in (key, *proj.get("aliases", [])) or os.path.realpath(proj.get("path") or "/nonexistent") == real:
            return key
    return None


def _refresh(args, reg) -> int:
    """`--refresh`: the ORCHE-owned files of a registered project from the current templates —
    the card link, personas and worktree settings (no project template file) — and the
    `orche_version` stamp. Reads no codebase, touches no Orca, never the user's files."""
    given = _setup_flags(args)
    if given or args.print_setup or args.automations:
        return fail(EXIT_USER, f"--refresh only re-renders; drop {', '.join(given) or '--print-setup/--automations'}")
    key = _registered(args.repo, reg)
    if key is None:
        return fail(EXIT_NOT_FOUND, f"--refresh needs a registered project; {args.repo!r} is not one "
                                    "(see: orche projects)")
    proj, dry_run = reg["projects"][key], args.dry_run
    root = os.path.realpath(proj.get("path") or "/nonexistent")
    try:
        os.listdir(root)
    except FileNotFoundError:
        return fail(EXIT_NOT_FOUND, f"project {key}: {root} is missing")
    except PermissionError:
        return fail(EXIT_BLOCKED, _unreadable(root))
    except OSError:
        pass   # not a folder: the isdir check names it
    if not os.path.isdir(root):
        return fail(EXIT_NOT_FOUND, f"project {key}: {root} is missing")
    if not dry_run:
        paths.ensure_dirs()
    detected = _detect(args, key, root)
    _set_publish(args, key)
    link, linked = _link_card(key, root, dry_run=dry_run)
    ts = time.strftime("%Y%m%dT%H%M%S")
    try:
        ref = hr.refresh_project(reg, key, root=root, ts=ts, dry_run=dry_run)
        onboarding.exclude_settings(root, dry_run=dry_run)   # every ORCHE file stays out of git
        if not dry_run:   # origin/HEAD names the base branch a done's PR targets (publish.base); best effort
            shell.git(root, "remote", "set-head", "origin", "-a", timeout=30)
    except onboarding.UnsafeTarget as exc:
        return fail(EXIT_BLOCKED, f"refresh {key} stopped: {exc}")
    except PermissionError as exc:
        return fail(EXIT_BLOCKED, _unreadable(exc.filename or root))
    except (OSError, util.JsonError) as exc:
        return fail(EXIT_USER, f"refresh {key} stopped: {exc}")
    stamped = not dry_run and hr.stamp_version(reg, key, __version__)
    gate_lines = []
    gate = None if dry_run else _merge_queue(reg, key, root, gate_lines)
    report = None if dry_run else _report(reg, key, root, detected=detected, gate=gate)
    refreshed = list(ref.changed) + ([link] if linked in ("created", "relinked") else [])
    restart = [] if dry_run else ([proj["main_session"]] if stamped and proj.get("main_session") else []) + ref.restart
    data = {"project": key, "path": root, "dry_run": dry_run, "refreshed": [str(p) for p in refreshed],
            "kept": [{"path": str(p), "why": w} for p, w in ref.kept], "restart": restart,
            "setup_report": str(report) if report else None, "publish": _publish_facts(key), "merge_queue": gate}
    lines = [f"{'would refresh' if dry_run else 'refreshed'} {key}: "
             + (f"{len(refreshed)} file(s)" if refreshed else "already current")]
    lines += [f"kept {_tilde(Path(p))}: {why}" for p, why in ref.kept] + gate_lines
    if restart:
        lines.append(PERSONAS_UPDATED.format(names=", ".join(restart)))
    out(args, data, lines)
    return EXIT_OK


def _merge_queue(reg, key, root, lines):
    """A live run's merge-queue check script (verifygate.ensure): its state, one `merge queue: …` line on
    `lines` and an open PR kept on the registry entry (`verify_pr`, saved only when it changes); None without
    a git checkout."""
    from . import verifygate
    from .hr import book
    if not (Path(root) / ".git").exists():
        return None
    gate = verifygate.ensure(root, key)
    lines.append(f"merge queue: {verifygate.running(gate)}")
    pr = {"number": gate["number"], "url": gate["url"]} if gate["state"] == "pr" else None
    if pr and key in reg["projects"] and reg["projects"][key].get("verify_pr") != pr:
        book.write(reg, lambda r: r["projects"][key].__setitem__("verify_pr", pr))
    return gate


def _report(reg, key, root, intake_line=None, detected=(), **kw):
    """write_report, best effort: a failure is one warning, never a failed init."""
    try:
        return write_report(reg, key, root, intake_line, detected, **kw)
    except (OSError, util.JsonError) as exc:
        warn(f"setup report not written: {exc}")
        return None


def _unreadable(path) -> str:
    """The refusal for a project folder this process may not read: a macOS privacy denial (a
    protected folder, privacy.protected) or a plain permission."""
    return f"unreadable ({'privacy' if privacy.protected(path) else 'permission'}): {path}"


def _csv(text) -> list:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


class RepoMissing(Exception):
    """A registry project matched but its Orca repo is not in `orca repo list`."""


class LinkedWorktree(Exception):
    """The path is a linked git worktree (its `.git` file points into `<main>/.git/worktrees/`)."""

    def __init__(self, path, main):
        super().__init__(f"{path} is a git worktree of {main}; put ORCHE on the main checkout")


def _find_repo(needle, reg):
    """(Orca repo, registry key or None) for `needle`, else (None, None).

    A registry project key or alias wins (matched to its Orca repo by id or path, and the project
    keeps its registered key); then an Orca repo id, display name or path. A registry match whose
    Orca repo is not listed raises RepoMissing(key) instead of falling through to the Orca names;
    so does a path that is a registered project's main checkout (never re-added to Orca).
    """
    result = orca.repo_list()
    repos = orca.repos_of(result)
    found = registry.project(reg, needle)
    if found:
        key, proj = found
        proj_real = os.path.realpath(proj["path"]) if proj.get("path") else None
        for repo in repos:
            if (proj.get("repo_id") and repo.get("id") == proj["repo_id"]) or \
                    (proj_real and repo.get("path") and os.path.realpath(repo["path"]) == proj_real):
                return repo, key
        # Never fall through to an Orca name match on the same word: another repo could be
        # onboarded under this project's alias.
        raise RepoMissing(key)
    want = needle.strip().lower()
    real = os.path.realpath(os.path.expanduser(needle))
    for repo in repos:
        if want in (str(repo.get("id", "")).lower(), str(repo.get("displayName", "")).lower()) or \
                (repo.get("path") and os.path.realpath(repo["path"]) == real):
            return repo, None
    for key, proj in reg["projects"].items():
        if proj.get("path") and os.path.realpath(proj["path"]) == real:
            raise RepoMissing(key)
    return None, None


def _repo_missing(needle, key) -> int:
    return fail(EXIT_NOT_FOUND, f"{needle!r} is registered as {key} but its Orca repo is not in `orca repo list`")


def _git_path(needle):
    """realpath of `needle` when it is a directory with a `.git` entry (file or dir), else None.

    A `.git` file whose `gitdir:` points into `<git dir>/worktrees/<name>` is a linked worktree:
    LinkedWorktree names the main checkout (the parent of that git dir). A submodule's `.git` file
    (pointing into `.git/modules/`) counts as a plain repository."""
    real = os.path.realpath(os.path.expanduser(needle))
    dotgit = os.path.join(real, ".git")
    if not (os.path.isdir(real) and os.path.lexists(dotgit)):
        return None
    if os.path.isfile(dotgit):
        try:
            with open(dotgit, encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            return real
        for line in head.splitlines():
            if line.startswith("gitdir:"):
                gitdir = os.path.realpath(os.path.join(real, line[len("gitdir:"):].strip()))
                parent = os.path.dirname(gitdir)
                if os.path.basename(parent) == "worktrees":
                    raise LinkedWorktree(real, os.path.dirname(os.path.dirname(parent)))
                break
    return real


def _cwd_repo():
    """The top of the git repository holding the current directory, or None outside one."""
    res = shell.run(["git", "rev-parse", "--show-toplevel"])
    return (res.out.strip() or None) if res.ok else None


def _add_repo(real):
    """`orca repo add` the git repo at `real`: (its `orca repo list` entry, None) or (None, exit code)."""
    try:
        orca.call("repo", "add", "--path", real)
    except orca.OrcaError as exc:
        return None, fail(EXIT_USER, f"orca repo add failed: {exc}")
    try:
        result = orca.repo_list()
    except orca.OrcaError as exc:
        return None, fail(EXIT_USER, f"added {real} to Orca but orca repo list failed: {exc}")
    for repo in orca.repos_of(result):
        if repo.get("path") and os.path.realpath(repo["path"]) == real:
            return repo, None
    return None, fail(EXIT_USER, f"added {real} to Orca but orca repo list does not show it")


def _switch_to_main(proj) -> bool:
    """Bring the `main-<proj>` tab to the front; a missing tab or a failed switch is one warning line
    (`could not switch to main-<proj>; open that tab in Orca`), never a failed init."""
    title = proj["main_session"]
    try:
        term = orca.find_terminal_by_title(title)
        if term is not None:
            orca.terminal("switch", "--terminal", term["handle"])
            return True
    except orca.OrcaError:
        pass
    warn(f"could not switch to {title}; open that tab in Orca")
    return False


def _init_window(root):
    """(handle, title, None) for the one connected non-ORCHE pane open on the project folder (the
    window that ran init), else (None, None, why)."""
    try:
        terms = orca.terminals_of(orca.terminal("list"))
    except orca.OrcaError as exc:
        return None, None, str(exc)
    found = [t for t in terms if not orca.disconnected(t) and t.get("worktreePath")
             and os.path.realpath(t["worktreePath"]) == root
             and not orca.normalize_title(t.get("title")).startswith(ORCHE_PANES)]
    if len(found) != 1:
        return None, None, f"{len(found)} windows open on this folder" if found else "no window found"
    return found[0].get("handle"), found[0].get("title"), None


def _setup_note(proj, key, intake_line, window_line) -> str:
    """The setup result Main reports to the owner: at most five lines (`inbox.MAX_LINES`); the pointer's
    `report_path` is the setup report."""
    count, names, where = _divisions_text(proj)
    lines = [f"setup done: {key} registered with {count} divisions: {names}",
             intake_line or "intake: off",
             f"workspaces: {where}; nothing is committed to the repository",
             window_line, SETUP_ASK]
    return "\n".join(line for line in lines if line)


def _divisions_text(proj):
    """(count, `names (model); …` grouped by model, the workspaces folder or "none")."""
    groups = {}
    for name in registry.divisions_of(proj):
        groups.setdefault((proj["divisions"].get(name) or {}).get("model") or "default", []).append(name)
    names = "; ".join(f"{', '.join(sorted(n))} ({m})" for m, n in groups.items())
    trees = [units.worktree_path(d["worktree"]) for d in proj["divisions"].values() if d.get("worktree")]
    where = f"{_tilde(Path(trees[0]).parent)}/div-<division>" if trees else "none"
    return sum(len(n) for n in groups.values()), names, where


def _intake_text(key, root, intake_line):
    """What the intake did, in plain words: it read the repository (this run or an earlier one, from the
    stamp), else why it was skipped and what to say to run it."""
    skipped = intake_line and intake_line.startswith(("intake skipped: ", "intake interrupted"))
    stamp = None if skipped else intake.read_stamp(root)
    if stamp:
        cost = stamp.get("cost_usd")
        money = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$?"
        return ("read the repository once and wrote the project card and the division charters "
                f"({stamp.get('model')}, {money}, {int(round(stamp.get('duration_s') or 0))} s).")
    why = ("interrupted" if intake_line.startswith("intake interrupted") else
           intake_line[len("intake skipped: "):].rsplit("; run `orche intake", 1)[0]) if skipped else "not run"
    return (f'skipped ({why}); say "run the intake" once there is code to read; Main runs `orche intake {key}`.')


def _running_lines(reg, key, root, cfg, gate=None) -> list:
    """The setup report's "Running now": what works without the owner, with this machine's real values; the
    merge queue's state from `gate` (verifygate.ensure) when this run has one, else read locally."""
    from . import guide, janitor, publish, queue, reconcile, verifygate
    from .install import launchd
    rec = reconcile.settings(cfg)
    beat = (f"every {reconcile.every_s(rec)} s (the reconcile job): wakes Main and the divisions when work waits"
            if rec.get("enabled", True) else "off (reconcile.enabled false); Main and the divisions wake only "
            "when you write")
    st = {**janitor.DEFAULTS, **(cfg.get("janitor") or {})}
    jan_on, _ = guide.janitor_line(cfg, Path.home())
    jan = (f"daily at {launchd.daily_at()}; moves retired units' workspaces, and build caches of units idle for "
           f"{st['idle_days']} days, to a trash it empties after {st['trash_days']} days; touches nothing in the "
           "repository" if jan_on else 'off; say "let the janitor clean up" (listed below)')
    git = (Path(root) / ".git").exists()
    base, ok = publish.gate(root, cfg, key) if git else (None, False)
    if gate is not None:
        state = verifygate.running(gate)
    elif ok:
        state = "on"
    elif not git:
        state = "waiting; the check script could not be pushed (the project folder is not a git repository)"
    elif verifygate.pending(root):
        state = f"waiting for {verifygate.awaiting(reg['projects'].get(key))} to be merged"
    else:
        state = f'waiting; no check script on {base or "the base branch"} and no PR'
    lands = ("lands it by itself when that passes" if queue.land_mode(cfg, key) == "auto"
             else 'lands it when you say "merge"')
    jev_on, _ = guide.jev_line(cfg)
    return [f"- Heartbeat: {beat}.",
            f"- Janitor: {jan}.",
            f"- Merge queue: on when the repository has scripts/verify/ci.sh on {base or 'its base branch'} (runs "
            f"that script on each approved PR; {lands}); this project: {state}.",
            "- Jev: " + ("on: sorts incoming requests and checks gates before Main spends tokens." if jev_on
                         else 'off; say "turn on Jev" (listed below).'),
            "- Notifications: Orca shows one when Main finishes a reply; ORCHE reminds you after 30 minutes when a "
            "decision still waits."]


def _team_lines(key, root, detected) -> list:
    """The setup report's team-convention lines: what this run read from the repository, the PR line (only
    the parts config sets), Entire when the repository uses it, and Linear when Orca lacks the issues team."""
    from . import detect
    facts = _publish_facts(key)
    lines = [f"- Read the repository's team conventions: {', '.join(detected)}."] if detected else []
    parts = ([f"base {facts['base']}"] if facts["base"] else []) \
        + ([f"branches {facts['branch_prefix']}/<name>"] if facts["branch_prefix"] else []) \
        + ([f"issues {facts['issues']}-<N> (name the worker {facts['issues'].lower()}-<N>-<topic>)"]
           if facts["issues"] else [])
    lines += [f"- Pull requests: {', '.join(parts)}."] if parts else []
    if detect.uses_entire(root):
        lines.append("- Entire: on in this repository; worker commits are linked to their session "
                     "(commit_linking always in .entire/settings.local.json).")
    if facts["issues"] and facts["issues"] not in orca.linear_teams():
        lines.append(f"- Linear: Orca is not connected to the {facts['issues']} team; connect it in Orca to link "
                     "worktrees to issues (the PR's Fixes line moves the issue either way).")
    return lines


def write_report(reg, key, root, intake_line=None, detected=(), created=(), gate=None) -> Path:
    """`<root>/.orche/SETUP.md`, the setup report Main gives the owner (untracked: `info/exclude`); `created`:
    the lines of a repository `--new` made (_created_lines); `gate`: this run's verifygate.ensure answer."""
    from . import guide
    proj, cfg = reg["projects"][key], config.load()
    count, names, where = _divisions_text(proj)
    most, preset = guide.parallel(cfg, reg)
    path = paths.project_local(root) / SETUP_REPORT
    text = "\n".join([f"# ORCHE is set up: {key}", "", *created,
                      f"- Divisions: {names or 'none'}",
                      *([PRESET_LINES[proj["preset"]]] if proj.get("preset") in PRESET_LINES else []),
                      f"- Agents: {count} division agent{'' if count == 1 else 's'}, one Orca tab each, always on. Each unit of work adds one "
                      f"worker while it runs; up to {most} run at once ({preset}).",
                      f"- Workspaces: {where}; nothing is committed to the repository; ORCHE's own files are "
                      "excluded from git",
                      f"- Intake: {_intake_text(key, root, intake_line)}", *_team_lines(key, root, detected),
                      f"- Talk to Main in the {proj.get('main_session') or 'main-' + key} tab, in any language. "
                      "Main plans and decides; divisions do the work; you only get decisions.",
                      "", "## Running now", "", *_running_lines(reg, key, root, cfg, gate),
                      "", "## Any time", "",
                      *guide.lines(cfg, reg, Path.home()), "",
                      'Say "what else can ORCHE do?" to see this list again (Main runs `orche doctor`).', ""])
    util.atomic_write(path, text)
    return path


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def _link_card(key, repo_root, *, dry_run):
    """The card symlink `ORCHE_HOME/cards/<key>.md` -> `<repo>/PROJECT_CARD.md`. Returns (link, what):
    "created", "relinked" (it pointed elsewhere), "file" (a regular file there is kept) or None (in place)."""
    link = paths.orche_home() / "cards" / f"{key}.md"
    target = Path(repo_root) / "PROJECT_CARD.md"
    if link.is_symlink():
        what = None if os.readlink(link) == str(target) else "relinked"
    else:
        what = "file" if link.exists() else "created"
    if what in ("created", "relinked"):
        if dry_run:
            dry(f"symlink {link} -> {target}")
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            if what == "relinked":
                link.unlink()
            link.symlink_to(target)
    return link, what


def automation_argvs(key, proj) -> list:
    audit = ["--name", f"orche-hr-audit-{key}", "--trigger", "weekly", "--day", "1", "--time", "09:00",
             "--provider", "claude", "--workspace", f"path:{paths.PLUGIN_ROOT}", "--prompt",
             "Run `orche hr audit --json` and act on its proposals; apply only sleep proposals with `--apply`."]
    card = ["--name", f"orche-card-refresh-{key}", "--trigger", "weekly", "--day", "1", "--time", "10:00",
            "--provider", "claude", "--workspace", f"path:{proj['path']}", "--prompt",
            f"Run `orche card refresh {key} --background` and stop; do nothing else."]
    return [audit, card]


def _is_tty() -> bool:
    return bool(sys.stdin) and sys.stdin.isatty()


def _interactive(args) -> bool:
    """The checklist runs on a terminal, without --json and without --divisions/--custom/--setup-file."""
    return args.divisions is None and not args.custom and not args.setup_file and not args.json and _is_tty()


def _new_count(setup, existing) -> int:
    have = (existing or {}).get("divisions", {})
    return sum(1 for n in setup_flow.names(setup) if n not in have)


def _summary(setup, existing, wake) -> str:
    return setup_flow.summary(setup, new=_new_count(setup, existing), registered=bool(existing), wake=wake,
                              models=setup_flow.models(setup, existing))


def _setup(args, key, existing, reg):
    """(setup, None) or (None, exit code). Interactive only on a terminal, without --json and
    without any of --divisions/--custom/--setup-file. A registered project keeps every division,
    custom definition, stage and aliases it has; init never removes or changes them."""
    base = setup_flow.of_project(existing) if existing else setup_flow.defaults()
    try:
        if args.setup_file:
            if args.divisions is not None or args.custom or args.stage or args.aliases is not None:
                return None, fail(EXIT_USER, "--setup-file holds the whole setup; drop --divisions, --custom, "
                                              "--stage and --aliases")
            setup = setup_flow.from_data(setup_flow.read_file(args.setup_file), base)
        else:
            setup = setup_flow.from_flags(args.divisions, args.custom, base)
            if args.stage:
                setup["stage"] = args.stage
            if args.aliases is not None:
                setup["aliases"] = [a.lower() for a in _csv(args.aliases)]
    except setup_flow.SetupError as exc:
        return None, fail(EXIT_USER, str(exc))
    if existing:
        for name, spec in (existing.get("custom_divisions") or {}).items():
            if setup["custom"].get(name) != spec:
                return None, fail(EXIT_USER, f"custom division {name} already exists with another definition; "
                                             "changing it is out of scope (edit the registry and its persona by hand)")
        dropped = [d for d in base["divisions"] if d not in setup["divisions"]]
        if dropped:
            warn(f"{','.join(dropped)} stay(s) onboarded: init never removes a division "
                 f"(sleep it: orche sleep div-{key}-<division>, or remove it by hand)")
        if setup["stage"] != existing["stage"]:
            warn(f"stage stays {existing['stage']} for registered project {key} "
                 f"(change it with: orche stage set {key} {setup['stage']} --yes)")
        if setup["aliases"] != base["aliases"]:
            warn(f"aliases of registered project {key} are not changed by init")
        setup = {**setup, "divisions": [d for d in registry.DIVISIONS
                                        if d in setup["divisions"] or d in base["divisions"]],
                 "stage": existing["stage"], "aliases": base["aliases"]}
        for name, model in (setup.get("models") or {}).items():
            old = ((existing.get("divisions") or {}).get(name) or {}).get("model")
            if old and old != model:
                return None, fail(EXIT_USER, f"{key}/{name} is on {old}; change it with "
                                             f"orche hr model {key} {name} {model}")
    new = {n: m for n, m in (setup.get("models") or {}).items()
           if not ((existing or {}).get("divisions") or {}).get(n, {}).get("model")}
    if any(vendors.vendor_of(m) == vendors.CODEX for m in new.values()):
        avail = vendors.available(config.load())
        if not avail.ok:
            return None, fail(EXIT_BLOCKED, vendors.refusal(avail))
    if _interactive(args):
        locked = setup_flow.names(base) if existing else []
        checklist = setup_flow.Checklist(key, setup, locked, sys.stdin, sys.stdout, wake=_csv(args.wake),
                                         session_owner=lambda name: hr.session_owner(
                                             reg, key, hr.session_name("div", key, name)),
                                         recorded={n: d.get("model") for n, d in
                                                   ((existing or {}).get("divisions") or {}).items() if d.get("model")})
        try:
            setup = checklist.run(bool(existing), lambda s: _summary(s, existing, _csv(args.wake)))
        except setup_flow.Aborted:
            print("aborted; nothing written")
            return None, EXIT_OK
        except KeyboardInterrupt:
            print("\naborted; nothing written")
            return None, EXIT_INTERRUPTED
    return setup, None


def _print_setup(args, reg) -> int:
    """`--print-setup`: a registered project's setup, else (the repo must be in Orca or be a git
    repository init would add) the defaults. Never writes: nothing is added to Orca here."""
    found = registry.project(reg, args.repo)
    if found:
        setup = setup_flow.of_project(found[1])
    else:
        try:
            repo, _ = _find_repo(args.repo, reg)
            git = None if repo else _git_path(args.repo)   # an unlisted git repo: init adds it to Orca
        except orca.OrcaError as exc:
            return fail(EXIT_USER, f"orca repo list failed: {exc}")
        except RepoMissing as exc:
            return _repo_missing(args.repo, exc)
        except LinkedWorktree as exc:
            return fail(EXIT_USER, str(exc))
        if repo is None and not git:
            return fail(EXIT_NOT_FOUND, f"no registry project or Orca repo matches {args.repo!r} "
                                        "(see: orche projects, orca repo list)")
        setup = setup_flow.defaults()
    print(json.dumps(setup, indent=2, ensure_ascii=False))
    return EXIT_OK


def _check_wake(setup, wake_text):
    """(the --wake divisions, None) or (None, exit code): each one a known division of the setup."""
    divisions, wake = setup_flow.names(setup), _csv(wake_text)
    bad = [d for d in wake if d not in registry.DIVISIONS and d not in setup["custom"]]
    if bad:
        return None, fail(EXIT_USER, f"unknown division(s): {','.join(bad)} (allowed: {','.join(registry.DIVISIONS)} "
                                      "or a custom division)")
    missing = [d for d in wake if d not in divisions]
    if missing:
        return None, fail(EXIT_USER, f"--wake {','.join(missing)} is not in --divisions")
    return wake, None


def _conflict(reg, key, repo, root):
    """Another key already registered for the same Orca repo id or main checkout path."""
    for other, proj in reg["projects"].items():
        if other != key and (proj.get("repo_id") == repo["id"]
                             or (proj.get("path") and os.path.realpath(proj["path"]) == root)):
            return other
    return None


def _intake(args, reg, key, root, names):
    """The closing project intake: (line or None, data). It runs when `intake.enabled`, without
    `--no-intake`, and when the stamp misses one of the project's division `names` (all of them
    without a stamp: a re-run of init that only resumes changes nothing; one that adds a division
    re-runs the whole intake: cheap, and the card stays consistent). Any failure is a skip line and
    Ctrl-C (or a SIGTERM) only ends the intake — claude's process group is killed on the way out
    (headless.run) — so init still prints its summary, refreshes the status line and exits 0."""
    cfg = config.load()
    if args.no_intake or not intake.settings(cfg).get("enabled", True):
        return None, {"status": "off"}
    if not intake.missing_divisions(names, intake.read_stamp(root)):
        return "intake: covered", {"status": "covered"}
    from . import detect
    later = f"run `orche intake {key}` later"
    if detect.tracked(root) < detect.EMPTY_FILES and detect.stage(root) == "build":
        return f"intake skipped: the repository is empty; {later}", {"status": "skipped",
                                                                     "reason": "the repository is empty"}
    if args.dry_run:
        return f"would run: orche intake {key}", {"status": "dry-run"}
    model = intake.settings(cfg).get("model") or "claude-sonnet-5-5"
    print(f"intake {key}: sweeping {root} (headless, read-only, {model}; usually under a minute; "
          "Ctrl-C skips it, `--no-intake` turns it off)…", file=sys.stderr, flush=True)
    try:
        with intake.progress(model):
            rec = intake.run(reg, key, cfg)
    except (KeyboardInterrupt, intake.Interrupted):
        print(file=sys.stderr)
        return f"intake interrupted; {later}", {"status": "interrupted"}
    except Exception as exc:  # noqa: BLE001 - init never fails because of its intake
        why = str(exc) if isinstance(exc, intake.IntakeError) else f"{type(exc).__name__}: {exc}"
        why = mask.redact(why)[0]
        return f"intake skipped: {why}; {later}", {"status": "skipped", "reason": why}
    return intake.summary_line(rec), {"status": "ran", **{k: rec[k] for k in ("head", "cost_usd", "duration_s",
                                                                              "model", "divisions", "turns")}}


def _taken(reg, key, repo, root) -> str:
    taken = reg["projects"][key]
    return (f"project key {key} is taken by repo {taken.get('repo_id')} ({taken.get('path')}), "
            f"not {repo['id']} ({root}); rename the Orca repo or remove the stale registry entry")


def _key_error(reg, key, repo, root):
    """The exit code refusing `key` for `repo` at `root` (no key, the repo registered under another
    key, or the key held by another repo), else None."""
    if not key:
        return fail(EXIT_USER, f"cannot derive a project key from {repo.get('displayName')!r}")
    other = _conflict(reg, key, repo, root)
    if other:
        return fail(EXIT_USER, f"repo {repo['id']} ({root}) is already registered as project {other}")
    existing = reg["projects"].get(key)
    if existing and (existing.get("repo_id") != repo["id"]
                     or os.path.realpath(existing.get("path") or "/nonexistent") != root):
        return fail(EXIT_USER, _taken(reg, key, repo, root))
    return None


def _plan(reg, key, setup, wake):
    """(hr.plan_divisions result, None) or (None, exit code) on a refusal."""
    try:
        return hr.plan_divisions(reg, key, setup, wake, models=setup.get("models")), None
    except hr.Refused as exc:
        return None, fail(exc.code, exc.msg)


def _new_repo(args):
    """`--new`: the repository in the target folder (args.repo, else the current directory), made end to end.
    Returns (root, lines, facts {visibility, url, github_error}) or (None, exit code, None); a dry run only prints."""
    root = os.path.realpath(os.path.expanduser(args.repo or os.getcwd()))
    if os.path.lexists(os.path.join(root, ".git")):
        return None, fail(EXIT_NOT_FOUND, f"{root} is already a git repository; run orche init without --new"), None
    name, vis = os.path.basename(root), args.new
    gh = [] if vis == "local" else [f"gh repo create {name} --{vis} --source . --push"]
    if args.dry_run:
        for step in ["git init -q -b main", "git add -A", 'git commit -q --allow-empty -m "init"', *gh]:
            dry(f"{step} (in {root})")
        return None, EXIT_OK, None
    os.makedirs(root, exist_ok=True)
    empty, lines = not os.listdir(root), []
    if empty:
        util.atomic_write(Path(root) / "README.md", f"# {name}\n")
    from . import verifygate
    verifygate.write(root)   # the merge queue's gate is on from the first push
    for label, argv in ((f"git init: {root} (branch main)", ("init", "-q", "-b", "main")),
                        (None, ("add", "-A")), ("first commit: init", ("commit", "-q", "--allow-empty", "-m", "init"))):
        res = shell.git(root, *argv, timeout=60)
        if not res.ok:
            print("\n".join(lines))
            return None, fail(EXIT_USER, f"git {argv[0]} failed: {github.first_line(res)}"), None
        if label:
            lines.append(label)
    facts = {"visibility": vis, "url": None, "github_error": None}
    if vis != "local":
        try:
            text = github.run(root, "repo", "create", name, f"--{vis}", "--source", ".", "--push", timeout=github.NET_S)
            facts["url"] = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), None)
            shell.git(root, "remote", "set-head", "origin", "main", timeout=10)   # the base, read locally
            lines.append(f"GitHub: {facts['url']} ({vis})")
        except github.GhError as exc:
            why = mask.redact(str(exc).removeprefix("gh: "))[0]
            facts.update(visibility="local", github_error=why)
            lines.append(f"GitHub: not created ({why}); going on without a remote")
    return root, lines, facts


def _created_lines(facts) -> list:
    """The setup report's line for a repository `--new` made (and the GitHub step when gh failed)."""
    if not facts:
        return []
    lines = [f"- Repository: created here ({facts['visibility']}, {facts['url'] or 'no remote'})."]
    if facts["github_error"]:
        lines.append(f'- GitHub: not created ({facts["github_error"]}); say "publish this repository to GitHub"; '
                     "Main runs gh repo create")
    return lines


def _apply_preset(reg, key, setup, dry_run) -> None:
    """The setup's preset recorded on the project; the `build` preset's standing order appended once."""
    from .hr import book
    name = setup.get("preset")
    if not name:
        return
    if dry_run:
        dry(f"record preset {name}" + (" and append Build's specialist standing order" if name == "build" else ""))
        return
    if reg["projects"][key].get("preset") != name:
        book.write(reg, lambda r: r["projects"][key].__setitem__("preset", name))
    proj = reg["projects"][key]
    path = Path(proj["path"]) / proj["standing"]
    if name == "build" and SPECIALISTS not in (path.read_text(encoding="utf-8") if path.exists() else ""):
        book.correct(reg, key, SPECIALISTS, by="orche init")


def cmd_init(args):
    new_lines, created = [], None
    if args.new is not None:
        if args.refresh or args.print_setup:
            return fail(EXIT_USER, "--new creates a repository; drop --refresh and --print-setup")
        if err := _publish_error(args):
            return fail(EXIT_NOT_FOUND, err)
        made = _new_repo(args)
        if made[0] is None:
            return made[1]
        args.repo, new_lines, created = made
    if args.repo is None:   # a bare `orche init`: the repository holding the current directory
        top = _cwd_repo()
        if not top:
            return fail(EXIT_USER, "not inside a git repository: pass the project path (orche init <path>)")
        try:
            _git_path(top)   # a linked worktree's top is refused, naming the main checkout
        except LinkedWorktree as exc:
            return fail(EXIT_USER, str(exc))
        args.repo = top
    if err := _publish_error(args):
        return fail(EXIT_NOT_FOUND, err)   # a malformed value: exit 2, one line
    reg = registry.load()
    if args.refresh:
        return _refresh(args, reg)
    if args.print_setup:
        given = _setup_flags(args)
        if given:
            return fail(EXIT_USER, f"--print-setup only prints; drop {', '.join(given)}")
        return _print_setup(args, reg)
    added = None   # an unlisted git repo: added to Orca right before the first write (never under --dry-run)
    try:
        repo, key = _find_repo(args.repo, reg)
        if repo is None:
            added = _git_path(args.repo)
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"orca repo list failed: {exc}")
    except RepoMissing as exc:
        return _repo_missing(args.repo, exc)
    except LinkedWorktree as exc:
        return fail(EXIT_USER, str(exc))
    if added:   # a placeholder until the add; the key comes from the folder name, as in a dry run
        repo = {"id": "pending", "path": added, "displayName": os.path.basename(added)}
    if repo is None:
        return fail(EXIT_NOT_FOUND, f"no registry project or Orca repo matches {args.repo!r} "
                                    "(see: orche projects, orca repo list)")
    key = key or util.slugify(repo.get("displayName") or Path(repo["path"]).name)
    root, dry_run = os.path.realpath(repo["path"]), args.dry_run
    if err := _key_error(reg, key, repo, root):
        return err
    existing = reg["projects"].get(key)
    interactive = _interactive(args)
    setup, err = _setup(args, key, existing, reg)
    if setup is None:
        return err
    stage = setup["stage"]
    wake, err = _check_wake(setup, args.wake)
    if wake is None:
        return err
    plan, err = _plan(reg, key, setup, wake)
    if plan is None:
        return err
    custom = setup["custom"]
    summary = _summary(setup, existing, plan.wake)   # before onboarding mutates `existing`
    renamed = None
    if added and not dry_run:   # confirmed: Orca learns the repo only now, right before the first write
        repo, err = _add_repo(added)
        if repo is None:
            return err
        live = util.slugify(repo.get("displayName") or Path(repo["path"]).name)
        if live != key:   # Orca's display name, not the folder name, names the project
            key, renamed = live, f"registered as {live} (Orca names the repo {repo.get('displayName')})"
            if err := _key_error(reg, key, repo, root):
                return err
            if key in reg["projects"]:   # stricter here: even the same repo under this key is refused
                return fail(EXIT_USER, _taken(reg, key, repo, root))
            plan, err = _plan(reg, key, setup, wake)
            if plan is None:
                return err
    if dry_run:
        dry(f"copy templates/project -> {root} (missing files only, *.tmpl skipped)")
    else:
        paths.ensure_dirs()
        templates.copy_project(root, PROJECT=key)
    detected = _detect(args, key, root)   # before the flags (a flag wins) and the worktrees (they start from the base)
    _set_publish(args, key)
    link, linked = _link_card(key, root, dry_run=dry_run)
    try:
        proj, panes = hr.onboard_project(reg, key, setup, plan, repo_id=repo["id"], root=root, card=_tilde(link),
                                         dry_run=dry_run, models=setup.get("models"))
    except hr.Refused as exc:
        return fail(exc.code, exc.msg)
    _apply_preset(reg, key, setup, dry_run)
    ts = time.strftime("%Y%m%dT%H%M%S")
    try:   # re-render the ORCHE-owned files of the registered units (design §15.2)
        ref = hr.refresh_project(reg, key, root=root, ts=ts, dry_run=dry_run)
    except onboarding.UnsafeTarget as exc:
        return fail(EXIT_BLOCKED, f"init {key} stopped: {exc} (re-run orche init {key} to resume)")
    except (OSError, util.JsonError) as exc:
        return fail(EXIT_USER, f"init {key} stopped: {exc} (re-run orche init {key} to resume)")
    before = proj.get("orche_version")
    stamped = not dry_run and key in reg["projects"] and hr.stamp_version(reg, key, __version__)
    upgraded = stamped and existing is not None   # a new project's first stamp is no upgrade
    refreshed = list(ref.changed) + ([link] if linked == "relinked" else [])
    kept = list(ref.kept) + ([(link, "a regular file, not a link")] if linked == "file" else [])
    backups = paths.orche_home() / "backups" / key / ts
    restart = [] if dry_run else ([proj["main_session"]] if upgraded else []) + ref.restart
    autos = automation_argvs(key, proj)
    live = args.automations and not dry_run
    try:
        for argv in autos:
            orca.automations("create", *argv, dry_run=not live)
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"automation create failed: {exc}")
    divisions, skipped = plan.divisions, plan.skipped
    statuses = {n: ("active" if n in plan.wake else "dormant") for n in divisions}
    data = {"project": key, "repo_id": repo["id"], "path": root, "stage": stage, "dry_run": dry_run,
            "divisions": statuses, "custom": [n for n in divisions if n in custom], "skipped": skipped,
            "wake_skipped": plan.wake_skipped, "panes": panes, "automations": "created" if live else "printed",
            "card": proj["card"], "summary": summary, "added_to_orca": bool(added) and not dry_run,
            "refreshed": [str(p) for p in refreshed], "kept": [{"path": str(p), "why": w} for p, w in kept],
            "rendered_by": {"before": before, "now": __version__} if upgraded else None, "restart": restart,
            "publish": _publish_facts(key), "new_repository": created}
    lines = new_lines + ([f"would add {added} to Orca (orca repo add --path {added})" if dry_run else
                          f"added {added} to Orca"] if added else [])
    if renamed:
        lines.append(renamed)
    lines.append(f"{'would init' if dry_run else 'initialized'} {key} ({root}) stage={stage}")
    if not interactive:   # the checklist already showed it
        lines.append(summary)
    lines += [f"{n}: {s}" + (" (gated by stage)" if n == "ops" else " (custom)" if n in custom else "")
              for n, s in statuses.items()]
    lines += [f"{n}: skipped (already onboarded)" for n in skipped]
    lines += [f"skipped --wake {n} (already onboarded; use `orche wake`)" for n in plan.wake_skipped]
    lines.append(f"panes: {', '.join(panes) if panes else 'main already open'}")
    lines.append(f"card: {proj['card']}")
    lines.append("automations: " + ("created" if live else "printed only (above); re-run with --automations"))
    lines += [f"{'would refresh' if dry_run else 'refreshed'} {_tilde(Path(p))}" for p in refreshed]
    lines += [f"kept {_tilde(Path(p))}: {why}" for p, why in kept]
    if not dry_run and backups.is_dir():
        lines.append(f"backups: {_tilde(backups)}")
    if upgraded:
        lines.append(f"rendered by {before or 'an earlier version'}, now {__version__}")
    gate = None if dry_run else _merge_queue(reg, key, root, lines)   # the check script on the base, or its PR
    data["merge_queue"] = gate
    names = list(dict.fromkeys(registry.divisions_of(proj) + list(divisions)))   # a dry run saved none
    intake_line, data["intake"] = _intake(args, reg, key, root, names)
    if intake_line:
        lines.append(intake_line)
    first = existing is None
    closing = first and getattr(args, "close_window", True)   # off when init runs inside setup/apply-all
    data["closed_window"], data["setup_note"], handle = None, False, None
    report = None if dry_run or key not in reg["projects"] else _report(reg, key, root, intake_line, detected,
                                                                         created=_created_lines(created), gate=gate)
    data["setup_report"] = str(report) if report else None
    if dry_run:
        data["switched"] = False
        lines.append(f"would switch to {proj['main_session']}")
        if closing:
            lines.append("would close the window that ran init")
    else:
        data["switched"] = _switch_to_main(proj)   # last, so the user lands on Main when all is done
        window_line = None
        if closing:   # decided before the note, which states the outcome; the close itself is last
            handle, _title, why = _init_window(root) if data["switched"] else \
                (None, None, f"the switch to {proj['main_session']} failed")
            window_line = CLOSED if handle else NOT_CLOSED.format(why=why)
        if first:
            from . import inbox
            title = proj["main_session"]
            try:
                inbox.post(inbox.make(title, "orche", title, "info", urgent=True, report_path=str(report or ""),
                                      summary=_setup_note(proj, key, intake_line, window_line)))
                data["setup_note"] = True
            except OSError as exc:   # Main still has the card
                warn(f"setup note not posted to {title}: {exc}")
        if data["switched"]:
            lines.append(f"Main is open in the {proj['main_session']} tab with the setup summary; from here on, talk to Main there.")
        if closing:
            lines.append("closing the window that ran init" if handle else window_line)
            if not handle and report:
                lines.append(f"setup report: {report}")
            data["closed_window"] = handle
    if restart:
        lines.append(PERSONAS_UPDATED.format(names=", ".join(restart)))
    out(args, data, lines)
    code = EXIT_OK
    if first and not dry_run and not args.json and getattr(args, "first_pass", True):
        code = _first_pass_if_protected(root)
    if handle:   # the very last action: it ends the shell that runs init
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            orca.terminal("close", "--terminal", handle)
        except orca.OrcaError as exc:
            warn(f"could not close the window that ran init: {exc}")
    return code


def _first_pass_if_protected(root) -> int:
    """A project registered under a protected folder (macOS privacy) while the heartbeat job is
    loaded: the install's first-pass check, so the owner gets the same guidance now rather than a
    silent job later. Human output only (apply-all's --json inits never wait)."""
    from . import privacy, units
    from .install import launchd
    if not privacy.protected(root) or not launchd.job_loaded():
        return EXIT_OK
    try:
        first = privacy.first_pass_check(units.now())
    except KeyboardInterrupt:
        return privacy.interrupted()
    print("\n".join(first["lines"]), flush=True)
    return EXIT_OK
