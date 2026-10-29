"""What ORCHE can switch on any time, as owner words: `orche doctor`'s "Next steps" block and the "Any
time" list of the setup report; and the setup report itself (`.orche/SETUP.md`: `write_report`, which
`orche init` calls, and `setup_note`, the setup result Main reports to the owner).

`lines(cfg, reg)` returns one English line per feature, shaped
`- <Feature>: <what it is>. <State>. Say "<owner words>"; <who runs what>.` so a reader needs no
glossary: the features that are off first, then the ones that are on, then the always-present lines; at
most MAX_LINES lines. Jev and the janitor drop out once on: the setup report's "Running now" names them. The
lines read no stored secret and call no network; only write_report writes a file.
"""
import shutil
from pathlib import Path

from . import config, intake, jev, jevkey, oauth, orca, paths, publish, registry, scale, units, util

MAX_LINES = 12
MAX_GATES = 2   # more projects without a gate share one line
ALWAYS = (
    '- Change the setup: any time. Say "add a design division", "switch research to Sonnet" or "set up again"; '
    "Main runs `orche hr add`, `orche hr model` or `orche init --refresh`.",
    '- Update: any time. Say "update"; Main runs `orche update`.',
    "- Other projects: ask another project's Main what changed. Any time. Say \"what changed in <project>?\"; "
    'Main runs `orche card <project>` and '
    '`orche ask <project> "…"`.',
    '- Cost: any time. Say "how much did this cost?"; Main runs `orche metrics`.',
    '- Something wrong: any time. Say "check the setup"; Main runs `orche doctor`.',
)


def _line(feature, what, state, say, runs) -> str:
    return f'- {feature}: {what}. {state}. Say "{say}"; {runs}.'


def jev_line(cfg):
    """(on?, line): on when `jev.enabled` and a key is stored (or in the environment)."""
    src, _err = jevkey.source(cfg["jev"].get("api_key_env") or jev.DEFAULT_KEY_ENV)
    on = bool(cfg["jev"].get("enabled")) and bool(src)
    return on, _line("Jev", "a small model that sorts incoming requests and checks gates before Main spends tokens",
                     "On" if on else "Off", "turn on Jev", "Main runs `orche jev setup`")


def codex_line():
    """(on?, line): on when `codex` is on PATH; the cross-check runs only when asked."""
    on = shutil.which("codex") is not None
    state = "On: runs when you ask" if on else "Off: Codex is not installed"
    runs = ("Main runs `orche review --pr 12 --codex`: Codex and Claude read the diff at once and only a defect both name "
            "sends it back" if on else "you run `npm install -g @openai/codex` and `codex login`; ORCHE then uses it")
    return on, _line("Codex cross-check", "a second AI vendor reviews a pull request when you ask", state,
                     "cross-check PR 12 with Codex", runs)


def token_line():
    src, _err = oauth.source()
    runs = ("Main runs `orche auth set` to replace it" if src else
            "you run `claude setup-token`, then Main runs `orche auth set`")
    return bool(src), _line("Background-run token", "lets ORCHE start agents while no Claude window is open",
                            "Set" if src else "Not set", "set the token for background runs", runs)


def janitor_line(cfg):
    """(on?, line): on when the janitor is enabled (the heartbeat starts its daily pass)."""
    from . import janitor
    st = {**janitor.DEFAULTS, **(cfg.get("janitor") or {})}
    on = janitor.enabled_flag().exists()
    what = (f"cleans up daily (the heartbeat starts it when the last pass is a day old); retired agents' "
            f"workspaces and build caches or scratch files idle {st['idle_days']} days go to a trash emptied after "
            f"{st['trash_days']} days")
    return on, _line("Janitor", what, "On" if on else "Off", "let the janitor clean up",
                     "Main runs `orche janitor enable`")


def parallel(cfg, reg) -> tuple:
    """(workers that may run at once in one project, the scale preset's label): `runner.max_parallel` as
    config has it and `default`, `preset <name>` or `custom`."""
    name = scale.current(cfg, reg)
    return cfg["runner"]["max_parallel"], name if name in ("default", scale.CUSTOM) else f"preset {name}"


def scale_line(cfg, reg):
    n, label = parallel(cfg, reg)
    return label == "preset 30", _line("More agents at once", "how many workers may run in parallel",
                                       f"Up to {n} at once ({label})", "run up to 30 agents",
                                       "Main runs `orche hr scale 30`")


def gate_lines(cfg, reg, project=None) -> list:
    """One line per registered project (only `project` when given) whose base branch lacks
    `scripts/verify/ci.sh` (the doctor's `queue gate` rows) and that has no PR adding it
    (verifygate.pending): init could not push the script. Merged into one line past MAX_GATES."""
    from . import verifygate
    from .install import doctor
    missing = []
    for key, root in doctor.roots(reg):
        if project not in (None, key):
            continue
        if (root / ".git").exists() and not verifygate.pending(root):
            base, ok = publish.gate(root, cfg, key)
            if not ok:
                missing.append((key, base))
    what = "lands approved pull requests by itself after its checks pass"
    if len(missing) > MAX_GATES:
        return [_line("Merge queue", what, f"{missing[0][0]} and {len(missing) - 1} more projects lack the check "
                      "script (scripts/verify/ci.sh), so a person merges each PR there",
                      "set up the merge queue for <project>", "Main runs `orche init --refresh <project>`")]
    return [_line(f"Merge queue for {key}", what, f"The check script (scripts/verify/ci.sh) could not be pushed to "
                  f"{base}, so a person merges each PR" if base else "No base branch found yet, so a person merges "
                  "each PR", f"set up the merge queue for {key}", f"Main runs `orche init --refresh {key}`")
            for key, base in missing]


def lines(cfg, reg, project=None) -> list:
    """The "Any time" lines: off first, then on, then the always-present ones (<= MAX_LINES); the merge
    queue lines of `project` only when given (a project's setup report), of every project otherwise."""
    jev_state, janitor_state = jev_line(cfg), janitor_line(cfg)
    states = [jev_state, codex_line(), token_line(), janitor_state, scale_line(cfg, reg)]
    states = [s for s in states if not (s[0] and s in (jev_state, janitor_state))]   # on: in Running now
    states += [(False, line) for line in gate_lines(cfg, reg, project)]
    off = [line for on, line in states if not on]
    on = [line for is_on, line in states if is_on]
    return (off + on)[:MAX_LINES - len(ALWAYS)] + list(ALWAYS)


# ---- the setup report (`.orche/SETUP.md`, written by `orche init`)

SETUP_ASK = ('read the report at report: and tell the owner, in their language, what was set up and the "Any time" '
             "list as short lines: translate every line, keep the numbers and the quoted owner words; then ask for "
             "the first request")
SETUP_REPORT = "SETUP.md"
PRESET_LINES = {"build": "- Preset: build-focused (Build's workers specialise in front, back and infra).",
                "operations": "- Preset: operations."}


def setup_note(proj, key, intake_line, window_line) -> str:
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
    where = f"{paths.tilde(Path(trees[0]).parent)}/div-<division>" if trees else "none"
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


def running_lines(reg, key, root, cfg, gate=None) -> list:
    """The setup report's "Running now": what works without the owner, with this machine's real values; the
    merge queue's state from `gate` (verifygate.ensure) when this run has one, else read locally."""
    from . import janitor, queue, reconcile, verifygate
    rec = reconcile.settings(cfg)
    beat = (f"every {reconcile.every_s(rec)} s (the reconcile job): wakes Main and the divisions when work waits"
            if rec.get("enabled", True) else "off (reconcile.enabled false); Main and the divisions wake only "
            "when you write")
    st = {**janitor.DEFAULTS, **(cfg.get("janitor") or {})}
    jan_on, _ = janitor_line(cfg)
    jan = (f"daily (the heartbeat starts it when the last pass is a day old); moves retired units' workspaces, and build caches of units idle for "
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
    lands = ("lands it by itself when that passes" if queue.land_mode(cfg, key, reg) == "auto"
             else 'lands it when you say "merge"')
    jev_on, _ = jev_line(cfg)
    plugin = registry.team(reg, key)
    team = []
    if plugin:
        from .hr import panes
        installed = "yes" if panes.plugin_install_path(plugin) is not None else 'no, say "install the team plugin"'
        team = [f"- Team: plugin {plugin} (installed for you: {installed}). Push, pull request and merge wait for "
                'your word: "push <unit>" and "merge <PR>"; the team\'s rules reach every ORCHE session.']
    return [*team, f"- Heartbeat: {beat}.",
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
    facts = publish.project_facts(key)
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
    the lines of a repository `orche init --new` made; `gate`: this run's verifygate.ensure answer."""
    proj, cfg = reg["projects"][key], config.load()
    count, names, where = _divisions_text(proj)
    most, preset = parallel(cfg, reg)
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
                      "", "## Running now", "", *running_lines(reg, key, root, cfg, gate),
                      "", "## Any time", "",
                      *lines(cfg, reg, key), "",
                      'Say "what else can ORCHE do?" to see this list again (Main runs `orche doctor`).', ""])
    util.atomic_write(path, text)
    return path
