"""Model strings as data (design 2026-10-23 §2, §4.1): the grammar, the vendor of a model, the
recommended model of a division kind, and the model a unit runs on.

A model string is a Claude model id (`claude-…`, an alias `sonnet`/`opus`/`haiku`, or an id listed in
config `models.catalog`), `codex`
(Codex's own default model) or `codex:<model id>`. A division records one (`divisions.<d>.model`); a
feature agent has none of its own and runs on its division's. No effort is stored: a headless run's
effort is its TIER's (EFFORT), a pane's is its persona's (the recommendation table or the custom spec).
Imports nothing from ORCHE at module top.
"""
import unicodedata
from typing import NamedTuple

EFFORT = {"mechanical": "low", "standard": "medium", "hard": "high"}   # --effort by the dispatch TIER
FEATURE_EFFORT = "high"   # a feature persona's effort (its model is its division's)
CODEX = "codex"
VENDORS = ("claude", CODEX)
ALIASES = ("sonnet", "opus", "haiku")   # the Claude CLI's model aliases
DOTS = {1: "●○○", 2: "●●○", 3: "●●●"}


class Choice(NamedTuple):
    """A recommendation: the model, the pane effort and the printed reason."""
    model: str
    effort: str
    why: str


def errors(model, cfg=None) -> list:
    """Why `model` is not a model string (messages without a field prefix). A plain id needs the
    `claude-` prefix, a Claude alias or an entry in `cfg` (None: the defaults) `models.catalog`."""
    if not isinstance(model, str) or not model \
            or any(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in model):
        return ["must be a model id without spaces or control characters"]
    if ":" in model:
        prefix, _, rest = model.partition(":")
        if prefix != CODEX:
            return [f"unknown vendor {prefix!r} — Claude ids are plain (claude-opus-5-5), "
                    "Codex is `codex` or `codex:<model id>`"]
        if not rest:
            return ["codex:<model id> needs a model id after `codex:` (or plain `codex` for Codex's default)"]
        return []
    if model == CODEX or model.startswith("claude-") or model in ALIASES or model in _catalog(cfg):
        return []
    return [f"unknown model {model!r} — Claude ids start with claude- (claude-opus-5-5) or are an alias "
            f"({', '.join(ALIASES)}), Codex is `codex` or `codex:<model id>`"]


def _cfg(cfg):
    if cfg is None:
        from . import config   # late: this module imports nothing from ORCHE at its top
        cfg = config.DEFAULTS
    return cfg


def _catalog(cfg) -> dict:
    return (_cfg(cfg).get("models") or {}).get("catalog") or {}


def _text(value, lang) -> str:
    """A {"ko", "en"} text in `lang` (English when that language is missing); a plain string as is."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or "")
    return str(value or "")


def vendor_of(model) -> str:
    """`codex` for `codex` and `codex:<id>`, else `claude`."""
    return CODEX if model == CODEX or str(model or "").startswith(CODEX + ":") else "claude"


def codex_id(model) -> str:
    """The Codex model id after `codex:`, or "" (Codex picks its own default; no `-m`)."""
    return str(model).partition(":")[2] if vendor_of(model) == CODEX else ""


def _kind_row(cfg, name) -> dict:
    table = _cfg(cfg).get("kinds") or {}
    row = table.get(name) or table.get("custom")
    if not isinstance(row, dict) or not row.get("model"):
        from . import config
        row = config.DEFAULTS["kinds"]["custom"]
    return row


def recommend(cfg, name) -> Choice:
    """The recommended model of division `name` from `cfg["kinds"]` (None: the defaults), falling
    back to its `custom` entry for a name the table does not list; `why` is the English reason."""
    row = _kind_row(cfg, name)
    return Choice(row["model"], row.get("effort") or "high", _text(row.get("why"), "en"))


def kind(cfg, name, lang) -> dict:
    """Kind `name` in `lang`: key, listed (False when `cfg["kinds"]` does not list it: the custom
    fallback), label, desc, charter, model, effort, why."""
    table = _cfg(cfg).get("kinds") or {}
    row = table.get(name) if isinstance(table.get(name), dict) else None
    rec = recommend(cfg, name)
    base = row or {}
    return {"key": name, "listed": row is not None,
            "label": _text(base.get("label"), lang) or name, "desc": _text(base.get("desc"), lang),
            "charter": base.get("charter") or None, "model": rec.model, "effort": rec.effort,
            "why": _text(_kind_row(cfg, name).get("why"), lang)}


def catalog(cfg, model, lang) -> dict:
    """Model `model` for the owner: label, desc, cost (1..3, None unknown), dots; an id the catalog
    does not list is its own label."""
    row = _catalog(cfg).get(model)
    if not isinstance(row, dict):
        return {"model": model, "label": label(model) if model != CODEX else "Codex", "desc": "",
                "cost": None, "dots": ""}
    cost = row.get("cost")
    return {"model": model, "label": str(row.get("label") or model), "desc": _text(row.get("desc"), lang),
            "cost": cost, "dots": DOTS.get(cost, "")}


def of(reg, member):
    """The model `member` (a roster.Member) runs on: a division's recorded model, a feature's
    division's; None for a project's Main (no division)."""
    if not member.owns_worktree or not member.division:
        return None
    div = (((reg.get("projects") or {}).get(member.project) or {}).get("divisions") or {}).get(member.division) or {}
    return div.get("model")


def label(model) -> str:
    """`model` for messages: Codex's default is spelled out."""
    return "codex (Codex's default model)" if model == CODEX else str(model)


class Availability(NamedTuple):
    """Whether Codex runs here: state `ok` (detail: its version), `broken` (detail: the first error
    line, masked) or `absent`; `fix` is the command that repairs it ("" when ok)."""
    state: str
    detail: str
    fix: str

    @property
    def ok(self) -> bool:
        return self.state == "ok"


INSTALL = "npm install -g @openai/codex, then codex login"
UPGRADE = "npm install -g @openai/codex@latest"


def available(cfg=None) -> Availability:
    """Codex's state, asked afresh (no cache): `<models.codex_bin> --version` (default `codex`, found
    on PATH or as an executable absolute path), 10 s at most, in the environment a Codex run gets
    (headless.base_env without Claude auth). Claude is always available."""
    import os
    from . import config, headless, mask, shell
    cfg = config.DEFAULTS if cfg is None else cfg
    name = str((cfg.get("models") or {}).get("codex_bin") or CODEX)
    found = name if os.path.isabs(name) and os.access(name, os.X_OK) else (
        None if os.path.isabs(name) else shell.which(name))
    if not found:
        return Availability("absent", "", INSTALL)
    res = shell.run([found, "--version"], timeout=10, env=headless.base_env(cfg, claude_auth=False))
    if res.ok:
        return Availability("ok", (res.out.strip().splitlines() or [""])[0], "")
    line = next((ln.strip() for ln in (res.err or res.out or "").splitlines() if ln.strip()),
                f"exit {res.code}")
    return Availability("broken", mask.redact(line)[0][:160], UPGRADE)


def unavailable_line(avail) -> str:
    """The `codex: …` line of a Codex that does not run."""
    if avail.state == "absent":
        return f"codex: not installed — {INSTALL}"
    return f"codex: installed but does not start — {UPGRADE}"


def refusal(avail) -> str:
    """Why a unit cannot move to (or start on) Codex."""
    if avail.state == "absent":
        return f"codex is not installed: {INSTALL}"
    return f"codex is installed but does not start ({avail.detail}): {UPGRADE}"


def _claude_recommendations(cfg) -> list:
    """The distinct Claude models recommend() gives the kinds of the table, in table order."""
    out = []
    for name in _cfg(cfg).get("kinds") or {}:
        model = recommend(cfg, name).model
        if vendor_of(model) == "claude" and model not in out:
            out.append(model)
    return out


def choice_labels(cfg, avail) -> list:
    """The `choices:` items of `orche hr add`."""
    out = _claude_recommendations(cfg) + ["any claude-… id"]
    if avail.ok:
        out += ["codex (Codex's default model; headless only)", "codex:<model id>"]
    return out


def codex_divisions(reg) -> list:
    """`<project>/<division>` of every live division recorded on Codex."""
    out = []
    for key, proj in sorted((reg.get("projects") or {}).items()):
        for name, entry in (proj.get("divisions") or {}).items():
            if entry.get("status") != "retired" and vendor_of(entry.get("model")) == CODEX:
                out.append(f"{key}/{name}")
    return out


def doctor_rows(cfg, reg) -> list:
    """The `vendors` doctor row (design §9): (level, name, detail)."""
    avail, divs = available(cfg), codex_divisions(reg or {})
    if avail.ok:
        return [("OK", "vendors", f"codex {avail.detail}; codex divisions: {', '.join(divs) or 'none'}")]
    if divs:
        which = "is not installed" if avail.state == "absent" else f"does not start ({avail.detail})"
        first = divs[0].split("/", 1)
        return [("WARN", "vendors", f"{', '.join(divs)} run on codex, which {which}: {UPGRADE}, "
                 f"or move them: orche hr model {first[0]} {first[1]} claude-opus-5-5")]
    if avail.state == "absent":
        return [("OK", "vendors", f"claude only; for a second vendor: {INSTALL}")]
    return [("OK", "vendors", f"codex is installed but does not start ({avail.detail}): {UPGRADE}")]
