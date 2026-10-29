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


# USD list price per 1M tokens: input, output, cache read, cache write.
PRICES = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
}
FALLBACK_MODEL = "claude-opus-5"
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
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

    File format: `{"<model>": {"input": n, "output": n, "cache_read": n, "cache_write": n}}`,
    USD per 1M tokens, every row with exactly those four numeric keys. Raises ValueError
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


def price_row(model: str, prices: dict) -> dict:
    """Exact model, else the longest known prefix (dated ids), else the fallback row."""
    if model in prices:
        return prices[model]
    prefixes = [m for m in prices if model.startswith(m)]
    return prices[max(prefixes, key=len)] if prefixes else prices[FALLBACK_MODEL]


def cost(usage: dict, row: dict) -> float:
    return (usage.get("input_tokens", 0) * row["input"]
            + usage.get("output_tokens", 0) * row["output"]
            + usage.get("cache_read_input_tokens", 0) * row["cache_read"]
            + usage.get("cache_creation_input_tokens", 0) * row["cache_write"]) / 1_000_000


def is_subagent(path: Path, root: Path) -> bool:
    return "subagents" in path.relative_to(root).parts


def compute(root=None, days=30, now=None, prices=None) -> dict:
    root = Path(root or default_root()).expanduser()
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    prices = prices or load_prices()
    cutoff = now - _dt.timedelta(days=days)
    seen_msgs, seen_users, ask_ids = set(), set(), set()
    usage_by_model = defaultdict(lambda: dict.fromkeys(USAGE_KEYS, 0))
    responses = defaultdict(int)
    context = defaultdict(int)
    by_day = defaultdict(lambda: defaultdict(int))
    wake = {"user": 0, "notification": 0}
    routine = 0
    files = {"main": 0, "subagent": 0}
    unreadable = 0

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
        for obj in transcript.records(path):
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
            "cost_usd": round(cost(usage_by_model[model], price_row(model, prices)), 6),
        }
    total = sum(responses.values())
    readable = paths is not None
    whole = readable and not unreadable       # a total over a partial read is not a real number
    return {
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
    }


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
                        total += cost({k: v for k, v in usage.items() if isinstance(v, int)},
                                      price_row(model, prices))
            except OSError:
                return None
    return round(total, 6)


def runner_summary(records, days=30, now=None) -> dict:
    """Sum `orche run` records (RUN.json dicts, runs.records) whose `finished` is in the window.

    {units, cost_usd, median_cost_usd, failures, by_status, by_model_effort}; failures = status
    `failed`; by_model_effort maps "<model> <effort>" ('?' for a missing one) to {units, cost_usd,
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
    return {"units": len(costs), "cost_usd": round(sum(costs), 6),
            "median_cost_usd": round(median, 6) if ordered else None}


def triage_summary(path, days=30, now=None) -> dict:
    """Triage decisions (ORCHE_HOME/triage/samples.jsonl rows whose `at` is in the window).

    main counts `main` and `tag` ([main]) rows; classifier_calls are rows with a backend; every
    direct row is one Main turn saved (approximately: Main would have spent a dispatch turn).
    None when the samples file cannot be read (triage never ran, or no access)."""
    if not os.access(path, os.R_OK):
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc).astimezone()
    cutoff = now - _dt.timedelta(days=days)
    counts, ms, model_ms = defaultdict(int), [], []
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
    seen = sum(counts.values())
    return {"prompts": seen, "direct": counts["direct"], "main": counts["main"], "bypass": counts["bypass"],
            "mean_ms": round(sum(ms) / len(ms)) if ms else None, "classifier_calls": len(model_ms),
            "mean_classifier_ms": round(sum(model_ms) / len(model_ms)) if model_ms else None,
            "main_turns_saved": counts["direct"]}


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
    lines += [f"estimated cost: {na(m['cost_usd'], '${:.2f}')} (list price)",
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
        lines.append("Runner (orche run, RUN.json):")
        if run is None:
            lines.append("  n/a (registry unreadable)")
        else:
            lines.append(f"  units {run['units']}, cost ${run['cost_usd']:.2f}, "
                         f"median {na(run['median_cost_usd'], '${:.2f}/unit')}, failures {run['failures']}")
            lines += [f"  {key}: units {g['units']}, cost ${g['cost_usd']:.2f}, "
                      f"median {na(g['median_cost_usd'], '${:.2f}/unit')}, failures {g['failures']}"
                      for key, g in run.get("by_model_effort", {}).items()]
    return lines
