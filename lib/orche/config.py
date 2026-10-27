"""Instance config at ORCHE_HOME/config.json, merged over defaults."""
import copy

from . import log, paths, util

DEFAULTS = {
    "orca_bin": "orca",
    "claude_bin": "claude",
    # macOS notifications (lib/orche/notify.py): Main's OWNER:/DONE: closing lines, a parked guard
    # refusal, HR, the Keymaster and the heartbeat's escalations. false: none.
    "notify": True,
    # How the Stop hook delivers a brief to a division/feature: `runner` spawns `orche run <unit>`
    # (headless `claude -p`); `pane` starts `orche deliver <unit>`, which types into the unit's Orca pane.
    "delivery": "runner",
    # queue_wait_s: how long a run waits for a project slot or its target's lock, then "runner busy".
    # log_keep_days: logs/runs/*.log older than this go at `orche run` start and `orche janitor run`.
    # turns_*: a run's --max-turns = clamp(ceil(TIMEBOX minutes × turns_per_minute), turns_floor, turns_ceiling).
    # max_parallel: runs at once per project; max_parallel_total: across projects; min_free_mb: a run
    # waits while the machine has less free memory. usage_limit: a run's output that names a vendor's
    # usage window (patterns), when it resets (reset_patterns) and the backoff minutes otherwise.
    "runner": {"turns_per_minute": 3, "turns_floor": 30, "turns_ceiling": 400, "max_parallel": 4,
               "queue_wait_s": 1800, "log_keep_days": 14, "max_parallel_total": 8, "min_free_mb": 2048,
               "usage_limit": {
                   "patterns": ["usage limit", "rate[ _-]?limit", "\\b429\\b[^\\n]*\\brate",
                                "\\brate[^\\n]*\\b429\\b", "\\bapi error:?\\s*429\\b", "too many requests",
                                "(?:\\d+-hour|daily|weekly) limit reached", "hit your (usage )?limit",
                                "usage quota", "quota exceeded for", "exceeded your (?:current )?quota",
                                "overloaded"],
                   "reset_patterns": [
                       "\\|(?P<epoch>\\d{10})\\b",
                       "resets?\\s+(?:at\\s+)?(?P<iso>\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}(?::\\d{2})?"
                       "(?:Z|[+-]\\d{2}:?\\d{2})?)",
                       "resets?\\s+(?:at\\s+)?(?P<hour>\\d{1,2})(?::(?P<minute>\\d{2}))?\\s*(?P<ampm>am|pm)?",
                       "(?:try again|resets?) in\\s+(?:(?P<days>\\d+)\\s*days?)?\\s*(?:(?P<hours>\\d+)\\s*hours?)?"
                       "\\s*(?:(?P<minutes>\\d+)\\s*min)?",
                       "retry[- ]after[:= ]\\s*(?P<seconds>\\d+)"],
                   "backoff_min": [5, 15, 60]}},
    # A feature agent: false = headless (no Orca pane, runs only through `orche run`); `orche feat wake`
    # opens a pane. feature_min_free_disk_mb: `hr feature` refuses below this much free disk.
    "feature_panes": False,
    "feature_min_free_disk_mb": 2048,
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
    # The guards' caps: revisions (G1) and briefs_per_hour (G3) inside W, runs_per_unit (G5), a lineage's
    # depth and fanout (G6).
    "flow": {"enabled": True, "quiet_hours": "23:00-08:00", "max_unit_cost_usd": 10, "max_project_cost_usd": 30,
             "revisions": 3, "briefs_per_hour": 20, "runs_per_unit": 4, "depth": 3, "fanout": 5},
    # publish: base = the PR base branch the queue lands on (null: origin/HEAD; neither: an OWNER step);
    # branch_prefix = the first path segment of a feature worker's branch (null: `feat`); issues = the issue
    # tracker's team key, e.g. `DES` (a worker is then named `des-<N>-<topic>`); per_project overrides land by
    # project name, like the queue's (`orche init --base/--branch-prefix/--issues` writes them, and init reads
    # the unset ones from the repository: lib/orche/detect.py).
    "publish": {"base": None, "branch_prefix": None, "issues": None, "per_project": {}},
    # The merge queue (lib/orche/queue): per_project overrides land by project name. lost_after_s:
    # a testing entry whose integrator's pid still lives while the lock is free counts as lost after this
    # (a dead pid at once).
    "queue": {"land": "auto", "timeout_s": 1200, "max_restarts": 2, "lost_after_s": 3600, "per_project": {}},
    # The review table: a cross-vendor reviewer from `order`, at most `rounds`; cross_vendor off (the
    # default), optional or required (the Codex cross-check runs only when asked). panel_min_lines: a change
    # with more added and removed lines, or a high-risk path, is pre-reviewed by a two-vendor panel (whatever
    # cross_vendor says) and needs its blast-radius fact proven by a test (lib/orche/review/__init__.py: deep).
    "review": {"cross_vendor": "off", "order": ["codex", "claude-opus-5-5", "claude-sonnet-5-5"],
               "rounds": 2, "build": True, "pane_lines": 400, "panel_min_lines": 400},
    # The tier ladder: a run's model and effort by TIER and vendor ("division": the unit's own model).
    "ladder": {"escalate": True,
               "claude": {"mechanical": {"model": "claude-haiku-5-5", "effort": "low"},
                          "standard": {"model": "division", "effort": "medium"},
                          "hard": {"model": "division", "effort": "high"}},
               "codex": {"mechanical": {"model": "division", "effort": "low"},
                         "standard": {"model": "division", "effort": "medium"},
                         "hard": {"model": "division", "effort": "high"}}},
    # Scale presets (lib/orche/scale.py, `orche hr scale`): the caps in scale.KEYS (dotted config keys) and
    # scale.LIMITS (registry limits); a preset lists only the values that differ from these defaults and
    # registry.LIMIT_DEFAULTS.
    "scale": {"presets": {
        "default": {},
        "20": {"estimate_usd_per_hour": 40,
               "config": {"runner.max_parallel": 12, "runner.max_parallel_total": 16, "runner.queue_wait_s": 7200,
                          "feature_min_free_disk_mb": 8192, "flow.fanout": 20, "flow.briefs_per_hour": 60,
                          "flow.max_project_cost_usd": 150},
               "limits": {"per_project_per_day": 20, "per_project_per_week": 60, "per_request_auto": 20,
                          "global_per_day_freeze": 40}},
        "30": {"estimate_usd_per_hour": 60,
               "config": {"runner.max_parallel": 20, "runner.max_parallel_total": 24, "runner.min_free_mb": 3072,
                          "runner.queue_wait_s": 7200, "feature_min_free_disk_mb": 10240, "flow.fanout": 30,
                          "flow.briefs_per_hour": 90, "flow.max_project_cost_usd": 250},
               "limits": {"per_project_per_day": 30, "per_project_per_week": 90, "per_request_auto": 30,
                          "global_per_day_freeze": 60}}}},
    # Division kinds (lib/orche/vendors.py, lib/orche/hr/choices.py): by kind (fallback `custom`),
    # the model `orche init` / `orche hr add` give a division, its pane effort (a headless run's
    # effort is its TIER's), the owner-facing label and description and the reason for the model
    # (English; Main translates for the owner), and a charter for a kind that is not standard (`hr add <p> design`).
    # Override one entry here.
    "kinds": {
        "rnd":      {"model": "claude-opus-5-5",   "effort": "high",   "charter": None,
                     "label": "R&D",
                     "desc": "prototypes and technical spikes",
                     "why": "prototypes and spikes: the strongest reasoning"},
        "research": {"model": "claude-sonnet-5-5", "effort": "medium", "charter": None,
                     "label": "Research",
                     "desc": "market, users, competitors",
                     "why": "reading and sourced summaries at half Opus's price"},
        "build":    {"model": "claude-opus-5-5",   "effort": "medium", "charter": None,
                     "label": "Build",
                     "desc": "integrates features, owns the ledger, opens PRs",
                     "why": "integration, tests and the ledger: correctness first (codex for a second vendor on code)"},
        "gtm":      {"model": "claude-sonnet-5-5", "effort": "medium", "charter": None,
                     "label": "GTM",
                     "desc": "launch, content, growth",
                     "why": "launch copy and content: writing quality at Sonnet's price"},
        "ops":      {"model": "claude-sonnet-5-5", "effort": "medium", "charter": None,
                     "label": "Ops",
                     "desc": "deploy and operate (dormant until stage public)",
                     "why": "runbooks and checklists; risky steps are owner-gated anyway"},
        "design":   {"model": "claude-opus-5-5",   "effort": "high",
                     "label": "Design",
                     "desc": "UI, UX and visual design; design reviews",
                     "why": "design calls: the strongest reasoning",
                     "charter": "Design owns the product's user interface and experience: flows, layouts, visual style "
                                "and design reviews of what Build ships. It drafts options with their reasons, prototypes "
                                "one when that settles a question, and hands Build concrete specs."},
        "content":  {"model": "claude-sonnet-5-5", "effort": "medium",
                     "label": "Content",
                     "desc": "blog posts, docs and social copy",
                     "why": "writing at Sonnet's price",
                     "charter": "Content writes the product's words: blog posts, docs, release notes and social copy in "
                                "the product's voice, from facts the other divisions provide. Nothing is published without "
                                "the owner's approval."},
        "custom":   {"model": "claude-opus-5-5",   "effort": "high",
                     "why": "an unknown charter: the strongest model"},
    },
    # Models (lib/orche/vendors.py). catalog: the models `orche hr choices` offers, with their label,
    # cost (1 ●○○ .. 3 ●●●) and description; a Claude id listed here passes the model
    # grammar as well. codex_bin: the Codex CLI; codex_sandbox: the sandbox of a Codex run; prices:
    # USD per million tokens [input, cached input, output] by Codex model id (`codex`: Codex's default
    # model), for the spend of a Codex run.
    "models": {
        "catalog": {
            "claude-opus-5-5":   {"label": "Opus 5.5", "cost": 3,
                                  "desc": "the strongest reasoning"},
            "claude-sonnet-5-5": {"label": "Sonnet 5.5", "cost": 2,
                                  "desc": "fast and capable at half Opus's price"},
            "codex":             {"label": "Codex", "cost": 3,
                                  "desc": "OpenAI Codex's default model: a second vendor, headless only"},
        },
        "codex_bin": "codex",
        "codex_sandbox": "workspace-write",
        "prices": {"gpt-6.1-sol": [2.0, 0.1, 10.0], "gpt-6-astra": [10.0, 1.0, 50.0], "codex": [10.0, 1.0, 50.0]},
    },
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


def for_project(cfg, section, project) -> dict:
    """The merged `cfg[section]` with `cfg[section]["per_project"][project]` laid over it (missing pieces: empty)."""
    sec = cfg.get(section) or {}
    return {**sec, **((sec.get("per_project") or {}).get(project) or {})}


def set_project(section, project, **values) -> None:
    """Write `section.per_project.<project>.<key>` in ORCHE_HOME/config.json for each value (None deletes
    the key); only the file's own keys are saved, never the defaults."""
    data = util.read_json(path(), default={})
    data = data if isinstance(data, dict) else {}
    mine = data.setdefault(section, {}).setdefault("per_project", {}).setdefault(project, {})
    for key, value in values.items():
        if value is None:
            mine.pop(key, None)
        else:
            mine[key] = value
    save(data)
