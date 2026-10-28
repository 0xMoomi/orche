"""Relay guards: what ORCHE refuses to relay while the owner is away.

The autonomy window W(p) starts at the later of local midnight and the owner's last prompt in Main
(`ORCHE_HOME/flow/<p>/owner-seen`, touched by hooks/report_inject.py). Every counter counts inside W
only, so one word from the owner resets them all. STOP always passes. `flow.enabled` false turns
G1–G7 off; the owner's pause (G8) stays on.

  G1 revisions  a unit's AMENDs (BRIEF.rev<N>.md, N ≥ 2) in W           → its next AMEND        (flow.revisions 3)
  G2 blocked    the unit's last two reports blocked/question, in W      → its AMEND, a same-GOAL
                                                                          brief to the same TO  (2)
  G3 rate       the project's briefs and AMENDs since max(W, now − 1 h) → every brief and AMEND  (flow.briefs_per_hour 20)
  G4 spend     runs/<p>/history.jsonl cost_usd, finished in W: a unit at its cap → its AMENDs and
                runs; the project at its cap → every brief, AMEND and run   (flow.max_*_cost_usd 10/30)
                Claude's figure, Codex's estimate, or the reservation of a run without one
                (killed, timed out, lost: history.reserve, at most unit_cap())
  G5 runs       the unit's history lines started in W, not busy/duplicate → its `orche run`  (flow.runs_per_unit 4)
  G6 chain      a division's brief: its current unit already 3 briefs deep along `parent:`, or
                already parent of 5 briefs first sent in W (BRIEF.rev1.md) → that division's brief (flow.depth 3 / flow.fanout 5)
  G7 streak     the session's refused blocks since its last delivered one (30 min) → nothing more;
                the refusal adds "refusal <n> in a row: stop re-sending; tell the user what you tried" (3)
  G8 pause      flow/<p>/pause.json {by, at, reason}                    → every brief and AMEND
"""
import datetime as _dt
import json
import re

from .. import brief, config, inbox, log, paths, units, util
from . import lineage

BLOCKED, STREAK = 2, 3
STREAK_TTL = _dt.timedelta(minutes=30)
ASK = "Ask the owner before {what}: close your reply with OWNER: <the decision>. STOP still works."
STREAK_TEXT = "refusal {n} in a row: stop re-sending; tell the user what you tried"
_REV_FILE = re.compile(r"^BRIEF\.rev(\d+)\.md$")


class Refused(str):
    """A guard's refusal text; `rule`, `unit` and `n` name it for the events line."""
    def __new__(cls, text, rule, unit, n):
        self = super().__new__(cls, brief.REFUSE + text)
        self.text, self.rule, self.unit, self.n = text, rule, unit, n
        return self


def folder(project):
    return paths.orche_home() / "flow" / util.slugify(project)


def _local(ts):
    return _dt.datetime.fromtimestamp(ts).astimezone()


def window(project, now=None):
    """W(p): the later of local midnight and the owner's last prompt (owner-seen's mtime)."""
    now = (now or units.now()).astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        seen = _local((folder(project) / "owner-seen").stat().st_mtime)
    except OSError:
        return start
    return max(start, seen)


def owner_seen(project):
    """The owner spoke in Main: touch owner-seen (the window restarts now)."""
    path = folder(project) / "owner-seen"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def paused(project):
    """pause.json ({by, at, reason}) or None."""
    return util.read_dict(folder(project) / "pause.json")


def pause(project, by, reason=""):
    data = {"by": by, "at": util.now_iso(), "reason": reason}
    path = folder(project) / "pause.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    util.write_json(path, data)
    log.log("events", f"flow pause project={project} by={by}")
    return data


def resume(project, by):
    """Remove pause.json when the owner spoke after the pause. (True, at) or (False, at)."""
    p = paused(project)
    if p is None:
        return True, None
    at = units.parse_iso(p.get("at"))
    try:
        seen = _local((folder(project) / "owner-seen").stat().st_mtime)
    except OSError:
        seen = None
    if at is not None and (seen is None or seen <= at + _dt.timedelta(seconds=1)):   # at has whole seconds
        return False, at
    (folder(project) / "pause.json").unlink(missing_ok=True)
    log.log("events", f"flow resume project={project} by={by}")
    return True, at


def _revs(folder_, since):
    """[(N, mtime)] of a unit's BRIEF.rev<N>.md written at or after `since` (a timestamp)."""
    out = []
    try:
        for p in folder_.iterdir():
            m = _REV_FILE.match(p.name)
            if m:
                t = p.stat().st_mtime
                if t >= since:
                    out.append((int(m.group(1)), t))
    except OSError:
        pass
    return sorted(out)


def _norm(goal):
    return " ".join(str(goal or "").lower().split())


def _blocked_twice(b, sender, since):
    """True when the unit's last two reports (from anyone but orche) to its sender are blocked or
    question and both were filed inside W."""
    who = b.sender or sender
    try:
        box = inbox.box(who)
    except ValueError:
        return False
    found = []
    for path in list(box.glob("*.json")) + list((box / inbox.DRAINED).glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            ptr = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(ptr, dict) or ptr.get("unit") != b.unit or ptr.get("from") == "orche":
            continue
        if ptr.get("quiet") or ptr.get("repeat"):      # a filed-again repeat is not a second block
            continue
        t = inbox.pointer_time(path, ptr)
        if t is not None:
            found.append((t, ptr.get("status")))
    last = [s for t, s in sorted(found, key=lambda x: x[0])[-BLOCKED:] if t >= since]
    return len(last) == BLOCKED and all(s in lineage.WAITING for s in last)


def _chain(reg, block, sender, w):
    """G6: a division's new brief would sit too deep under its current unit (the `parent:` chain), or its
    current unit already sent FANOUT child briefs inside W (BRIEF.rev1.md mtimes; the owner's word resets
    it). Worded for the division, the only sender it refuses: it cannot ask the owner itself."""
    if block.kind != "brief" or sender.kind != "div":
        return None
    p = brief.newest(reg, sender.project, sender.session)
    if p is None:
        return None
    d = lineage.depth(reg, p)
    depth, fanout = cap("depth"), cap("fanout")
    if d >= depth:
        return Refused(f"{p.unit} is already {d} briefs deep (cap {depth}); a brief from {sender.session} "
                       f"would make it {d + 1}. Do not dispatch it yourself: name the work in your report and ask "
                       f"Main to dispatch it. STOP still works.", "G6", p.unit, d + 1)
    n = lineage.children(reg, p, lineage.unit_dirs(reg, sender.project), since=w.timestamp())
    if n >= fanout:
        return Refused(f"{p.unit} already has {n} child briefs since {units.hhmm(w)} without the owner (cap "
                       f"{fanout}). Do not send another: report it to your sender (`orche report blocked "
                       f"--summary \"<what is left>\"`). STOP still works.", "G6", p.unit, n + 1)
    return None


def safe(fn, by, unit, *args, **kwargs):
    """fn(*args, **kwargs); a guard that raises fails open: the events line names it, and None."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a broken guard must not stop the relay
        log.log("events", f"flow guard-error by={by or 'unknown'} unit={unit or '-'} {type(exc).__name__}")
        return None


def check(reg, block, sender, now=None, to=None, runner=False, accepted=0):
    """The first guard that refuses `block` from `sender` (a Member), as a Refused text, or None.
    `to` is a brief's resolved target session (G2). `runner`: an AMEND that the runner will run (G5
    at the relay). `accepted`: briefs and AMENDs ahead of this block in the same call (G3)."""
    if block.kind == "stop":
        return None
    proj = sender.project
    now = (now or units.now()).astimezone()
    p = paused(proj)
    if p is not None:
        at = units.parse_iso(p.get("at"))
        reason = f" ({p['reason']})" if p.get("reason") else ""
        return Refused(f"{proj} is paused by the owner since {units.hhmm(at) if at else '?'}{reason}. Nothing is "
                       f"relayed until the owner resumes it: tell the user, and wait for their word "
                       f"(orche flow resume {proj}). STOP still works.", "G8", block.unit or "-", 1)
    if not (config.load().get("flow") or {}).get("enabled", True):
        return None
    w = window(proj, now)
    hit = _chain(reg, block, sender, w)
    if hit is not None:
        return hit
    since = w.timestamp()
    unit = block.unit
    if block.kind == "amend":
        b = brief.find(reg, unit, project=proj)
        if b is not None:
            revs = [n for n, _ in _revs(b.folder, since) if n >= 2]
            if len(revs) >= cap("revisions"):
                return Refused(f"{unit} was revised {len(revs)} times since {units.hhmm(w)} without the owner "
                               f"(rev {revs[0]}–{revs[-1]}). " + ASK.format(what="revising it again"),
                               "G1", unit, len(revs))
            if _blocked_twice(b, sender.session, w):
                return Refused(f"{unit} reported blocked {BLOCKED} times in a row since {units.hhmm(w)} without "
                               f"the owner. " + ASK.format(what="re-sending it"), "G2", unit, BLOCKED)
    else:
        goal = _norm(block.fields.get("GOAL"))
        for d in lineage.unit_dirs(reg, proj):
            b = brief.find(reg, d.name, project=proj)
            if (b is not None and b.rev and b.to == to and _norm(b.fields.get("GOAL")) == goal
                    and _blocked_twice(b, sender.session, w)):
                return Refused(f"{b.unit} reported blocked {BLOCKED} times in a row since {units.hhmm(w)} without "
                               f"the owner, and this brief sends {to} the same GOAL again. "
                               + ASK.format(what="re-sending it"), "G2", b.unit, BLOCKED)
    hit = _spend(proj, unit if block.kind == "amend" else None, w)
    if hit is not None:
        return Refused(hit[0] + ". " + ASK.format(what="spending more"), "G4", unit or "-", hit[1])
    if block.kind == "amend" and runner:
        from ..runs import history
        n = history.runs_of(proj, unit, w)
        runs = cap("runs_per_unit")
        if n >= runs:
            return Refused(f"{unit} ran {n} times since {units.hhmm(w)} without the owner (cap {runs}). "
                           + ASK.format(what="running it again"), "G5", unit, n)
    start = max(w, now - _dt.timedelta(hours=1))
    n = accepted + sum(len(_revs(d, start.timestamp())) for d in lineage.unit_dirs(reg, proj))
    rate = cap("briefs_per_hour")
    if n >= rate:
        return Refused(f"{proj} sent {n} briefs and AMENDs since {units.hhmm(start)} without the owner "
                       f"(cap {rate} an hour). " + ASK.format(what="sending more"), "G3", unit or "-", n)
    return None


def _flow():
    return config.load().get("flow") or {}


def _cap(f, key, default):
    """A flow cap from config; a missing or malformed value falls back to the default (one `config` line)."""
    v = f.get(key, default)
    try:
        if isinstance(v, bool) or float(v) < 0:
            raise ValueError(v)
        return float(v)
    except (TypeError, ValueError):
        log.log("config", f"flow.{key}={v!r} is not a number >= 0; using {default}")
        return float(default)


def cap(key):
    """A count cap (revisions, briefs_per_hour, runs_per_unit, depth, fanout) from config.flow, as an int."""
    return int(_cap(_flow(), key, config.DEFAULTS["flow"][key]))


def unit_cap(f=None):
    """G4's cap a unit (flow.max_unit_cost_usd, default 10; `f` the `flow` config, else loaded): also
    the most one run's reservation counts (history.reserve)."""
    return _cap(_flow() if f is None else f, "max_unit_cost_usd", 10)


def _spend(proj, unit, w):
    """G4: (text, n) when the project, or `unit` (an AMEND or a run), spent its cap inside W, else None."""
    from ..runs import history
    f = _flow()
    cap = _cap(f, "max_project_cost_usd", 30)
    spent = history.spend(proj, w)
    if spent >= cap:
        return (f"{proj} spent ${spent:.2f} on headless runs since {units.hhmm(w)} without the owner "
                f"(cap ${cap:g})", int(spent))
    if unit:
        cap = unit_cap(f)
        spent = history.spend(proj, w, unit)
        if spent >= cap:
            return (f"{unit} spent ${spent:.2f} on headless runs since {units.hhmm(w)} without the owner "
                    f"(cap ${cap:g} a unit)", int(spent))
    return None


def run_refusal(proj, unit, now=None):
    """G4/G5 for an `orche run` start, as a Refused (its `.text` has no prefix), or None."""
    if not _flow().get("enabled", True):
        return None
    w = window(proj, now)
    hit = _spend(proj, unit, w)
    if hit is not None:
        return Refused(hit[0], "G4", unit, hit[1])
    from ..runs import history
    n = history.runs_of(proj, unit, w)
    runs = cap("runs_per_unit")
    if n >= runs:
        return Refused(f"{unit} ran {n} times since {units.hhmm(w)} without the owner (cap {runs})", "G5", unit, n)
    return None


def spend_status(project, now=None):
    """{spent, since, cap} when the project is at its spend cap inside W (`orche status`), or None."""
    if not _flow().get("enabled", True):
        return None
    from ..runs import history
    w = window(project, now)
    cap = _cap(_flow(), "max_project_cost_usd", 30)
    spent = history.spend(project, w)
    return {"spent": spent, "since": units.hhmm(w), "cap": cap} if spent >= cap else None


def _marker(session):
    return paths.orche_home() / "sessions" / f"{util.slugify(session)}.relayed.json"


def streak(session, project, outcomes, now=None):
    """Count the session's refusals in a row (G7) over `outcomes` (True delivered, False refused, in
    block order). Returns the count after each outcome (0 after a delivered one). The count restarts
    after 30 min or when the owner spoke since its first refusal."""
    now = (now or units.now()).astimezone()
    path = _marker(session)
    try:
        data = util.read_json(path, default={})
    except util.JsonError:
        data = {}
    data = data if isinstance(data, dict) else {}
    r = data.get("refusals") or {}
    n, first = int(r.get("n") or 0), units.parse_iso(r.get("first_at"))
    if first is None or now - first > STREAK_TTL or first < window(project, now):
        n, first = 0, None
    counts = []
    for ok in outcomes:
        if ok:
            n, first = 0, None
        else:
            n, first = n + 1, first or now
        counts.append(n)
    data["refusals"] = {"n": n, "first_at": first.isoformat(timespec="seconds") if first else None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        util.write_json(path, data)
    except OSError as exc:
        log.log("hooks", f"flow: cannot write {path.name}: {exc}")
    return counts


def logged(rule, by, unit, n):
    log.log("events", f"flow refused rule={rule} by={by or 'unknown'} unit={unit or '-'} n={n}")


def looping(reg, project, now=None):
    """Units G1 holds: revised flow.revisions or more times inside W. [{unit, n, since}] for `orche status`."""
    if not (config.load().get("flow") or {}).get("enabled", True):
        return []
    w = window(project, now)
    out = []
    for d in lineage.unit_dirs(reg, project):
        n = len([r for r, _ in _revs(d, w.timestamp()) if r >= 2])
        if n >= cap("revisions"):
            out.append({"unit": d.name, "n": n, "since": units.hhmm(w)})
    return out


def pause_status(project):
    """pause.json with its `since` (HH:MM) for `orche status`, or None."""
    p = paused(project)
    if p is None:
        return None
    at = units.parse_iso(p.get("at"))
    return dict(p, since=units.hhmm(at) if at else "?")
