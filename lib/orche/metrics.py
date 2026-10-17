"""Token-economy metrics from Claude Code transcripts (`~/.claude/projects/**/*.jsonl`). No model calls.

Assistant responses are deduplicated by `message.id` (Claude Code writes one line per content
block, and resumed sessions repeat history). Files under a `subagents/` directory are subagent
transcripts; every other file is a main session.
"""
import datetime as _dt
import json
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
ROUTINE_RE = re.compile(
    r"진행\s*상황|상황\s*알려|멈춘|이어서|계속\s*진행|컨펌|승인|진행해"
    r"|\b(?:status|progress|continue|go\s+ahead|approve)\b",
    re.IGNORECASE)
ROUTINE_MAX_CHARS = 80
SKIP_MODELS = {"<synthetic>"}


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


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


def _timestamp(obj):
    try:
        ts = _dt.datetime.fromisoformat(str(obj.get("timestamp")))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def _user_text(message):
    """Prompt text of a user line, or None for tool results / empty content."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return text.strip() or None


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


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

    paths = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    for path in paths:
        try:
            if _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        sub = is_subagent(path, root)
        files["subagent" if sub else "main"] += 1
        for obj in _lines(path):
            ts = _timestamp(obj)
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
                text = _user_text(message)
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
    return {
        "root": str(root),
        "days": days,
        "files": files,
        "responses": total,
        "avg_context": round(sum(context.values()) / total) if total else 0,
        "cost_usd": round(sum(m["cost_usd"] for m in models.values()), 6),
        "models": models,
        "by_day": {day: dict(sorted(v.items())) for day, v in sorted(by_day.items())},
        "wake": wake,
        "routine": {"messages": wake["user"], "routine": routine,
                    "share": round(routine / wake["user"], 4) if wake["user"] else 0.0},
        "ask_user_question": len(ask_ids),
    }


def render(m: dict) -> list:
    lines = [f"window: last {m['days']} days, {m['files']['main']} main + "
             f"{m['files']['subagent']} subagent transcripts",
             f"responses: {m['responses']} (avg context {m['avg_context']} tokens)"]
    rows = [("MODEL", "RESPONSES", "PER_DAY", "AVG_CONTEXT", "COST_USD")]
    for model, r in m["models"].items():
        rows.append((model, str(r["responses"]), f"{r['per_day']:.2f}", str(r["avg_context"]),
                     f"{r['cost_usd']:.2f}"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in rows]
    rt = m["routine"]
    lines += [f"estimated cost: ${m['cost_usd']:.2f} (list price)",
              f"main wake-ups: user {m['wake']['user']}, notification {m['wake']['notification']}",
              f"routine messages: {rt['routine']}/{rt['messages']} ({rt['share']:.0%})",
              f"AskUserQuestion: {m['ask_user_question']}"]
    return lines
