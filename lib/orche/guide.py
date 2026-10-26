"""What ORCHE can switch on any time, as owner words: `orche doctor`'s "Next steps" block and the "Any
time" list of the setup report (`.orche/SETUP.md`, written by `orche init`).

`lines(cfg, reg, home)` returns one English line per feature, shaped
`- <Feature>: <state>. Say "<owner words>"; Main runs <command>.`: the features that are off first,
then the ones that are on, then the always-present lines; at most MAX_LINES lines. Nothing here
reads a stored secret, calls the network or writes a file.
"""
import shutil

from . import jev, jevkey, oauth, publish, scale
from .install import launchd
from .review import pre

MAX_LINES = 12
MAX_GATES = 2   # more projects without a gate share one line
JANITOR_LABEL = "com.orche.janitor"
ALWAYS = (
    '- Change the setup: any time. Say "add a design division", "switch research to Sonnet" or "set up again"; '
    "Main runs `orche hr add`, `orche hr model` or `orche init --refresh`.",
    '- Update: any time. Say "update"; Main runs `orche update`.',
    '- Other projects: any time. Say "what changed in <project>?"; Main runs `orche card <project>` and '
    '`orche ask <project> "…"`.',
    '- Cost: any time. Say "how much did this cost?"; Main runs `orche metrics`.',
    '- Something wrong: any time. Say "check the setup"; Main runs `orche doctor`.',
)


def _line(feature, state, say, runs) -> str:
    return f'- {feature}: {state}. Say "{say}"; {runs}.'


def jev_line(cfg):
    """(on?, line): on when `jev.enabled` and a key is stored (or in the environment)."""
    src, _err = jevkey.source(cfg["jev"].get("api_key_env") or jev.DEFAULT_KEY_ENV)
    on = bool(cfg["jev"].get("enabled")) and bool(src)
    return on, _line("Jev (the triage classifier and gates)", "on" if on else "off", "turn on Jev",
                     "Main runs `orche jev setup`")


def codex_line(cfg):
    """(on?, line): on when `codex` is on PATH and `review.cross_vendor` is not off."""
    installed, mode = shutil.which("codex") is not None, pre.mode_of(cfg)
    on = installed and mode != "off"
    state = "on" if on else "Codex is not installed" if not installed else "cross-vendor review is off"
    runs = ("ORCHE already uses it" if on else "Main sets `review.cross_vendor` to `required` in the config"
            if installed else "you run `npm install -g @openai/codex` and `codex login`; ORCHE then uses it")
    return on, _line("Codex cross-check (a second vendor reviews each PR)", state,
                     "cross-check PRs with Codex", runs)


def token_line():
    src, _err = oauth.source()
    runs = ("Main runs `orche auth set` to replace it" if src else
            "you run `claude setup-token`, then Main runs `orche auth set`")
    return bool(src), _line("Background-run token", "set" if src else "not set", "set the token for background runs",
                            runs)


def janitor_line(home):
    """(on?, line): on when the janitor is enabled and its daily schedule is installed."""
    from . import janitor
    on = janitor.enabled_flag().exists() and launchd.plist_path(JANITOR_LABEL, home).is_file()
    return on, _line("Janitor (daily cleanup)", "on" if on else "off", "let the janitor clean up",
                     "Main runs `orche janitor enable` and `orche janitor install-schedule`")


def notify_line():
    """(on?, line): on when terminal-notifier is on PATH (a notification click then opens the Main tab)."""
    on = shutil.which("terminal-notifier") is not None
    return on, _line("Notification click", "opens the project's Main tab" if on else
                     "opens Orca (enough for most; the exact tab needs terminal-notifier)",
                     "make notification clicks open the tab",
                     "Main runs `brew install terminal-notifier`; you allow it once")


def scale_line(cfg, reg):
    name = scale.current(cfg, reg)
    state = "preset 30" if name == "30" else f"preset {name}"
    return name == "30", _line("More agents at once", state, "run up to 30 agents", "Main runs `orche hr scale 30`")


def gate_lines(cfg, reg) -> list:
    """One line per registered project whose base branch lacks `scripts/verify/ci.sh` (the doctor's
    `queue gate` rows), merged into one line past MAX_GATES."""
    from .install import doctor
    missing = []
    for key, root in doctor._roots(reg):
        if (root / ".git").exists():
            base, ok = publish.gate(root, cfg)
            if not ok:
                missing.append((key, base))
    runs = "Build's first unit adds it from templates/verify/ci.sh"
    if len(missing) > MAX_GATES:
        return [_line("Merge-queue gate", f"missing in {missing[0][0]} and {len(missing) - 1} more projects",
                      "set up the merge queue for <project>", runs)]
    return [_line(f"Merge-queue gate for {key}", f"no scripts/verify/ci.sh on origin/{base}" if base else
                  "no base branch", f"set up the merge queue for {key}", runs if base else
                  f"Main runs `orche init {key} --refresh` (it finds the base branch), then {runs}")
            for key, base in missing]


def lines(cfg, reg, home) -> list:
    """The "Any time" lines: off first, then on, then the always-present ones (<= MAX_LINES)."""
    states = [jev_line(cfg), codex_line(cfg), token_line(), janitor_line(home), scale_line(cfg, reg),
              notify_line()]
    states += [(False, line) for line in gate_lines(cfg, reg)]
    off = [line for on, line in states if not on]
    on = [line for is_on, line in states if is_on]
    return (off + on)[:MAX_LINES - len(ALWAYS)] + list(ALWAYS)
