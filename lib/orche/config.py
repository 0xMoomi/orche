"""Instance config at ORCHE_HOME/config.json, merged over defaults."""
import copy

from . import log, paths, util

DEFAULTS = {
    "orca_bin": "orca",
    "claude_bin": "claude",
    # macOS notifications (lib/orche/notify.py): Main's OWNER:/DONE: closing lines, a parked guard
    # refusal, HR, the Keymaster and the heartbeat's escalations. false: none.
    "notify": True,
    "lang": "en",
    # How the Stop hook delivers a brief to a division/feature: `runner` spawns `orche run <unit>`
    # (headless `claude -p`); `pane` starts `orche deliver <unit>`, which types into the unit's Orca pane.
    "delivery": "runner",
    # queue_wait_s: how long a run waits for a project slot or its target's lock, then "runner busy".
    # log_keep_days: logs/runs/*.log older than this go at `orche run` start and `orche janitor run`.
    # turns_*: a run's --max-turns = clamp(ceil(TIMEBOX minutes × turns_per_minute), turns_floor, turns_ceiling).
    "runner": {"turns_per_minute": 3, "turns_floor": 30, "turns_ceiling": 400, "max_parallel": 4,
               "queue_wait_s": 1800, "log_keep_days": 14},
    "jev": {"enabled": False, "base_url": "", "model": "jev-1.13.0", "timeout_s": 1.0,
            "api_key_env": "JEV_API_KEY"},
    # Triage of a Main pane's prompts (lib/orche/triage.py): backend auto = Jev when enabled and
    # configured, else claude-haiku (`claude -p`); max_latency_s bounds the Haiku call (Jev keeps
    # jev.timeout_s); keep_prompts stores the masked prompt in triage/samples.jsonl.
    "triage": {"enabled": False, "backend": "auto", "min_confidence": 0.8, "max_latency_s": 12,
               "keep_prompts": False},
    # Project intake (lib/orche/intake.py): a read-only `claude -p` sweep of the main checkout into
    # .orche/div/<division>/CONTEXT.md (max_chars each) and PROJECT_CARD.md. enabled: `orche init`
    # runs it at the end and `orche janitor run` refreshes a stamped project once HEAD moved
    # refresh_commits commits or the stamp is refresh_days old (`orche intake` itself always runs).
    # timeout_s 540: a sweep ends inside the Bash tool's 10-minute maximum (/orche-apply).
    "intake": {"enabled": True, "model": "claude-sonnet-5-5", "max_turns": 30, "timeout_s": 540,
               "max_chars": 2000, "refresh_commits": 50, "refresh_days": 14},
    # The Liaison (lib/orche/liaison.py): one read-only `claude -p` per `orche ask` and
    # `orche card refresh`; timeout_s 100 ends an ask inside the Bash tool's 2-minute default.
    "liaison": {"model": "claude-sonnet-5-5", "effort": "low", "max_turns": 12, "timeout_s": 100},
    # The wake (R-INBOX-4): posting a waking pointer starts `orche nudge <recipient>`, which types
    # `[orche] inbox: new reports` into the recipient's idle pane. false: reports wait for a prompt.
    # quiet_s: the nudger waits until the newest waking pointer is this old (one wake per batch),
    # at most quiet_max_s more; an urgent pointer (inbox.URGENT) skips the wait.
    "inbox": {"wake": True, "quiet_s": 90, "quiet_max_s": 240},
    # The heartbeat (lib/orche/reconcile.py): `orche reconcile` runs one bounded pass every every_s
    # (the LaunchAgent `orche install` writes, or the hook kick when the heartbeat is stale) that
    # re-nudges unread sessions and re-delivers undelivered briefs (tries and backoff are constants in
    # reconcile.py), then one escalation. false: the pass only writes the heartbeat.
    "reconcile": {"enabled": True, "every_s": 300},
    # Flow guarantees (C10): the autonomy window's quiet hours and the headless spend caps.
    "flow": {"enabled": True, "quiet_hours": "23:00-08:00", "max_unit_cost_usd": 10, "max_project_cost_usd": 30},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def path():
    return paths.orche_home() / "config.json"


def load() -> dict:
    try:
        data = util.read_json(path(), default={})
    except util.JsonError as exc:
        log.log("config", f"using defaults: {exc}")
        data = {}
    return _merge(DEFAULTS, data if isinstance(data, dict) else {})


def save(cfg: dict) -> None:
    util.write_json(path(), cfg)
