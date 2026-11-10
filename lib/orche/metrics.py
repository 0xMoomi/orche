"""Token-economy metrics from Claude Code transcripts (`~/.claude/projects/**/*.jsonl`). No model calls.

Assistant responses are deduplicated by `message.id` (Claude Code writes one line per content
block, and resumed sessions repeat history). Files under a `subagents/` directory are subagent
transcripts; every other file is a main session.

A number whose source cannot be read is None (rendered `n/a`), never 0: a missing or unlistable
transcript root, a transcript that cannot be opened (the totals), a mean over no samples.
"""
import datetime as _dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path


# USD list price per 1M tokens: input, output, cache read, cache write (5-minute cache, 1.25x input),
# one-hour cache write (2x input).
PRICES = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.0, "cache_write_1h": 16.0},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5, "cache_write_1h": 20.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0, "cache_write_1h": 8.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5, "cache_write_1h": 4.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125, "cache_write_1h": 0.2},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25, "cache_write_1h": 10.0},
}
FALLBACK_MODEL = "claude-opus-5"
PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
NOTIFICATION_TAG = "<task-notification>"
# A short user message matching this is "routine" (status / resume / confirm).
ROUTINE_RE = re.compile(r"\b(?:status|progress|continue|go\s+ahead|approve)\b", re.IGNORECASE)
ROUTINE_MAX_CHARS = 80
SKIP_MODELS = {"<synthetic>"}


def default_root() -> Path:
    """Claude Code's transcript root (`$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`)."""
    from .hr import panes       # hr loads metrics: import on use
    return panes.claude_projects()


def na(value, fmt="{}") -> str:
    """`value` formatted, or `n/a` when it is None (its source could not be read)."""
    return "n/a" if value is None else fmt.format(value)


def load_prices(path=None) -> dict:
    """PRICES, with whole rows from a JSON file merged over (a row replaces that model's row).

    File format: `{"<model>": {"input": n, "output": n, "cache_read": n, "cache_write": n,
    "cache_write_1h": n}}`, USD per 1M tokens, every row with exactly those five numeric keys. Raises ValueError
    naming the offending model otherwise.
    """
    prices = {model: dict(row) for model, row in PRICES.items()}
    if not path:
        return prices
    with open(path, encoding="utf-8") as fh:
        override = json.load(fh)
    if not isinstance(override, dict):
        raise ValueError(f"{path}: expected an object of model -> price row")
    for model, row in override.items():
        if not isinstance(row, dict) or set(row) != set(PRICE_KEYS):
            raise ValueError(f"{path}: model {model!r}: row must be an object with exactly the keys "
                             f"{', '.join(PRICE_KEYS)}")
        for key in PRICE_KEYS:
            value = row[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{path}: model {model!r}: {key} must be a non-negative number")
        prices[model] = {key: float(row[key]) for key in PRICE_KEYS}
    return prices


def known_row(model: str, prices: dict):
    """Exact model, else the longest known prefix (dated ids); None for a model `prices` does not price.
    The one lookup: price_row here and the ladder's ceiling (ladder.price) both use it."""
    if model in prices:
        return prices[model]
    prefixes = [m for m in prices if model.startswith(m)]
    return prices[max(prefixes, key=len)] if prefixes else None


def price_row(model: str, prices: dict) -> dict:
    """known_row, else the fallback row (compute names such a model in `unpriced`)."""
    return known_row(model, prices) or prices[FALLBACK_MODEL]


def _count(d, key) -> int:
    value = d.get(key) if isinstance(d, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def cost(usage: dict, row: dict) -> float:
    """List-price USD of one response's usage. Cache writes are priced by TTL from
    `usage.cache_creation` (`ephemeral_5m_input_tokens`, `ephemeral_1h_input_tokens`); without
    that split every write is priced at the 5-minute rate."""
    split = usage.get("cache_creation")
    if isinstance(split, dict) and ("ephemeral_5m_input_tokens" in split or "ephemeral_1h_input_tokens" in split):
        writes_5m, writes_1h = _count(split, "ephemeral_5m_input_tokens"), _count(split, "ephemeral_1h_input_tokens")
    else:
        writes_5m, writes_1h = _count(usage, "cache_creation_input_tokens"), 0
    return (_count(usage, "input_tokens") * row["input"]
            + _count(usage, "output_tokens") * row["output"]
            + _count(usage, "cache_read_input_tokens") * row["cache_read"]
            + writes_5m * row["cache_write"]
            + writes_1h * row["cache_write_1h"]) / 1_000_000


def is_subagent(path: Path, root: Path) -> bool:
    return "subagents" in path.relative_to(root).parts


def compute(root=None, days=30, now=None, prices=None, classify=None) -> dict:
    """The transcript metrics. `classify` (classifier()): also `roles`, the spend by role, each transcript
    counted under the role of its session (a subagent's: its parent session's)."""
    root = Path(root or default_root()).expanduser()
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    prices = prices or load_prices()
    cutoff = now - _dt.timedelta(days=days)
    seen_msgs, seen_users, ask_ids = set(), set(), set()
    usage_by_model = defaultdict(lambda: dict.fromkeys(USAGE_KEYS, 0))
    responses = defaultdict(int)
    spend = defaultdict(float)
    context = defaultdict(int)
    by_day = defaultdict(lambda: defaultdict(int))
    wake = {"user": 0, "notification": 0}
    routine = 0
    files = {"main": 0, "subagent": 0}
    unreadable = 0
    unpriced = set()
    sessions, names = defaultdict(float), {}    # (folder, session id) -> spend; -> session name

    from . import transcript       # hr loads metrics; transcript stays out of its imports
    try:
        paths = sorted(root.rglob("*.jsonl")) if root.is_dir() and os.access(root, os.R_OK | os.X_OK) else None
    except OSError:
        paths = None
    for path in paths or []:
        try:
            if _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc) < cutoff:
                continue
        except OSError:
            unreadable += 1
            continue
        if not os.access(path, os.R_OK):
            unreadable += 1
            continue
        sub = is_subagent(path, root)
        files["subagent" if sub else "main"] += 1
        parts = path.relative_to(root).parts
        session = (parts[0] if len(parts) > 1 else "", parts[1] if sub and len(parts) > 2 else path.stem)
        for obj in transcript.records(path):
            if not sub and obj.get("type") in ("custom-title", "agent-name"):
                name = obj.get("customTitle") or obj.get("agentName")
                if isinstance(name, str) and name:
                    names[session] = name
                continue
            ts = transcript.timestamp(obj)
            if ts is None or ts < cutoff or ts > now:
                continue
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if obj.get("type") == "assistant":
                content = message.get("content") if isinstance(message.get("content"), list) else []
                for i, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") == "AskUserQuestion":
                        ask_ids.add(block.get("id") or (message.get("id"), i))
                mid, model = message.get("id"), message.get("model") or "unknown"
                if not mid or mid in seen_msgs or model in SKIP_MODELS:
                    continue
                seen_msgs.add(mid)
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                responses[model] += 1
                if known_row(model, prices) is None:
                    unpriced.add(model)
                usd = cost(usage, price_row(model, prices))
                spend[model] += usd
                sessions[session] += usd
                for key in USAGE_KEYS:
                    value = usage.get(key)
                    usage_by_model[model][key] += value if isinstance(value, int) else 0
                u = usage_by_model[model]
                context[model] = (u["input_tokens"] + u["cache_read_input_tokens"]
                                  + u["cache_creation_input_tokens"])
                by_day[ts.astimezone(now.tzinfo).date().isoformat()][model] += 1
            elif obj.get("type") == "user" and not sub and not obj.get("isMeta"):
                text = transcript.user_text(message)
                key = obj.get("uuid") or (str(path), text, obj.get("timestamp"))
                if text is None or key in seen_users:
                    continue
                seen_users.add(key)
                if NOTIFICATION_TAG in text:
                    wake["notification"] += 1
                else:
                    wake["user"] += 1
                    if len(text) <= ROUTINE_MAX_CHARS and ROUTINE_RE.search(text):
                        routine += 1

    models = {}
    for model in sorted(responses):
        n = responses[model]
        models[model] = {
            "responses": n,
            "per_day": round(n / days, 2) if days else float(n),
            "avg_context": round(context[model] / n),
            **usage_by_model[model],
            "cost_usd": round(spend[model], 6),
        }
    total = sum(responses.values())
    readable = paths is not None
    whole = readable and not unreadable       # a total over a partial read is not a real number
    out = {
        "root": str(root),
        "readable": readable,
        "unreadable": unreadable,
        "days": days,
        "files": files,
        "responses": total if readable else None,
        "avg_context": round(sum(context.values()) / total) if total else None,
        "cost_usd": round(sum(m["cost_usd"] for m in models.values()), 6) if whole else None,
        "models": models,
        "by_day": {day: dict(sorted(v.items())) for day, v in sorted(by_day.items())},
        "wake": wake,
        "routine": {"messages": wake["user"], "routine": routine,
                    "share": round(routine / wake["user"], 4) if wake["user"] else None},
        "ask_user_question": len(ask_ids) if readable else None,
        "unpriced": sorted(unpriced),
    }
    if classify is not None:
        roles, named = defaultdict(float), defaultdict(float)
        for key, usd in sessions.items():
            roles[classify(key[0], names.get(key))] += usd
            if names.get(key):
                named[names[key]] += usd
        out["roles"] = {role: round(usd, 6) for role, usd in sorted(roles.items())} if readable else None
        out["named"] = {name: round(usd, 6) for name, usd in sorted(named.items())} if readable else None
    return out


# ---- spend by role ------------------------------------------------------------------------------
# Transcripts: a session named `main-<p>` is Main, `staff-<p>` Staff, `div-<p>-<d>` the `<d>` pane, a feature
# agent's its feature pane; a session without a name in a division's or feature's worktree is an `orche run`
# worker; any other transcript is OTHER. Headless calls that keep no transcript come from Orche's own records:
# review calls and Codex worker attempts (runs/<p>/history.jsonl), intake and the Liaison's ask and card refresh
# (logs/events.log, the line each successful call writes), triage (triage/samples.jsonl).
MAIN, STAFF, WORKERS, FEATURES, OTHER = "Main", "Staff", "workers", "feature panes", "other (not Orche)"
HEADLESS = ("review", "intake", "ask", "triage")
WORK_KINDS = (None, "", "work", "escalation", "finish")
REVIEW_KINDS = ("review", "build-review")
_EVENT_COST = re.compile(r"^(\S+)\tevents\t(intake|ask|card) project=\S+ .*?\bcost=([0-9][0-9.eE+-]*)(?:\s|$)")


def classifier(reg):
    """classify(folder, session name) -> role, from the registry's projects (compute's `classify`)."""
    from . import staff, units
    from .janitor import discover
    named, worker_dirs = {}, set()
    for key, proj in ((reg or {}).get("projects") or {}).items():
        named[proj.get("main_session") or f"main-{key}"] = MAIN
        named[staff.session_of(key)] = STAFF
        for name, div in (proj.get("divisions") or {}).items():
            div = div or {}
            named[div.get("session") or f"div-{key}-{name}"] = f"{name} pane"
            for entry in [div, *(div.get("features") or [])]:
                where = discover.real(units.worktree_path(entry.get("worktree") or ""))
                if where:
                    worker_dirs.add(discover.claude_slug(where))
                if entry is not div and entry.get("session"):
                    named[entry["session"]] = FEATURES

    def classify(folder, name):
        if name in named:
            return named[name]
        return WORKERS if name is None and folder in worker_dirs else OTHER
    return classify


def headless_spend(keys, days=30, now=None) -> dict:
    """{role: USD} of the headless calls without a transcript in the window, for registered projects `keys`:
    `review` and Codex `workers` from each project's run history, `intake` and `ask` (the Liaison's ask and
    card refresh) from the events log. Triage comes from triage_summary."""
    from . import paths
    from .runs import history
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    cutoff = now - _dt.timedelta(days=days)
    out = defaultdict(float)
    for key in keys:
        for row in history.read(key, since=cutoff):
            usd = row.get("cost_usd")
            if not isinstance(usd, (int, float)) or isinstance(usd, bool):
                continue
            if row.get("kind") in REVIEW_KINDS:
                out["review"] += usd
            elif row.get("kind") in WORK_KINDS and row.get("vendor") == "codex":
                out[WORKERS] += usd      # a Claude worker is in its transcript
    try:
        with open(paths.orche_home() / "logs" / "events.log", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _EVENT_COST.match(line)
                if not m:
                    continue
                try:
                    at = _dt.datetime.fromisoformat(m.group(1))
                    usd = float(m.group(3))
                except ValueError:
                    continue
                at = at if at.tzinfo else at.replace(tzinfo=_dt.timezone.utc)
                if cutoff <= at <= now:
                    out["intake" if m.group(2) == "intake" else "ask"] += usd
    except OSError:
        pass
    return {role: round(usd, 6) for role, usd in sorted(out.items())}


def role_rows(m) -> list:
    """[(role, USD, share)] of `m["roles"]` (transcripts) plus `m["headless"]` and the triage cost: Main,
    Staff, the division panes, feature panes, workers, review, intake, ask, triage, other; a role with no
    spend is left out. None when the transcripts or the registry cannot be read."""
    if m.get("roles") is None or m.get("headless") is None:
        return None
    total = defaultdict(float)
    for source in (m["roles"], m["headless"]):
        for role, usd in source.items():
            total[role] += usd
    tri = m.get("triage") or {}
    if tri.get("cost_usd"):
        total["triage"] += tri["cost_usd"]
    head = [MAIN, STAFF]
    tail = [FEATURES, WORKERS, *HEADLESS, OTHER]
    order = head + sorted(r for r in total if r not in head + tail) + tail
    whole = sum(total.values())
    return [(role, round(total[role], 6), total[role] / whole if whole else 0.0)
            for role in order if total.get(role)]


def spent_since(root, names, since, now=None, prices=None):
    """List-price cost (USD) of the assistant responses at or after `since` in the transcripts under
    `root`/<name> for each of `names` (Claude Code project dirs; a missing one adds nothing),
    deduplicated by message id; None when `root` or a transcript written since cannot be read."""
    root = Path(root).expanduser()
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    prices = prices or load_prices()
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        return None
    # a line is parsed only when it has a usage block and a UTC date the window touches
    days = {d.encode() for d in {since.astimezone(_dt.timezone.utc).date().isoformat(),
                                 now.astimezone(_dt.timezone.utc).date().isoformat()}}
    seen, total = set(), 0.0
    for name in sorted(set(names)):
        folder = root / name
        if not folder.is_dir():
            continue
        try:
            files = sorted(folder.rglob("*.jsonl"))
            fresh = [f for f in files if _dt.datetime.fromtimestamp(f.stat().st_mtime, _dt.timezone.utc) >= since]
        except OSError:
            return None
        for path in fresh:
            try:
                with open(path, "rb") as fh:
                    for line in fh:
                        if b'"usage"' not in line or not any(d in line for d in days):
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(obj, dict) or obj.get("type") != "assistant":
                            continue
                        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                        mid, model = message.get("id"), message.get("model") or "unknown"
                        try:
                            ts = _dt.datetime.fromisoformat(str(obj.get("timestamp")).replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        ts = ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)
                        if not mid or mid in seen or model in SKIP_MODELS or not since <= ts <= now:
                            continue
                        seen.add(mid)
                        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                        total += cost(usage, price_row(model, prices))
            except OSError:
                return None
    return round(total, 6)


def attempts(keys) -> list:
    """Every worker attempt in the registered projects' run history (runs/<p>/history.jsonl): a work,
    escalation or finishing line whose worker started (it has a cost figure; a run that never started has none)."""
    from .runs import history
    return [row for key in keys for row in history.read(key)
            if row.get("kind") in WORK_KINDS and isinstance(row.get("cost_usd"), (int, float))]


def runner_summary(records, days=30, now=None) -> dict:
    """Sum worker attempts (history lines, attempts()) whose `finished` is in the window: every attempt counts,
    a re-run, an escalation and a finishing call too (RUN.json keeps only a unit's last run).

    {attempts, cost_usd, median_cost_usd, failures, by_status, by_model_effort}; failures = status
    `failed`; by_model_effort maps "<model> <effort>" ('?' for a missing one) to {attempts, cost_usd,
    median_cost_usd, failures}.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    cutoff = now - _dt.timedelta(days=days)
    costs, statuses, groups = [], defaultdict(int), defaultdict(lambda: ([], [0]))
    for rec in records:
        try:
            at = _dt.datetime.fromisoformat(str(rec.get("finished")))
        except (ValueError, AttributeError):
            continue
        at = at if at.tzinfo else at.replace(tzinfo=_dt.timezone.utc)
        if not cutoff <= at <= now:
            continue
        status = str(rec.get("status"))
        statuses[status] += 1
        cost_usd = rec.get("cost_usd")
        cost = float(cost_usd) if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool) else 0.0
        costs.append(cost)
        group = groups[f"{rec.get('model') or '?'} {rec.get('effort') or '?'}"]
        group[0].append(cost)
        group[1][0] += status == "failed"
    return {**_cost_stats(costs), "failures": statuses.get("failed", 0),
            "by_status": dict(sorted(statuses.items())),
            "by_model_effort": {key: {**_cost_stats(c), "failures": f[0]}
                                for key, (c, f) in sorted(groups.items())}}


def _cost_stats(costs) -> dict:
    ordered = sorted(costs)
    mid = len(ordered) // 2
    median = (ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2) if ordered else None
    return {"attempts": len(costs), "cost_usd": round(sum(costs), 6),
            "median_cost_usd": round(median, 6) if ordered else None}


# ---- rework (K1, design c120 section 5) -----------------------------------------------------------
# A run is a history `work` line whose worker started. A re-run is any later run of the same unit; its cause is the
# newest revision's cause word (brief.causes), `untagged` without one, or `retry` when the revision is unchanged;
# its $ includes the escalation and finishing lines after it. Killed: the run ended `stopped`. Landed: the unit's
# PR merged (the queue log). Returns: AMENDs, queue returns and head-review returns a model made.
RETRY, UNTAGGED = "retry", "untagged"


def _at(text):
    from . import units
    return units.parse_iso(str(text or "").replace("Z", "+00:00"))


def _runs(rows) -> list:
    """The unit's runs from its worker lines (oldest first): [{rev, started, cost, stopped}]."""
    from .runs import history
    out = []
    for row in sorted(rows, key=lambda r: str(r.get("started") or "")):
        usd = float(row["cost_usd"])
        if row.get("kind") in history.COUNTED:
            out.append({"rev": row.get("rev") or 0, "started": _at(row.get("started")), "cost": usd,
                        "stopped": row.get("stop_reason") == "stopped"})
        elif out:
            out[-1]["cost"] += usd
    return out


def _amends(folder, inside) -> int:
    """The unit's revisions >= 2 written inside the window (`inside`: an aware datetime -> bool)."""
    n = 0
    for path in Path(folder).glob("BRIEF.rev*.md"):
        rev = path.name[len("BRIEF.rev"):-len(".md")]
        try:
            at = _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc)
        except OSError:
            continue
        n += rev.isdigit() and int(rev) >= 2 and inside(at)
    return n


def rework_summary(reg, days=30, now=None, named=None) -> dict:
    """The Rework block's numbers for the registered projects (design c120 section 5), from the run history, the
    unit folders' revisions, the queue log and the review ledger; `named` (compute's per session name spend, None:
    unreadable) gives each project's Main spend a day, at the model it runs."""
    from . import brief, ledger, staff
    from .queue import store
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    cutoff = now - _dt.timedelta(days=days)

    def inside(at):
        return at is not None and cutoff <= at <= now
    cause_keys = (*brief.CAUSES, RETRY, UNTAGGED)
    by_cause = {c: {"reruns": 0, "usd": 0.0} for c in cause_keys}
    n_units = n_runs = landed = landed_runs = returns = reruns = killed = 0
    worker = rerun_usd = killed_usd = 0.0
    q = {"merged": 0, "retested": 0, "retested_green": 0, "returned": 0, "returned_by": {}}
    hr = {"pass": 0, "routed": 0, "returned": 0, "pane_pass": 0, "pre_rounds": 0, "pre_returned": 0}
    main = {}
    projects = (reg or {}).get("projects") or {}
    for key in sorted(projects):
        log = store.read_log(key)
        merged_units = {ln.get("unit") for ln in log if ln.get("event") == "merged" and ln.get("unit")}
        grouped = defaultdict(list)
        for row in attempts([key]):
            grouped[row.get("unit")].append(row)
        for unit, rows in grouped.items():
            runs = _runs(rows)
            mine = [i for i, r in enumerate(runs) if inside(r["started"])]
            if not mine:
                continue
            b = brief.find(reg, unit, project=key) if unit else None
            tags = brief.causes(b.folder) if b is not None else {}
            n_units += 1
            n_runs += len(mine)
            returns += _amends(b.folder, inside) if b is not None else 0
            if unit in merged_units:
                landed, landed_runs = landed + 1, landed_runs + len(mine)
            for i in mine:
                r = runs[i]
                worker += r["cost"]
                if r["stopped"]:
                    killed, killed_usd = killed + 1, killed_usd + r["cost"]
                if i == 0:
                    continue
                prev = runs[i - 1]
                why = (tags.get(r["rev"]) or UNTAGGED) if r["rev"] > prev["rev"] else RETRY
                reruns, rerun_usd = reruns + 1, rerun_usd + r["cost"]
                by_cause[why]["reruns"] += 1
                by_cause[why]["usd"] += r["cost"]
        green = {ln.get("id") for ln in log if ln.get("event") == "merged"}
        for ln in log:
            if not inside(_at(ln.get("at"))):
                continue
            event = ln.get("event")
            if event == "merged":
                q["merged"] += 1
            elif event == "retested":
                q["retested"] += 1
                q["retested_green"] += ln.get("id") in green
            elif event == "returned":
                q["returned"] += 1
                why = str(ln.get("reason") or "?")
                q["returned_by"][why] = q["returned_by"].get(why, 0) + 1
        returns += sum(1 for ln in log if ln.get("event") == "returned" and inside(_at(ln.get("at"))))
        for row in ledger.read_reviews(key):
            if not inside(_at(row.get("ts"))):
                continue
            kind, verdict = row.get("kind"), row.get("verdict")
            if kind == "pre":
                hr["pre_rounds"] += 1
                hr["pre_returned"] += verdict == "return"
            elif kind == "build" and verdict == "pass":
                hr["pane_pass" if str(row.get("reviewer") or "").endswith("(pane)") else "pass"] += 1
            elif kind == "build" and verdict == "return":
                route = str(row.get("note") or "").startswith("route:")
                hr["routed" if route else "returned"] += 1
                returns += not route and row.get("model") != "facts"
        if named is not None:
            proj = projects[key] or {}
            usd = named.get(proj.get("main_session") or f"main-{key}", 0.0)
            main[key] = {"usd_per_day": round(usd / days, 2), "model": staff.main_model(proj)[0]}

    def share(x):
        return round(x / worker, 4) if worker else None
    not_owner = reruns - by_cause["owner"]["reruns"]
    not_owner_usd = rerun_usd - by_cause["owner"]["usd"]
    return {"days": days, "units": n_units, "runs": n_runs,
            "runs_per_unit": round(n_runs / n_units, 2) if n_units else None,
            "landed": landed, "runs_per_landed": round(landed_runs / landed, 2) if landed else None,
            "returns": returns, "returns_per_unit": round(returns / n_units, 2) if n_units else None,
            "reruns": reruns, "rerun_usd": round(rerun_usd, 2), "worker_usd": round(worker, 2),
            "rerun_share": share(rerun_usd), "reruns_not_owner": not_owner,
            "rerun_not_owner_usd": round(not_owner_usd, 2), "rerun_not_owner_share": share(not_owner_usd),
            "killed": killed, "killed_usd": round(killed_usd, 2),
            "by_cause": {c: {"reruns": v["reruns"], "usd": round(v["usd"], 2)} for c, v in by_cause.items()},
            "queue": q, "head_review": hr, "main": main if named is not None else None}


def rework_lines(w, days) -> list:
    """The Rework block (`orche metrics`), as design c120 section 5 prints it."""
    head = f"Rework (last {days} days):"
    if w is None:
        return [head, "  n/a (registry unreadable)"]

    def pct(x):
        return na(x, "{:.0%}")
    causes = " ".join(f"{c} {v['reruns']}·${v['usd']:.0f}" for c, v in w["by_cause"].items()
                      if v["reruns"] and c != UNTAGGED)
    causes = f"{causes} {UNTAGGED} {w['by_cause'][UNTAGGED]['reruns']}".strip()
    q, hr = w["queue"], w["head_review"]
    back = ", ".join(f"{k} {n}" for k, n in sorted(q["returned_by"].items()))
    lines = [head,
             f"  units {w['units']} · runs {w['runs']} · {na(w['runs_per_unit'])} runs/unit · landed {w['landed']} "
             f"({na(w['runs_per_landed'])} runs/landed unit) · {na(w['returns_per_unit'])} returns/unit",
             f"  re-runs {w['reruns']} · ${w['rerun_usd']:.0f} of ${w['worker_usd']:.0f} worker spend "
             f"({pct(w['rerun_share'])}) · without owner revisions {w['reruns_not_owner']} · "
             f"${w['rerun_not_owner_usd']:.0f} ({pct(w['rerun_not_owner_share'])})",
             f"  killed mid-way {w['killed']} runs · ${w['killed_usd']:.0f}",
             f"  AMENDs by cause (re-runs · $): {causes}",
             f"  queue: merged {q['merged']} · re-tested {q['retested']} (green {q['retested_green']}) · "
             f"returned {q['returned']}" + (f" ({back})" if back else ""),
             f"  head review: pass {hr['pass']} · routed {hr['routed']} · returned {hr['returned']} · "
             f"pane pass {hr['pane_pass']} · pre-review {hr['pre_rounds']} rounds, {hr['pre_returned']} returned"]
    if w["main"] is None:
        lines.append("  Main spend: n/a (transcripts cannot be read)")
    elif w["main"]:
        lines.append("  Main spend: " + " · ".join(f"{k} ${v['usd_per_day']:.2f}/day ({v['model']})"
                                                   for k, v in w["main"].items()))
    return lines


def triage_summary(path, days=30, now=None) -> dict:
    """Triage decisions (ORCHE_HOME/triage/samples.jsonl rows whose `at` is in the window) and their
    classifier cost (`cost_usd`).

    main counts `main` and `tag` ([main]) rows; classifier_calls are rows with a backend; every
    direct row is one Main turn saved (approximately: Main would have spent a dispatch turn).
    None when the samples file cannot be read (triage never ran, or no access)."""
    if not os.access(path, os.R_OK):
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    cutoff = now - _dt.timedelta(days=days)
    counts, ms, model_ms, usd = defaultdict(int), [], [], 0.0
    from . import transcript
    for row in transcript.records(Path(path)):
        try:
            at = _dt.datetime.fromisoformat(str(row.get("at")))
        except ValueError:
            continue
        at = at if at.tzinfo else at.replace(tzinfo=_dt.timezone.utc)
        if not cutoff <= at <= now:
            continue
        decision = str(row.get("decision"))
        counts["main" if decision == "tag" else decision] += 1
        if isinstance(row.get("ms"), int):
            ms.append(row["ms"])
        if row.get("backend") not in (None, "none") and isinstance(row.get("model_ms"), int):
            model_ms.append(row["model_ms"])
        if isinstance(row.get("cost_usd"), (int, float)) and not isinstance(row.get("cost_usd"), bool):
            usd += row["cost_usd"]
    seen = sum(counts.values())
    return {"prompts": seen, "direct": counts["direct"], "main": counts["main"], "bypass": counts["bypass"],
            "mean_ms": round(sum(ms) / len(ms)) if ms else None, "classifier_calls": len(model_ms),
            "mean_classifier_ms": round(sum(model_ms) / len(model_ms)) if model_ms else None,
            "main_turns_saved": counts["direct"], "cost_usd": round(usd, 6)}


def render(m: dict) -> list:
    lost = f", {m['unreadable']} unreadable" if m.get("unreadable") else ""
    if not m.get("readable", True):
        lines = [f"window: last {m['days']} days, transcripts n/a ({m['root']} cannot be read)"]
    else:
        lines = [f"window: last {m['days']} days, {m['files']['main']} main + "
                 f"{m['files']['subagent']} subagent transcripts{lost}"]
    lines.append(f"responses: {na(m['responses'])} (avg context {na(m['avg_context'], '{} tokens')})")
    rows = [("MODEL", "RESPONSES", "PER_DAY", "AVG_CONTEXT", "COST_USD")]
    for model, r in m["models"].items():
        rows.append((model, str(r["responses"]), f"{r['per_day']:.2f}", str(r["avg_context"]),
                     f"{r['cost_usd']:.2f}"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in rows]
    rt = m["routine"]
    lines.append(f"estimated cost: {na(m['cost_usd'], '${:.2f}')} (list price)")
    if m.get("unpriced"):
        lines.append(f"unpriced models (counted at {FALLBACK_MODEL}'s price): {', '.join(m['unpriced'])}")
    lines += [
              f"main wake-ups: user {m['wake']['user']}, notification {m['wake']['notification']}",
              f"routine messages: {rt['routine']}/{rt['messages']} ({na(rt['share'], '{:.0%}')})",
              f"AskUserQuestion: {na(m['ask_user_question'])}"]
    if "triage" in m:
        tri = m["triage"]
        lines.append("Triage (Main prompts, triage/samples.jsonl):")
        lines.append("  n/a (no samples file)" if tri is None else
                     f"  prompts {tri['prompts']}, direct {tri['direct']}, main {tri['main']}, bypass {tri['bypass']}, "
                     f"mean latency {na(tri['mean_ms'], '{} ms')} ({tri['classifier_calls']} classifier calls, mean "
                     f"{na(tri['mean_classifier_ms'], '{} ms')}), Main turns saved ≈ {tri['main_turns_saved']}")
    if "runner" in m:
        run = m["runner"]
        lines.append("Runner (orche run, history.jsonl):")
        if run is None:
            lines.append("  n/a (registry unreadable)")
        else:
            lines.append("  every attempt counts: re-runs, escalations and finishing calls (no longer a unit's last run)")
            lines.append(f"  attempts {run['attempts']}, cost ${run['cost_usd']:.2f}, "
                         f"median {na(run['median_cost_usd'], '${:.2f}/attempt')}, failures {run['failures']}")
            lines += [f"  {key}: attempts {g['attempts']}, cost ${g['cost_usd']:.2f}, "
                      f"median {na(g['median_cost_usd'], '${:.2f}/attempt')}, failures {g['failures']}"
                      for key, g in run.get("by_model_effort", {}).items()]
    if "rework" in m:
        lines += rework_lines(m["rework"], m["days"])
    if "roles" in m:
        rows = role_rows(m)
        if rows is None:
            lines += ["Spend by role:", "  n/a (" + ("registry unreadable" if m.get("headless") is None
                                                   else "transcripts cannot be read") + ")"]
        else:
            lines.append(f"Spend by role (list price, ${sum(r[1] for r in rows):.2f}):")
            table = [("ROLE", "COST_USD", "SHARE")] + [(r, f"{usd:.2f}", f"{share:.0%}") for r, usd, share in rows]
            widths = [max(len(row[i]) for row in table) for i in range(3)]
            lines += ["  " + "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in table]
    return lines
