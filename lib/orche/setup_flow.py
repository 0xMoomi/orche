"""Division setup for `orche init`: the setup model, its sources (flags, `--setup-file`, the
interactive checklist) and the one-line summary.

A setup is `{"divisions": [standard names], "custom": {name: {label, charter, model, effort}},
"stage": str, "aliases": [str]}` — the same JSON `orche init --print-setup` prints and
`--setup-file` reads. Defaults (an unregistered repo): every standard division except `ops`, no
custom division, stage `building`, no aliases. A registered project's setup is what it has.
Stdlib only; the checklist is plain stdin/stdout (no curses).
"""
import json
import re

from . import registry

STANDARD = {
    "rnd": "R&D — prototypes and technical spikes",
    "research": "Research — market, users, competitors",
    "build": "Build — integrates features, owns the ledger, opens PRs",
    "gtm": "GTM — launch, content, growth",
    "ops": "Ops — deploy and operate (dormant until stage public)",
}
DEFAULT_DIVISIONS = ("rnd", "research", "build", "gtm")
SETUP_KEYS = ("divisions", "custom", "stage", "aliases")
CUSTOM_INPUT_KEYS = ("label", "charter", "model", "effort")
_BLANK_LINE = re.compile(r"\n\s*\n")
_ITEM_NUMBER = re.compile(r"[0-9]+")   # ASCII digits only: `²` or `１` is no item number


class SetupError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class Aborted(Exception):
    """The user answered `n`, or stdin ended, before anything was written."""


def defaults() -> dict:
    return {"divisions": list(DEFAULT_DIVISIONS), "custom": {}, "stage": "building", "aliases": []}


def of_project(proj: dict) -> dict:
    """The setup a registered project has now."""
    return {"divisions": [d for d in registry.DIVISIONS if d in (proj.get("divisions") or {})],
            "custom": {n: dict(s) for n, s in (proj.get("custom_divisions") or {}).items()},
            "stage": proj["stage"], "aliases": list(proj.get("aliases", []))}


def custom_spec(name, raw) -> dict:
    """Validated, normalised spec for custom division `name` (whitespace collapsed, model/effort
    defaulted). Raises SetupError."""
    errors = list(registry.custom_name_errors(name))
    if not isinstance(raw, dict):
        raise SetupError(errors + [f"custom division {name}: must be an object"])
    errors += [f"custom division {name}: unknown key {k!r} (allowed: {'|'.join(CUSTOM_INPUT_KEYS)})"
               for k in raw if k not in CUSTOM_INPUT_KEYS]
    errors += [f"custom division {name}.{k}: must be a string, got {type(raw[k]).__name__}"
               for k in CUSTOM_INPUT_KEYS if raw.get(k) is not None and not isinstance(raw[k], str)]
    if errors:
        raise SetupError(errors)
    label, charter = raw.get("label"), raw.get("charter")
    if charter is not None and _BLANK_LINE.search(charter.strip()):
        errors.append(f"custom division {name}: charter must be one paragraph (no blank lines)")
    # The label is kept as typed (a line break or tab in it is refused); the charter's whitespace,
    # single line breaks included, collapses to one paragraph.
    spec = {"label": label.strip(" ") if label is not None else label,
            "charter": " ".join(charter.split()) if charter is not None else charter,
            "model": (raw.get("model") or "").strip() or registry.CUSTOM_MODEL,
            "effort": (raw.get("effort") or "").strip() or registry.CUSTOM_EFFORT}
    if not errors:
        errors += registry.custom_spec_errors(spec, f"custom division {name}")
    if errors:
        raise SetupError(errors)
    return spec


def parse_custom_flag(text) -> tuple:
    """`name=label:charter[:model[:effort]]` -> (name, spec). A charter or model id containing ':'
    goes in `--setup-file`. Raises SetupError."""
    name, sep, rest = (text or "").partition("=")
    parts = rest.split(":")
    if not sep or not 2 <= len(parts) <= 4:
        raise SetupError([f"--custom {text!r}: want name=label:charter[:model[:effort]] "
                          "(a charter or model id containing ':' goes in --setup-file)"])
    raw = dict(zip(CUSTOM_INPUT_KEYS, parts))
    return name.strip(), custom_spec(name.strip(), raw)


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise SetupError([f"setup file: key {key!r} appears twice"])
        seen[key] = value
    return seen


def read_file(path) -> dict:
    """The raw setup object of a `--setup-file` (duplicate keys refused). Raises SetupError."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=_no_duplicate_keys)
    except OSError as exc:
        raise SetupError([f"setup file {path}: {exc.strerror or exc}"]) from None
    except ValueError as exc:
        if isinstance(exc, SetupError):
            raise
        raise SetupError([f"setup file {path}: invalid JSON ({exc})"]) from None
    if not isinstance(data, dict):
        raise SetupError([f"setup file {path}: must be a JSON object"])
    return data


def from_data(data: dict, base: dict) -> dict:
    """Setup from a raw setup object; keys it leaves out come from `base`. Raises SetupError."""
    errors = [f"setup: unknown key {k!r} (allowed: {'|'.join(SETUP_KEYS)})" for k in data if k not in SETUP_KEYS]
    setup = {**base, "custom": dict(base["custom"])}
    custom = data.get("custom", {})
    if not isinstance(custom, dict):
        errors.append("setup: custom must be an object of name -> {label, charter[, model, effort]}")
        custom = {}
    new_custom = {}
    for name, raw in custom.items():
        try:
            new_custom[name] = custom_spec(name, raw)
        except SetupError as exc:
            errors += exc.errors
    setup["custom"].update(new_custom)
    if "divisions" in data:
        divisions = data["divisions"]
        if not isinstance(divisions, list) or not all(isinstance(d, str) for d in divisions):
            errors.append("setup: divisions must be a list of division names")
        else:
            bad = [d for d in divisions if d not in registry.DIVISIONS and d not in setup["custom"]]
            if bad:
                errors.append(f"unknown division(s): {','.join(bad)} (allowed: {','.join(registry.DIVISIONS)}; "
                              "custom divisions go under \"custom\")")
            setup["divisions"] = [d for d in registry.DIVISIONS if d in divisions]
    if "stage" in data:
        if data["stage"] not in registry.STAGES:
            errors.append(f"setup: stage must be one of {'|'.join(registry.STAGES)}, got {data['stage']!r}")
        else:
            setup["stage"] = data["stage"]
    if "aliases" in data:
        aliases = data["aliases"]
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            errors.append("setup: aliases must be a list of strings")
        else:
            setup["aliases"] = [a.strip().lower() for a in aliases if a.strip()]
    if errors:
        raise SetupError(errors)
    return setup


def from_flags(divisions, customs, base: dict) -> dict:
    """Setup from `--divisions` (comma text, None = base) and `--custom` values. Raises SetupError."""
    errors, data, custom = [], {}, {}
    for text in customs or []:
        try:
            name, spec = parse_custom_flag(text)
        except SetupError as exc:
            errors += exc.errors
            continue
        if name in custom:
            errors.append(f"--custom {name}: given twice")
        custom[name] = spec
    if divisions is not None:
        data["divisions"] = [d.strip() for d in divisions.split(",") if d.strip()]
    if custom:
        data["custom"] = custom
    if errors:
        raise SetupError(errors)
    return from_data(data, base)


def names(setup: dict) -> list:
    """Every division of a setup: standard ones in catalogue order, then custom ones."""
    return [d for d in registry.DIVISIONS if d in setup["divisions"]] + list(setup["custom"])


def summary(setup: dict, *, new: int, registered: bool, wake=()) -> str:
    """`Summary: build, gtm, design(custom) · stage building · 3 worktrees · pane main`."""
    divs = ", ".join(d + ("(custom)" if d in setup["custom"] else "") for d in names(setup)) or "no divisions"
    worktrees = f"{new} {'new ' if registered else ''}worktree{'' if new == 1 else 's'}"
    line = f"Summary: {divs} · stage {setup['stage']} · {worktrees} · pane main"
    return line + (f" · wake {','.join(wake)}" if wake else "")


# ---- interactive checklist ---------------------------------------------------------------------

def _short(text, width=40):
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


class Checklist:
    """The plain-stdin division checklist. `locked` names are already part of the project; `wake`
    names must stay ticked; `session_owner(name)` is the other project already holding the session
    of division `name` (None when it is free). A row whose session is taken is shown unticked and
    unavailable from the start, and cannot be ticked."""

    def __init__(self, key, setup, locked, reader, writer, *, wake=(), session_owner=lambda name: None):
        self.key, self.reader, self.writer = key, reader, writer
        self.locked, self.wake, self.session_owner = set(locked), list(wake), session_owner
        self.items = [(d, d in setup["divisions"]) for d in registry.DIVISIONS]
        self.custom = dict(setup["custom"])
        self.items += [(n, True) for n in self.custom]
        self.taken = {n: owner for n, _ in self.items if n not in self.locked and (owner := session_owner(n))}
        self.items = [(n, on and n not in self.taken) for n, on in self.items]
        self.setup = setup

    def taken_text(self, name) -> str:
        return f"session div-{self.key}-{name} is taken by project {self.taken[name]}"

    def say(self, text=""):
        self.writer.write(text + "\n")
        self.writer.flush()

    def ask(self, prompt) -> str:
        self.writer.write(prompt)
        self.writer.flush()
        line = self.reader.readline()
        if not line:
            self.say()
            raise Aborted("stdin closed")
        return line.strip()

    def describe(self, name):
        if name in self.custom:
            spec = self.custom[name]
            text = f"{spec['label']} — {_short(spec['charter'])}"
        else:
            text = STANDARD[name]
        if name in self.taken:
            return text + f"  (unavailable: {self.taken_text(name)})"
        return text + ("  (onboarded)" if name in self.locked else "")

    def show(self):
        width = max(9, *(len(n) + 1 for n, _ in self.items))
        self.say(f"Divisions for {self.key}  (type numbers to toggle, a to add a custom division, Enter to continue)")
        for i, (name, on) in enumerate(self.items, 1):
            self.say(f"{i:>2} [{'x' if on else ' '}] {name:<{width}} {self.describe(name)}")

    def toggle(self, tokens):
        for tok in tokens:
            idx = int(tok) - 1
            if not 0 <= idx < len(self.items):
                self.say(f"  no item {tok}")
                continue
            name, on = self.items[idx]
            if name in self.locked:
                self.say(f"  {name} is already onboarded; removing a division is out of scope "
                         f"(sleep it: orche sleep div-{self.key}-{name})")
                continue
            if name in self.taken:
                self.say(f"  {name} is unavailable: {self.taken_text(name)}")
                continue
            self.items[idx] = (name, not on)

    def unwoken(self) -> bool:
        """Say which `--wake` divisions are not ticked; True when any is not."""
        ticked = {n for n, on in self.items if on}
        listed = {n for n, _ in self.items}
        missing = [w for w in self.wake if w not in ticked]
        for name in missing:
            what = (f"is unavailable ({self.taken_text(name)})" if name in self.taken
                    else "is unticked; tick it again" if name in listed else "is not in the list; add it with `a`")
            self.say(f"  --wake {name}: {name} {what} (or press Ctrl-D and re-run without it in --wake)")
        return bool(missing)

    def _field(self, prompt, check, default=None):
        while True:
            value = self.ask(prompt)
            if not value and default is not None:
                return default
            error = check(value)
            if not error:
                return value
            self.say(f"  ! {error}")

    def add(self):
        taken = {n for n, _ in self.items}

        def check_name(value):
            if not value:
                return None
            errs = registry.custom_name_errors(value)
            if errs:
                return errs[0]
            if value in taken:
                return f"{value} is already in the list"
            owner = self.session_owner(value)
            return f"session div-{self.key}-{value} is taken by project {owner}; pick another name" if owner else None

        name = self._field(" name (slug, Enter to cancel): ", check_name)
        if not name:
            return
        label = self._field(" label: ", lambda v: next((f"label {e}" for e in registry.label_errors(v)), None))
        charter = self._field(" charter (one paragraph): ",
                              lambda v: next((f"charter {e}" for e in registry.charter_errors(" ".join(v.split()))),
                                             None))
        model = self._field(f" model [{registry.CUSTOM_MODEL}]: ",
                            lambda v: next((f"model {e}" for e in registry.model_errors(v)), None),
                            default=registry.CUSTOM_MODEL)
        effort = self._field(f" effort [{registry.CUSTOM_EFFORT}]: ",
                             lambda v: None if v in registry.EFFORTS else f"effort is one of {'|'.join(registry.EFFORTS)}",
                             default=registry.CUSTOM_EFFORT)
        self.custom[name] = custom_spec(name, {"label": label, "charter": charter, "model": model, "effort": effort})
        self.items.append((name, True))

    def run(self, registered: bool, summarise) -> dict:
        """Ask until confirmed; returns the chosen setup. Raises Aborted on `n` or end of input."""
        self.show()
        while True:
            answer = self.ask("> ")
            if not answer:
                if self.unwoken():
                    self.show()
                    continue
                break
            tokens = answer.replace(",", " ").split()
            if answer.lower() == "a":
                self.add()
            elif all(_ITEM_NUMBER.fullmatch(t) for t in tokens):
                self.toggle(tokens)
            else:
                self.say("  type item numbers to toggle, a to add a custom division, or Enter to continue")
                continue
            self.show()
        setup = {**self.setup,
                 "divisions": [n for n, on in self.items if on and n in registry.DIVISIONS],
                 "custom": {n: self.custom[n] for n, on in self.items if on and n in self.custom}}
        if registered:
            self.say(f"Stage: {setup['stage']} · aliases: {','.join(setup['aliases']) or '-'}  "
                     "(registered; change the stage with orche stage set)")
        else:
            setup["stage"] = self._field(f"Stage [{setup['stage']}]: ",
                                         lambda v: None if v in registry.STAGES
                                         else f"stage is one of {'|'.join(registry.STAGES)}",
                                         default=setup["stage"])
            aliases = self.ask(f"Aliases (comma-separated) [{','.join(setup['aliases'])}]: ")
            if aliases:
                setup["aliases"] = [a.strip().lower() for a in aliases.split(",") if a.strip()]
        self.say(summarise(setup))
        while True:
            answer = self.ask("Apply? [Y/n] ").lower()
            if answer in ("", "y", "yes"):
                return setup
            if answer in ("n", "no"):
                raise Aborted("declined")
