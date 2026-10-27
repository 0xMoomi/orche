"""What ORCHE can switch on any time, as owner words: `orche doctor`'s "Next steps" block and the "Any
time" list of the setup report (`.orche/SETUP.md`, written by `orche init`).

`lines(cfg, reg, home)` returns one English line per feature, shaped
`- <Feature>: <what it is>. <State>. Say "<owner words>"; <who runs what>.` so a reader needs no
glossary: the features that are off first, then the ones that are on, then the always-present lines; at
most MAX_LINES lines. Jev and the janitor drop out once on: the setup report's "Running now" names them. Nothing here reads a stored secret, calls the network or writes a file.
"""
import shutil

from . import jev, jevkey, oauth, publish, scale
from .install import launchd
from .review import pre

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


def codex_line(cfg):
    """(on?, line): on when `codex` is on PATH and `review.cross_vendor` is not off."""
    installed, mode = shutil.which("codex") is not None, pre.mode_of(cfg)
    on = installed and mode != "off"
    state = "On" if on else "Off: Codex is not installed" if not installed else "Off: cross-vendor review is off"
    runs = ("ORCHE already uses it" if on else "Main sets `review.cross_vendor` to `required` in the config"
            if installed else "you run `npm install -g @openai/codex` and `codex login`; ORCHE then uses it")
    return on, _line("Codex cross-check", "a second AI vendor reviews each pull request before a person does", state,
                     "cross-check PRs with Codex", runs)


def token_line():
    src, _err = oauth.source()
    runs = ("Main runs `orche auth set` to replace it" if src else
            "you run `claude setup-token`, then Main runs `orche auth set`")
    return bool(src), _line("Background-run token", "lets ORCHE start agents while no Claude window is open",
                            "Set" if src else "Not set", "set the token for background runs", runs)


def janitor_line(cfg, home):
    """(on?, line): on when the janitor is enabled and its daily schedule is installed."""
    from . import janitor
    st = {**janitor.DEFAULTS, **(cfg.get("janitor") or {})}
    on = janitor.enabled_flag().exists() and launchd.plist_path(launchd.JANITOR, home).is_file()
    what = (f"cleans up daily; retired agents' workspaces and build caches or scratch files idle {st['idle_days']} "
            f"days go to a trash emptied after {st['trash_days']} days")
    return on, _line("Janitor", what, "On" if on else "Off", "let the janitor clean up",
                     "Main runs `orche janitor enable` and `orche janitor install-schedule`")


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
    for key, root in doctor._roots(reg):
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


def lines(cfg, reg, home, project=None) -> list:
    """The "Any time" lines: off first, then on, then the always-present ones (<= MAX_LINES); the merge
    queue lines of `project` only when given (a project's setup report), of every project otherwise."""
    jev_state, janitor_state = jev_line(cfg), janitor_line(cfg, home)
    states = [jev_state, codex_line(cfg), token_line(), janitor_state, scale_line(cfg, reg)]
    states = [s for s in states if not (s[0] and s in (jev_state, janitor_state))]   # on: in Running now
    states += [(False, line) for line in gate_lines(cfg, reg, project)]
    off = [line for on, line in states if not on]
    on = [line for is_on, line in states if is_on]
    return (off + on)[:MAX_LINES - len(ALWAYS)] + list(ALWAYS)
