"""Dispatch relay: parse ```dispatch blocks, allocate unit ids, write BRIEF.md revisions, deliver.

Also the Stop-hook completion gate and overnight predicate (dispatch_relay.py stays thin).
"""
import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, inbox, ledger, log, mask, orca, paths, registry, runs, shell, transcript, units, util

REQUIRED = ("TO", "GOAL", "SCOPE", "CONTEXT", "ACCEPT", "VERIFY", "TIMEBOX", "FORBID", "REPORT", "TIER")
BRIEF_KEYS = REQUIRED + ("ROLLBACK",)
TIERS = ("mechanical", "standard", "hard")
REFUSE = "[orche] dispatch refused: "
BODY_MARK = re.compile(r"^## Brief\b.*$", re.MULTILINE)
CLAIM = re.compile(r"^(STATUS|Status):\s*(release-ready|done|완료)", re.MULTILINE)
UNIT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*-\d{8}-\d+$")
BLOCK = re.compile(r"^[ \t]*```dispatch[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL)


class Refusal(Exception):
    """A dispatch block that must go back to the model (Stop hook exit 2)."""


@dataclass
class Block:
    kind: str                       # "brief" | "amend" | "stop"
    fields: dict = field(default_factory=dict)
    unit: str = ""
    rev: int = 0
    note: str = ""


def find_blocks(text: str) -> list:
    return [m.rstrip("\n") for m in BLOCK.findall(text or "")]


def _check_keys(fields, unknown, allowed):
    bad = list(unknown) + [k for k in fields if k not in allowed]
    if bad:
        raise Refusal(f"{REFUSE}unknown keys {', '.join(bad)}. Allowed: {' '.join(allowed)}.")
    tier = fields.get("TIER", "").strip()
    if tier and tier not in TIERS:
        raise Refusal(f"{REFUSE}TIER must be one of {'|'.join(TIERS)}, got {tier!r}.")


def parse_block(body: str) -> Block:
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    first_i = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_i is None:
        raise Refusal(f"{REFUSE}missing {', '.join(REQUIRED)}. Fill every field or drop the block.")
    first = lines[first_i].strip()
    rest = lines[:first_i] + lines[first_i + 1:]
    if first.startswith("AMEND"):
        m = re.fullmatch(r"AMEND\s+(\S+)\s+rev\s+(\d+)", first)
        if not m:
            raise Refusal(f"{REFUSE}AMEND needs 'AMEND <unit> rev <N>'.")
        note = ""
        kept = []
        for ln in rest:
            sm = re.match(r"^SUPERSEDES\s+(.*)$", ln)
            if sm:
                note = sm.group(1).strip()
            else:
                kept.append(ln)
        fields, unknown = util.parse_kv_block("\n".join(kept))
        _check_keys(fields, unknown, BRIEF_KEYS)
        return Block("amend", fields, m.group(1), int(m.group(2)), note)
    if first.startswith("STOP"):
        m = re.fullmatch(r"STOP\s+(\S+)(?:\s+(.*))?", first)
        if not m:
            raise Refusal(f"{REFUSE}STOP needs 'STOP <unit>'.")
        fields, unknown = util.parse_kv_block("\n".join(rest))
        _check_keys(fields, unknown, ("TO",))
        return Block("stop", fields, m.group(1), 0, (m.group(2) or "").strip())
    fields, unknown = util.parse_kv_block("\n".join(lines))
    _check_keys(fields, unknown, BRIEF_KEYS)
    missing = [k for k in REQUIRED if not fields.get(k, "").strip()]
    if missing:
        raise Refusal(f"{REFUSE}missing {', '.join(missing)}. Fill every field or drop the block.")
    return Block("brief", fields)


# ---- targets -------------------------------------------------------------

def resolve_target(reg: dict, to: str, here_project=None):
    """Session name for TO: an exact session, a division shorthand of the current project, or a
    project key/alias (-> its main session). None if unknown."""
    to = (to or "").strip()
    if to in units.all_sessions(reg):
        return to
    if here_project and to in registry.divisions_of(reg["projects"].get(here_project, {})):
        div = reg["projects"][here_project].get("divisions", {}).get(to)
        if div and div.get("session"):
            return div["session"]
    hit = registry.project(reg, to)
    if hit:
        return hit[1].get("main_session")
    return None


def project_of_session(reg: dict, session: str):
    for key, proj in reg.get("projects", {}).items():
        if session in (proj.get("main_session"), proj.get("liaison_session")):
            return key
    unit = units.find_unit(reg, session)
    return unit.project if unit else None


def wake_state(reg: dict, session: str) -> str:
    """What to log when the target has no pane (Task 7 owns waking; this never creates panes)."""
    unit = units.find_unit(reg, session)
    entry = unit.entry if unit else {}
    default = session.startswith("main-") or bool(unit and unit.kind == "div" and unit.name == "build")
    if entry.get("wake_on_dispatch", default):
        return "wake-requested"
    if entry.get("status") in ("dormant", "paused"):
        return "needs-wake"
    return "no-pane"


# ---- briefs --------------------------------------------------------------

def unit_dir(repo, unit: str) -> Path:
    if not UNIT_ID.match(unit or ""):
        raise Refusal(f"{REFUSE}bad unit id {unit!r} (want <proj>-<slug>-<yyyymmdd>-<n>).")
    return Path(repo) / ".orche" / "units" / unit


def allocate_unit_id(repo, proj: str, goal: str, date=None) -> str:
    """`<proj>-<slug>-<yyyymmdd>-<n>`; slug = first 4 GOAL words; n = next free for the project today.

    The unit directory is created here so concurrent relays cannot pick the same id.
    """
    date = date or units.now().date()
    goal = mask.redact(goal)[0]  # the id lands in paths, pointers, nudges and events.log
    slug = util.slugify(" ".join(goal.split()[:4])) or "unit"
    base = Path(repo) / ".orche" / "units"
    base.mkdir(parents=True, exist_ok=True)
    pat = re.compile(rf"^{re.escape(util.slugify(proj))}-.+-{date.strftime('%Y%m%d')}-(\d+)$")
    n = 1 + max([int(m.group(1)) for m in (pat.match(p.name) for p in base.iterdir()) if m] or [0])
    while True:
        unit = util.unit_id(proj, slug, n, date)
        try:
            (base / unit).mkdir()
            return unit
        except FileExistsError:
            n += 1


def current_rev(repo, unit: str) -> int:
    revs = [int(m.group(1)) for m in (re.fullmatch(r"BRIEF\.rev(\d+)\.md", p.name)
                                      for p in unit_dir(repo, unit).glob("BRIEF.rev*.md")) if m]
    return max(revs or [0])


@dataclass
class Where:
    """Where the target works: its own worktree + branch, and the project root holding the brief."""
    frm: str
    worktree: str
    branch: str
    root: str


MAIN_CHECKOUT = " (main checkout)"


def where(reg, to, frm, proj, timeout=0.5) -> Where:
    """A unit target works in its registered worktree; Main works in the main checkout.

    A unit with no recorded worktree is shown as the project root, marked `(main checkout)`, never `.`.
    `timeout` bounds the `git branch` call for the main checkout; at or below 0 it is skipped (`-`).
    """
    root = str(units.project_root(reg, proj) or "")
    unit = units.find_unit(reg, to)
    if unit is not None and units.worktree_path(unit.entry.get("worktree", "")):
        return Where(frm, str(unit.worktree_path), unit.entry.get("branch") or "-", root)
    recorded = unit.entry.get("branch") if unit is not None else None
    res = (shell.run(["git", "-C", root, "branch", "--show-current"], timeout=timeout)
           if root and timeout > 0 and not recorded else None)
    branch = recorded or (res.out.strip() if res is not None and res.ok else "") or "-"
    return Where(frm, root + (MAIN_CHECKOUT if unit is not None else ""), branch, root)


def render_brief(unit, rev, standing, to, fields, at, supersedes=None, note="") -> str:
    lines = [f"rev: {rev}"]
    if supersedes:
        lines.append(f"supersedes: rev {supersedes}")
        if note:
            lines.append(f"supersedes-note: {note}")
    lines += [f"unit: {unit}",
              f"to: {to}   from: {at.frm}",
              f"worktree: {at.worktree}   branch: {at.branch}",
              f"project root: {at.root}   (brief + REPORT.md live under project root/.orche/units/{unit}/)",
              "Work only inside `worktree`. SCOPE paths are relative to it."]
    lines += ["", standing.rstrip(), "", f"persona: .claude/agents/{to}.md", "", f"## Brief {unit}"]
    for key in BRIEF_KEYS:
        if key in fields:
            first, *more = str(fields[key]).split("\n")
            lines.append(f"{key:<9} {first}".rstrip())
            lines += [f"  {m}" for m in more]
    return "\n".join(lines) + "\n"


def write_brief(repo, unit, rev, text) -> Path:
    folder = unit_dir(repo, unit)
    util.atomic_write(folder / f"BRIEF.rev{rev}.md", text)
    util.atomic_write(folder / "BRIEF.md", text)
    return folder / "BRIEF.md"


def read_brief_fields(repo, unit) -> dict:
    try:
        text = (unit_dir(repo, unit) / "BRIEF.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    marks = list(BODY_MARK.finditer(text))
    if not marks:
        return {}
    return util.parse_kv_block(text[marks[-1].end():])[0]


def standing_text(reg, proj_key) -> str:
    proj = reg["projects"][proj_key]
    path = Path(units.project_root(reg, proj_key) or ".") / proj.get("standing", ".orche/STANDING.md")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.log("hooks", f"dispatch: no STANDING.md at {path}")
        return "(no STANDING.md)"


def repo_of_unit(reg, unit):
    """Project key + repo whose `.orche/units/<unit>` exists (longest project-key prefix wins)."""
    for key in sorted(reg.get("projects", {}), key=len, reverse=True):
        root = units.project_root(reg, key)
        if unit.startswith(util.slugify(key) + "-") and root and unit_dir(root, unit).is_dir():
            return key, root
    return None, None


# ---- relay ---------------------------------------------------------------

@dataclass
class Plan:
    block: Block
    to: str
    proj: str
    repo: Path
    unit: str = ""
    external: bool = False          # TO <other project> -> external-request pointer to its Main


def _target_error(reg, to):
    return f"{REFUSE}unknown TO {to!r}. Valid targets: {', '.join(units.all_sessions(reg))}."


def _cross_project_error(to, target_proj, sender_proj):
    return (f"{REFUSE}{to} belongs to project {target_proj}, not {sender_proj}. Cross-project work goes "
            f"through Main({target_proj}): use `TO {target_proj}` (files an external-request).")


def _external_project(reg, to, sender_proj):
    """Project key when TO names ANOTHER project by key/alias (not a session), else None."""
    to = (to or "").strip()
    if not sender_proj or to in units.all_sessions(reg):
        return None
    hit = registry.project(reg, to)
    return hit[0] if hit and hit[0] != sender_proj else None


def plan_blocks(reg, bodies, here_project, sender_proj=None) -> list:
    """Parse + resolve every block first so nothing is written when any block is refused.

    `bodies` are block texts or already-built `Block`s (triage builds its brief as a Block, so a
    prompt line starting with `#` is never read as a comment).

    Targets must belong to the sender's project (R-XPROJ): `TO <other project key|alias>` becomes
    an external-request pointer to that project's Main; any other cross-project target is refused.
    """
    plans, errors = [], []
    home = sender_proj or here_project
    for body in bodies:
        try:
            block = body if isinstance(body, Block) else parse_block(body)
            if block.kind == "brief":
                ext = _external_project(reg, block.fields["TO"], sender_proj)
                if ext:
                    main = reg["projects"][ext].get("main_session")
                    if not main:
                        raise Refusal(f"{REFUSE}project {ext} has no main session.")
                    plans.append(Plan(block, main, ext, units.project_root(reg, ext), external=True))
                    continue
                to = resolve_target(reg, block.fields["TO"], home)
                if to is None:
                    raise Refusal(_target_error(reg, block.fields["TO"]))
                proj = project_of_session(reg, to)
                if sender_proj and proj != sender_proj:
                    raise Refusal(_cross_project_error(to, proj, sender_proj))
                plans.append(Plan(block, to, proj, units.project_root(reg, proj)))
                continue
            proj, repo = repo_of_unit(reg, block.unit)
            if proj is None:
                raise Refusal(f"{REFUSE}unknown unit {block.unit!r} (no .orche/units/{block.unit}/BRIEF.md).")
            if sender_proj and proj != sender_proj:
                raise Refusal(_cross_project_error(block.unit, proj, sender_proj))
            old_to = read_brief_fields(repo, block.unit).get("TO", "")
            raw_to = block.fields.get("TO") or old_to
            to = resolve_target(reg, raw_to, proj)
            if to is None:
                raise Refusal(_target_error(reg, raw_to))
            if sender_proj and project_of_session(reg, to) != sender_proj:
                raise Refusal(_cross_project_error(to, project_of_session(reg, to), sender_proj))
            plans.append(Plan(block, to, proj, repo, block.unit))
        except Refusal as exc:
            errors.append(str(exc))
    if errors:
        raise Refusal("\n".join(errors))
    return plans


def run_argv(unit) -> list:
    """`orche run <unit>` under this very interpreter (the 3.11+ running the hook): no launcher
    has to find Python again after the spawn, so a run never dies there without a pointer."""
    return [sys.executable, str(paths.PLUGIN_ROOT / "bin" / "orche.py"), "run", unit]


def spawn_run(unit) -> None:
    """Start `orche run <unit>` detached (own session, output to logs/runs/<unit>.log); never waits.

    ORCHE_RUN_SINCE (now, ns) dates the request: a STOP marker written after it stops the run even
    if it lands before the new process got going; an AMEND's marker, written before, never does.
    The plugin `bin` leads PATH; ORCHE_RUN_ID (set when the hook itself runs inside a run's worker)
    is dropped, so that run's end-of-run sweep never takes this new run with it."""
    out = paths.orche_home() / "logs" / "runs" / f"{unit}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "ORCHE_RUN_ID"}
    env["ORCHE_RUN_SINCE"] = str(time.time_ns())
    env["PATH"] = f"{paths.PLUGIN_ROOT / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    with open(out, "ab") as fh:
        subprocess.Popen(run_argv(unit), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True, env=env)


def runner_wanted(reg, plan, cfg) -> bool:
    """A brief, AMEND or STOP for a division/feature goes through the runner unless `delivery: pane`
    (or a hard TIER with `runner.hard_in_pane`). Main/liaison targets keep the pane: a human reads them."""
    if cfg.get("delivery", "runner") != "runner":
        return False
    if units.find_unit(reg, plan.to) is None:
        return False
    tier = plan.block.fields.get("TIER", "")
    if not tier and plan.block.kind != "brief":
        tier = read_brief_fields(plan.repo, plan.unit).get("TIER", "")
    hard = tier.strip() == "hard"
    return not (hard and (cfg.get("runner") or {}).get("hard_in_pane"))


RUNNER_NOT_LIVE = " (no live run)"


def _run_delivery(plan, terms) -> str:
    """Runner delivery, never waiting: a brief starts `orche run`; STOP writes the stop marker and
    stops a live run (runs.stop: the marker, which the run polls every 2 s, plus SIGTERM for a
    `ps`-verified pid; the runner files `info` "stopped by Main"; a run still
    waiting for a slot gives up, "… before start") — `runner-stop-requested` either way, with
    " (no live run)" when nothing was running; AMEND stops a live run and starts a fresh one for
    the new rev (it waits for the stopped run's slot). The `ps` pid check inside runs.stop gets at
    most what is left of the hook budget."""
    kind = plan.block.kind
    if kind == "stop":
        live = runs.stop(plan.proj, plan.unit, timeout=terms.remaining())
        return "runner-stop-requested" + ("" if live else RUNNER_NOT_LIVE)
    restarted = kind == "amend" and runs.stop(plan.proj, plan.unit, timeout=terms.remaining())
    spawn_run(plan.unit)
    return "runner-restarted" if restarted else "runner-started"


def _deliver(reg, plan, rev, status, summary, frm, terms, cfg=None, by=None):
    """Pointer always; then the runner (spawned, never awaited) or a pane nudge while the hook budget
    lasts (one terminal list per run). A failed runner delivery falls back to the nudge.
    `by` names who relayed in the events line (default the sender; triage passes `triage`)."""
    brief = unit_dir(plan.repo, plan.unit) / "BRIEF.md"
    inbox.write(inbox.make(plan.unit, frm, plan.to, status, report_path=str(brief), brief_rev=rev,
                           summary=summary))
    units.touch_activity(plan.to)
    prefix = ""
    by = by or frm
    if runner_wanted(reg, plan, cfg if cfg is not None else config.load()):
        try:
            state = _run_delivery(plan, terms)
            log.log("events", f"dispatch kind={plan.block.kind} unit={plan.unit} rev={rev} to={plan.to} "
                              f"by={by} delivery={state}")
            return state
        except Exception as exc:  # noqa: BLE001 - fall back to the pane nudge
            log.log("hooks", f"dispatch: runner delivery for {plan.unit} failed: {exc}")
            prefix = "runner-failed → "
    state = _nudge(reg, plan, rev, terms)
    log.log("events", f"dispatch kind={plan.block.kind} unit={plan.unit} rev={rev} to={plan.to} "
                      f"by={by} delivery={prefix}{state}")
    return prefix + state


def _nudge(reg, plan, rev, terms):
    term = orca.match_title(terms.list(), plan.to) if terms.remaining() > 0 else None
    if terms.remaining() <= 0:
        state = "budget-exceeded"
    elif term is None:
        state = "nudge-failed" if terms.error else wake_state(reg, plan.to)
    else:
        try:
            terms.send(term, f"[orche] inbox: {plan.unit} rev {rev}")
            state = "nudged"
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort; brief + pointer are on disk
            state = "budget-exceeded" if terms.remaining() <= 0 else "nudge-failed"
            log.log("hooks", f"dispatch: nudge {plan.to} failed: {exc}")
    return state


def _feedback(reg, frm, unit, to, rev, state, kind=None, by=None):
    """One non-urgent `info` pointer to the sender: unit id + delivery state (seen at its next prompt).

    Nothing opens a pane for a division/feature target, so the sender is told to wake it (pane
    delivery) or to re-run it with `orche run` (a failed runner delivery); runner states never
    mention `orche wake`. A brief triage routed from a prompt reads "routed <unit> → <to> (<state>)".
    """
    summary = (f"routed {unit} → {to} ({state})" if by == "triage"
               else f"relayed {unit} → {to} rev {rev} ({state})")
    pane_state = state.rsplit("→ ", 1)[-1]   # after a failed runner spawn: the nudge outcome
    if state.startswith("runner-failed") and kind == "stop":
        summary += " — the STOP pointer is filed; check `orche status` for a live run"
    elif state.startswith("runner-failed"):
        summary += f" — runner did not start: run `orche run {unit}`"
    elif state == "runner-stop-requested" + RUNNER_NOT_LIVE:
        summary += (" — already finished (or still queued: a queued run will not start); "
                    "the STOP pointer is filed")
    elif pane_state in ("wake-requested", "needs-wake") and units.find_unit(reg, to) is not None:
        summary += f" — target has no pane: run `orche wake {to}`"
    elif pane_state == "needs-wake":
        summary += f" — target is asleep: run `orche wake {to}`"
    try:        # best-effort: after a started runner nothing may turn the relay into an error
        inbox.write(inbox.make(unit, "orche", frm, "info", brief_rev=rev, summary=summary))
    except (ValueError, OSError) as exc:
        log.log("hooks", f"dispatch: cannot file feedback for {frm!r}: {exc}")


def _external(reg, plan, frm, sender_proj):
    """TO <projB>: an external-request pointer to Main(B) carrying the brief body (no brief written)."""
    fields = plan.block.fields
    body = "; ".join(f"{k} {' '.join(str(fields[k]).split())}" for k in BRIEF_KEYS if k in fields and k != "TO")
    goal = util.slugify(" ".join(mask.redact(fields.get("GOAL", ""))[0].split()[:4])) or "request"
    plan.unit = f"xreq-{util.slugify(sender_proj or 'unknown')}-{goal}-{units.now():%Y%m%d}"
    inbox.write(inbox.make(plan.unit, frm, plan.to, "external-request", summary=mask.redact(body)[0],
                           from_project=sender_proj))
    units.touch_activity(plan.to)
    log.log("events", f"dispatch kind=external-request unit={plan.unit} to={plan.to} by={frm} "
                      f"from_project={sender_proj}")
    return "external-request filed"


def sender_role(reg, session) -> str:
    """main | division | feature | liaison | unknown for a session name."""
    for proj in reg.get("projects", {}).values():
        if session and session == proj.get("main_session"):
            return "main"
        if session and session == proj.get("liaison_session"):
            return "liaison"
    unit = units.find_unit(reg, session) if session else None
    if unit is None:
        return "unknown"
    return "division" if unit.kind == "div" else "feature"


def check_sender(reg, frm, identified=True) -> None:
    """Only a project's Main or a division (Build dispatches to features) may dispatch.

    `identified=False`: the sender was guessed from the main checkout (no ORCHE_SESSION) -> refuse.
    """
    if not identified:
        main = frm if sender_role(reg, frm) == "main" else "main-<proj>"
        raise Refusal(f"dispatch requires an identified session: set ORCHE_SESSION={main} "
                      "(ORCHE panes set it automatically)")
    role = sender_role(reg, frm)
    if role not in ("main", "division"):
        article = "an" if role[0] in "aeiou" else "a"
        raise Refusal(f"only Main or a division may dispatch; {frm} is {article} {role} session")


def relay(reg, bodies, here_project, frm, deadline=None, identified=True) -> list:
    """Write briefs + pointers and nudge targets. Returns the unit ids. Raises Refusal."""
    return [d["unit"] for d in relay_detail(reg, bodies, here_project, frm, deadline, identified)]


def relay_detail(reg, bodies, here_project, frm, deadline=None, identified=True, by=None) -> list:
    """relay(), returning `{"unit", "to", "state"}` per block. `by` (e.g. `triage`) is who relayed:
    it names the events line's `by=` and words the sender's feedback pointer."""
    check_sender(reg, frm, identified)
    sender_proj = project_of_session(reg, frm)
    plans = plan_blocks(reg, bodies, here_project, sender_proj)
    terms = orca.Terminals(deadline if deadline is not None else orca.hook_deadline())
    cfg = config.load()
    done = []
    for plan in plans:
        block = plan.block
        if plan.external:
            state = _external(reg, plan, frm, sender_proj)
            _feedback(reg, frm, plan.unit, plan.to, 0, state)
        elif block.kind == "stop":
            rev = current_rev(plan.repo, plan.unit) or 1
            state = _deliver(reg, plan, rev, "stop", block.note or "STOP requested", frm, terms, cfg, by)
            _feedback(reg, frm, plan.unit, plan.to, rev, state, kind="stop", by=by)
        else:
            standing = standing_text(reg, plan.proj)
            if block.kind == "brief":
                plan.unit = allocate_unit_id(plan.repo, plan.proj, block.fields["GOAL"])
                rev, fields, supersedes = 1, dict(block.fields), None
            else:
                prev = current_rev(plan.repo, plan.unit)
                if block.rev != prev + 1:
                    log.log("hooks", f"dispatch: AMEND {plan.unit} said rev {block.rev}; using {prev + 1}")
                rev, supersedes = prev + 1, prev or None
                fields = {**read_brief_fields(plan.repo, plan.unit), **block.fields, "TO": plan.to}
            fields["TO"] = plan.to
            at = where(reg, plan.to, frm, plan.proj, timeout=min(0.5, terms.remaining()))
            write_brief(plan.repo, plan.unit, rev,
                        render_brief(plan.unit, rev, standing, plan.to, fields, at, supersedes, block.note))
            goal = mask.redact(fields.get("GOAL", "").split("\n")[0])[0]
            state = _deliver(reg, plan, rev, "info", goal, frm, terms, cfg, by)
            _feedback(reg, frm, plan.unit, plan.to, rev, state, by=by)
        done.append({"unit": plan.unit, "to": plan.to, "state": state})
    return done


# ---- completion gate + overnight predicate -------------------------------

def git_head(cwd, timeout=2):
    res = shell.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], timeout=timeout)
    out = res.out.strip()
    return out if res.ok and re.fullmatch(r"[0-9a-f]{7,64}", out) else None


GATE_PREFIX = "[orche] completion blocked: missing "
GATE_SUFFIX = ". Record the verdict / update STATUS.md, then report again."


def ledger_missing(project, head, minimum) -> tuple:
    """([missing items], row) for the canonical ledger row of `head` at verdict >= `minimum`."""
    where = f"ORCHE_HOME/ledger/{project}.tsv"
    if head is None:
        return ["LEDGER row (cannot read git HEAD in cwd)"], None
    row = ledger.row_for_sha(project, head)
    if row is None:
        return [f"LEDGER row for HEAD {head[:12]} in {where} (verdict >= {minimum})"], None
    if not ledger.satisfies(row.get("verdict", ""), minimum):
        return [f"LEDGER row for HEAD {head[:12]} has verdict {row.get('verdict')!r}; need >= {minimum}"], row
    return [], row


def gated_sender(unit) -> bool:
    """Build division and feature agents carry the ledger gate."""
    return unit is not None and (unit.kind == "feat" or unit.name == "build")


def completion_gate(text, reg, cwd, transcript_path, claimant=None, deadline=None) -> list:
    """Missing items for a `STATUS: done|release-ready|완료` claim from build/feature sessions.

    Reads the canonical ledger ORCHE_HOME/ledger/<proj>.tsv; release-ready also needs a verifier
    other than the claimant (writer != judge).
    """
    claim = CLAIM.search(text or "")
    if not claim:
        return []
    unit = units.unit_from_cwd(reg, cwd)
    if not gated_sender(unit):
        return []
    root = Path(os.path.realpath(unit.worktree_path))
    minimum = "unit-test-verified" if claim.group(2) == "release-ready" else "type-check-only"
    left = (deadline - time.monotonic()) if deadline is not None else 2
    head = git_head(cwd, timeout=min(2, max(left, 0.2)))
    missing, row = ledger_missing(unit.project, head, minimum)
    if row is not None and not missing:
        if claim.group(2) == "release-ready" and row.get("verifier") in {claimant, unit.session} - {None, ""}:
            missing.append(f"independent verifier for HEAD {head[:12]} (LEDGER row verifier is "
                           f"{row.get('verifier')}; writer != judge)")
    rel = Path(".orche") / unit.kind / unit.name / "STATUS.md"
    status = root / rel
    if not status.is_file():
        missing.append(f"{rel} (missing)")
    else:
        started = transcript.first_timestamp(transcript_path) if transcript_path else None
        if started is not None and status.stat().st_mtime < started.timestamp():
            missing.append(f"{rel} update in this session (last written before the session started)")
    return missing


def night_predicate(root, session):
    """(ok, message). Missing/non-executable predicate -> ok."""
    path = Path(root) / ".orche" / "night" / f"{session}.predicate.sh"
    if not session or not path.is_file() or not os.access(path, os.X_OK):
        return True, ""
    res = shell.run([str(path)], timeout=10)
    if res.ok:
        return True, ""
    return False, res.out.strip() or f"[orche] night predicate {path.name} not satisfied (exit {res.code})."


# ---- Stop hook entry -----------------------------------------------------

def _relayed_marker(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or "")) or "unknown"
    return paths.orche_home() / "sessions" / f"{safe}.relayed.json"


def _refusal_to_self(me, message):
    """Under stop_hook_active a refusal must not loop: park it in the session's own inbox."""
    log.log("hooks", f"dispatch refused under stop_hook_active: {message}")
    try:
        inbox.write(inbox.make("dispatch-refused", "orche", me, "question", summary=message))
    except ValueError as exc:
        log.log("hooks", f"dispatch: cannot park refusal for {me!r}: {exc}")


def final_text(payload: dict):
    """(msg_id, text) of the turn's final assistant message.

    Claude Code writes the transcript asynchronously, so a Stop hook can see a lagging file; the
    hook input's `last_assistant_message` is authoritative. The transcript is parsed only when the
    field is absent (older Claude Code).
    """
    if "last_assistant_message" in payload:
        text = payload.get("last_assistant_message")
        return None, text if isinstance(text, str) else ""
    tpath = payload.get("transcript_path")
    return transcript.last_assistant(tpath) if tpath else (None, "")


def handle_stop(payload: dict, started=None):
    """(exit_code, stderr_message) for the Stop hook."""
    deadline = orca.hook_deadline(started)
    cwd = payload.get("cwd") or os.getcwd()
    tpath = payload.get("transcript_path")
    msg_id, text = final_text(payload)
    reg = registry.load()
    env = os.environ.get("ORCHE_SESSION")
    me = env or units.session_from_cwd(reg, cwd) or "unknown"
    # Identity needs the env, except in a unit worktree (the worktree itself names the unit).
    unit = units.unit_from_cwd(reg, cwd)
    identified = bool(env) or bool(unit is not None and unit.session and unit.session == me)
    if identified:
        units.touch_activity(me)
    bodies = find_blocks(text)
    if bodies:
        marker = _relayed_marker(payload.get("session_id"))
        seen = util.read_json(marker, default={}) or {}
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if not (seen.get("text_hash") == text_hash or (msg_id is not None and seen.get("msg_id") == msg_id)):
            try:
                relay(reg, bodies, units.project_from_cwd(reg, cwd), me, deadline, identified=identified)
            except Refusal as exc:
                if not payload.get("stop_hook_active"):
                    return 2, str(exc)
                _refusal_to_self(me, str(exc))
            util.write_json(marker, {"text_hash": text_hash, "msg_id": msg_id, "at": util.now_iso()})
    if payload.get("stop_hook_active"):
        return 0, ""
    missing = completion_gate(text, reg, cwd, tpath, me, deadline)
    if missing:
        return 2, GATE_PREFIX + "; ".join(missing) + GATE_SUFFIX
    unit = units.unit_from_cwd(reg, cwd)
    proj = units.project_from_cwd(reg, cwd)
    root = (unit.worktree_path if unit else None) or (units.project_root(reg, proj) if proj else None) or cwd
    ok, message = night_predicate(root, me)
    return (0, "") if ok else (2, message)
