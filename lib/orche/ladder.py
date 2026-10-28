"""The tier ladder (design 0.9.0 §7): a headless attempt's model and effort by TIER and vendor.

`ladder.<vendor>.<tier>` in config names a rung: a model (`division`: the unit's own model) and an
effort. The division's model is the ceiling: a rung model of another vendor, or one priced above the
division's (output price: metrics.PRICES for Claude, `models.prices` for Codex), resolves to the
division's model, with a hooks-log line. A brief's MODEL pin replaces the rung's model, never its
effort. Haiku is named here only, never in `models.catalog` (R25).

escalate(): a mechanical work attempt that ended `failed` or `info "unverified: …"` re-runs once, in
the same run, at the standard rung, with templates/run/escalation.md appended to the brief. A pinned brief, a blocked
or stopped attempt, a timeout or a turn cap does not escalate.

audit(): with Jev enabled, Jev's Choice about the brief's TIER; a disagreement at MIN_CONFIDENCE or
more is one events line. It never changes anything.
Imports nothing from ORCHE at module top but brief, config, templates and vendors.
"""
from typing import NamedTuple

from . import brief, config, templates, vendors

DIVISION = "division"
TAIL_CHARS = 1500
MIN_CONFIDENCE = 0.8


class Rung(NamedTuple):
    model: str
    effort: str
    why: str        # "mechanical rung", "MODEL", "ceiling", "Main"


def full_id(model) -> str:
    """A Claude alias as its full id (`sonnet` -> claude-sonnet-5-5); anything else as given."""
    return f"claude-{model}-5-5" if model in vendors.ALIASES else model


def price(cfg, model):
    """The output price of `model` per 1M tokens, or None when unknown."""
    model = full_id(model)
    if vendors.vendor_of(model) == vendors.CODEX:
        from . import codex
        prices = ((cfg or {}).get("models") or {}).get("prices") or config.DEFAULTS["models"]["prices"]
        mid = vendors.codex_id(model)
        row = codex.price_row(prices, mid) if mid else codex.price_row(prices, vendors.CODEX)
        return row[2] if row else None
    from . import metrics
    known = [m for m in metrics.PRICES if model.startswith(m)]
    return metrics.PRICES[max(known, key=len)]["output"] if known else None


def above(cfg, model, base) -> str:
    """Why `model` cannot run in place of `base` (the ceiling), or "": another vendor, or priced above
    it; with either price unknown only `base` itself passes."""
    model, base = full_id(model), full_id(base)
    if model == base:
        return ""
    if vendors.vendor_of(model) != vendors.vendor_of(base):
        return "vendor"
    p, ceiling = price(cfg, model), price(cfg, base)
    if p is None or ceiling is None or p > ceiling:
        return "above"
    return ""


def rung(cfg, base, tier, pin=None) -> Rung:
    """The model and effort an attempt of `tier` runs on for a unit whose division model is `base`."""
    tier = tier if tier in brief.TIERS else "standard"
    row = cfg["ladder"][vendors.vendor_of(base)][tier]
    effort = row.get("effort") or "medium"
    if pin:
        return Rung(full_id(pin), effort, "MODEL")
    model = row.get("model") or DIVISION
    if model == DIVISION:
        return Rung(base, effort, f"{tier} rung")
    if above(cfg, model, base):
        from . import log
        log.log("hooks", f"ladder: {tier} rung {model} is above {base}: runs {base}")
        return Rung(base, effort, "ceiling")
    return Rung(full_id(model), effort, f"{tier} rung")


def pin_error(cfg, pin, base, division, available=None) -> str:
    """Why a brief's `MODEL pin` cannot run on `division` (model `base`), or "". `available` is
    vendors.available, called only for a Codex pin."""
    errs = vendors.errors(pin, cfg)
    if errs:
        return f"MODEL {pin}: {errs[0]}"
    if vendors.vendor_of(pin) == vendors.CODEX:
        avail = (available or vendors.available)(cfg)
        if avail.state != "ok":
            return f"MODEL {pin}: {vendors.refusal(avail)}"
    if vendors.vendor_of(pin) != vendors.vendor_of(base):
        return (f"MODEL {pin} is another vendor than {division} ({base}): move the division with "
                f"`orche hr model`, or send the brief to a Codex division" if vendors.vendor_of(pin) == vendors.CODEX
                else f"MODEL {pin} is another vendor than {division} ({base}): move the division with "
                     f"`orche hr model`, or send the brief to a Claude division")
    if above(cfg, pin, base):
        return f"MODEL {pin} is above {division}'s model {base}"
    return ""


def header(cfg, base, tier, pin=None) -> str:
    """The brief header's `model:` line value."""
    r = rung(cfg, base, tier, pin)
    if r.why == "MODEL":
        return f"{r.model} (MODEL)"
    return f"{r.model} ({tier if tier in brief.TIERS else 'standard'} rung; ceiling {base})"


def escalate(cfg, tier, pinned, status, summary, *, stopped=False, timed_out=False, turn_cap=False,
             dead=False) -> bool:
    """True when a mechanical work attempt that ended so re-runs once at the standard rung."""
    if not cfg["ladder"]["escalate"]:
        return False
    if tier != "mechanical" or pinned or stopped or timed_out or turn_cap or dead:
        return False
    return status == "failed" or (status == "info" and str(summary).startswith("unverified"))


def previous(model, effort, outcome, text) -> str:
    """run/escalation.md filled in: the last TAIL_CHARS of the attempt's final message."""
    return "\n\n" + templates.load("run/escalation.md", MODEL=model, EFFORT=effort, OUTCOME=outcome,
                                    TAIL=(text or "")[-TAIL_CHARS:]) + "\n"


def doctor_row(cfg) -> str:
    """The `ladder` row text after the name (C.5)."""
    c, x = cfg["ladder"]["claude"], cfg["ladder"][vendors.CODEX]
    claude = " · ".join(f"{t} {c[t]['model']} {c[t]['effort']}" for t in brief.TIERS)
    codex = "/".join(x[t]["effort"] for t in brief.TIERS)
    esc = "escalate once" if cfg["ladder"]["escalate"] else "no escalation"
    return f"claude: {claude} · codex: {codex} · {esc}"


def audit(unit, tier, text, cfg, ask=None):
    """Jev's view of the brief's TIER, logged on disagreement; never changes anything. Returns
    (jev's tier, confidence) or None."""
    jcfg = (cfg or {}).get("jev") or {}
    if not jcfg.get("enabled"):
        return None
    from . import jev, log
    q = jev.choice("tier", "Which dispatch TIER fits this brief?",
                   {"mechanical": "a scoped, rote change with no design decisions",
                    "standard": "an ordinary implementation, fix or test",
                    "hard": "design, deep debugging or a wide change"})
    answers = (ask or jev.ask)(text, [q])
    a = (answers or {}).get("tier")
    if not isinstance(a, dict) or a.get("value") not in brief.TIERS:
        return None
    conf = float(a.get("confidence") or 0.0)
    if a["value"] != tier and conf >= MIN_CONFIDENCE:
        log.log("events", f"tier-audit unit={unit} tier={tier} jev={a['value']} conf={conf:.2f}")
    return a["value"], conf
