# ORCHE Specification

Status: canonical. Current as of the release at the top of [CHANGELOG.md](../CHANGELOG.md).
Audience: AI agents and engineers implementing or operating ORCHE.
Scope: an orchestration layer for Claude Code sessions managed by the Orca desktop multi-agent manager.

Conventions in this document:
- MUST / MUST NOT / SHOULD / MAY are normative.
- `<project>` = registry key of a project. `<app>` = a deployable app inside a project. `<org>` = a GitHub organization.
- `~/.claude/orche/` = the ORCHE instance root on the operator's machine.
- Rule IDs (e.g. `R-DISPATCH-6`) are stable; tests and hooks SHOULD reference them.
- Numbers marked "measured in one deployment (Oct 2026)" are motivating evidence from a single operator's 30-day transcript analysis. They are not guarantees.

Attribution: Several conventions are adapted from Cursor's pstack plugin (MIT, by Lauren Tan): the verification ledger, the 8-field brief, standing orders, completions-as-queue-events with drain discipline, the finish predicate with escape hatch, the decision TSV with cross-context attention review, prototype-instead-of-ask, the always-pause list, ketchup-style reports, `/correct`, the verification skill pair, `watch-pr`, worktree-cleanup gates, and pause/pickup.

---

## 0. Purpose

### 0.1 Two goals

ORCHE has two goals. Neither may be traded for the other.

| # | Goal | Operational definition |
|---|---|---|
| G1 | Good code | A unit is complete only when a verification-ledger row exists for its PR + head SHA with an accepted verdict. |
| G2 | Token economy | Reduce (a) the number of times expensive models are invoked and (b) the context size per invocation, by structure rather than by asking models to be brief. |

### 0.2 Summary

ORCHE runs one orchestrator session ("Main", the most capable model) per project. Main understands requests, decomposes them, and hands out structured dispatch blocks with `orche dispatch`; it does not write code and does not read worker output in full. Hooks — not model turns — relay dispatches to the division sessions (R&D, Research, Build, GTM, Ops) — the heads — and to the workers each head staffs, each in its own Orca worktree. Workers report by writing a small inbox pointer; Main drains pointers in batches at four defined points. Completion is defined by a verification ledger keyed by PR and SHA. Memory lives in files (persona, agent memory, STATUS/DECISIONS/HANDOFF, preserved transcripts), so sessions are cleared at unit boundaries instead of growing. Visibility (status line, Orca sidebar, `orche` CLI, macOS notifications) costs zero tokens. A model-free secrets broker, a state-based janitor, and Git/CI rules complete the system. An optional fast typed-decision model (Jev) gates routine traffic.

### 0.3 What changes

| Area | Before ORCHE | With ORCHE |
|---|---|---|
| Command | Orchestrator both decides and reads every subagent result | One Main per project. Main decides; hooks relay; divisions do long work |
| Organization | Many long-lived topic sessions, no roles | Main + 5 divisions (heads) + the workers each head staffs + global HR; a headless Liaison answers other projects |
| Definition of done | "Tests pass" | A ledger row (PR + SHA, verdict enum). CI green is not a verdict |
| Instructions | Re-typed in chat each time | 8-field brief + STANDING.md auto-prepended by hook |
| Reports | Every completion wakes Main | Completions are queue events, drained at 4 points; only urgent ones interrupt |
| Sessions | Grow until compacted; default 30-day deletion | `/clear` at unit end after memory-save; sessions preserved; archived only on retire/discard |
| Secrets | Hand-edited env files copied between environments | Broker with names only; operator pastes once into a dialog; developers get IAM access |
| Cleanup | None | State-based janitor; ledger line before any deletion |
| Status | Ask an agent (tokens) | Status line, Orca sidebar, `orche` CLI, notifications (0 tokens) |

### 0.4 Motivating measurements (measured in one deployment, Oct 2026)

| Measurement | Value |
|---|---|
| Share of token cost that was prompt-cache read + write | 93% (read 60%, write 33%; output 7%; fresh input ≈0%) |
| Average context per response | Orchestrator 518K · Opus workers 243K · Sonnet workers 169K |
| Share of orchestrator wake-ups caused by subagent completion notifications | ~70% |
| Routine user messages (confirm / status / "continue") | 13–15% of user messages |
| Orchestrator questions to the user | 2% of turns, mostly taste decisions |
| Same "continue" instruction re-typed across sessions | 29 times / 30 days |

Interpretation: cost ≈ context size × turn count. Turns are driven by notifications, not by the user. Context grows because the orchestrator accumulates subagent output.

### 0.5 Weekly targets

| Metric | Baseline (one deployment) | Target |
|---|---|---|
| Main turns/day | ≈108 | ≈50 |
| Main context per response | 518K | ≤200K |
| Worker context per response | 243K | ≤120K |
| Status-question latency | 32 s | <1 s |
| Ledger coverage of release-ready units | 0% | 100% |

---

## 1. Principles

| # | Principle | Checkable consequence |
|---|---|---|
| P1 | Memory in files, context short | Sessions `/clear` at unit boundaries; identity survives via persona + STATUS/DECISIONS/HANDOFF |
| P2 | Expensive model decides; hooks deliver | Relaying a dispatch or a report costs 0 model turns |
| P3 | Done means evidence | No ledger row ⇒ unverified. "Compiles" and "CI green" are not evidence |
| P4 | Documents over conversations | Cross-project and cross-division exchange uses cards, reports, ledger rows |
| P5 | Visibility is free | Status, alerts, lookups are files, status line, shell commands |
| P6 | An instruction written twice becomes structure | Add it to STANDING.md, a hook, a lint, or a test |

---

## 2. Topology

### 2.1 Diagram

```
User ──(talks to Main; may open any panel)──▶ Main (per project)
                                               │ orche dispatch <<'EOF' … EOF → relay
               ┌──────────┬───────────┬────────┼────────┬──────────┐
               ▼          ▼           ▼        ▼        ▼
              R&D      Research     Build     GTM      Ops (dormant while stage=building)
                                      │
                         ┌────────────┼────────────┐
                    feature agent  feature agent  feature agent …
                    (child worktrees of Build; created by HR)
HR (one global) ── registry.json ── create · sleep · retire · rate limits · weekly audit
Liaison (headless) ── orche ask <proj> · orche card refresh <proj> (read-only Sonnet calls in the main checkout)
```

### 2.2 Role table

| Role | Model / effort | Worktree | Responsibilities | Guards |
|---|---|---|---|---|
| Main | Fable 5.1 / xhigh | project main checkout | Understand, decompose, assign, confirm loop, approve integration, prepare release | MUST NOT edit code (rulebook; see note below). Long work goes to divisions. No deep review during drain. Reports with the status card |
| Liaison | Sonnet 5.5 / low | none: headless calls in the main checkout (§14.14) | Refresh PROJECT_CARD.md (`orche card refresh`); answer other projects' questions (`orche ask`) | Read-only tools; card first; no model call when nothing landed since the base commit; cannot assign work |
| R&D | Opus 5.5 / high (+ deep-reasoner at max) | `div/rnd` | Product and architecture planning, design, prototypes | MUST NOT edit product code (prototype folder only). gstack only for important decisions (§13) |
| Research | Sonnet 5.5 / medium | `div/research` | Market, technical, competitive research reports | Read-only. Conclusion ≤1 page. Escalate synthesis to Opus if heavy |
| Build | Opus 5.5 / high | `div/build` | Traffic control: splits briefs into slices, staffs a worker per slice, decides PASS or RETURN, owns the ledger; the merge queue lands PRs | Code that lands is written by workers; Build never merges by hand |
| Feature agent | Opus 5.5 / high (exec per TIER) | the branch Orca names by default; `<prefix>/<name>` when the project sets `publish.per_project.<p>.branch_prefix` (ORCHE renames it right after creation), Orca parent = its head (its division's session) | One slice's code within its scope; a worker of any division but Research, R&D and Ops | No edits outside own worktree (scope hook). `/clear` at unit end. 14 idle days ⇒ sleep |
| GTM | Opus 5.5 / high | `div/gtm` | Copy, landing pages, launch, marketing, customer-facing text | External publication (post, send) requires user approval |
| Ops | Opus 5.5 / high (mostly scripts) | `div/ops` | Monitoring, incident first response, release ladder, backup verification, patching, cost, report triage, runbooks | Dormant unless stage=`public`. Automatic rollback only via pre-approved runbook; everything else Always-pause |
| HR | Sonnet 5.5 / low (audit: Haiku 5.5) | ORCHE repo | registry, notifications, weekly audit, `/correct` | Rate limits and freeze; every change logged |
| exec / Explore / deep-reasoner / runner | Opus or Sonnet medium / Sonnet / Opus max / Haiku 5.5 | ephemeral | Hands | Brief ≤300 words, report ≤200 words, `maxTurns` set, auto-compact window 200K |

Rules:
- R-TOPO-1: All five divisions are created from a fixed template at project onboarding. Unused divisions are `dormant` (no panel, zero cost).
- R-TOPO-2: Feature agents (workers) exist under any division; the division's own session is their head. Research and R&D (and Ops) have none and use ephemeral subagents.
- R-TOPO-3: Exactly one writer per worktree. A file's ownership belongs to one division or feature agent.
- R-TOPO-4: Exactly one Main per project.
- R-TOPO-5: Model IDs are configuration, not code. Implementations MUST read them from registry/persona files.
- Implementation note (models): a division's model is the `model` string in its registry record, set by `orche init` (the recommendation, or the custom spec), `orche hr add` or `orche hr model`; the table's models are the `kinds` defaults. `orche depts`, `orche status`, briefs and `orche doctor` show it.
- Implementation note: the `scope-gate` hook acts only inside division and feature worktrees (edits outside the worktree or a feature's scope globs are denied; `.orche/` and `docs/` are always allowed). Main's "no code edits" rule is enforced by its persona and the `orche-main` rulebook, not by a hook.
- Implementation note (setup):
  - R-TOPO-1 is relaxed. `orche init` onboards the divisions the user chooses — by default every standard division except `ops` — plus any custom divisions the project defines; re-running adds only new ones and never removes one (sleep it, or remove it by hand).
  - A custom division (name a slug `^[a-z][a-z0-9]*(-[a-z0-9]+)*$` of 2-16 chars, not a standard name, not `main|liaison|feat|hr|jev`; label ≤ 24 chars, not equal (case-insensitive) to a standard division's name or label (`rnd research build gtm ops`, `R&D Research Build GTM Ops`); charter one paragraph ≤ 400 chars; no control or format characters (Unicode Cc/Cf) in the label, charter or model; model default `claude-opus-5-5`, effort default `high`) is a division like the others: session `div-<proj>-<name>`, worktree `div-<name>`, branch `div/<name>`, unit files in `.orche/div/<name>/`, dormant until woken, persona rendered from `templates/personas/div-custom.md` with the charter inserted.
  - `registry.divisions_of(proj)` — the project's standard divisions in catalogue order (rnd, research, build, gtm, ops), then its custom ones in the order they were added — is the one enumeration used by `orche status`, `orche depts`, the status line and the dispatch `TO` shorthand; HR audit, sleep/wake and the awake limit read the `divisions` records, which include custom ones.
  - Session names are unique registry-wide: `orche init` refuses (exit 1, naming the other project) any division, standard or custom, whose session `div-<proj>-<name>` another project has, or would have for one of its standard divisions or custom definitions (project `acme` cannot add `shop-build` next to project `acme-shop`, and `acme-shop` cannot onboard `build` after `acme` took it), and `orche feat new` refuses the same way a feature whose session `feat-<proj>-<name>` another project has (`hr.session_owner`, one helper for both).
  - A setup given wrongly through flags or `--setup-file` (unknown key, wrong type or value) is one `orche:` line and exit 1 (a user error, like argparse usage errors; exit 2 stays "not found"); `--print-setup` combined with `--divisions`, `--custom`, `--setup-file`, `--stage`, `--aliases` or `--wake` is refused the same way. A label refuses line breaks; a single line break in a charter is folded into a space and a blank line is refused.
  - On a terminal, a `--wake` division that is unticked in the checklist is named and the checklist shown again before the stage and `Apply?` prompts; a row whose session another project holds is shown unticked as `(unavailable: …)` from the start and cannot be ticked. A re-run that adds nothing leaves `registry.json` byte-identical.
- Implementation note (craft): each standard division, a Design or Content division and the worker carry a `## Craft` section after their Guards: the working practice of the role, written out in full. No persona names an installed skill, a skill pack or a slash command other than `/clear`. A standard division and the worker hold theirs in their own template (at most 60 lines, the worker 50); a custom division of a listed kind gets `templates/personas/craft/<kind>.md` (design, content) through the `{{CRAFT}}` line of `div-custom.md`, and any other custom division gets none (`hr/onboarding.craft_text`; `render_persona` folds a run of blank lines into one). Build's craft carries the owner's engineering rules, the questions before slicing, tracer-bullet slices and a lens for the diffs that reach its pane; the headless head review still judges only its eight checklist lines. `tests/test_agents_skills.py` checks each craft's placement, budget and anchors, and that no persona names a skill.

- Implementation note (per-division models): R-TOPO-5 holds as data. A division record carries one model string (`divisions.<d>.model`): a Claude model id (`claude-opus-5-5`; aliases such as `sonnet` too), `codex` (Codex's own default model) or `codex:<model id>`; a prefix other than `codex` is refused (`unknown vendor`), as are spaces and control or format characters (`vendors.errors`; `vendors.vendor_of` gives `claude` or `codex`). No effort is stored: a headless run's effort is its TIER's, a pane's is its persona's. A feature agent has no model of its own and runs on its division's (persona effort `high`). `orche init` gives a new division the recommended model of its name from `kinds.<name>` in config (`vendors.recommend`; a name not in the table takes the `custom` row), and a persona refresh (`hr.refresh_project`) keeps the recorded model while re-rendering the effort from the table. A Codex division's persona says `model: inherit`; waking one is refused (exit 3, "codex divisions run headless only (v1); briefs reach them through orche run"), and a feature under a Codex Build opens no pane and is recorded `paused`. The recommendation table (default):

| Division | Model | Pane effort | Why |
|---|---|---|---|
| rnd | claude-opus-5-5 | high | prototypes and spikes: the strongest reasoning |
| research | claude-sonnet-5-5 | medium | reading and sourced summaries at half Opus's price |
| build | claude-opus-5-5 | medium | integration, tests and the ledger: correctness first (codex for a second vendor on code) |
| gtm | claude-sonnet-5-5 | medium | launch copy and content: writing quality at Sonnet's price |
| ops | claude-sonnet-5-5 | medium | runbooks and checklists; risky steps are owner-gated anyway |
| design | claude-opus-5-5 | high | design calls: the strongest reasoning |
| content | claude-sonnet-5-5 | medium | writing at Sonnet's price |
| custom (any other name) | claude-opus-5-5 | high | an unknown charter: the strongest model |

### 2.3 Lifecycle stage

Each project has `stage ∈ {building, public}` in the registry.

| Rule | Statement |
|---|---|
| R-STAGE-1 | New projects start at `building`. |
| R-STAGE-2 | While `building`, the Ops division is `dormant` and MUST NOT be woken by automations or alerts. |
| R-STAGE-3 | Only Main may set `stage: public`, and only after explicit user approval recorded in DECISIONS.tsv. |
| R-STAGE-4 | On transition to `public`, HR wakes Ops, which installs `docs/runbooks/` and `scripts/ops/health.sh` from templates and registers monitoring automations. Implementation note: `orche stage set <proj> public --yes` (`hr.set_stage`, §14.12) records the stage, wakes Ops (unless `--no-wake`; the awake limit applies) and prints the Ops checklist (monitoring, release ladder, backup verification, runbooks); Ops creates the runbooks and scripts itself as its first units. |
| R-STAGE-5 | Reverting to `building` puts Ops back to sleep (`paused` via `orche sleep`, HANDOFF check applies; a refusal is reported and the stage still flips); runbooks and scripts stay. Implementation note: `orche stage set <proj> building` (`hr.set_stage`, §14.12). |

### 2.4 Ops division duties (stage = public)

| Need | Mechanism |
|---|---|
| Incident detection | Monitoring alert → Orca automation wakes Ops → read-only diagnosis → rollback only via pre-approved runbook → notify Main and user |
| Release ladder | dev → staging → canary → prod; each step requires a ledger verdict; `ROLLBACK` is mandatory in briefs that deploy; DB migrations are TIER `hard` and require a backup first |
| Backup | Weekly restore-test script; result written as a ledger row |
| Patching | Monthly patch unit with `security-review` + ledger row; Keymaster reminds 90-day key rotation |
| Cost | Cloud budget alerts + weekly model-usage report; on token runaway, HR lowers `max_awake_sessions` automatically |
| User reports | Triage (reproducible?, severity); create a Build unit only after 2 independent observations; GTM writes human-facing replies |
| Runbooks | `docs/runbooks/*.md`, readable by a non-developer at night |
| Observability | `scripts/ops/health.sh`; status line gets one service-health cell |

Limit: Ops executes known runbooks and rollbacks only. Novel incident types are escalated to the user.

### 2.5 Feature agent lifecycle (HR)

Creation trigger: the head's rule (skill orche-build, "Your team"; teams design §4.3): when a brief arrives, the head staffs one worker per slice (a landed worker whose scope covers it, else a new one); standing workers are `infra` under Build and, only after the same files caused two queue returns in a week, one more owner. The head calls HR (`orche feat new`); HR creates without asking and emits 4 notifications (status line, Orca card dot, macOS, one line to Main). Implementation note: of the four, the status line, the Orca unread dot and the macOS notification are built; the line to Main is not.

| Limit (registry `limits`) | Value | On breach |
|---|---|---|
| `per_project_per_day` | 8 | Require user confirmation |
| `per_project_per_week` | 40 | Require user confirmation |
| `per_request_auto` | 4 | 5th+ in one request requires confirmation |
| `global_per_day_freeze` | 20 | Set `frozen: true`; no creation until `orche hr unfreeze` |
| `idle_days_to_sleep` | 14 | Feature agent → `paused` (not deleted). Implementation note: `orche hr audit` proposes the sleep; `--apply` performs it |
| `max_awake_sessions` | 12 | Per machine; a wake past it is refused (exit 3) until a unit sleeps |

Creation outputs (all MUST exist after creation):
1. Orca child worktree, the branch Orca names by default; `<prefix>/<name>` when the project sets `publish.per_project.<p>.branch_prefix` (ORCHE renames it right after creation); `--base-branch origin/<base>` when the config names the project's base, and `--linear-issue KEY` / `--issue N` when the worker's name carries an issue key (§14.15); its branch starts on origin/<base> (reset when Orca forked elsewhere); no pane when `feature_panes` is false.
2. `.orche/feat/<name>/{CHARTER.md,STATUS.md,HANDOFF.md,DECISIONS.tsv}`.
3. `.claude/agents/feat-<project>-<name>.md` persona with `memory: project`.
4. Worktree `.claude/settings.local.json` with `agent` set to that persona.
5. Session name `feat-<project>-<name>`; Orca terminal renamed to match.
6. Registry entry with `status: active`, `created`, `last_activity`, `scope` globs.

Sleep: update STATUS + HANDOFF → close panel; keep worktree and session.

Implementation note: unit statuses are `active | dormant | paused | retired | onboarding` for divisions and `active | paused | retired | onboarding` for features. `dormant` = onboarded but never started; `paused` = slept (`orche sleep`, HANDOFF.md must be under 30 minutes old); `onboarding` = the worktree exists but onboarding has not finished (re-running `orche init` / `orche feat new` resumes it without a second worktree).

Wake: `claude --resume <session>`; read HANDOFF as authoritative input; MUST NOT re-verify prior work (pause/pickup).
Retire (user declaration via Main): preserve uncommitted work (`wip:` commit) → final HANDOFF → archive tag → remove worktree → compress session to archive → one GRAVEYARD line → registry `status: retired`.

Implementation note: `orche feat retire <proj> <name> --yes` refuses uncommitted work (any `git status` entry other than an untracked ORCHE-owned path: `.orche/**`, `.claude/agents/<session>.md`, `.claude/settings.local.json`, `.claude/agent-memory/**`), a HANDOFF.md without a `final:` line, or the main checkout. It copies the unit memory files to `ORCHE_HOME/archive/<session>/unit-files/<tag>` (other ORCHE-owned untracked files under `_worktree/`), tags `archive/feat-<name>-<yyyymmdd>`, deletes the ORCHE-owned untracked files from the worktree, runs `orca worktree rm`, archives the unit's transcripts under `ORCHE_HOME/archive/<session>/`, appends a GRAVEYARD row and sets `retired`. A re-run reuses kept files that are byte-identical to the unit dir and a tag that already points at the branch head.

---

## 3. Memory model

### 3.1 Four layers

| Layer | Content | Location | When read | Cost |
|---|---|---|---|---|
| L1 Identity | Role, scope, conventions, prohibitions | Persona file + CHARTER.md + STANDING.md | Every session start (cache prefix) | Fixed, small |
| L2 Agent memory | Lessons, pitfalls, preferences, recurring patterns for this area | Claude Code `memory: project` → `.claude/agent-memory/<name>/MEMORY.md` (first 200 lines auto-injected; topic files on demand) | Every session, automatic | Small |
| L3 Current state | Progress, rationale, in-flight work | STATUS.md · DECISIONS.tsv · HANDOFF.md | Unit start and resume | Small |
| L4 Raw record | Full past conversations | Preserved transcripts (§11) + Orca session search `orca search "<phrase>"` | Only when a "why did we…" question arises; read matched span only | On use |

`/clear` discards nothing in L1–L3, nor code or git history. It only stops re-reading L4 every turn.

### 3.2 Session lifecycle rules

| ID | Rule |
|---|---|
| R-MEM-1 | `/clear` happens only at a unit boundary. |
| R-MEM-2 | `/clear` happens only after the memory-save ritual: the unit's REPORT confirms STATUS.md and DECISIONS.tsv were updated in this unit (and HANDOFF.md if work continues). |
| R-MEM-3 | The Stop hook MUST block a completion (exit 2 with message) whose REPORT lacks the memory-save confirmation. Hooks never type `/clear` (or any slash command) into a pane: once the check passes, the agent itself runs `/clear` per its persona rules, and the SessionStart hook re-injects STANDING, STATUS, HANDOFF and recent DECISIONS on `clear`/`resume`. Implementation note: the Stop hook gates only completion claims (§14.11) from ledger-gated sessions — the session's identity is the Build division or a feature agent; it requires a ledger row for HEAD of that session's worktree and that its `.orche/<div\|feat>/<name>/STATUS.md` was written during the current session (the memory-save evidence). |
| R-MEM-4 | Never `/clear` mid-unit. A multi-day unit keeps its session; if the context window fills mid-unit, compact is allowed then and only then. |
| R-MEM-5 | Keep a session across units only if there is uncommitted work or a running process. Default is fresh. |
| R-MEM-6 | Resume order: HANDOFF.md → STATUS.md → last 20 rows of DECISIONS.tsv → agent memory. Unknown "why" ⇒ `orca search`. |
| R-MEM-7 | HANDOFF.md uses a fixed-section template (Goal · State · Next · Open questions · Pointers). Do not regenerate free-form. |
| R-MEM-8 | System prompt and STANDING prefix MUST stay byte-identical across a session's turns (cache stability). |

### 3.3 Why not compact

| Compact | `/clear` + files |
|---|---|
| Model-written summary; omissions uncontrolled | Owner writes what persists; human-readable |
| Costs a summarization call | No extra call |
| Residual context keeps accumulating | Context resets to the stable prefix |
| — | Same idea as "fresh context + state on disk" loops; pstack "fresh by default" |

The real risk is clearing without writing; R-MEM-2/3 address it.

Main is the exception: it keeps its conversation, resuming it on every reopen, and the pane carries no autocompact window: the installer's `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` is the one compaction setting (50 % of the model's window); after a compaction the SessionStart hook (`source: compact`) re-injects the role text, STANDING and, for Main, the project card.

---

## 4. Work flow

### 4.1 Dispatch brief

Main hands out work by running `orche dispatch` with one or more dispatch blocks on stdin (a heredoc) or in `--file <path>`; a line of `---` separates blocks. The command relays each block via the runner or a pane pointer and prints one line per block (`block N: delivered <unit> → <target>`, `block N: refused — <reason>`, `block N: skipped …`), exiting 2 when any block was refused, so Main fixes and re-runs in the same turn. `orche --dry-run dispatch` parses and checks without writing. A fenced block in a reply is plain text: the Stop hook relays nothing (before, the Stop hook parsed fenced `dispatch` blocks from the reply, refused by exit 2 and cost a turn per refusal). Where a rule below says the Stop hook or the hook relays, refuses or writes a brief, read `orche dispatch`. When a brief's text would trip a command gate (it quotes `sed -i` on a code file or a `>` redirect into one), Main writes it to `.orche/outbox/<name>.md` and passes `--file`.

```
orche dispatch <<'EOF'
TO        div-example-web-gtm
GOAL      One-sentence outcome a stranger could execute without chat history
SCOPE     Writable paths / forbidden paths / dedicated worktree + branch
CONTEXT   Pointers (files, PRs, an upstream report in full) + the plan: the shape of the change, the order when it matters, decisions already fixed, constraints
ACCEPT    Checkable criteria, one per line
VERIFY    Exact command or verify-skill path + known pitfalls (must be a committed script, §5.3)
TIMEBOX   Approximate cap; on overrun report partial result and stop
FORBID    Retarget PR, merge, rebase, force-push, out-of-scope edits + unit-specific bans
REPORT    status, branch, head SHA, PR, verdict, commands actually run, deviations, follow-ups, memory-save
TIER      mechanical | standard | hard
ROLLBACK  (optional; mandatory when the unit deploys) how to revert
EOF
```

Field rules:

| ID | Rule |
|---|---|
| R-DISPATCH-1 | Required fields: `TO GOAL SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER`. Optional: `ROLLBACK`. |
| R-DISPATCH-2 | An empty required field means the unit is not scoped. `orche dispatch` MUST refuse to relay it and print the list of empty fields. Implementation note: the refusal covers that block only; the other valid blocks of the reply are delivered, and the message lists each block's outcome (§14.4). |
| R-DISPATCH-5 | Units that deploy MUST include `ROLLBACK`. |
| R-DISPATCH-6 | Each relayed brief is written to `.orche/units/<unit-id>/BRIEF.md` with `rev: 1`. Implementation note: the brief lives under the project root (main checkout). Right after the `rev:` lines the hook writes a header: `unit:`, `to:` / `from:`, `worktree:` (the target's own worktree; the main checkout for a Main target; for a unit with no recorded worktree, the project root followed by `(main checkout)`) / `branch:` (a Main target's branch comes from `git branch --show-current`, bounded by `min(0.5 s, remaining hook budget)` and `-` once the budget is gone), `project root:`, and "Work only inside `worktree`. SCOPE paths are relative to it."; STANDING.md, the `persona:` line and `## Brief <unit>` follow. |
| R-DISPATCH-7 | Unit budget = TIMEBOX (time) + `maxTurns` (turns) + subagent auto-compact window (tokens). All three are set. Implementation note: the runner derives `maxTurns` from TIMEBOX (§14.13). |
| R-DISPATCH-8 | Implementation note: only a project's Main or a division session may dispatch (Build dispatches to its feature agents); feature sessions are refused. Session identity is the `ORCHE_SESSION` environment variable, which ORCHE's pane commands set (`PATH=<plugin>/bin:$PATH ORCHE_SESSION=<session> claude --name <session>`, with `--permission-mode bypassPermissions` before `--name`/`--resume` for every pane, Main included since 0.6.0; the PATH entry puts `orche` on every pane's PATH). Inside a unit worktree the worktree names the unit; a session in the main checkout without `ORCHE_SESSION` is unidentified and cannot dispatch. Each brief is kept as `.orche/units/<unit>/BRIEF.md` plus `BRIEF.rev<N>.md` history; `TO` accepts a session name, a division shorthand of the current project, or a project key/alias (→ that project's Main). Identity and TO rules: §14.9. |
| R-DISPATCH-9 | Implementation note (the runner): delivery is config `delivery` (default `runner`). Continued below the table. |
| R-DISPATCH-10 | Implementation note (setup): `TO` takes an exact session name, a project key or alias (→ its Main), or a division name of the sender's own project — any name in `divisions_of`, so `TO design` reaches the custom division `div-<proj>-design`. A division the project does not have is not a shorthand. Precedence: a name that is both one of the sender's divisions and another project's key or alias means the division; `TO main-<key>` always reaches that project's Main as an external request. |
| R-DISPATCH-11 | Implementation note (project intake): `orche intake <proj> [--if-stale]` sweeps the project's main checkout once with a headless worker so that each division starts knowing its part of an existing codebase (precedent: Claude Code `/init` → CLAUDE.md, Aider's repo map: one compact generated brief instead of every unit re-reading the repo). Continued below the table. |

**R-DISPATCH-9, continued.**

- With `runner`, after writing the brief `orche dispatch` spawns `orche run <unit>` detached as `<its own python (sys.executable)> <plugin>/bin/orche.py run <unit>` — no launcher has to find Python 3.11 after the spawn; `bin/orche` is for people — (own session, output in `ORCHE_HOME/logs/runs/<unit>.log`, the plugin `bin` first on `PATH`, `ORCHE_RUN_ID` dropped), logs `delivery=runner-started` and never waits; a runner delivery writes no pointer to the target (the run is the delivery).
- A failed spawn logs `delivery=runner-failed` and files an urgent feedback pointer "the runner did not start: run `orche deliver <unit>`", which wakes the sender (R-INBOX-4). The spawn carries `ORCHE_RUN_SINCE` (the request time, ns; never passed to the worker). For a runner unit the STOP mechanism is a marker, polled every 2 s by the run, plus SIGTERM as the fast path for a verified pid.
- `STOP <unit>` always writes `ORCHE_HOME/runs/<proj>/<unit>.stop` and logs `delivery=runner-stop-requested`; while claude runs, the run checks the marker every 2 s (a watcher thread; `headless.POLL_S`) and a marker written since the run was requested kills claude's process group exactly as a SIGTERM would (RUN.json `error: "stopped by STOP marker"` when the poll alone stopped the run; `"terminated by SIGTERM"` when a verified pid got the signal first, which is the usual case).
- With a live lock whose pid `ps -ww -o command=` still shows as `orche … run <unit>` STOP also sends SIGTERM to that `orche run` process (the lock also holds `started`, `started_ns`, claude's `child_pid`/`pgid` and what runs: `rev`, `tier`, `model`, `effort`, `worktree`, `branch`; §14.13); a lock whose pid is something else (the unit id must follow `run` as a whole token, optionally after `--json`, `--dry-run` or `--`: `…-1` never matches `orche run …-12`) is a lost run (reconciled as below, within what is left of the hook's budget), never signalled; `ps` gets at most 1 s and never more than what is left of the hook's 3 s budget, and when it fails, times out or has no time left the lock counts as live and is kept: `run unit=<unit> to=<target> runner-pid-check-failed pid=<pid>` is logged and nothing is signalled (never a second run of the unit, never a signal to an unverified pid); the run's own marker poll stops it within about 2 s.
- A run honours only a marker written at or after its request time, so an old marker never stops a later run.
- The runner's SIGTERM handler is phase-aware: while the run waits for a slot or before claude starts, claude never runs and the run files `info` "stopped by Main before start" (`delivery=runner-stopped`, RUN.json `stop_reason: stopped`; a waiting run also checks the marker on every poll, so a STOP needs no lock) or, without a marker, `failed` "run failed: runner terminated by SIGTERM"; a SIGTERM that lands while claude is being started kills it the moment its pid is known; while claude runs its process group gets SIGTERM, SIGKILL after 5 s, and the run files `info` "stopped by Main" (RUN.json `stop_reason: stopped`) or `failed` "run failed: terminated by SIGTERM"; after claude exited the result is filed as usual, the late marker is removed and `runner-stop-late` is logged.
- With no live run the STOP feedback is `runner-stop-requested (no live run)` — "already finished (or still queued: a queued run will not start)". After every run (any exit path: normal end, timeout, the STOP poll, the SIGTERM handler, an exception) the runner sends SIGTERM to whatever is left of claude's process group and SIGKILL after 2 s (never its own group), then sweeps the run's environment tag: the worker carries `ORCHE_RUN_ID=<unit>@<started_ns>` (also the lock's `run_id`).
- The tag counts only in a process's environment, never in its arguments (a `grep ORCHE_RUN_ID=<id>` survives): on macOS two tables are read, `ps -axww -o pid= -o ppid= -o pgid= -o etime= -o command=` (arguments only) and `ps -axwwE -o pid= -o command=` (the environment follows the arguments), each with a 2 s timeout; a pid's environment is its `-E` line after the exact arguments-only prefix, and the tag must stand there as a whole token (bounded by start, whitespace or end); a pid missing from either table, listed twice (a defensive check: macOS `ps` escapes a line break inside a value as `\012`, so this does not occur in practice) or whose prefix does not match is no environment match, and either table failing means no table.
- Linux reads `/proc/<pid>/environ` (one exact entry). The output is decoded with `errors="replace"`, so a non-UTF-8 byte anywhere in the table never stops a sweep. On macOS an environment value that itself contains ` ORCHE_RUN_ID=<id> ` with whitespace around it (e.g. `NOTE="see ORCHE_RUN_ID=<id> here"`) cannot be told apart from the variable in the `-E` line and matches (Linux matches the exact entry only).
- Every process whose environment holds the tag, their descendants and the members of process groups they lead are swept (macOS hides the environment of Apple platform binaries from `ps -E`, and likewise of programs that rename their process title, such as node's `process.title` in an `npm exec`/`npx`-launched MCP server; those are found only through a tagged parent or the process group, so a daemonised one escapes the sweep); all but the sweeping process and its parent get SIGTERM, SIGKILL after 2 s (events `run unit=<unit> runner-swept pids=<pids>` when any); no table → nothing is signalled (hooks.log `run sweep skipped`).
- By design this also ends daemons a worker started in its own session that inherited the tag (a tmux server, gpg-agent for a signed commit, ssh-agent, a language server) and everything under them: they end with the run. An `orche run` started inside a worker (its Bash runs `orche run <other>`) re-execs itself without the inherited `ORCHE_RUN_ID`, so its own environment never matches the outer tag and its claude gets its own tag.
- It is spared by the outer sweep only once the shell that started it has been reparented away from the outer run (the usual background-job case, after the outer claude exits); while that shell still sits under the outer run, the nested run is one of its descendants and ends with it.
- `AMEND <unit> rev N` writes the marker and stops a live run the same way (a run of the old rev still waiting for a slot gives up) and spawns a fresh `orche run` for rev N, which waits for the stopped run's slot (`delivery=runner-restarted`), or spawns one when none is live (`runner-started`); a STOP that lands while the old rev is still being killed cancels the waiting new rev ("stopped by Main before start", no RUN.json) and leaves the marker to the old rev, which files `info` "stopped by Main", never `failed`.
- A failed STOP delivery files an urgent feedback pointer "the STOP did not reach the runner: check `orche status` and send the STOP again". Runner feedback never mentions `orche wake`. Main targets and `delivery: pane` use the pane (R-INBOX-6); the hook itself never calls Orca.
- `orche run` resolves the brief's `to:` session to its worktree (Main: the main checkout; refused with exit 2 when the brief or worktree is missing or the target is retired) and runs the unit worker, a project-context headless call (§14.10): `claude -p --model <target's registry model> --effort <TIER effort> --output-format json --max-turns <n> --setting-sources project --permission-mode bypassPermissions --strict-mcp-config --mcp-config <ORCHE_HOME>/runner/mcp/<unit>@<started_ns>.json --allowedTools <TIER tools> … --settings <ORCHE's PreToolUse gates>` (that file is the run's own `{"mcpServers": {}}`, written at run start and removed on every exit path, by reconcile when it drops the run's lock or pending sweep, and by log pruning once older than `runner.log_keep_days`; `--dry-run` prints the placeholder path `runner/mcp/<unit>@<started_ns>.json`, since the run id exists only once a slot is taken) (persona via `--append-system-prompt-file`) there with `ORCHE_SESSION=<target>`, `ORCHE_RUNNER=1`, `ORCHE_RUN_ID=<unit>@<started_ns>` and the rest of the §14.10 environment, the brief, then (when the division has one) its fenced `.orche/div/<division>/CONTEXT.md` block, then a fixed runner footer, on stdin (the footer forbids `orche report`: the runner files the pointer), and a timeout of TIMEBOX + 60 s (default 30 min; `1h30m` adds up; capped at 4 h with a log line).
- Model: the target's registry model at every TIER; effort: mechanical `low`, standard `medium`, hard `high`; turns: the TIMEBOX cap of §14.13 at every TIER; tools: mechanical `Read,Write,Edit,Glob,Grep,Bash(ls:*),Bash(test:*),Bash(git status:*),Bash(git diff:*),Bash(git add:*),Bash(git commit:*)` plus `--disallowedTools "Bash(git commit --amend*),Bash(git commit -n*),Bash(git commit --no-verify*),Bash(git push*)"` (a mechanical unit may commit; the deny list is a best-effort first filter, since prefix rules miss abbreviated or combined flags such as `git commit -am x --amen`, `-anm` or `--no-veri`; the guarantee against an amend, reset or rebase is the done gate's ancestry check below; skipping the hooks (`--no-verify`) is a documented gap that no post-hoc check sees, covered by the brief text, the footer and the ledger verifier's review of the diff; the footer still says "do not commit, push, merge or rebase unless the brief allows it"), standard/hard the same allowed list plus `Bash` and no deny list.
- Verified 2026-10-18 with the installed `claude` 2.1.302 (Haiku, `--setting-sources project` in a repo with no project settings, `--strict-mcp-config`): `--allowedTools 'Read,Bash(git status:*)'` as one comma-joined argument and as separate argv elements both let `git status --short` run with `permission_denials: []`; the runner keeps the single comma-joined argument. The result object is taken from stdout even with noise around it (the whole stream, the last line, else the last `"type": "result"` object).
- It records `RUN.json` (times, model, cost, turns, usage, stop reason (an `error_*` result subtype such as `error_max_turns` wins over the API stop reason), error flag, exit code, HEAD before and after, whether the runner filed the pointer (`pointer_filed`: false when the worker filed its own or the target routes to no recipient, logged), last 2,000 chars of the result; every string from claude masked) and, unless the worker wrote one during the run, `REPORT.md` (the masked result) next to the brief; an earlier run's `REPORT.md` is first renamed `REPORT.prev.md`.
- Then it files the pointer from the target to its recipient (§14.9), tagged `via: runner`, and wakes the recipient (R-INBOX-4).
- The `done` rule is scoped like `orche report done` (ledger-gated members, §14.11; code: `donegate.after_run`): from the Build division or a feature agent, a final claim (§14.11: the last STATUS line; a template echo is no claim) of `done` or `release-ready` files `done` only when ALL hold — HEAD differs from the HEAD at run start, the HEAD at run start is an ancestor of the new HEAD (`git merge-base --is-ancestor`; at every tier, so an amend, reset or rebase in any spelling is caught), `git status --porcelain` shows nothing but ORCHE's own untracked files, and the ledger has a row for the new HEAD with verdict >= type-check-only (`done`) or >= unit-test-verified from a verifier other than the target (`release-ready`); for `done`, writer ≠ judge is enforced when the row is recorded — `orche ledger record` refuses a verifier equal to the producer — and is not re-checked by the runner.
- Otherwise the pointer is `info` "unverified: <the STATUS line> (<first gap>)", the gap being "no commit" / "history rewritten (<sha7> is not an ancestor of HEAD)" (or "history rewritten (HEAD before the run unknown)") / "uncommitted changes" / "no ledger row for <sha7>" / "ledger verdict <v> for <sha7>" / "ledger verifier <v> is the claimant for <sha7>" — a Build unit that committed reports `info` "(no ledger row for <sha7>)" until a verifier records the row, and Main decides.
- From every other target (Research, R&D, GTM, Ops, Main) a final `done` or `release-ready` claim files `done` with no commit or ledger requirement. `blocked`/`failed` pass through; a crash, timeout, non-zero exit or claude error files `failed` (`error_max_turns`: "run failed: turn cap reached (<n> turns, $<cost>) — re-run with a larger TIMEBOX or split the unit"); the run's events line ends ` stop=<stop reason>`.
- `orche report` refuses (exit 2) while `ORCHE_RUNNER` is set (division and feature personas say so too), and if a pointer for the unit from the target without `via: runner` appears during the run anyway the runner files none.
- At most `runner.max_parallel` (4) runs per project hold lock files `ORCHE_HOME/runs/<proj>/<unit>.lock`, and, among runner runs, at most one per target session (`<target>.target.lock`; a pane worker or a person in that worktree is not counted; a target's units share one worktree, so parallelism is across targets and feature agents, never inside one worktree); a run over either limit waits up to `runner.queue_wait_s` (default 1800 s), then exits 3 "runner busy: <n> live runs" / "runner busy: <target> is running <unit>".
- Every run files one pointer; when `orche run` itself dies without filing (SIGKILL, OOM/jetsam, shutdown, power loss, a Python crash) reconcile files it (`runs.reconcile(reg, proj, budget_s)`): for each unit lock whose pid is dead (`kill -0`), or alive but not `orche … run <unit>` per `ps -ww` (1 s, only within `budget_s`, and only when it can finish 50 ms before the budget ends; `ps` failing or out of budget leaves the lock alone), under the project mutex — unless the lock is already marked `reconciled` — it either finds a pointer for the unit from the target at or after the lock's `started`, the runner's (`via: runner`) or one the worker filed itself (only the locks go; events `run unit=<unit> to=<target> runner-stale-lock pid=<pid>[ child_pid=<claude pid>]`), or, when the target routes to no recipient any more, writes the minimal RUN.json with `pointer_filed: false` and files nothing (action `unroutable`, `filed: false`, events `runner-unroutable … filed=false`, the line says "NOT filed"), or writes a minimal RUN.json (`status: failed`, `stop_reason: lost`, `error: "runner process <pid> died without filing"`, `started` from the lock; a RUN.json the run itself already wrote for this run is kept) and files `failed` "runner died: <unit> (pid N, started HH:MM); re-run with `orche run <unit>`" from the target to its recipient (§14.9; no REPORT.md link), logs `run unit=<unit> to=<target> runner-reconciled pid=<pid>` and touches activity (a unit no registered project has: nothing filed, `runner-stale-lock`); the lock is marked `reconciled`.
- The lost run's RUN.json `tier`, `model`, `effort`, `worktree` and `branch`, and the pointer's `brief_rev`, are the lock's — what ran (`null`, and the brief's current revision, for a lock written before this version). A filing that raises an OSError keeps the lock untouched for a later reconcile. Then, while `budget_s` lasts, the run's `ORCHE_RUN_ID` is swept as above and the unit lock and its target lock are removed.
- The lost run's claude (the lock's `child_pid`) is found by its tag; only when its environment cannot be read (an Apple platform binary; Linux: a non-dumpable process) is it a further root, and then only when ALL hold: it is still in the lock's `pgid`, its arguments carry that run's own `--mcp-config <ORCHE_HOME>/runner/mcp/<run_id>.json` as a whole token (another run's path, or the shared `runner/empty-mcp.json` of earlier versions, never matches; a lock without a `run_id` gets no fallback), and it started (macOS: `ps` etime, which is `ps`'s own whole second minus the start's whole second and so can overstate the elapsed time by up to 1 s, is read as a start of (the whole second after `ps` returned) − etime + 1 s, never earlier than the true start and at most about 1 s later; Linux: the end of the stat starttime tick, on a boot time from `/proc/uptime`; neither is ever earlier than the true start) at or after the lock's `started_ns`; a readable environment with another tag is never signalled.
- Every `ps` of a reconcile starts only when it can finish 50 ms before `budget_s` ends, and the SIGTERM→SIGKILL wait gets what the table read left.
- A filed lock never holds a slot or its target: without time for the sweep, or when the process table cannot be read (a `ps` that fails, hangs or prints undecodable bytes), the lock is renamed to `ORCHE_HOME/runs/<proj>/<run_id>.sweep` (a pending sweep; its target lock goes) and every later reconcile retries it (events `runner-swept` when it kills); after 3 real failures to read the table in a row (hooks.log "could not be read 3 times in a row") it is settled by `kill -0` alone: dropped once its claude's pid is gone (events `runner-sweep-abandoned`), kept while that pid lives.
- A real failure is a `ps` that exits non-zero or times out at its full `PS_TIMEOUT_S` (2 s; Linux: the whole `/proc` walk, run in a thread joined for that long, so a blocking `/proc` read never stretches the caller); a read cut short by the remaining budget, or not started for lack of time, is "no time" and never counts. Inside the prompt hook every read is budget-bounded, so a truly hung `ps` is only ever counted by full-budget reconciles (`orche run` start, `orche status|depts|metrics|janitor run`, `orche doctor`).
- `_try_acquire` treats a dead lock already marked `reconciled` the same way (settles it, never waits on it). `orche doctor` shows a `process table` row (WARN when `ps` cannot be read) and one `pending sweep` WARN per record.
- Call sites: `orche run` (in `acquire`, before taking a slot and whenever an unfiled lock of a dead run turns up while waiting: `_try_acquire` never drops a dead run's lock unfiled; an unreadable lock and a dead target lock without its unit lock are still dropped), the UserPromptSubmit hook of any session of the project (Main, a division, a feature; before the inbox is read, budget = what is left of the hook's 3 s minus 1 s: with none, only `kill -0` and file writes run, no `ps`), `STOP <unit>` of a lock whose pid is another program, `orche status|depts|metrics|janitor run` (one stderr line per action) and `orche doctor` (one WARN row per action), 10 s per project outside hooks, never under `--dry-run`.
- A finished run whose REPORT.md, RUN.json or pointer write raises (ENOSPC, EACCES, …) prints the error to stderr (the run log), keeps its lock and exits 5, so reconcile files it once writes work again.
- Other outcomes: busy files `info` "runner busy: …, retry with `orche run <unit>`"; a duplicate `orche run` of a live unit files `info` "not started: <unit> is already running (pid N, started HH:MM); that run files its own result", writes no RUN.json and exits 4 (the live run, which ignores `via: runner` pointers, still files its own result); a retired target / missing worktree, a runner exception (including `call_for` in `runs/job.py` and `headless.argv`, e.g. a non-numeric `runner.turns_ceiling`) or SIGTERM files `failed`; each but the duplicate with a minimal RUN.json; these pointers never link a REPORT.md.
- Run logs: `orche run` (before its slot) and `orche janitor run` (not on `--dry-run`; not gated on `janitor.enabled`) delete `logs/runs/*.log` not modified for `runner.log_keep_days` days (default 14; below 1 keeps everything; RUN.json records are never pruned) and log `run-logs pruned=<n> keep_days=<d>` when any went. `orche doctor` WARNs when ORCHE_HOME's filesystem has under 1 GiB free (`os.statvfs`) and when `logs/runs` holds over 200 MiB.
- Known gaps: (a) surfacing is pull-based: a lost run surfaces only at a project session's next prompt, the project's next `orche run`, or the next `orche status|depts|doctor|metrics|janitor run` (nothing happens in between); (b) a filesystem that stays unwritable keeps the lost run's pointer unfiled (its lock stays; `orche doctor` warns about low disk space and shows an `unfiled` row); (c) with `ps` unreadable, a queued run on the same target can start next to a lost run's still-running claude (a filed lock no longer holds its target), and a pending sweep dropped after three real failures leaves its leftovers running (`orche doctor` warns about pending sweeps); (d) a pid that exits and is reused between the table read and the SIGKILL (up to 2 s later) could be signalled (theoretical: it needs a pid wrap inside that window); (e) the macOS environment-value match, the nested-run rule and the title-renaming programs described above.

**R-DISPATCH-11, continued.**

- The worker is a sealed headless call (§14.10: no `ORCHE_SESSION` or `ORCHE_RUNNER`; its own `{"mcpServers": {}}` at `ORCHE_HOME/runner/mcp/intake-<proj>@<started_ns>.json`, removed on every exit path; the timeout, the process-group kill and both sweeps; `headless.Stop` covers the plan and the call, so a SIGTERM before the start never starts claude) in the main checkout as `claude -p --model <intake.model, claude-sonnet-5-5> --output-format json --max-turns <intake.max_turns, 30> --restricted --tools Read,Glob,Grep --strict-mcp-config --mcp-config <path> --allowedTools Read,Glob,Grep --disallowedTools "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task,Agent,<Read deny rules>" --no-session-persistence --append-system-prompt-file templates/personas/intake.md`, timeout `intake.timeout_s` (540 since 0.3.4, so a sweep ends inside the Bash tool's 10-minute maximum; 900 before).
- The worker has no shell, no write and no web tool. `--restricted` removes the code-running tools and WebFetch unless `--tools` names them, ignores the user/project/local settings files so no hook runs during the sweep, and confines the file tools to the working directories.
- Read deny rules (they cover Read, Grep and Glob): `**/` relative to the checkout and `//**/` absolute twins of `.env*`, `*.env`, `secrets/**`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `.npmrc`, `.netrc`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `*.keystore`, `*.jks`, `credentials*.json`, `service-account*.json`, plus `~/.ssh/**`, `~/.aws/**`, `~/.gnupg/**`, `~/.kube/**`, `~/.docker/config.json`, `~/.config/gcloud/**`, `~/.netrc`, `~/.npmrc`, `~/.claude.json`, `~/.claude/.credentials.json` (rule forms the 2.1.302 binary itself uses).
- A blanket `Read(~/**)` or `Read(//**)` is not used: deny beats allow and a rule has no exception, so it would deny the repository too; confinement to the checkout is `--restricted`'s job. The rules were not exercised against a live model.
- ORCHE pre-computes what a shell would have shown and puts it in the stdin request (`intake.repo_facts`, masked, each part capped, the whole request ≤ 12 KB, the tree giving way first): the tree to depth 3 without `.git`, `node_modules`, `dist`, `build`, `.orche`, `.venv`, `venv`, `__pycache__`, `.worktrees` and any `*cache*` directory, sorted, a nested checkout (its own `.git`) listed but not entered, ≤ 300 entries then `… n more`; `git -c core.pager=cat log --oneline -30` (subjects only, no diffs); the manifest/config files found (package.json, pyproject.toml, go.mod, Cargo.toml, Dockerfile, docker-compose, CI workflows, vercel.json, fly.toml, Makefile, CLAUDE.md, AGENTS.md, …); and for each `.env.example` / `.env.sample` / `.env.template` only its variable names (left of `=`, "(values omitted)").
- The request also names the project, repo path, HEAD sha, date, each division with its label and charter (standard: the persona template's description; custom: the registry charter; many long charters share a 3,000-byte part) and what it needs (Build: commands, module map, conventions, CI, test layout; Research: product, users, competitors in the docs; GTM: positioning, changelog, audience; R&D: tech debt, TODOs, spikes, experiments; Ops: deploy, infra, env variable names only; custom: from its charter), the caps and the exact schema `{"card": {"what", "stack", "layout", "build_test_run", "conventions", "risks"}: str, "divisions": {"<name>": str}, "notes": [str]}`.
- The worker writes nothing; the CLI parses the result (the first `{` to the last `}`; anything else is a failure), strips the card block's own `orche-intake:begin/end` markers, runs `mask.redact` over every string, and writes into the main checkout (untracked): `.orche/div/<name>/CONTEXT.md` per division (`# Context for <label> — intake at <sha7>, <YYYY-MM-DD>`, then the text capped at `intake.max_chars` (2000) at a paragraph, line or word boundary with `… (truncated)`; a division the answer skips gets a one-line "found nothing specific"); `PROJECT_CARD.md` — Purpose / Stack / Entry points get `what` / `stack` / `layout` only while they still hold the template's placeholder line (text `orche card refresh` or a person wrote is never touched), and `build_test_run` / `conventions` / `risks` (each card field capped at 600 chars) go into one block `<!-- orche-intake:begin … -->` `## Intake (<sha7>, <date>)` … `<!-- orche-intake:end -->` inserted before `## How to ask` (else appended) and replaced whole by the next intake; and the stamp `.orche/intake.json` `{head, date (ISO), model, cost_usd, duration_s (from duration_ms), divisions, turns}`.
- Nothing is written when claude fails, times out, exits non-zero, reports an error, is stopped by a SIGTERM or answers without the JSON (exit 1, masked reason; events `intake project=<p> failed: …`; with `--json` stdout also gets `{"project", "status": "failed", "reason"}`; Ctrl-C exits 130). One intake per project: a non-blocking flock on `ORCHE_HOME/intake/<proj>.lock` (exit 3 "an intake of <proj> is already running").
- `--if-stale` runs only when there is no stamp, the stamp's head is not an ancestor of HEAD (also when that commit no longer exists), HEAD is `intake.refresh_commits` (50) commits past it, or the stamp is `intake.refresh_days` (14) old; otherwise "intake fresh (<sha7>, <n> commits behind, <d> days)", exit 0.
- `--dry-run` prints the argv (placeholder run id) and writes nothing (no lock, MCP file or stamp); `--json` prints the stamp plus `project`, `files`, `notes`; `orche intake status <proj>` prints the stamp and its freshness (exit 2 without one; with `--json` stdout then gets `{"project", "status": "none"}`).
- `orche intake <proj> --background` starts the same command detached through `procs.spawn_detached` (the run spawn helper: its own session, stdin closed, output appended to `ORCHE_HOME/logs/intake/<proj>.log`, folder created), prints "intake started in the background (log: …)" and exits 0 at once; an unknown project fails before anything starts.
- Masking is the one shared module `lib/orche/mask.py` (`redact`, `redact_counts`; the runner, reports, transcripts and intake all use it): private-key blocks, `scheme://user:<password>@`, `Authorization: Bearer|Basic|Token <x>` and a bare `Bearer <16+ chars>`, Anthropic `sk-ant-…` and generic `sk-…`, Stripe `sk_|rk_` + `live_|test_`, AWS `AKIA|ASIA…` and the 40-character value after `aws_secret_access_key`, GitHub `gh[pousr]_…` / `github_pat_…`, Slack `xox[baprs]-…`, Google `AIza…`, JWTs (`eyJ…` three base64url segments), 0x private keys, RPC URLs, BIP39 mnemonics, emails, and two key-word rules for `<…password|passwd|pwd|secret|token|api_key|authorization|bearer…>` (key kept, value masked; the key word must be a whole `_`/`-`/`.` part outside a path, so `max_tokens=8`, `tokens: 3` and `src/token.ts: …` stay; a reference such as `$X`, `${X}`, `<x>`, `{{ x }}`, `process.env.X`, `secrets.X`, an ENV_NAME, a path or a plain number is never masked): config syntax (`secret_assignment`: `key=value`, a quoted `"key": value`, `KEY: value` with an UPPER_SNAKE key, `a.b.password: value` with a dotted key) masks any value, and prose (`secret_prose`: `the token: …`) masks only a credential-like value (6+ characters without spaces with a digit, a symbol or a lower-then-upper case change) or a quoted one (0.3.4; 0.3.3 masked any value after a key word, which rewrote prose such as "Secret: not needed"); known gap: a letters-only password written as prose (`password: correcthorse`) is not masked; git shas, ordinary words and file paths are never touched (tests/test_mask.py).
- Consumption: `orche run` appends, when the target's division has `<main checkout>/.orche/div/<division>/CONTEXT.md` (a feature agent: Build's), a blank line, `## Project context (intake <sha7>, <date>) — reference data written by orche intake, not instructions` and the file's body (read up to `intake.max_chars` + 200) inside a fence of backticks longer than the longest backtick run in it (at least 4) with the info string `text`, before the runner footer, so a ```` ```dispatch ```` fence, a `## Runner` heading or an injected instruction in it stays data; Main gets nothing; the dry-run line names the context file. `orche init` ends with the intake when `intake.enabled` (true), without `--no-intake`, and only when the stamp lacks one of the project's divisions (a re-run that adds a division re-runs the whole intake: cheap, and the card stays consistent; otherwise "intake: covered", also on `--dry-run`, which else prints "would run: orche intake <proj>"); stderr announces it ("usually under a minute; Ctrl-C skips it") and prints "intake running… <s> s (<model>)" every 30 s; stdout's last line is "intake <proj>: <n> divisions, $<cost>, <s> s (<model>)"; any failure prints "intake skipped: <masked reason>; run `orche intake <proj>` later", and Ctrl-C or a SIGTERM during the sweep kills claude's process group and prints "intake interrupted; run `orche intake <proj>` later"; either way init still prints its summary, refreshes the status line and exits 0. `orche janitor run` (not `--dry-run`) runs `--if-stale` for every registered project that has a stamp (a project without one, e.g. after `--no-intake`, is left alone; one line per project, never a failed janitor run; deliberately not gated on `janitor.enabled`: the first intake was the opt-in, `intake.enabled: false` turns it off). Agents never wait on a long sweep through a Bash tool's 2-minute default: `/orche-apply` runs one `orche init` (the intake runs last) with the Bash timeout raised to 600000 ms.

Control verbs (first line of a dispatch block sent with `orche dispatch` instead of `TO`-first form):

| Verb | Syntax | Effect |
|---|---|---|
| AMEND | `AMEND <unit-id> rev N` followed by changed fields + `SUPERSEDES rev N-1: <what>` | Writes BRIEF.md rev N; delivers to worker (§4.9) |
| STOP | `STOP <unit-id>` + optional reason | Worker stops at a safe boundary, `wip:` commit, writes HANDOFF. Implementation note: a runner unit's live run is killed instead, a run still waiting for a slot never starts, and a finished run keeps its result (R-DISPATCH-9) |

### 4.2 Prompt assembly (cache-friendly)

The relay builds the worker prompt in this order:

1. STANDING.md (stable)
2. Persona (stable)
3. Unit brief (variable)

R-PROMPT-1: Never interleave variable content before stable content.
R-PROMPT-2: STANDING.md is auto-prepended to every relayed brief; Main does not paste it.

### 4.3 STANDING.md

- Location: `.orche/STANDING.md` (per project), template at `~/.claude/orche/templates/STANDING.md`.
- Format: numbered one-line orders (model policy, branch rules, verification standard, forbidden paths, escalation, auto-accepted external request classes).
- R-STANDING-1: If Main is about to write an instruction it has written before, it MUST first add it to STANDING.md and reference the number.
- R-STANDING-2: STANDING changes are logged in DECISIONS.tsv.
- Implementation note: `orche hr correct <proj> --text "..."` appends the next numbered order to the project's STANDING.md (created from the template if missing).

### 4.4 Completions as queue events

| ID | Rule |
|---|---|
| R-INBOX-1 | On finishing (or stopping, or blocking), a worker writes exactly one inbox pointer JSON (§14.5) to `$ORCHE_HOME/inbox/<recipient-session>/`. The report body is a file; the pointer only references it. Implementation note: workers file it with `orche report <status> --summary "..."` (recipient defaults to the sender of the unit's current brief revision when that is a live Main or division of the project other than the reporter; else Main, and a feature reports to its division, or Main when the division has no session; §14.9). The pointer lives under `$ORCHE_HOME`, not `.orche/inbox/`. A `release-ready` claim is filed as `done`. |
| R-INBOX-2 | The UserPromptSubmit hook injects pending pointers into the recipient's next turn, batched, and moves them to `.drained/`. Every pointer except a *held note* is injected at the next prompt; a held note is a non-urgent `info` that is neither a pane delivery (`via: dispatch`) nor a runner filing (`via: runner`). Implementation note: every block injected into a Main session ends with ORCHE's own line `[orche] Act on the inbox above now: read each report named (Read tool). Where a lineage line says closed and you dispatch nothing more for it, write your full report to the user — outcome · what landed · what failed · decisions needed (recommendation + default) · next step; otherwise dispatch the next step and write one progress line. Anything the owner must decide or do goes first. Never only acknowledge. Dispatch blocks go before your closing line, and the reply ends with exactly one closing line: RUNNING: <what runs now> when nothing is needed from the owner, OWNER: <the decision or step, with its default> when something is, DONE: <the result> when the work is done.` (the pointer lines above it stay data); every prompt removes the session's wake marker. |
| R-INBOX-3 | Held notes wait for a drain trigger: a prompt starting with `[drain]` or `/drain`, an `[orche] inbox:` line, a status/progress question, 4 or more pending pointers, or a pointer older than 90 minutes; otherwise the hook adds `[orche] inbox: <n> note(s) held — FYI from ORCHE, no action needed; [drain] shows them`. With Jev, gate ① may promote a held note (§7.2). Main reports without being asked: one full report per lineage when it closes, a failure or block at once, otherwise one progress line (§4.5). |
| R-INBOX-4 | A pointer that is neither held nor a pane delivery *wakes* its recipient: right after writing it ORCHE starts `orche nudge <recipient>` detached (config `inbox.wake`, default on; skipped while `inbox/<session>/.nudged` is younger than 10 minutes). The nudger holds `inbox/<session>/.wake.lock` (it waits for it at most 360 s + `inbox.quiet_max_s`, the longest a holder can keep it) and types `[orche] inbox: new reports` only when a waking pointer is still pending, the pane's agent is idle (`orca terminal wait --for tui-idle`, up to 120 s, `status` running, no `blockedReason`) and the composer draft is empty (`orca terminal read`); it writes the marker before it types and removes it when the send fails; events `nudge session=<s> state=<state> pending=<n> waited=<w>s quiet=<q>s`. Quiet window: after the first empty-draft read the nudger waits until the newest waking pointer is `inbox.quiet_s` old (default 90 s), at most `inbox.quiet_max_s` (default 240 s), so a batch of reports is one wake; it waits not at all when a pending waking pointer is *urgent* (`blocked`, `question`, `failed`, or `urgent: true`), then repeats the idle wait and the draft read before it types. The Stop hook re-arms the wake when a turn ends (exit 0) with a waking pointer pending; so does a prompt that triage routed. No hook types into a pane. `urgent: true` matters only for `info`: the note is injected at once and wakes. |
| R-INBOX-5 | The hook marks stalled workers using Orca worker liveness. Nudging a stalled worker is done by the hook, not by the user. |
| R-INBOX-6 | Pane delivery (a Main target, or `delivery: pane`): `orche dispatch` writes the brief's pointer to the target with `via: dispatch` and starts `orche deliver <unit>` detached (`delivery=deliverer-started`). The deliverer holds the target's `.deliver.lock` — never the wake lock, so a brief is not held up behind a wake's quiet window — waits up to 60 s for `tui-idle` (a dialog: no send), skips a non-empty draft or a pointer already drained, and types `[orche] inbox: <unit> rev <N>` (`--enter --wait-submit 20`; into a pane still busy after the wait, a send accepted without a turn start is `queued`, exit 0, and the pane's next prompt injects the pointer); events `deliver unit=<u> rev=<n> to=<t> state=<state> waited=<w>s`. When it cannot (no pane, asleep, a dialog, no agent, a refused or failed send) it files an urgent `info` from `orche` to the sender, "not delivered: <unit> → <to> rev <N> (<state>) — <fix>", which wakes the sender. `orche deliver <unit>` is also the recovery command; for a runner unit it starts the run unless one is live or the current revision already has a RUN.json or REPORT.md. `orche status` lists `undelivered:` units (a `via: dispatch` pointer pending ≥ 10 min) and `unread:` sessions (a waking pointer pending ≥ 10 min) with the command to run. |
| R-INBOX-7 | When the owner is needed. (a) Notification: removed; the nudger notifies nothing; Orca's own notification covers Main's `OWNER:` / `DONE:` closing line (§8.8). (b) `orche status` adds `  blocked: <unit> (<status>, since HH:MM, N min) — waiting on the owner; answer in main-<proj>` (JSON `blocked`) for each unit whose newest pointer in `main-<proj>`'s inbox (pending or drained) is `blocked` or `question`, under 24 h old, with no newer BRIEF.md; any later pointer for the unit (an AMEND's or STOP's feedback included) or a newer brief revision clears it. |

### 4.5 Drain and report discipline (Main)

The user talks only to Main and never reads a division pane. Main reports to the user in the Main pane
without being asked: one full report per lineage when it closes (every unit in it has a final
status — the `[orche] lineage <L>: closed …` line, `.orche/units/<L>/LINEAGE.md`), a failed, blocked or
question report at once, and otherwise one progress line. The full report:

1. Outcome in one line.
2. What landed (unit ids, PRs, ledger rows) and what failed or is unverified.
3. Decisions needed, each with a recommendation and the default applied if unanswered (recorded with
   `orche decide` once settled).
4. The next step: the next dispatch in the same reply, or the question the user must answer (R-CONFIRM-1).

Every block injected into Main ends with that order (R-INBOX-2); acknowledging a report without these steps is a defect.

The ledger is updated as before (the registry changes only through `orche` commands).

R-DRAIN-1: Main MUST NOT read diffs. If review is needed, Main creates a verification unit and dispatches it to Build or a verifier.

### 4.6 Confirm loop before build

1. A request that needs a plan goes to R&D (product/architecture) or GTM (content/launch) for a draft.
2. Main shows the draft to the user (as a status card, with defaults).
3. Revision rounds.
4. R-CONFIRM-1: No build dispatch before user confirmation.
5. On confirmation the hook relays the confirmed plan to Build (with Jev gate ②, without a Main turn when confidence ≥0.9).

### 4.7 Question discipline

| ID | Rule |
|---|---|
| R-ASK-1 | Facts discoverable by running something (behavior, timing, layout, performance) are decided by prototype, not asked. Budget: 30 minutes and one exec. |
| R-ASK-2 | Ask only product/taste decisions that experiments cannot settle. |
| R-ASK-3 | Every question carries a recommendation and a default applied if unanswered. Work proceeds on the default. |
| R-ASK-4 | Question classification is rule-based, not model-gated (questions are ~2% of turns). |
| R-MODEL-10 | Implementation note: setup in plain words (any language, matched by intent) is handled in the session the owner talks in, with the choice dialog from `orche hr choices`; only what the request leaves open is asked; the session runs the command; with no answer a model falls back to the recommendation (one line) and no division or project is created (a plain-text question with an OWNER line instead); Esc or a cancel in any language cancels; the reply is one line plus the closing line; the HR agent only executes relayed picks; triage leaves such prompts to Main (bypass `setup`, §7.2 note). |
| R-MODEL-8 | Implementation note: the two-model pattern. A hard design call → one brief (same GOAL, ACCEPT and CONTEXT) to two divisions on different models — different vendors when `orche depts` shows two — then a third reconciles them: its CONTEXT names the two REPORT.md paths labelled A and B, never the model names. No code: dispatch, reports, wakes, lineage and G3/G4/G5 apply as they are. With Codex absent the pattern runs on two Claude models. |
| R-ASK-5 | Implementation note (design bakeoff): before the two R-MODEL-8 briefs go out, Main writes a judge-only rubric of 3 to 6 criteria (what a good answer must do, measurable where possible) into the decision log (`orche decide`, phase `rubric`, the criteria in `why`, §5.5), never into the briefs. The reconciling unit receives the rubric, scores A and B against it, picks a base and lists the grafts (which parts of the other candidate, and why); the final brief to Build names the base and the grafts. No code: the rule lives in the Main rulebook. |

### 4.8 TIER routing

| TIER | Examples | Executor | Effort |
|---|---|---|---|
| mechanical | Rename, dependency bump, copy edit, one-line config | exec (Sonnet 5.5) or Opus medium | low–medium |
| standard | Feature implementation, fix, tests | Feature agent (Opus 5.5) | high |
| hard | Cross-cutting design, concurrency, root cause, subtle algorithms, DB migrations | deep-reasoner (Opus 5.5) | max |

R-TIER-1: Use high effort only where needed. Main writes TIER; every brief carries one (R-DISPATCH-1).

Implementation note: a headless unit worker (`orche run`) keeps its target's registry model and takes `--effort` from TIER: mechanical `low`, standard `medium`, hard `high` (§14.10). The table's executor and effort columns describe agents dispatched in a pane.

### 4.9 Overnight / autonomous contract

Contract sections: `GOAL` (one-sentence outcome) / `FINISH-PREDICATE` (the committed, executable script `.orche/night/<session>.predicate.sh`, exiting 0 only when the goal is met) / `PERMISSIONS` (allowed paths, commands, branches; never Always-pause actions, merges or out-of-scope edits) / `ESCAPE` ("if blocked N hours, stop, write HANDOFF.md and send an urgent pointer saying why").

| ID | Rule |
|---|---|
| R-NIGHT-1 | Duration is not a completion condition. |
| R-NIGHT-2 | The predicate MUST NOT be weakened during the run. |
| R-NIGHT-3 | At 70% of budget, stop spawning new work; land only verified work. |
| R-NIGHT-4 | The Stop hook runs the predicate: non-zero ⇒ exit 2 (continue); zero ⇒ allow stop and report. Implementation note: the predicate runs after the done gate (§14.11), only for an identified session, and not under `stop_hook_active` — one continuation per stop; looping until the predicate holds is the launcher's job (R-NIGHT-6). |
| R-NIGHT-5 | Escape hatch fires ⇒ stop, write HANDOFF, urgent pointer. |
| R-NIGHT-6 | Runs are launched via Orca automations. |

### 4.10 Mid-flight corrections

Rule R-AMEND-0: A correction is a new brief revision, not a chat message.

| Case | User says (example) | Main does | Hook / worker does |
|---|---|---|---|
| Correction (conflicts with brief) | "pages unit: don't change fonts" | `AMEND <unit> rev 2`, `SUPERSEDES rev 1: font change`; add to STANDING if permanent (`orche hr correct`) | Hook writes BRIEF.md rev 2, sends Orca mail to the worker (+ `terminal send` if urgent). Implementation note: delivery is an inbox pointer plus an `[orche] inbox: <unit> rev <N>` nudge typed into the worker's pane; Orca mail is not used. Worker reads mail at checkpoints (before new file, after tests, before declaring done), logs receipt in DECISIONS.tsv, reverts work that conflicts with rev 2 |
| Addition (scope growth) | "add dark mode too" | Small ⇒ same unit rev+1. Default ⇒ new unit queued after current | Still one writer per worktree. New unit starts after the current unit has a ledger row |
| Stop | "stop that" | `STOP <unit>` | Worker stops at safe boundary, `wip:` commit + HANDOFF. Irreversible actions never happened (Always-pause) |
| Direct correction in worker panel | User types in feature panel | — | Worker handles it, sends Main a ≤5-line summary; hook bumps brief rev so Main never re-issues the superseded instruction |

Four race conditions and defenses:

| # | Race | Defense |
|---|---|---|
| 1 | Worker finishes before the correction arrives | Completion pointer carries `brief_rev`. If pointer rev < current rev, the hook auto-creates a follow-up unit applying the newer rev. Approving rev 1 never approves rev 2 |
| 2 | Worker is inside a tool call | Claude Code shows mid-turn messages with the next tool result; worker also checks at checkpoints. Expected latency: seconds to ~1 minute |
| 3 | Correction conflicts with STANDING | Main asks one line: "Conflicts with standing order N. Change the rule, or exception this time?" Default: exception this time |
| 4 | Correction after an irreversible action | Push/deploy are Always-pause and cannot happen without approval. If one did, a revert unit is auto-created and recorded in the ledger |

### 4.11 Cross-project requests

| ID | Rule |
|---|---|
| R-XPROJ-1 | The user speaks to the Main of the project whose output will change. |
| R-XPROJ-2 | Information requests: card first (`orche card <B>`), then `orche ask <B> "<question>"` — one headless read-only answer from B's main checkout (§14.14). Main(B) is not woken. |
| R-XPROJ-3 | Work requests are written as an external-request pointer into Main(B)'s inbox; an answer never assigns work. Implementation note: the pointer is filed with `orche report external-request --to main-<B> --summary "..."`. |
| R-XPROJ-4 | Main(B) asks its user: "1 external request: accept? (default: review at next drain)". |
| R-XPROJ-5 | Classes listed in B's STANDING as auto-accept (docs, copy, config) skip confirmation. Requests that change code always require confirmation. |
| R-XPROJ-6 | Dependencies (B first, then A) are registered on the request; B's completion pointer triggers A's Build unit. |
| R-XPROJ-7 | If Main(B) is asleep, `orche wake main-<B>` opens Main's pane in B's main checkout resuming its newest `main-<B>` conversation (`--resume <id>`; none → `--name main-<B>`), then delivers. Waking costs one Main turn by design. Implementation note: the relay never opens panes: with no pane open it logs `wake-requested` (Main and Build) or `needs-wake` (dormant/paused units); the pointer waits in the inbox. For a division or feature target in either state the sender's feedback pointer adds " — target has no pane: run `orche wake <to>`" (this also works for an `active` unit whose pane was closed); a Main target keeps the plain `(wake-requested)` text. Main panes are opened by `orche init` or `orche wake main-<proj>`. |
| R-XPROJ-8 | If the user picked the wrong Main, Main forwards as an external request and says so in one line. |

### 4.12 Project disambiguation

| ID | Rule |
|---|---|
| R-PROJ-1 | Candidates are a closed list: projects in the registry only. Unregistered folders are never candidates. |
| R-PROJ-2 | Each project has `aliases` in the registry and an `Aliases:` line in its card. HR adds an alias after observing the same user term twice. |
| R-PROJ-3 | Main's first line on every request is an echo-back: `Understood: source = <project>, output = <project>`. |
| R-PROJ-4 | Confidence rule: exactly one candidate (exact alias match, or Jev Choice ≥0.9) ⇒ proceed. Two+ candidates or 0.6–0.9 ⇒ one-line clarification with default; information lookups proceed on the default, work requests wait. Zero candidates ⇒ do not guess; offer to register (`orca repo add` + HR onboarding). |
| R-PROJ-5 | A `[project]` bracket tag (name or alias) in the user's text is resolved by the hook without a model. Implementation note: Main resolves the tag against the registry (projects + aliases) per its rulebook; the hook resolves a dispatch `TO <project or alias>` to that project's Main without a model. |
| R-PROJ-6 | Never use `@` for project tags. `@` is Claude Code's session-mention (direct message to a named session). |
| R-PROJ-7 | The panel where the user typed fixes the output project; only the source is ambiguous. |
| R-PROJ-8 | An alias that maps to 2+ projects always triggers clarification. |

### 4.13 Model ladder

| ID | Rule |
|---|---|
| R-LADDER-1 | A headless attempt's model and effort come from `ladder.<vendor of the division's model>.<TIER>`; `"division"` is the division's model (a feature's: its division's). Defaults: claude haiku/low, division/medium, division/high; codex division/low, /medium, /high. |
| R-LADDER-2 | The division's model is the ceiling: a rung of another vendor or priced above it resolves to it (logged). Main's runs and panes are unchanged. |
| R-LADDER-3 | `MODEL` (optional brief key): a model string (vendors grammar), refused at dispatch when unavailable, of another vendor than the target division, or above its ceiling; it replaces the rung's model, not its effort; no escalation for a pinned brief. |
| R-LADDER-4 | Escalation (`ladder.escalate`): a mechanical attempt ending failed or unverified is re-run once in the same run at the standard rung; a revision written after its previous revision's done also runs at standard. Never twice; not for blocked, STOP, a hold or a worker that did not start. |
| R-LADDER-5 | RUN.json records the last call's `model`, `vendor`, `effort` and `pr`; history keeps one line per attempt with `kind`; G5 counts `work`; `orche metrics` groups runs by model and effort. |
| R-LADDER-6 | TIER audit: with Jev enabled, `orche run` logs `tier-audit` when Jev disagrees with the TIER at ≥ 0.8 (`ladder.MIN_CONFIDENCE`); nothing else changes. |

---

## 5. Quality assurance

### 5.1 Verification ledger

File: `docs/LEDGER.tsv` per repo. CLI: `orche ledger record|check`.

Implementation note: the canonical ledger is `ORCHE_HOME/ledger/<proj>.tsv` (one per project); `<repo>/docs/LEDGER.tsv` is an export written by `orche ledger export`. `pr` accepts a number or `none`; `orche ledger check [--sha SHA] [--pr PR] [--min VERDICT]` (sha defaults to `git rev-parse HEAD`). The recorder refuses a verifier equal to the producer session.

Columns (tab-separated, header required):

| Column | Type | Notes |
|---|---|---|
| `pr` | int | PR number |
| `sha` | 40-hex | Head SHA verified |
| `verdict` | enum | See below |
| `evidence` | path/URL | Script output, screenshot, log |
| `verifier` | string | Session or script name; MUST differ from the writer session |
| `ts` | ISO-8601 | UTC |

Verdict enum, strongest first: `live-ui-verified` > `unit-test-verified` > `type-check-only`; failure states: `verifier-blocked`, `verifier-failed`.

| ID | Rule |
|---|---|
| R-LEDGER-1 | A new head SHA on a PR voids all prior rows for that PR. |
| R-LEDGER-2 | No row for (pr, current sha) ⇒ unverified. `orche ledger check --pr <pr> --sha <sha>` exits 2. |
| R-LEDGER-3 | The Stop hook blocks any "release-ready" or completion claim for a unit with a PR but no valid row. Implementation note: keyed by HEAD sha: `STATUS: done` needs a row of at least `type-check-only`; `STATUS: release-ready` needs at least `unit-test-verified` from a verifier other than the claimant. The claimant is the session's identity (§14.9) and HEAD is read in its worktree. The runner applies the same minimums to a run's final claim (R-DISPATCH-9). See §14.11. |
| R-LEDGER-4 | CI green alone is never a verdict. |
| R-LEDGER-5 | Build owns the ledger; feature agents may self-report, but only Build or an independent verifier writes accepted verdicts; the merge queue writes rows with verifier `queue`. |
| R-LEDGER-6 | Rows are append-only. |
| R-LEDGER-7 | Release ladder steps (§2.4) each require a row for the deployed SHA. |

### 5.2 Rubric and grader

- Each repo has a rubric file (`docs/RUBRIC.md`) at the top of the ledger process.
- The grader runs in a separate context (fresh session) with: diff + ACCEPT + VERIFY output + rubric. It never sees the writer's conversation.
- R-GRADE-1: Maximum 3 grader iterations per unit. After 3 failures, mark `verifier-failed` and send an urgent pointer to Main.
- `orche ledger grade --unit <unit> --rubric <file> --diff BASE..HEAD [--proj <p>]` writes the next grader package (the rubric, the diff stat and the changed files) under `ORCHE_HOME` and refuses a fourth iteration (exit 3); a person says it when a unit's diff should get a separate-context grade.

### 5.3 Verification as scripts

- R-VERIFY-1: A brief's VERIFY MUST name a committed script (`scripts/verify/*.sh`) or a verify-skill entry point.
- R-VERIFY-2: Ledger `evidence` points at that script's output.
- Re-verification costs 0 model tokens.

### 5.4 `verify-<app>` skill (one per app)

| Aspect | Rule |
|---|---|
| Creation | Build interviews the repo and generates `.claude/skills/verify-<app>/` with sections Launch / Doctor / Drive / Evidence / Cleanup + a feature map |
| Acceptance | Handed over only after proving one feature end-to-end |
| Drift check | Weekly via Orca automation |
| Output limit | At most 1 PR per drift check |
| Prohibition | MUST NOT edit product code |
| Failure | If Cleanup deletes evidence, the run fails |

Implementation note: the drift check runs in the janitor's weekly pass (§11.4), not an Orca automation. A project whose repository holds `.claude/skills/verify-<app>/SKILL.md` gets one drift unit to Build per week (one app per week in turn), relayed with Main as the sender and the janitor as the relayer (the way triage relays, `janitor/weekly.DRIFT`, at most 20 lines): GOAL `Re-verify the verify-<app> feature map against the current app`; SCOPE the skill's files; ACCEPT one live pass, map corrections only, product regressions reported, not papered over; VERIFY the skill's own Doctor and Drive steps; TIMEBOX 60 min; TIER mechanical; REPORT at most 15 lines. At most one drift unit per project is open at a time: while the last one (`weekly.drift.<p>` in the janitor's state) is not done, failed or stopped, none is filed. No skill, no unit.

### 5.5 Decision log

- File: `DECISIONS.tsv` (project and per-feature). Writer: `orche decide <phase> <decision> <why> <evidence> <result>`, the only writer (cells single-line; cells starting with `= + - @` are prefixed with `'`).
- Columns: `ts, phase, decision, why, evidence, result`. Append-only; wrong rows are superseded by a new row, never edited.
- After long or overnight runs: audit the log against the transcript; a fresh-context reviewer writes an "Attention" section listing anything suspicious.

### 5.6 Writer ≠ judge

| ID | Rule |
|---|---|
| R-JUDGE-1 | The judge always runs in a fresh session and receives only diff + ACCEPT + VERIFY results. Independence comes from context, not from model family. |
| R-JUDGE-2 | Claude models only. Normal units: fresh-session `/code-review` (Opus 5.5 high), `/simplify`, `security-review`. |
| R-JUDGE-3 | High-risk units (payments, auth, migrations, deploy scripts, wallets/keys): Fable 5.1 judge. |
| R-JUDGE-4 | Scripted verification runs before any model judge. |
| R-JUDGE-5 | Findings are classified Act on / Consider / Noted / Dismissed; nit inflation is filtered. |

### 5.7 Ten principles

Agents state which principles they applied and which decision changed. Unnamed appeals ("best practice says…") are forbidden.

1. Prove with evidence.
2. Fix the root cause.
3. Split into verifiable units.
4. Subtract before adding.
5. Make the smallest change.
6. Shape the data first.
7. Validate at boundaries; trust the interior.
8. Separate shared state first.
9. Protect the context.
10. Encode lessons in structure.

### 5.8 Always-pause list

Require explicit user approval: force-push to a shared branch · deploy · data deletion · customer messages · external publication · payments. All other reversible actions proceed without asking. "No" is a valid answer.

Exception: Ops may execute a rollback defined in a pre-approved runbook.

### 5.9 Destructive-command safety net

ORCHE registers no third-party approval hook: its own gates (`env-read-gate`, `scope-gate`) and each brief's FORBID lines are the safety net, because an `ask` dialog stalls an unattended pane; `orche doctor` WARNs when gstack's careful hook (`check-careful.sh`) is still registered in `settings.json`.

### 5.10 `/correct`

Part of HR weekly audit. A mistake seen twice is promoted to enforcement, preferring in order: architecture > types/lint > tests > hooks. Map of rule → enforcement lives in `~/.claude/orche/rules.md`. A rule without enforcement that recurs is marked `repeat`.

#### 5.10.1 Mistake classes and the Enforcement table

`orche hr audit <p> [--days N] [--apply]` (`lib/orche/hr/mistakes.py`; without a project the weekly HR report carries the same section for every project) mines the last N days (default 30) of three durable records:

| Source | Where it is read | One event |
|---|---|---|
| Head-review returns | `ORCHE_HOME/ledger/<p>.review.tsv` rows `kind build`, `verdict return`, and each row's evidence file | one checklist line `- N: why`, except 6, 7 and 8 (overlap, risk and size route a PR, they are not mistakes); the unit is the evidence file's `unit:` line |
| Queue returns | `ORCHE_HOME/queue/<p>/log.jsonl` `returned` events (they now carry the failed `step`) | `queue return <reason> <step>` |
| Reverts | `git log origin/<base>` (no fetch): a subject starting with `Revert` or a body saying `This reverts commit` | the body's reason line, else `revert of a landed pr`; a `#N` naming a PR the queue merged gives the unit |

A finding's class key is its first line, lowercased, without paths, numbers and quotes, cut to 6 words. A class is reported when units that differ share its key (seen twice), at most 8, most frequent first, each with its units and dates, a proposed level and one sentence why, and a draft standing-order line. The level is the first that the key's words point at, in this order: a structural change (type, lint rule, build step), a test, a hook or gate, a standing order (docs last).

`--apply` keeps one section `## Enforcement (proposed by the audit)` in the project's STANDING.md: a table `| class | seen | proposed level | status |`, one row per class (a new row is `proposed`; a known row keeps its status while its count and level follow the audit). This table is the rule-to-enforcement map named above; `~/.claude/orche/rules.md` is not used. The numbered standing orders are never touched; `orche hr correct` adds its line above the section. A project with no class prints `no mistake classes in the last N days`.

### 5.11 TDD when cheap

Failing minimal test → fix → pass. If impractical, use the nearest executable verification.

---

### 5.12 Head review

| ID | Rule |
|---|---|
| R-REVIEW-7 | After every publish (and after a requested pre-review's PASS or skip; a pre row with another verdict leaves the head to that path) and with `review.build` on, a headless head review (the writer's head (Build for PR entries): its persona and model at the standard rung, fresh, the eight-line checklist) decides: PASS → the runner queues the entry (its parent the brief's `parent:` unit) and posts quietly; RETURN or error → a waking pointer to the head's pane with both verdicts. The reviewer is read-only exactly as R-REVIEW-3 (`review.call_for`: Claude sealed with Read, Glob, Grep; Codex `-s read-only`, no writable root, no network) in a temporary detached worktree at the PR head, `git status --porcelain` compared before and after (a change is `error`); every package value is masked and the facts, brief fields, pre-review and diff are fenced as data; the evidence file and every pointer are masked. The answer's first non-blank line is a lone `VERDICT: PASS` or `VERDICT: RETURN`, else `error`; a `- N: why` line after PASS makes it RETURN. |
| R-REVIEW-10 | The head review runs once per (PR, head) (`reviews/<proj>/pr<N>-<h7>.lock`, a `build` row in the review ledger, evidence `pr<N>-<h7>-build.md`). Facts decide alone, without a model call: `[skip ci]` on code (line 4), files outside SCOPE (3), an open PR touching the same files (6, "wait for PR #N"), a high-risk path (7) or a diff over `review.pane_lines` or TIER hard (8); lines 7 and 8 go to the pane. A queue refusal wakes the pane too. Before the call: G4 (the unit's and the project's spend in W; over a cap → a `skipped` row and the pane decides) and the usage hold of the reviewer's vendor (held → no call, no row, one quiet `held: <vendor> usage limit until HH:MM; this unit runs then (stage build-review)`; a limit hit during the call holds the vendor and defers the same way). Every call is a history line `kind: build-review` (vendor, model, cost; §7.3's reserve without a cost figure). |
| R-REVIEW-11 | Triggers: the run's publish success path calls `review.build.after_publish(project, pr, unit)` once (detached `orche review --pr N --head --unit U`: the head review alone; inside a sandbox it is left to the heartbeat; never raises); when it returns true (a head review decides) the run files its done quiet and RUN.json and the pointer carry `review.build: {"verdict": "pending"}`. The heartbeat's `review` step reads `review.build.pending(project)` — those runs (RUN.json as new as BRIEF.md, status done) with no build row at their head, never a pane unit's (the pane reviews) — and starts at most one per project a pass, with its unit, after `claim()`: the PR open at that head (`gh pr view --json state,headRefOid`; closed or moved → dropped quietly), the reviewer's vendor not held (not counted), fewer than 3 starts (`build.MAX_STARTS`) (then one waking pointer to the writer's head (Build for PR entries) and no more starts; `reviews/<proj>/starts.json`). `orche review --pr N [--project P] [--unit U] [--background]` (the request, R-REVIEW-1, R33): one pre-review round at the PR's head, whatever the diff looks like (the writer's worktree at that head, else a temporary detached worktree; a `pre` row, so `orche queue add --pr N` finds it; a pass, skipped or return row there is not reviewed again), then on PASS or skipped the head review; a pre-review RETURN or error exits 3 with the findings; `--background` runs it detached and tells the caller the pre-review verdict (waking, `via: review`). |

### 5.13 Review depth

`lib/orche/review/__init__.py` (`deep`, `proves`), `lib/orche/review/pre.py` (`pair`, the panel, `group`),
`lib/orche/queue/__init__.py` (admission) and `lib/orche/queue/integrator.py` (the live rule).

| ID | Rule |
|---|---|
| R-REVIEW-12 | A user-visible change lands only on a live verdict. The head review records `surface: user` when the change alters what a user sees or does (a screen, a flow, copy users read, an API response a client renders, CLI output users read), else `surface: internal` (the default): the headless review from its answer's `SURFACE: user\|internal` line (`templates/review/build.md`; a `user` line wins, none means internal), the pane with `orche queue add --surface user\|internal` (default: the head review record's, else internal). The queue entry carries it (`surface`). After a green gate that ran `scripts/verify/ci.sh`, the integrator lands a `surface: user` entry only when the script's `verdict:` level is `live-ui-verified` (the queue is the verifier, never the writer); a lower level returns it as a red gate is returned (a unit: an AMEND to its worker from `templates/queue/live.md`; a PR alone: a failed note to the writer's head; log reason `live`) with the reason `user-visible change: live verification required (the check script's verdict was <level>)`. A gate without the check script (VERIFY scripts alone) has no level: the entry lands as before. |
| R-REVIEW-13 | The panel is the shape of a requested review (R-REVIEW-1), not a trigger: no deep gating, any head. The panel (`pre.pair`): the first model of `review.order` that is up (its vendor not held, §8.5; Codex installed and starting) and the first up model of the other vendor read the same package at once, the first in this thread (the round's tag), the second in a thread beside it under its own tag, so neither's sweep reaches the other; both calls are history lines (R-REVIEW-8). The findings merge by agreement (`pre.group`): `[<level>] both: <text>` first (the same file with overlapping lines, `path:12` or `path:12-20`, or the same normalised text; the stronger level, in its reviewer's words), then `[consider] one (<model>): <text>` (a `[noted]` stays noted); only a `both` `[act]` makes the round RETURN (R-REVIEW-5). The row's model is `<first>+<second>` (the writer's vendor only when both are), its note `panel`. One vendor up: the single review runs (R-REVIEW-2), its note starting `panel: only one vendor up (<why>): a single review` (`review.order names one vendor`, `codex not installed`, `codex does not start` or `<vendor> held: usage limit until HH:MM`); one half without a verdict: the other's review stands alone (`panel; <model> gave no verdict`, or `panel; fallback: same vendor (<model> gave no verdict)` when the one left is the writer's vendor); neither, or a changed tree: `error`. The head review reads the grouped report: a `both` defect is a RETURN unless the pane, once the rounds are spent, queues it with `--accept "<why not>"`. A panel costs two reviews. |
| R-REVIEW-14 | A deep change names the one fact it is safe because of, and a test in the PR proves it. Deep (`review.deep`): a changed path of line 7's high risk (`review.RISK`) or more than `review.deep_lines` (400) added plus removed lines against origin/<base>; never a mechanical unit. Admission refuses a deep head without the fact: `<why>: name the one fact it is safe because of (--safe-because "<fact>"); a test in the PR must exercise it` (`<why>`: `high risk (<paths>)` or `size (+A -R)`; lines 7 and 8 already send most deep heads to the pane, and a refused auto-queue wakes it). The pane names it with `orche queue add --safe-because "<fact>"`, kept in its record as `safe_because`. A named fact, deep or not, needs added lines in a test of the PR (`review.proves`: a path under `tests/` or `__tests__/`, or named `*.test.*`, `*_test.*` or `*.spec.*`), else admission refuses: `no test in the PR exercises the fact it is safe because of; RETURN it: add a test that proves: <fact>`, and the pane RETURNs the unit with that line. `--accept` waives neither. |

- **Head review record.** `ORCHE_HOME/reviews/<p>/pr<N>-<h7>-build.md` (the headless head review,
  R-REVIEW-10: `verdict`, `model`, `reviewer`, `pre-review`, `surface`, then `## Facts` and `## Answer`) and
  `pr<N>-<h7>-pane.md` (the pane's PASS, written by `orche queue add` with a `build` review row, verdict pass,
  reviewer `<session> (pane)`: `verdict`, `reviewer`, `surface`, `safe_because`, `accept`). A record's fields are
  its lines above the first `## ` section; `review.build.record(project, pr, sha)` reads `surface` and
  `safe_because`, the pane's record over the headless one's.
- **Config.** `review.deep_lines` (400, formerly `review.panel_min_lines`): the added plus
  removed lines against origin/<base> above which a change is deep (R-REVIEW-14), counted as for
  `review.pane_lines`. The review table is `order` (the panel's two vendors and the fallback order), `rounds`
  (RETURNs per revision counted at admission), `build` (the head review after every publish), `pane_lines` and
  `deep_lines`; `review.cross_vendor` and its modes are gone.

### 5.14 Team repositories

`lib/orche/detect.py` (`team`), `lib/orche/registry.py` (`team`), `lib/orche/hr/panes.py` (`plugin_install_path`,
`session_env`), `lib/orche/headless.py` (`gates`, `base_env`), `lib/orche/runs/job.py` (`team_rules`, the done),
`lib/orche/cmd_report.py`, `lib/orche/cmd_queue.py` (`_team_publish`) and `lib/orche/queue/integrator.py`.

| ID | Rule |
|---|---|
| R-TEAM-1 | A repository that declares a team plugin in its committed `.claude/settings.json` (`enabledPlugins: {"<id>": true}` plus its marketplace under `extraKnownMarketplaces`) is a team repository; that declaration is the team's own definition, and ORCHE reads nothing else. `detect.team(root)` is the first key `<name>@<marketplace>` (sorted) whose value is true and whose marketplace is a key of `extraKnownMarketplaces` in the same file; a plugin from a marketplace the file does not declare (such as an ordinary plugin from the official marketplace) is never a team plugin, and an unreadable or invalid file, or none, is no team. `orche init`, `orche init --refresh` and so `orche update` keep it on the project's registry entry as `team: "<id>"`, saved only when it changes and removed when the repository no longer declares one; `registry.team(reg, key)` is the one read site. The plugin counts as installed for this user when `<claude config dir>/plugins/installed_plugins.json` (`CLAUDE_CONFIG_DIR`, else `~/.claude`) has `plugins[<id>]` (a dict or a list of dicts) with `scope: user` and an `installPath` that is a directory (`panes.plugin_install_path`). |
| R-TEAM-2 | Owner-word mode. In a team project nothing is pushed, opened or merged by itself. A gated done (`orche report done`, a headless run) publishes nothing and starts no head review: it is filed with `pr: none` (RUN.json `pr` null) and the summary line `ready: say "push <unit>" (<branch>)` (`publish.ready_line`), the ledger row still required; Build hears it as any done. `orche queue add <unit>`, run by Main on the owner's "push" (Build or a head is refused), publishes first when the unit's branch has no open PR at HEAD, exactly as a done would (`cmd_report._publish`: the PR title and body, the issue key from the worker's name), starts the head review (`review.build.after_publish`), then admits as today; a publish gap prints its text (and OWNER step) and exits 3. `queue.land_mode` is `owner` whatever the config says, so the entry waits for `orche queue approve` (the owner's "merge"). The integrator skips the live rule (R-REVIEW-12): the team's finish step shows the change on the issue before Main says ready. |
| R-TEAM-3 | The team's rules and guard reach ORCHE's headless workers, which load no user settings (`--setting-sources project`). When the plugin is installed for this user, a worker's prompt (Claude or Codex) carries `## Team rules` with `<installPath>/hooks/team-rules.md`, read at call time (absent: nothing), after the context block and before `## Runner`, and `headless.gates` adds the plugin's `hooks/hooks.json` PreToolUse entries (`${CLAUDE_PLUGIN_ROOT}` replaced by the install path) after ORCHE's own; only PreToolUse, and an unreadable plugin hooks.json leaves ORCHE's gates alone. Not installed: nothing is injected, and SETUP.md and the doctor say so. Config `team.env` pairs are set in every ORCHE session: `panes.session_env` adds each `NAME=value` (shell-quoted) after `ORCHE_SESSION`, `headless.base_env` after PATH. Main and division panes are interactive sessions and load the plugin natively. |

- **Registry field.** `projects.<p>.team`: optional non-empty string, the team plugin id (R-TEAM-1).
- **Config.** `team.env` (default `{}`): environment names and values added to every ORCHE
  session, panes and headless runs alike (the team's hook switches, such as the ones that keep its decision log
  and auto-commit hooks out of ORCHE's sessions); Main writes them on the owner's words. A name starting with
  `ORCHE_`, `ORCA_`, `CLAUDE` or `ANTHROPIC`, or one that is not a variable name, is dropped at load with one
  `config` log line.
- **Setup report and doctor.** "Running now" opens, for a team project, with `- Team: plugin <id>
  (installed for you: yes | no, say "install the team plugin"). Push, pull request and merge wait for your word:
  "push <unit>" and "merge <PR>"; the team's rules reach every ORCHE session.`; the detected-keys line lists `team`.
  The doctor's `team` row per team project: `OK <p>: plugin <id> installed, owner-word mode` or `WARN <p>: plugin
  <id> is not installed for this user: say "install the team plugin"`.

## 6. Token economy

### 6.1 Levers (ordered by effect)

| # | Lever | Change | Expected effect |
|---|---|---|---|
| 1 | Unit-end `/clear` + file memory | Clear instead of compact; keep session only with uncommitted work or running process | Worker context 243K→120K; Main 518K→200K |
| 2 | Hook relay + drain discipline | Relay costs 0 Main turns; a completion costs one Main turn (the wake) by design; batched notifications | Main turns per request 8–12 → 3–4 |
| 3 | TIER routing | mechanical → Sonnet/medium; max only for hard | Fewer Opus-high calls |
| 4 | Main never reads results | Long work in divisions; review as verification units | Main context protected |
| 5 | Verification as scripts | 0-token re-verification | Less rework and re-asking |
| 6 | Model per role | Research/Liaison/HR Sonnet; runner Haiku; Fable only for Main | Fewer Fable calls |
| 7 | Jev gates (§7) | Routine messages, urgency | Routine 13–15% handled with 0 Main turns |
| 8 | Cache-friendly ordering | STANDING + persona as prefix; brief last. Evaluate `subagentPromptCacheTtl: 1h` after one week of measurement | Fewer cache writes |
| 9 | Parallel width | Concurrent exec 3; awake sessions 12; in-flight workers ≈10 rolling | Prevents runaway |
| 10 | Visibility | Status line, `orche`, sidebar | 0 lookup tokens |
| 11 | Large one-off jobs as Workflow scripts | Fan-out and collection in a Claude Code Workflow script; Main receives final result only | Main context protected |

Implementation note: for headless unit workers, lever 3 routes effort only; the model stays the target's registry model (§4.8 note).

Parallelism note: fan-out multiplies tokens. Use it only for truly independent large tasks.

### 6.2 Weekly metrics (`orche metrics`, no model)

Computed by script from transcripts and registry:

| Metric | Purpose |
|---|---|
| Main turns/day; wake cause (user vs notification) | Levers 2, 7 |
| Context per response, by role | Levers 1, 4 |
| List-price-equivalent cost by division/feature | Find leaks |
| Ledger coverage (release-ready with a row) and verdict distribution | G1 |
| Instruction repetition rate (same instruction reappearing across sessions) | STANDING effect |
| Questions asked; default-applied rate | Question discipline |
| Routine-message auto-handling rate; Jev confidence distribution | §7 |
| Janitor reclaimed/archived volume; secret request turnaround | §10, §11 |
| Runner: units run, cost, median cost per unit, failures, summed from the registered projects' `RUN.json` records whose `finished` is in the window | R-DISPATCH-9 |

### 6.3 Expected effect

Anonymized simulation: 30 days of one deployment's real logs replayed under ORCHE rules.

| Scenario | Main triggers/day | Cost change |
|---|---|---|
| Current | 106 | — |
| Conservative | 52 | −49% |
| Expected | 41 | −68% |

Assumptions (conservative / expected): Main context 518K → 300K / 200K; responses per trigger 2.98 → 2.5 / 2.0; 6 / 4 drains per day with 25% / 15% urgent; routine messages removed 70% / 100%; worker context 243K → 160K / 120K; 15% / 30% of units mechanical on Sonnet; worker response count 0% / −5%. Excluded: Sonnet/Haiku/Jev additions and one-time ledger/verify-skill setup.

Caveats (MUST be reported with any savings figure):
1. Token reduction correlates only weakly with cost reduction (published work reports r≈0.15; aggressive compression can lower patch success). Extra summarization or re-exploration offsets part of the savings.
2. Track savings together with ledger pass rate. Confirm with the real bill during the pilot, and measure the `/clear` effect separately from wake-up removal.

---

## 7. Jev integration (typed-decision model)

Jev is an optional fast model for typed decisions (routing, scoring, true/false). It is never used for code.

### 7.1 Vendor facts (as of 2026-10-16; verify before relying)

| Fact | Value / caveat |
|---|---|
| Price | Input $0.042 / 1M tokens; output free |
| Latency | 70–500 ms (vendor-measured) |
| Modality | Text only |
| Limits | 64K per request; 32K per question |
| Primitives | Choice (≤255 options), Score (2–10), Noul (0–1); parallel questions per request |
| Versions | `jev-latest` → 1.13.0; pin a version |
| Access | Vercel AI Gateway (`typesafe-ai/jev`) or direct key (console.typesafe.ai); SDKs on PyPI/npm 0.7.x |
| Data | Vendor states inputs are not used for training; retention period unstated; ZDR enterprise-only; US-hosted |
| Reliability | No SLA; 5 incidents in 4 weeks (~99.8% availability) |
| Evaluation | No independent evaluation published |
| Vendor guidance | Not a replacement for coding-agent LLMs; intended for routing, grading, true/false |

### 7.2 Placements

| # | Placement (hook) | Input | Questions | Action | Replaces | Phase |
|---|---|---|---|---|---|---|
| ① | Drain urgency (report injection) | Pointer summary + Main's pending work | Noul "Main must act now", Noul "user must see now", Choice affected division | Urgent ⇒ inject now; else hold until a drain trigger (held notes only, R-INBOX-3) | Main wake-ups | 5 |
| ② | Routine-message handling (UserPromptSubmit) | User message + in-flight units + "plan draft pending" flag | Choice {status, resume-nudge, confirm, revision, new request} | status ⇒ answer from STATUS + ledger (0 Main turns); resume ⇒ nudge stalled worker; confirm (≥0.9) ⇒ relay confirmed plan to Build | Main turns | 5 |
| ③ | Intake tag + risk flag (UserPromptSubmit) | User message + division/feature list + registry projects | Choice target unit/project, Noul needs plan, Noul hard to reverse, Score urgency | Inject 1 tag line; risk ≥0.7 ⇒ add Always-pause confirmation flag (block direction only) | Main classification | 5 |
| ④ | TIER suggestion (dispatch hook) | Brief text | Score complexity 1–5, Noul needs design/root cause | Fill empty TIER only | Wasted Opus-high | 5 |
| ⑤ | HR scope matching | Request + existing feature scopes | Choice best unit (+other), Noul new agent justified | other + justified ≥0.8 ⇒ create; else assign existing | HR turns | 7 (optional) |
| ⑥ | Card refresh gate | Commit messages since base commit | Noul user-visible change | None ⇒ skip Liaison call | Liaison turns | 7 (optional) |

Not placed (solved deterministically): brief completeness (field check), report quality (REPORT field check), question classification (rule), Liaison response gating (low frequency).

Implementation note: ⑥'s common case is deterministic — no commit outside PROJECT_CARD.md and `.orche/` since the card's base commit ⇒ no Liaison call (§14.14). The Jev gate is not built.

Implementation note: gate ④ is removed and was never built, with the two dispatch rules that went with it (an empty TIER filled by Jev; small units folding fields into one paragraph), which were dropped. The 2026-10-19 Jev tier study found a tier read from the prompt alone a weak predictor of a unit's cost, and Jev does not pick tiers or models: Main writes TIER on every brief.

Implementation note (setup prompts): division and model setup has no bypass of its own: a setup prompt naming no target reaches Main at stage 3, and Main asks with the choice dialog (R-MODEL-10).

Implementation note (triage gate):
- gate ② is generalised to every prompt typed into a Main pane: "does this need orchestration?" is decided in the UserPromptSubmit hook (`hooks/report_inject.py` → `lib/orche/triage.py`) before Main spends a turn. Off unless `triage.enabled` (default false; `orche triage enable|disable`); only a session identified (§14.9) as a project's Main, and only with `delivery: runner`; otherwise the hook is unchanged.
- Stages, each fail-open (error, timeout or low confidence → the prompt reaches Main unchanged, and the hook's `runs.reconcile` and inbox injection run as before): (1) tags — the whole first token (followed by whitespace or the end: `[build]s …` is no tag), case-insensitive: `[main]` → Main;
- `[build]|[research]|[rnd]|[gtm]|[ops]` → that division, `[direct]` → the classifier's division (Build when none), TIER `standard` unless the classifier says `mechanical` (no classifier call when a division tag's prompt names a target);
- `[drain]`, `/…`, `!…` → bypass;
- (2) bypasses (no model) — fewer than 12 characters, a question (`?`/`？` at the end, emphasis or a closing bracket allowed after it), or Main's last assistant text (read from the last 64 KB of `transcript_path`, 512 KB at most), with its trailing fenced blocks (Main's ```` ```dispatch ```` block, ```` ``` ```` or `~~~`, found by `util.fence_spans`: a closer has the opener's character and at least its length, and inside a `dispatch`/`amend`/`stop` block only a bare fence indented no deeper than its opener closes it, so a fenced snippet in an indented continuation line stays inside), `STATUS:` lines and blank lines stripped, ends with `?`/`？` (reason `reply-to-main`);
- (3) structure (no model) — a candidate names a target (a path `a/b.ext`, a file name with a known extension, or a quoted identifier) and is not a question;
- no word lists, so one rule holds in every language and intent is left to the classifier (until 0.10.1 English and Korean imperative, negation, continuation, question-word and setup lists ran here);
- (4) classifier (candidates only) — typed `{route: main|direct, division, tier, confidence 0..1, goal, accept, verify, scope: [paths]}` from `triage.backend`: `jev` (`jev.JevClient`, three Choice questions with every option described, confidence = Jev's probability for the route it chose, else the route answer's confidence, `jev.timeout_s` 1.0) or `claude-haiku` (a blank headless call, §14.10: `claude -p --model claude-haiku-5-5 --output-format json --max-turns 1 --setting-sources project --tools "" --strict-mcp-config --no-session-persistence --system-prompt <JSON-only instructions>` from a fresh directory outside `$HOME`, removed afterwards (Claude Code's upward CLAUDE.md discovery would load `~/.claude/CLAUDE.md` from any directory under `$HOME`); no `ORCHE_SESSION` and no Jev key; the masked prompt on stdin; its process group SIGKILLed after `triage.max_latency_s`, default 12; one probe on 2026-10-18 with claude 2.1.302 used 2 + 957 cache-creation input tokens, 138 output tokens, $0.00026, 2.9 s);
- `auto` (the default) is Jev when enabled and configured, else Haiku — a hook therefore uses Jev before the §7.3 condition-5 offline validation has run for the triage placement;
- run `orche triage eval` on labelled prompts before enabling Jev for triage;
- an explicit `jev` backend that is not configured makes no call and logs `backend=none`;
- direct only when `route == direct`, `confidence ≥ triage.min_confidence` (0.8), `tier != hard` and the division is one of the project's;
- (5) action — a brief (TO the division; GOAL = the classifier goal (≤ 500 chars), else the prompt when it has at most `GOAL_CHARS` (300) characters, else a `GOAL_EXCERPT` (200) character excerpt on one line (whitespace collapsed, cut at a word boundary, `…`) followed by "(excerpt; the full prompt is quoted in CONTEXT)"; SCOPE = the classifier scope, else the paths found at stage 3, each kept only when it is a plain path (`[A-Za-z0-9_./-]+`: no spaces, no shell characters), does not start with `-` or `/`, is relative, has no `..` segment and resolves (symlinks followed) inside the division's worktree (else the main checkout), else `.`; a classifier scope entry may also be a glob (`SCOPE_GLOB`: path characters plus `*`, `?` and `**` only, an entry with `[` or `]` is dropped; same rules, and its literal base directory before the first wildcard segment must resolve inside the worktree), meaning what it means in a feature scope (`units.scope_match`, where `[` is literal so `app/[slug]/**` admits only that directory); a glob never reaches VERIFY (refused) and the commit clause tells the worker never to `git add` one, which, like SCOPE, is an instruction, and the scope gate does not read a division brief's SCOPE (it enforces feature scopes and the worktree boundary only), so SCOPE is an instruction to the worker; CONTEXT "routed by triage from a Main prompt; the prompt is the whole instruction" when GOAL is the prompt verbatim, else "routed by triage from a Main prompt; the prompt, quoted below, is the whole instruction" followed by every prompt line as a `> ` continuation line (a blank line as `>`), so the brief always holds the whole prompt; ACCEPT (the classifier's, ≤ 300 chars, or the default) followed by "; `git add` only the files you changed, by name (never `-A`, `-u`, `.` or a glob); exactly one commit with a conventional message", VERIFY = the classifier's only when it is one line ≤ 200 chars starting with `ls`, `test`, `git status` or `git diff` with no shell syntax or wildcards (`;`, `&`, `|`, `<`, `>`, `$`, backticks, backslashes, `*`, `?`, `[`, `]`), and, for `git`, only plain paths and the options `--short`, `--stat`, `--name-only`, `--name-status`, `--cached`, `--porcelain` and `--` (so no `--output`, `-o`, `--ext-diff`, `--textconv`, `-c` or `--config`, which write files or run programs), i.e. runnable with the mechanical tool list, else `ls -l <scope files>` (plain paths only) when that passes the same check, else `git status --short` (also with no files), TIMEBOX 30m, FORBID "push, amend, --no-verify, files outside SCOPE", REPORT standard, TIER) relayed by `dispatch.relay_detail(…, by="triage")` exactly as a Main dispatch (brief, pointer, detached `orche run` spawn, events `by=triage`, feedback pointer "routed <unit> → <to> (runner-started)");
- when the runner started, the hook prints `{"decision": "block", "reason": "[orche] routed to <to> as <unit> (runner-started, TIER <t>): \"<the GOAL's first 60 characters, masked, `…` when cut>\". Results arrive in this inbox; prefix the prompt with [main] to talk to Main instead (to cancel: [main] STOP <unit>)."}`, touches Main's activity (`units.touch_activity`, as `inbox.handle_prompt` does on every other Main prompt, so idle counters stay current) and Main spends no turn.
- Once the runner started the block is final: recording the decision (events line, samples row) and the sender's feedback pointer are best-effort and never change it. A routed Build unit commits (R-DISPATCH-9 mechanical tools), so the shared Build worktree stays clean, and its pointer reads `info` "unverified: STATUS: done (no ledger row for <sha7>)" until a verifier records the row. Any other outcome (a refusal, a runner that did not start) does not block: the prompt reaches Main with "[orche] triage could not route this prompt: <reason>" injected.
- Budget: the hook may exceed `orca.hook_deadline` (3 s) only while a classifier call is in flight; the inbox part gets the full budget less the other triage time, which is under 20 ms. Every decision logs `triage session=<s> decision=main|direct|bypass|tag stage=<n> conf=<c> backend=<b> ms=<n> unit=<u>` to `events` and appends a row (prompt SHA-256 prefix, length, decision, stage, reason, confidence, backend, latency, division, TIER, unit, cost) to `ORCHE_HOME/triage/samples.jsonl` (mode 0600); the masked prompt only with `triage.keep_prompts`.
- A routed prompt's unit id (row `unit`, events `unit=`) carries a slug of the prompt's first words, masked. `orche triage status` (backend, classifier p50/p95, last 20 decisions), `orche triage eval <file>` (labelled JSONL → precision/recall of `direct`) and the `orche metrics` Triage section read that file.
- `orche triage eval` (not `--dry-run`) records its summary per backend in `ORCHE_HOME/triage/eval.json` (`{"last": <backend>, "runs": {<backend>: {at, file (absolute), n, tp, fp, fn, tn, precision, recall, classifier_calls, p50_ms}}}`, never prompts); `orche triage status` shows the last run, its labelled file and whether that file still exists, "none on file for the <backend> backend" when the resolved backend has no run, and, when triage is enabled on Jev with no Jev run, a warning naming §7.3 condition 5, which `orche triage enable` repeats; `orche status` prints one triage line (backend, last eval, the warning) while triage is enabled and nothing otherwise (its `--json` shape is unchanged).

### 7.3 Adoption conditions (all MUST hold)

1. Masking before send: keys, seeds, addresses, emails masked; code, diffs, and customer data are never sent. Same regex library as the pre-commit secret scan.
2. Version pinned; thresholds stored per version.
3. 1-second timeout, fail-open (on error or timeout, behave as if Jev is absent and route to Main).
4. Never decides irreversible actions; gate ③ may only add a confirmation, never remove one.
5. Offline validation first: ≥250 logged samples (e.g., 150 user messages + 100 reports), masked, labeled by Opus 5.5, accuracy and calibration measured per placement. Hooks are connected only after passing.

Implementation note: Jev is off by default (`jev.enabled: false`; `orche jev enable|disable`). `orche jev eval extract|run|report` works without `enabled`, so validation can run before any hook uses Jev; a separate Claude session writes `labels.jsonl` (its format is in `orche jev eval --help`). The client replaces code, diffs and blobs with `[code omitted]` and passes every string through `mask.redact` before sending. Only placement ① (drain urgency, add-only) is wired in v0.1.0.

Implementation note (Jev onboarding and wire shapes):
- Jev stays optional; with no key, triage uses the subscription's Haiku and the other placements are simply skipped. **Key.** `lib/orche/jevkey.py` resolves the key as the env var named by `jev.api_key_env` (default `JEV_API_KEY`) → the stored key → none. Storage: on macOS the login keychain (generic password, service `orche`, account `JEV_API_KEY`), written through `security -i` with the command on stdin so the key is never in argv, looked up for status with `find-generic-password` without `-w` (the value is not read to say where it is); elsewhere `ORCHE_HOME/secrets/jev.key`, mode 0600.
- `orche jev key set` (macOS: a native hidden-answer dialog via `osascript … display dialog … with hidden answer`, the answer read from osascript's stdout inside the CLI process, 120 s timeout; the hidden terminal prompt when there is no GUI session — no `osascript`, not macOS, `SSH_CONNECTION` set, or osascript refused — or with `--no-dialog`; `--stdin` for scripts — stdin is read ONLY with that flag: with no dialog and no terminal, `key set` refuses with "no terminal: use the dialog, or --stdin from a trusted script", exit 1, and the dialog path never reads stdin), `orche jev key rm`, `orche jev key status` (env, keychain, file or none; never the value).
- Keys holding whitespace, `"` or `\` are refused. Every error string that could carry the key passes `secrets.store.scrub` (the key replaced by `<value>`, then `mask.redact`). The key is sent only to an `https` `jev.base_url` (else no call: "jev.base_url must be https"), as an unredirected header, through an opener whose redirect handler raises: a 3xx is a masked error (`HTTP302`), never followed. The env-read gate (`hooks/env_read_gate.py` → `lib/orche/jevgate.py`; Bash split into simple commands by `lib/orche/cmdscan.py`) works in four steps.
- (1) Prefilter: a command with none of `security`, `jev.`, `secrets/`, `secrets\`, `jevkey`, `JEV_API_KEY`, `--stdin`, `-stdin`, `.key(`, `orche`, `$'` and not the configured ORCHE_HOME path is allowed unscanned. (2) Raw text: the ORCHE_HOME-anchored key path (`~/.claude/orche/secrets/jev.key`, `$HOME/…`, `${HOME}/…`, `$ORCHE_HOME/secrets/jev.key`, any path ending `/orche/secrets/jev.key`, normalised for `//`, `/./`, `x/../` and backslashes) is refused anywhere, quoted, in a heredoc or a commit message.
- (3) Structure, on command words only, with wrappers skipped by basename (`timeout`/`gtimeout` + duration, `stdbuf`, `nice`, `nohup`, `time`, `command`, `exec -a`, `env -i -u X=1`, `caffeinate`, `sudo`, `doas`, `xargs`, `watch`, `script`, `unbuffer`, `chrt`, `ionice`, `setsid`, `flock` + file, `busybox`, `builtin`, `arch`): refused are `security find-generic-password|find-internet-password` with `-w`/`-g`, `dump-keychain -d`, `security -i`; a word, redirect target or cd/pushd-relative name for `secrets/jev.key` (normalised) or a glob in a secrets dir matching it — the pattern argument of `rg|grep|egrep|fgrep|ag|ack|git grep` is a pattern, not a path, so `rg secrets/jev.key lib/` passes; a read (`cat head tail less more xxd od strings base64 cp mv tee dd open source .`, or `< path`) of anything under `…/orche/secrets/` (`ls`, `stat`, `test`, `file`, `rm` of other files there pass); a search tool whose path is ORCHE_HOME (no glob, or a glob matching `jev.key`), the secrets dir or the key file; orche's stdin key flags (argparse prefixes too); and inline interpreter code (python, perl, ruby, node, php, osascript, awk: `-c`/`-e`/`--eval`/…, a heredoc, a here-string or text piped in; Python `#` comment lines stripped) checked as TEXT for the key path (relative too, or `secrets` + `jev.key`, or a glob under `orche/secrets/`), `find-*-password` with `-w`/`-g`, `dump-keychain -d`, `security -i`, `jevkey`, `.key()`, or `JEV_API_KEY` next to `environ|getenv|env|keychain`.
- Shell strings (`sh|bash|zsh|dash|ksh -c`, a heredoc or text piped into a shell, `eval`, `watch`, `script -c`, `flock -c`, `env -S`) are re-scanned as commands. Read/Edit/Write paths are normalised and refused for the key file and anything under the secrets dir; Grep as for the search tools, plus an ancestor of ORCHE_HOME with a glob matching `jev.key`. (4) Fail closed: past the nesting limits, on a RecursionError or any exception, a prefiltered command is refused with "[orche] command too complex to check for a key read; simplify it (no nested eval/sh -c)".
- Known gaps (best effort): a script file that reads the key; variable indirection (`s=security; $s …`); brace expansion (`{-w,}`); a glob in a directory component (`secre*/jev.key`); `tar`/`cp -r`/`find -exec` on the directory; flags fed by `xargs` from stdin; `source <(…)`; Read through a symlink; Grep over an ancestor of ORCHE_HOME without a glob.
- What holds: the key is never printed or logged by ORCHE; agents' normal read paths (environment, keychain value lookup, the key file, the stdin flags) are refused by the gate; inline interpreter code is checked as text, a script file that reads the key is not; the gate is not a sandbox, so the keychain's own access control and the OS remain the boundary.
- **Onboarding.** `orche install` on a terminal asks once (recorded as `jev_asked` in `install-state.json`; not asked when Jev is already configured, under `--dry-run` or `--json`): "Jev API key (optional, Enter to skip)" (the dialog on macOS), then "Key type: 1) Vercel AI Gateway (default) 2) TypeSafe direct" (skipped when `--jev-kind` is given); Enter, Cancel, EOF (Ctrl-D) or Ctrl-C at either question skips Jev ("Jev skipped …", install exit 0, `jev setup` exit 0 `skipped`, no traceback, nothing changed), and without a terminal nothing is asked and stdin is not read (install says nothing; `jev setup` prints "Jev setup skipped: no terminal …", exit 0).
- It sends ONE probe with the key held in memory (`jev.probe`: one Choice question with a fixed text, 10 s timeout, a few hundred input tokens ≈ $0.00001), and only when it answers stores the key and then writes `jev.base_url`/`jev.model` (`https://ai-gateway.vercel.sh/typesafe` + `jev`, or `https://api.typesafe.ai` + `jev-1.13.0`) with `jev.enabled` true ("Jev on (gateway, 0.4 s)"). Nothing changes unless the probe answered AND the key was stored: a failed probe or store prints the masked error, keeps the previously stored key, and leaves config.json as it was.
- A config write failing after the store puts config.json's bytes back and then the previous key (read into memory by the user's own orche process before the store) or removes the new one when there was none; if that rollback fails, the message says the new key is still stored and how to fix it. A config.json that is not valid JSON or not UTF-8 is checked before any question: "config.json unreadable (<reason>): Jev skipped, nothing changed; fix the file and run `orche jev setup`" (install exit 0, `jev setup` exit 1, no traceback). It never fails the install.
- `--no-jev` skips; `--jev-key-stdin [--jev-kind gateway|direct]` configures without prompts.
- `orche jev setup` runs the same flow later, `orche jev probe` re-checks (recording `ORCHE_HOME/jev-probe.json`: ok, ms, kind, masked error), and `orche doctor` shows `jev`: OK `<kind> (key: keychain|env|file)`, otherwise WARN, never FAIL (Jev is optional and fails open): `not configured (Haiku fallback)`, `jev.base_url must be https; …`, `disabled (<kind> configured, …)`, or ``last probe failed <masked reason>; ORCHE works without Jev; run `orche jev probe` `` (from the last probe of the configured kind; doctor makes no network call; `orche jev disable` forgets the record).
- `orche uninstall` keeps the stored key and says so (`orche jev key rm` deletes it). A Choice answer naming an option that was not asked is dropped.
- **Wire shapes** (checked 2026-10-18 against docs.typesafe.ai/api and vercel.com/docs/ai-gateway/sdks-and-apis/typesafe; the v0.1.0 guesses — a question list with `question`/`options`/`min`/`max` and an answer list with `id`/`value`/`confidence` — were wrong): `POST {base_url}/v1/systemone`, `Authorization: Bearer <key>`, body `{"model", "state", "questions": {id: {"type": "noul"|"choice"|"score", "instructions", "criteria"}}}` where Choice `criteria` maps each option to a description (`null` here) and Score `criteria` lists the level labels low to high; answers `{"answers": {id: …}}` with Noul `{"noul": p}` (no confidence: ORCHE uses max(p, 1 − p)), Choice `{"choice", "probabilities", "confidence"}`, Score `{"score": 0-based, "legend", "probabilities", "confidence"}` (ORCHE reports `min + score`, so urgency and complexity stay 1–5; the eval harness rounds urgency).
- The gateway's TypeSafe-compatible API takes the same body with model `typesafe-ai/jev`; its native `POST /v1/evaluate` (Boolean questions answering `probability`) is not used. The probe is the first live call; until it succeeds these shapes are documentation-verified only.

Calibration (open item for condition 5; no behaviour change yet): the three primitives report confidence on different scales. A Score `confidence` measures the spread of its level distribution (the vendor's example has a 0.57 peak with confidence 0.35); a Choice `confidence` is the vendor's own; a Noul has none, and ORCHE's max(p, 1 − p) is its own derivation, consistent with the vendor's advice to pick per-use cutoffs (their example: 0.8 / 0.2 with review in between) but uncalibrated. `_min_conf` takes the lowest of them across a request, so a request containing the urgency or complexity Score will often band as `defer`. Before Jev is enabled by default, the offline evaluation must band per question (or rescale each primitive against labels) and store the thresholds per version (condition 2). This still holds for the other placements: the urgency and complexity Scores still band low.

Implementation note (triage gate): the triage placement gates on the route question alone, using Jev's probability for the route it chose (else the route answer's confidence); the division and TIER answers never lower it. Every option is described to Jev (divisions and tiers with the Haiku prompt's words, the route with a short paraphrase), and `triage.min_confidence` defaults to 0.8. Live run on 84 labelled prompts (2026-10-19, Jev after 3d7fef5; 46 reach the classifier): P ≥ 0.95 precision 1.0, recall 0.71; ≥ 0.9 precision 0.968, recall 0.79; ≥ 0.8 precision 0.972, recall 0.92; ≥ 0.7 precision 0.974, recall 0.97. From 0.9 down the one false direct is always "Fix the flaky test in tests/test_sync.py" (none at 0.95). Before 3d7fef5 the same gate at 0.9 had recall 0.00–0.05; Haiku on the same set: precision 1.0, recall 0.31 at 0.9 and precision 1.0, recall 0.59 at 0.8. Checks: on 546 masked real Main prompts (120 days, this machine), the heuristics of that version (word lists, replaced by the structural rule in 0.10.1) passed 2 to the classifier and Jev routed none (no false directs). Asking Jev on every prompt without the heuristics was rejected (precision 0.63 at P ≥ 0.8). A Jev-answered status check was also rejected (placement ②: 2 of 8 flagged prompts were status questions).

### 7.4 Confidence bands

| Band | Action |
|---|---|
| ≥0.9 | Act |
| 0.6–0.9 | Act with confirmation (one-line echo/default) |
| <0.6 | Defer to Main |

The triage gate uses `triage.min_confidence` (default 0.8, chosen from the §7.3 v0.3.5 measurement) instead of this table; the structural candidate rule and the `tier != hard` guard are its other two filters.

### 7.5 Scope statement

Jev never touches code quality. Quality is decided by the ledger and scripts. A wrong Jev answer can cost a turn or a delay, never a bad merge.

Expected volume: ~120–150 calls/day, a few cents/day.

---

## 8. Visibility (0 tokens)

### 8.1 Status line

HR writes `~/.claude/orche/status/<project>.line`; the status line script appends it with `cat`.

Implementation note: `orche statusline` and every HR record (§14.12) write `ORCHE_HOME/status/<project>.line`. The snippet `orche install` adds to `~/.claude/statusline-command.sh` walks up from the session directory to the first `.orche/project` marker (one line: the project key, written by `orche init`) and prints the cached line using shell builtins only. Actual segment: `R&D● RS○ BUILD●3 GTM● OPS○ · new1 · 🔑2 · unverified1` (`◐` = onboarding; zero counters omitted; no `svc` cell yet). Implementation note: a `runN` cell (live `orche run` workers of the project) follows the division cells; `orche status` adds `RUNNING n` per project and `orche depts` a RUNNING column per session, both from live lock files.

Implementation note (setup): the segment shows one cell per division the project has (`divisions_of`), no placeholder for a standard division it never onboarded. A standard division's cell keeps its short name (`R&D RS BUILD GTM OPS`); a custom division's cell comes from its label's letters, upper case (the name's when the label has none): the first 3, else 4, else 5 letters, whichever is not yet taken by a standard cell or an earlier custom division of the project, else the 3-letter form plus the first free digit from 2 (`Design` → `DES○`, then `Designers` → `DESI○`; `G.T.M.` → `GTM2○`), so cells are unique within a project and never equal a standard cell. `orche status` prints `divisions: -` for a project with none.

Segment format:

```
R&D● Research○ Build●3 GTM● Ops○ · new1 · 🔑2 · unverified1 · svc●
```

| Token | Meaning |
|---|---|
| `●` / `○` | Division awake / dormant |
| `<cell>●N` | N active workers of that division (any division's cell, e.g. `R&D○ RS○ BUILD●4 DES●2 GTM○ · run6`) |
| `newN` | Feature agents created today |
| `🔑N` | Pending secret requests |
| `unverifiedN` | Release-ready units without a ledger row |
| `svc●/○` | Service health (only when stage = public) |

### 8.2 Orca sidebar mapping

Hierarchy: repo → worktree card → agents (terminal tabs). One card per role.

```
<project>
 ├─ main [default]          1 agent:  main-<project>
 ├─ div-rnd                 1 agent:  div-<project>-rnd
 ├─ div-build               1 agent:  div-<project>-build
 │   ├─ feat-<feature-a>    1 agent:  feat-<project>-<feature-a>
 │   └─ feat-<feature-b>    1 agent:  feat-<project>-<feature-b>
 ├─ div-gtm                 1 agent:  div-<project>-gtm
 └─ (div-research, div-ops) dormant: card only, 0 agents
```

| ID | Rule |
|---|---|
| R-VIS-1 | Card title = role name (set via `orca terminal rename`). |
| R-VIS-2 | Card one-line comment = first line of STATUS.md. |
| R-VIS-3 | Card status ∈ todo / in-progress / in-review / completed. |
| R-VIS-4 | Unread dot on creation, secret request, urgent report. |
| R-VIS-5 | Feature worktrees are Orca children (lineage) of the Build worktree. |
| R-VIS-6 | Session names: `main-<proj>`, `div-<proj>-<division>`, `feat-<proj>-<feature>`. Set with `claude --name` or `/rename`. |

Implementation note: worktrees are named `div-<division>` (no Orca parent) and `feat-<feature>` (Orca parent = the Build worktree); pane titles equal the session names. R-VIS-2 and R-VIS-3 (card comment and status) are not set by ORCHE yet; the unread dot is set when a feature agent is created. See `docs/orca-sidebar.md`.

### 8.3 macOS notifications

Events: feature agent created / retired; HR freeze / unfreeze; project public; secret request; urgent report; confirmation awaiting; Ops incident.

Implementation note: Orca's own notification covers Main's `OWNER:` / `DONE:` closing line (§8.8); the ask's reminders, HR, the Keymaster and the heartbeat's escalations notify through ORCHE's one path. The 0.6.2 notification per urgent pointer is removed.

### 8.4 `orche` CLI

| Command | Purpose |
|---|---|
| `orche projects` | List registered projects + aliases |
| `orche depts [project]` | Divisions and feature agents with state |
| `orche status` | All projects, one line each; `orche status <p>` prints that project's card (`--json`: an object). Implementation note: a project whose `orche_version` is missing or differs from the running version gets `  upgrade: <proj> rendered by <version \| an earlier version>, now <version> — run orche init <proj>` under its line (JSON `upgrade`). `orche doctor`: one `WARN upgrade` line per such project. |
| `orche hr log` / `freeze` / `unfreeze` | HR events and freeze control |
| `orche hr audit [<proj>] [--days N] [--apply]` | Implementation note: with a project, only its mistake classes of the last N days (default 30) and, with `--apply`, its Enforcement table in STANDING.md (§5.10.1); without one, the weekly HR report, which carries the same section for every project. |
| `orche hr add <proj> <division>` | Implementation note: add one division to an onboarded project (`--model`, `--label`/`--charter` for a custom one, `--wake`, `--no-intake`). Prints the recommendation, the `choices:` line and a `codex:` line when Codex is unavailable, then `created div-<proj>-<division> (dormant) on <model>`; the intake starts in the background. Exit 1 existing division or missing label/charter, 2 unknown project, 3 Codex unavailable or `--wake` on Codex. |
| `orche hr model <proj> <division> <model>` | Implementation note: record a division's model (`<session>: <old> → <new>` and how it switches; `is already on` when unchanged). An awake pane is never restarted; moving one to Codex is refused (exit 3, `sleep its pane first`). |
| `orche hr choices [<project>]` | Implementation note: the owner's setup questions as data (global `--json` for JSON), writing nothing, in English (Main asks them in the owner's language). Keys `project`, `stage`, `recommended_preset`, `kinds`, `custom`, `models`, `ask`; `ask` holds `where`, `add`, `change`, `model` (per kind, `*` for a new one), and for a project not under ORCHE yet `setup` and `pick`. Implementation note: for a project not under ORCHE yet, run in a folder outside any git repository, `ask.where` is two entries: `Where is the project?` (header `Project`; `Create a new repository in this folder (<folder>) (recommended)` → `{"new": true}`, `Use another folder (type its path)` → `{"path": "*"}`, the path the owner types) and `Should the new repository go to GitHub?` (header `GitHub`; `Private (recommended)`, `Public`, `Local only (no GitHub yet)` → `private`, `public`, `local`); inside a repository it is null. `ask.setup[0]` is `How will this project run?` (header `Preset`): the preset `detect.stage` recommends first and marked, `Build-focused` (build, design, rnd on Opus 5.5 and research on Sonnet 5.5; Build's workers specialise in front, back and infra) and `Operations` (build, design, rnd, research and gtm), then `Pick them`; a preset's value is a setup-file fragment `{"preset", "divisions", "custom": {"design": {}}}`. `ask.pick` lists the recommended preset's divisions. Each entry is `{tool: {question, header, multiSelect, options[{label, description}]}, values: {label: value}}`, or null under two options; at most 4 options, the recommended one first with `(recommended)`, `Keep` first (marked) when the current model is the recommendation, else last; Codex only when it runs (its state otherwise appended to the question). Retired and onboarding divisions get no model question. Unknown project: exit 2. Kinds and models come from config `kinds` and `models.catalog` (`lib/orche/hr/choices.py`). |
| `orche auth set` / `orche auth rm` / `orche auth status` | Implementation note: the Claude OAuth token headless runs use (`claude setup-token` makes it: one year, Pro/Max). `set` reads it from a hidden prompt, or one stdin line when stdin is not a terminal; a token on argv is refused without echo (exit 1); `--dry-run` reads and stores nothing. Stored like the Jev key (`lib/orche/jevkey.py`, account `CLAUDE_CODE_OAUTH_TOKEN`: the macOS login keychain, else `ORCHE_HOME/secrets/oauth.token`, mode 0600). `status` (and the `orche doctor` row `oauth token`, OK or WARN, never FAIL) names the source (`env`, `keychain`, `file`, not set), never the value. `rm` deletes it. See §14.10. |
| `orche doctor` | One `OK`/`WARN`/`FAIL` row per check (exit 1 only on a FAIL). Implementation note: the human output ends with a blank line and `Next steps (optional, any time):` followed by `guide.lines` (the setup report's "Any time" list); `--json` is `{"checks": [...], "next_steps": [...]}`. The block never changes the exit code. |
| `orche sleep <unit>` / `orche wake <unit>` | Sleep/wake a role session. Continued below the table. |
| `orche init [repo]` | Onboard a project (divisions, worktrees, the `main-<proj>` pane, registry entry, intake). Continued below the table. |
| `orche init` check-script reason | The `<reason>` in `waiting; the check script could not be pushed (<reason>)` is the failed push's first output line naming an error, a failure, a refusal or a rejection (case-insensitive), else its last non-empty line, masked (`verifygate.reason`), so a pre-push hook's progress line (`INF 1 commits scanned.`) is never the reason. The setup report's "Any time" list carries only this project's merge-queue line (`guide.lines(..., project)`); `orche doctor`'s next steps keep every project's. |
| `orche card <project> [--path]` | Print PROJECT_CARD.md (or its path) |
| `orche card refresh <project> [--background]` | Refresh the card from what landed since its base commit; no model call when nothing did (§14.14) |
| `orche ask <project> "<question>"` | One headless read-only answer about a project, on stdout as data (§14.14) |
| `orche ledger record ...` / `orche ledger check --pr <pr> --sha <sha>` | Ledger write/check (check exits 2 if unverified) |
| `orche secret request|wait|fill|ls|missing|run|render|rotate|share|revoke|add|redact-transcripts` | Keymaster (§10) |
| `orche dev add|remove` | Developer IAM access (§10) |
| `orche janitor run|undo|restore` / `orche discard <unit>` | Janitor (§11) |
| `orche janitor run --weekly` | Implementation note: run the weekly pass now (§11.4) |
| `orche metrics` | Weekly metrics (§6.2) |
| `orche run <unit>` | Run one unit headless (`claude -p`) in its target's worktree; `orche dispatch` starts it for every new division/feature brief, by hand it re-runs a unit; `--dry-run` prints the exact `claude` argv (R-DISPATCH-9) |
| `orche deliver <unit>` | Deliver a unit's current revision now (runner: start it unless live or already run; pane: type its nudge); the recovery command `orche status` names (R-INBOX-6) |
| `orche nudge <session>` | Type `[orche] inbox: new reports` into an idle pane with unread reports (R-INBOX-4) |
| `orche update [--dry-run]` | Pull the newest ORCHE (fast-forward only), reinstall it and refresh every project; a development checkout is not pulled, the install is aligned with it (R-PLAIN-2) |

**`orche sleep <unit>` / `orche wake <unit>`, continued.**

- Implementation note: `wake` (and `feat wake`) on an `active` unit with no Orca pane titled with its session opens a pane that resumes it (`claude --permission-mode bypassPermissions --resume`), prints "<session> pane reopened" and writes an hr-log line; with a pane it prints "already active".
- `orche wake main-<proj>` opens Main in the main checkout when Orca shows no pane with that title, resuming its newest `main-<proj>` transcript like a woken unit (`orche init` alone starts Main fresh), and posts one urgent `info` from orche (`resumed: say in five lines what was done last, what runs now and what waits on the owner`); a Main created by `orche init` gets its setup note from init instead (`cmd_init` posts it after the switch; see `orche init`); the closed-Main repair and a disconnected-pane reopen do the same and otherwise switches Orca to it (`terminal switch`); no awake limit, stage gate or onboarding check applies.
- Every ORCHE pane starts `claude --permission-mode bypassPermissions` with no autocompact window (the installer's 50 % applies to the model's window; the SessionStart hook re-injects the role text and, for Main, the project card on `compact`) (then `--resume <session id>` for a woken unit or a reopened Main, or `--name <session>` for a never-started unit; Main since 0.6.0: a woken Main must not stop at a prompt nobody watches).
- Implementation note: a woken unit resumes by session id, not by name: `--resume <name>` matches the transcript title, so a name shared by an earlier session of the same unit opens the interactive picker and a cleaned-up transcript fails, and either way the unattended pane never starts.
- ORCHE reads the transcripts in Claude Code's project dir for the unit's worktree (`$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`) and resumes the newest one whose `custom-title`/`agent-name` record equals the session name (`--resume <id>`); none named, or no project dir → `--name <session>`, a fresh session, with the stderr note `orche: no transcript named <session>; the pane starts a fresh session` (`liaison-<proj>` is no unit since 0.5.0: exit 2).

**`orche init [repo]`, continued.**

- The repo is optional: without it, the project is the git repository holding the current directory (`git rev-parse --show-toplevel`; outside one, exit 1 "not inside a git repository: pass the project path (orche init <path>)"; a linked worktree is refused as below).
- When no registry project or Orca repo matches and the argument is a directory holding `.git` (a directory, or a submodule's `.git` file), init runs `orca repo add --path <realpath>` once the checklist is confirmed, right before its first write, and re-reads `orca repo list` (exit 1 "added <path> to Orca but orca repo list failed: <error>" or "… does not show it"); an aborted checklist leaves Orca untouched, dry-run prints `would add <path> to Orca (orca repo add --path <path>)` and adds nothing, `--json` reports `added_to_orca`, and when Orca names the repo differently from the folder init prints `registered as <key> (Orca names the repo <displayName>)`.
- A linked git worktree is refused (exit 1 "<path> is a git worktree of <main>; put ORCHE on the main checkout"). A registered project whose Orca repo is gone, addressed by key or path, is refused with exit 2 before any add. Existing panes are left as they are during init; a live init ends with one `terminal switch` to `main-<proj>` (a failure is one warning, the exit code is unchanged) and the line "Main is open in the main-<proj> tab with the setup summary; from here on, talk to Main there." followed by the window line.
- Implementation note: every live init and every `--refresh` writes the setup report `<project>/.orche/SETUP.md` (in `info/exclude`; `--json` key `setup_report`): `# ORCHE is set up: <proj>`, the divisions grouped by model, the workspaces, the intake (this run's line, else the stamp's, else `off`), where to talk to Main, then `## Any time` with `guide.lines` (one line per optional feature, `- <Feature>: <state>. Say "<owner words>"; Main runs <command>.`, the ones that are off first, at most 12) and the closing line naming "what else can ORCHE do?"; a write failure is one warning.
- On a first registration, right after the switch, init posts one urgent `info` from orche to `main-<proj>` (an OSError is one warning) with `report_path` = the setup report and a summary of at most `inbox.MAX_LINES` (5) lines: `setup done: <proj> registered with <n> divisions: <names grouped by model>`, the intake line as printed (`intake: off` without one), `workspaces: <worktree root>/div-<division>; nothing is committed to the repository`, the window outcome, and `cmd_init.SETUP_ASK` (read the report, tell the owner in their language what was set up and the "Any time" list, then ask for the first request).
- When the window is not closed the printed lines end with `setup report: <path>`.
- Init then closes the window it ran from: exactly one connected pane (`orca terminal list`) whose `worktreePath` is the project folder (realpaths) and whose title does not start with `main-`, `div-` or `feat-` is closed with `terminal close` as the very last action, after the output is printed (the window line reads `closing the window that ran init`; the note says `the window that ran init was closed`); none, several, a failed switch or an Orca error closes nothing and prints `could not close the window that ran init: <reason>` (`no window found`, `<k> windows open on this folder`), which is also the note's line.
- A re-run and `--refresh` post no note and close nothing; `--json` reports `switched`, `setup_note` and `closed_window` (the handle or null); dry-run prints `would switch to main-<proj>` and `would close the window that ran init`.
- Implementation note (upgrade): re-running init on a registered project re-renders its ORCHE-owned files — division and feature personas in the main checkout and in each worktree (a `generated by ORCHE` header line marks them; an overwritten file is first copied to `~/.claude/orche/backups/<proj>/<time>/`), the worktrees' `settings.local.json` keys, the `.orche/project` marker, the exclude lines and the card link — and prints one `refreshed <path>` line per changed file (`--dry-run`: `would refresh`).
- It never touches PROJECT_CARD.md sections, STANDING.md, LEDGER.tsv, GRAVEYARD.md, RUBRIC.md, `.env.example` or unit memory files; units without a persona template, `retired` units and missing worktrees are reported as `kept`. It records `orche_version` in the project's registry entry (only when it changes) and ends with `personas updated for <sessions>: read at their next session start` (Main when the version changed, plus awake units whose persona changed); nothing is restarted or typed.
- Implementation note (onboarding): `orche init [path] --new [private|public|local]` (default `private`) is for a folder without `.git` (one with it: exit 2 "<path> is already a git repository; run orche init without --new"; the folder is created when missing).
- It prints one line per step and stops at the first failure with git's text: `git init -q -b main`, a `README.md` holding `# <folder name>` when the folder is empty, the rendered `scripts/verify/ci.sh`, `git add -A` and `git commit --allow-empty -m init`, then for private/public `gh repo create <folder name> --<visibility> --source . --push` (a gh failure is one `GitHub: not created (<reason>)` line and the run goes on as local), then the normal flow, which adds the folder to Orca with `orca repo add --path <root>`.
- The setup report then opens with `- Repository: created here (<visibility>, <remote url or "no remote">).` and, after a gh failure, `- GitHub: not created (<reason>); say "publish this repository to GitHub"; Main runs gh repo create`. A setup (`--setup-file`) may name a `preset`: `build` (build, design, rnd, research) or `operations` (the same and gtm); `design` is the custom division of its kind; without `divisions` the preset gives them.
- The preset is recorded on the project (`preset`) and named in the report (`- Preset: build-focused (Build's workers specialise in front, back and infra).` or `- Preset: operations.`); `build` appends, once, the standing order `cmd_init.SPECIALISTS` (Build's workers are specialists: front, back and infra, named `front-<topic>`, `back-<topic>`, `infra-<topic>`, a unit spanning two areas split into two workers) through `orche hr correct`'s path, so every brief carries it; Build stays one division.
- `detect.stage(root)` is `operations` when the repository has code (more than 20 tracked files, or package.json, pyproject.toml, go.mod or Cargo.toml) and a sign of running in production (vercel.json, fly.toml, Dockerfile, render.yaml or netlify.toml, a workflow under `.github/workflows/` whose name or text says deploy or release, or a tag fetched from origin), else `build`; local reads only. The intake is skipped with the reason `the repository is empty` when the stage is `build` and fewer than 5 files are tracked.
- A live run ends its steps with the merge queue's check script (`verifygate.ensure`): when origin/<base> (fetched) lacks `scripts/verify/ci.sh`, ORCHE renders it (`templates/verify/ci.sh` with the install and check commands of `detect.toolchain`: package.json's `verify`, else `test`, script through pnpm, yarn or npm by the lockfile; `python -m pytest`; `go test ./...`; `cargo test`; the verdict `unit-test-verified`, or `type-check-only` with no command), commits it on `orche/verify-gate` cut from origin/<base> in a temporary worktree, pushes it and opens one PR to the base titled `Add the merge queue's check script`; an open PR from that branch, or the branch on origin, means none is opened again; never under `--dry-run`.
- `orche init --refresh <p>`, and so `orche update` for every registered project, runs the same step, so an existing project reaches the queue with at most one PR. Its state is one `merge queue: …` line and the report's "Running now" merge-queue line: `on`, `waiting for PR #<n> (<url>) to be merged` or `waiting; the check script could not be pushed (<reason>)`; the PR's number and URL are kept on the project's registry entry (`verify_pr`).
- The "Any time" merge-queue line appears only while the script is missing and no `orche/verify-gate` branch was pushed: `Say "set up the merge queue for <p>"; Main runs orche init --refresh <p>`. `orche doctor`'s `queue gate` row reads the same local state, no gh call: `OK <p>: scripts/verify/ci.sh on origin/<base>`, `WARN <p>: waiting for PR #<n> (<url>)` (the branch is pushed) or `WARN <p>: no check script on origin/<base> and no PR; orche init --refresh <p> opens one`.

| ID | Rule |
|---|---|
| R-MODEL-3 | Implementation note: `orche hr add <proj> <division> [--model M] [--label L --charter C] [--wake] [--no-intake]` adds one division through init's own path (`hr.plan_divisions`, `hr.onboard_project`). The division is a standard name or a valid custom slug not yet in the project (1); a custom name needs `--label` and `--charter` unless its kind has a charter (1). The model is `--model` or the recommendation (R-MODEL-2); a Codex model needs a working Codex (3, the fix named) and refuses `--wake` (3). It prints `recommended for <division>: <model> — <why>` and `choices: …` first (also under `--dry-run`), starts the intake in the background (never inline), and ends `created <session> (<dormant\|active>) on <model>`; the `create` record gains ` on <model>`. |
| R-MODEL-4 | Implementation note: `orche hr model <proj> <division> <model>` records a division's model: project and division known (2), not `retired`/`onboarding` (3), grammar (1), the same model → `<session> is already on <model>` (0, nothing written). To Codex: Codex must work (3) and the division (build: each live feature) must not be `active` (3: `sleep its pane first: orche sleep <session>`). It writes `divisions.<d>.model` (and `custom_divisions.<d>.model`), re-renders the personas and records `model <session> <old> -> <new> by <by>` (registry save, HR-log line, status line; **no registry event**: `registry.EVENT_TYPES` stays as it is). Panes are never closed or restarted; a live run keeps the model it started with. |
| R-MODEL-9 | Implementation note: `orche hr choices [<project>]` is read-only; JSON `project, stage, kinds, custom, models, ask`; an `ask` entry is `{tool, values}`, `tool` one AskUserQuestion question (2–4 options, header ≤ 12 characters, unique labels without ", "); the recommendation first and marked; an unavailable or blocked model is never an option and is named in the question text; an existing division's model question always offers Keep; an entry with fewer than two options is `null`; the texts are English and Main asks them in the owner's language, mapping the picked label through `values`. |
| R-MODEL-11 | Implementation note (`orche init`): the setup key `models` sets the models of divisions being created (an existing one changes only through `orche hr model`); config `kinds` holds each kind's model, effort and ko/en label, description and reason, `design`/`content` a default charter; config `models.catalog` the offered models' label, cost mark and description; a kind with a charter needs no `--label`/`--charter`. |
| R-PLAIN-1 | Implementation note: the owner never needs a command. Main's card and rulebook (`## Owner's plain words`, one table) map the owner's intent, in any language and never by its wording, to the `orche` command Main runs itself — status → `orche status` plus the reports in its prompt, a check → `orche doctor`, an update → `orche update`, divisions and models → skill orche-apply, a halt → `orche flow pause` or `STOP`, and one row for every other owner action; the commands stay for sessions and scripts, and Main never asks the owner to type one. |
| R-PLAIN-2 | Implementation note: `orche update [--dry-run]` updates the checkout holding the package to `origin/main`; only a checkout without `.git` is refused (exit 2). A development checkout (uncommitted changes or another branch, known before any fetch, or commits not on `origin/main`) pulls nothing (`development checkout (<why>): nothing pulled` on stderr): when the install records another version than the checkout's, install and refresh run with the checkout's code (`ORCHE <recorded> → <checkout>`), else `already up to date (<v>; development checkout, nothing pulled)`; `--json` carries `development` (the reason or null). Same version → `already up to date (<v>)`; else `git merge --ff-only`, then the new code runs `install --mode <last mode> --no-jev` (plugin mode also `claude plugin marketplace update orche` and `claude plugin update orche@orche`) and `init <project> --refresh` for every registered project. Output: `ORCHE <old> → <new>`, one `refreshed …` line per project, the install's heartbeat line and the line `open panes keep running; they read the new cards and skills at their next start` when a skill, template, agent or plugin file changed (or plugin mode, or an aligned install). A failed step is named, exit 1. `--dry-run` fetches into FETCH_HEAD only and prints the plan. `orche init <project> --refresh` re-renders ORCHE-owned files only (missing templates, the card link, personas and worktree settings, the version stamp) and opens the merge queue's check-script PR when the base lacks it (at most one per project): no Orca call, pane, automation or intake; setup flags are refused (1), an unregistered project exits 2. |
| R-PLAIN-3 | Implementation note: the README install prompt asks the first setup with choices — `orche hr choices --json` with no project (works on an empty registry, writes nothing), the `ask.where` questions first when the folder has no git repository (then `orche init --new <visibility>`, or `orche init` in the folder typed), the `ask.setup` questions in the owner's language with the recommended preset first, the answers as a `--setup-file` for `orche init`, a one-line confirmation; without a choice dialog or an answer it keeps the defaults and asks nothing. |
| R-PLAIN-4 | Implementation note: Main answers the owner in the owner's language with a short plain summary — at most five lines for status, each doctor WARN/FAIL row with its one fixing step, the version change and the restart need for an update — never raw command output. |

Users need only `orche depts` and `orche status`; everything else is requested from Main in natural language.

Implementation note: `orche --help` is the authoritative list. Additions: `orche report`, `orche decide`, `orche feat new|sleep|wake|retire`, `orche hr audit|correct`, `orche stage set <proj> building|public [--yes]`, `orche init <project>` (a registry key or alias first, then an Orca repo id, display name or path; a registry match keeps its key), `orche install|uninstall|doctor`, `orche jev`, `orche statusline`, `orche version`. The ledger check takes `--pr`/`--sha` flags, not a positional PR.

### 8.5 Logs

`~/.claude/orche/hr-log.md`, `~/.claude/orche/graveyard.md`, `~/.claude/orche/secrets/audit.log`.

### 8.6 Reconcile heartbeat

`orche reconcile [--dry-run]` runs one pass under a non-blocking lock (`ORCHE_HOME/reconcile/`; a held lock
exits 0). It finds stuck flow — a waking pointer unread ≥ `inbox.LATE` whose deliver lock is free, and the
newest `via: dispatch` pointer per unit undelivered ≥ `inbox.LATE` with no live run — and repairs it (nudge, or
`orche deliver --retry`). Attempts fall due once the condition has held STALL_S (600 s), 2× and 4× (10, 20,
40 min); after CAP (3) attempts, at 80 min, one urgent info to the sender (Main for a division). STALL_S, CAP
and BUDGET_S are constants in `lib/orche/reconcile.py`, not config. A pass starts no new action after
BUDGET_S (60 s), saves `state.json` after each action and writes `heartbeat.json` last.
`reconcile.enabled false` turns repairs off (the heartbeat still says so); `reconcile.every_s` (300) is the
interval. Rows: R-RECONCILE-1…6 below.

Schedulers:
- macOS: `orche install` writes `~/Library/LaunchAgents/com.orche.reconcile.plist` — `ProgramArguments`
  `[<plugin>/bin/orche, reconcile]`, `StartInterval` = `reconcile.every_s` (300), `RunAtLoad`, environment
  `ORCHE_HOME` and `PATH` (plus `CLAUDE_CONFIG_DIR` when set at install time), stdout and stderr to
  `ORCHE_HOME/logs/reconcile.log` — and loads it with `launchctl bootout gui/<uid>/com.orche.reconcile` then
  `launchctl bootstrap gui/<uid> <plist>`, only when `--home` is the real home. `--no-reconcile` skips it;
  `orche uninstall` boots it out and removes the file. Elsewhere: run `orche reconcile` every 5 min (cron/systemd).
- Hook kick: the prompt hook (`report_inject`) and the Stop hook (`dispatch_relay`) call `reconcile.kick()`:
  when the heartbeat is missing or older than 3 × `every_s` and `reconcile/.kicked` is older than `every_s`,
  it touches `.kicked` and starts one detached pass. A kick error never changes the hook's result.

Further repairs (T3/T4), each bounded:
- drain: a dispatch pointer whose revision already ran, runs or was reported on is drained at once once its
  `.deliver.lock` is free (uncapped; nothing else is touched).
- busy: a revision whose RUN.json (newer than BRIEF.md) says `stop_reason: busy` and has no live run is
  restarted with `orche run <unit>` once (cap 1, no escalation).
- closed: a session with waiting reports ≥ `inbox.LATE` and no pane: a Main is reopened in place —
  `hr.project_pane(switch=False)` (no focus switch) — and an active division or feature is woken (`hr.wake`),
  then `orche nudge`; at most REOPEN_CAP (2) a day (`reopens:<session>` in `state.json`), STALL_S apart, never
  while the pane exists and never inside `flow.quiet_hours`; past the cap, one owner notification naming the
  decision (`orche wake main-<p>`; default: reopen tomorrow). A dormant or paused unit is never woken: one
  urgent `info` to Main instead.
- silent: the target's newest unit, a pane unit (never Main, takes no runner) whose dispatch was delivered but
  not run, running or reported on at this rev, with no open child of its own, whose pane has been idle
  SILENT_IDLE_S (20 min) or whose rev is 1.5 × TIMEBOX old, gets one `orche deliver --remind` per revision;
  SILENT_ESCALATE_S (30 min) later, or at 2 × TIMEBOX, one urgent `info` to the brief's sender naming the
  decision (STOP, AMEND, or let it run; default: let it run).
- lineage: a lineage close a crash missed is posted when the newest member report is ≤ 24 h old (§8.7).
- owner: the owner step (§8.8) runs first in each pass, before the classes above.

`--dry-run` prints `would …` lines and writes nothing (no state, heartbeat or unreachable marker). Each action
logs `reconcile action=<kind> project= target= rev= attempt=a/c` to events.log.

`orche status` prints one `heartbeat:` line (`reconcile.heartbeat_line()`: `last pass N min ago (every M min,
K repairs today)`, `STALE — … run orche doctor`, `never — install the job with orche install`, or `off`) and, per project, `stalled: <class> <target> [rev R] — ORCHE retrying
(k/cap)` or `— escalated — waiting on you` for each repair on file (`--json`: a `stalled` list);
`orche doctor` has a `heartbeat` row (WARN when older than
2 × `every_s` or never written) and a `reconcile job` row (WARN when the plist is missing, unreadable, has
another interval, or is not loaded).

Watchdog and privacy:
- A pass still running WATCHDOG_S (2 × BUDGET_S, 120 s) after it took the lock is aborted: SIGALRM raises in
  the main thread; when the signal cannot interrupt the call (a read blocked inside the kernel, such as a macOS
  privacy check), a thread ends the process WATCHDOG_GRACE_S (15 s) later. Either way
  `ORCHE_HOME/reconcile/last-abort.json` records `{at, phase, path, hint}` (and `interpreter` for the job's
  own aborts) — `phase` where the pass was (start, privacy, runs, lineage, owner, observe, apply, heartbeat),
  `path` the innermost path a stack frame held (walking out from where it stopped, never past `run_pass`)
  that is under a registered project's root, a protected folder or a volume under `/Volumes` (else empty),
  `hint` `privacy` when that path is under `~/Desktop`, `~/Documents`, `~/Downloads`, `~/Library/Mobile
  Documents`, `~/Library/CloudStorage` or `/Volumes/*` (string checks against the home directory and its real
  path), else `slow` — one events line `reconcile abort phase=<phase> hint=<hint>` is logged (the thread also
  prints the abort line to reconcile.log, flushed), the lock is released and the pass exits 3. A pass the
  LaunchAgent ran (its plist sets `ORCHE_RECONCILE_JOB=1`; launchd also sets `XPC_SERVICE_NAME` to the label)
  with reconcile on stamps `job_at` in `heartbeat.json`, records `interpreter` (the real path of its
  `sys.executable`) and lists each protected project's `.orche`: any error but "not found" (a denial is EPERM,
  while a glob there returns nothing) puts the key in `unreadable`; other passes carry all three over. A
  repair's attempt is recorded before it runs, so an abort mid-apply still spends it.
- macOS keeps those folders from a LaunchAgent-started Python until its `Python.app` (for a framework build,
  `…/Python.framework/Versions/X.Y/Resources/Python.app`, found from the job's `interpreter`, else
  `sys.executable`'s real path; else the interpreter itself, e.g. uv's bare `python3.x`, whose file name the
  texts then show) is in System Settings › Privacy & Security › Full Disk Access; only the user or MDM can
  grant it. After loading the job, `orche install` prints `checking the heartbeat's first pass… (up to 45 s)` and waits up to
  45 s for a job pass (`job_at` at or after the install start, no `unreadable`), checking before it runs a plain
  `launchctl kickstart gui/<uid>/com.orche.reconcile` (a running pass is never killed): `reconcile job: first
  pass OK (<took_s> s)`; else, when a registered project is under a protected folder or a privacy abort at
  most 24 h old and newer than `job_at` says so, `open -R <app>` and `open
  "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"` and the guidance (one English
  line, then the app's path), printed after every other line; else the reconcile.log hint. The install
  still exits 0. `orche init` of a new project under a protected folder runs the same check when the job is
  loaded (not under `--json`). `orche install --grant` installs nothing: `nothing to grant — no project is
  under a protected folder` (exit 0) when none is; the job not loaded: `the heartbeat job is not loaded — run:
  orche install --mode <mode>` naming the installed mode (exit 1); a job pass within 15 s is `granted —
  heartbeat OK`; else the three steps, both windows, and up to 5 min (progress lines
  `checking access… (up to 15 s)` / `(up to 5 min)`) with a kickstart every 30 s, `-k` only for the first (`granted …`,
  exit 0, or `still blocked — switch <app> on in Full Disk Access, then: orche install
  --grant`, exit 1). Ctrl-C during any of these waits prints one line and exits 130.
- `orche doctor`: `heartbeat` WARNs `heartbeat job cannot read <keys> (macOS privacy) — run: orche install
  --grant` while the job's last pass (at most 24 h old) left keys `unreadable`, `heartbeat job cannot read
  <path> (macOS privacy) — run: orche install --grant` for a privacy abort at most 24 h old with no job pass
  after it, and `heartbeat job loaded, no pass yet — run: orche install --grant` (a project under a protected
  folder; else `— run orche reconcile and read <ORCHE_HOME>/logs/reconcile.log`) when the loaded job has not
  passed within 2 × `every_s` of its load (the plist's mtime). `privacy` is OK when no project is under a
  protected folder, or the job is not loaded (the `reconcile job` row says so), or the job passed since it
  loaded and since its last privacy abort and read every protected project; WARN otherwise, naming `orche
  install --grant`. `orche status`: `heartbeat: blocked by macOS privacy — run orche install --grant` for the
  first two.

| Rule | Requirement |
|---|---|
| R-RECONCILE-1 | Pass: `orche reconcile [--dry-run]` runs one bounded, idempotent pass under a non-blocking lock (held → exit 0): settle lost runs, the owner step (§8.8), observe the classes, plan due attempts, apply through existing paths only, save `state.json` after each action, write `heartbeat.json` last. No daemon. (§8.6) |
| R-RECONCILE-2 | Classes, each with predicate → repair → cap → after the cap: unread (waking pointer pending ≥ `inbox.LATE`, pane quiet, lock free → `orche nudge`; CAP → one urgent info to the sender); closed (waiting reports, no pane → reopen Main in place / wake an active division or feature, then nudge; REOPEN_CAP a day → one owner notification; dormant or paused → one urgent info to Main); undelivered (`via: dispatch` pointer pending ≥ `inbox.LATE` → `orche deliver --retry`; CAP → one urgent info); silent (delivered, unrun pane unit idle SILENT_IDLE_S or 1.5 × TIMEBOX → `orche deliver --remind` once per rev; SILENT_ESCALATE_S later or 2 × TIMEBOX → one urgent info to the sender); busy (`stop_reason: busy`, no live run → `orche run` once; no escalation); lineage (missed close ≤ 24 h → post it); owner (§8.8); drain (revision already ran → drain at once, uncapped). (§8.6) |
| R-RECONCILE-3 | Constants, not config: STALL_S 600 (attempts at 10, 20, 40 min), CAP 3 (escalation at 80 min), BUDGET_S 60 (no new action after it). Config: `reconcile.enabled` (true) and `reconcile.every_s` (300). (§8.6) |
| R-RECONCILE-4 | Scheduling: `orche install` writes and loads the `com.orche.reconcile` LaunchAgent (`StartInterval` = `every_s`, `RunAtLoad`); `--no-reconcile` skips it; `orche uninstall` removes it. The prompt and Stop hooks kick one detached pass when the heartbeat is missing or older than 3 × `every_s` (at most once per `every_s`); a kick error never changes a hook's result. (§8.6) The janitor has no LaunchAgent: a full pass starts its daily run when the last one is a day old (§11.4). |
| R-RECONCILE-5 | Heartbeat: `orche status` prints a `heartbeat:` line and per-project `stalled:` lines; `orche doctor` WARNs `heartbeat` past 2 × `every_s` or never written, and `reconcile job` when the plist is missing, unreadable, has another interval or is not loaded. (§8.6) |
| R-RECONCILE-6 | Safety: a pass sends text only by `orche nudge` and `orche deliver --remind`; it never switches focus, never kills a pane or run, never wakes a dormant or paused unit; `--dry-run` prints `would …` and writes nothing; events lines carry ids, counts and times, never text. (§8.6) |
| R-RECONCILE-7 | Watchdog and privacy: a pass running 120 s after it took the lock is aborted (SIGALRM, then a thread 15 s later), `last-abort.json` {at, phase, path, hint: privacy|slow}, one events line, lock released, exit 3; `job_at` marks the LaunchAgent's passes; `orche install` waits 45 s for the job's first pass and, blocked by macOS privacy, opens Finder and Full Disk Access with the guidance (exit 0); `orche install --grant` verifies the grant (5 min); `orche doctor` WARNs `heartbeat` and `privacy` while the job cannot read a project. (§8.6) |

---

### 8.7 Flow guards and pause

Autonomy window W(p): from the later of local midnight and the mtime of `ORCHE_HOME/flow/<slug>/owner-seen`,
which the prompt hook stamps on every prompt typed to Main (not `[orche] inbox:` nudges, `[orche] report due:`
reminders, or a bare `[drain]`/`/drain`). `orche dispatch` checks each local brief, AMEND and STOP before
writing (`flow.guard.check`); a refused block exits 2 and nothing is written. STOP is never refused.

| Rule | Refuses | Threshold |
|---|---|---|
| G1 | an AMEND to a unit revised ≥ 3 times inside W | 4th AMEND |
| G2 | an AMEND, or a brief with the same TO and GOAL (case and spaces aside), to a unit whose last 2 reports inside W are blocked | 2 in a row |
| G3 | a brief or AMEND when the project sent ≥ 20 since max(W, now − 1 h) | 21st |
| G7 | not a refusal: the 3rd refusal in a row (`sessions/<slug>.relayed.json`, 30 min) appends "(refusal n in a row: stop re-sending; tell the user what you tried)" | 3 |
| G8 | every brief and AMEND while `flow/<slug>/pause.json` exists | — |

G1–G3 end "Ask the owner before …: close your reply with OWNER: <the decision>. STOP still works." G1–G7 are off
with `flow.enabled false` (G7 drops its suffix); G8 is not. Events: `flow refused rule=<G> by=<s> unit=<u> n=<count>`,
`flow pause project=<p> by=<s>`, `flow resume project=<p> by=<s>`.

`orche flow pause <p> [--reason <text>]` writes `pause.json {by, at, reason}`; `orche flow resume <p>` removes it
only when owner-seen is newer than `at` (else exit 3 "resume needs the owner's word after HH:MM"); an unknown
project exits 2. While paused, reconcile only drains. `orche status` adds
`looping: <unit> — revised N× without the owner since HH:MM: the next AMEND needs the owner` and
`paused: by the owner since HH:MM (<reason>) — resume on their word: orche flow resume <p>` (`--json`: `looping` `[{unit, kind: revisions|repeats, n, since, status, rev}]` and `paused` `[{by: spend|owner, since, spent, cap, reason}]`, a key that does not apply is null).

Lineage (T7): the brief header line after `to:` is `lineage: <id>   parent: <unit|->` (`brief.Brief.lineage`,
`.parent`; missing line → lineage = the unit, no parent). AMEND keeps both. A new brief from a division takes the
lineage of the division's newest brief and names that unit as parent. From Main: an owner turn (prompt not
starting `[orche]`) starts one lineage shared by all its briefs (the first brief's unit); a wake turn whose
injected pointers name exactly one lineage joins it, otherwise it starts one. The prompt hook records the turn in
`ORCHE_HOME/sessions/<slug>.turn.json {owner, lineages, at, started}`. Triage briefs start a new lineage. Report
pointers (`orche report`, runner filing, `unrun`, `lost`) carry `lineage` and, when there is one, `parent`.

| Rule | Refuses | Threshold |
|---|---|---|
| G6 | a brief from a division whose parent (the division's newest brief) is ≥ 3 deep by parent links, or has ≥ 5 child briefs | 4th level / 6th child |

Texts: "<parent> is already N briefs deep (cap 3); a brief from <s> would make it N+1. Do not dispatch it yourself:
name the work in your report and ask Main to dispatch it." and "<parent> already has N child briefs since HH:MM without
the owner (cap 5). Do not send another: report it to your sender (`orche report blocked --summary "<what is left>"`).",
each ending "STOP still works." — no owner ask: the division reports and Main decides. Main's briefs, AMENDs and STOP
are not capped; G6 is off with `flow.enabled false`.

Run history (T6): `ORCHE_HOME/runs/<p>/history.jsonl`, one O_APPEND JSON line per RUN.json written (finished,
`unrun`, `lost`): `{unit, rev, to, model, vendor, effort, status, stop_reason, cost_usd, num_turns, started, finished}`.
A failed append is logged to hooks and never fails the run; a corrupt line is skipped on read. `cost_usd` is
Claude's figure, Codex's estimate (§14.13), or a reservation when the run's worker started but left no figure
(killed, timed out, crashed, Codex without a completed turn, lost): whole minutes of wall time rounded up, at
least one, × $0.25, at most `flow.max_unit_cost_usd` (`history.reserve`); a run that never started its worker
(busy, duplicate, stopped before start, exit 127, a codex that printed no event) records none.

| Rule | Refuses | Threshold | Checked |
|---|---|---|---|
| G4 | every brief, AMEND and run when the project's `cost_usd` (lines finished inside W; Codex estimates and reservations included) reached `flow.max_project_cost_usd`; a unit's AMEND and run when the unit's reached `flow.max_unit_cost_usd` | $30 / $10 | `orche dispatch` (`guard.check`), `orche run` (`job.execute`) |
| G5 | a unit's run when its history lines started inside W (not `busy`, `duplicate`, `capped`) number ≥ 4 | 5th run | `orche run` (`job.execute`) |

Implementation note: G4 counts Codex runs at their estimated cost and runs without a cost figure at their reservation (R-MODEL-7); `flow/guard.py` is unchanged, the figures come from history.

Texts: "<p> spent $X on headless runs since HH:MM without the owner (cap $30)", "<unit> spent $X … (cap $10 a
unit)", "<unit> ran N times since HH:MM without the owner (cap 4)". A dispatch refusal appends ". Ask the owner
before spending more: …" and exits 2. A refused run exits 3, prints "orche: not started: <text>", starts no claude,
writes no RUN.json or history line, and files one quiet `info` pointer "not started: <text>; ask the owner".
`orche status` adds `paused: autonomous spend $X since HH:MM (cap $30) — until the owner answers` (`--json`: a `paused` entry with `by: spend`).

Repeats (T6, `flow.shape`, in `inbox.post` before the write): a report filed under a brief by a unit session
(not orche, a dispatch, a STOP, a runner's filing (`via: runner`), an `external-request` or a brief-less
`adhoc-`/`xreq-` unit), urgent or not, whose previous such pointer since W (of the recipient's project) from the
same sender about the same unit in the same inbox has the same `brief_rev` and `status` gets `quiet: true` and
`repeat: <n>` (2 on the first repeat); text is not compared. A shaping error is logged (`inbox: shape failed for <unit>`) and the pointer is written unshaped.
`orche status` adds `looping: <unit> — same report n× (<status>, rev <rev>): repeats did not wake Main` when the
unit's newest report inside W has n ≥ 3.

Lineage close (T8, `flow.lineage`, inside `inbox.post` under the per-project lock `ORCHE_HOME/flow/<slug>/.lock`,
held at most 5 s): in a lineage of ≥ 2 units, a unit's `done` report, a STOP, or a runner `info` without retry
gets `quiet: true`; `failed`, `blocked`, `question` and `external-request` wake Main as before, and a single-unit
lineage is unchanged. When every unit's newest report is terminal (done, failed, stopped), ORCHE writes
`<root>/.orche/units/<L>/LINEAGE.md` (`# Lineage L`, `closed: <iso>   units: n — d done, f failed, s stopped`,
then a table `| unit | to | rev | status | report |`), records the members' signature (`unit@rev:status`) in
`ORCHE_HOME/flow/<slug>/closed.json`, logs `lineage closed L project=<p> n=<n>` and posts one waking `done` pointer
from `orche` to Main with `lineage_closed: true`, `report_path` = LINEAGE.md and the summary
`lineage L closed: n units — d done, f failed, s stopped` plus ≤ 4 `unit status` lines. The same signature never
closes twice; an AMEND that changes a revision can close again. Reconcile (`lineage.repair`) posts a close a crash
missed when the newest member report is ≤ 24 h old (`--dry-run`: `would close lineage L (p)`).
After the Inbox block injected into Main, one line per lineage named: `[orche] lineage L: closed — N units (d done,
f failed)` or `[orche] lineage L: open — k of n units running; waiting on <unit> (<status>)`; then the report line
above (R-INBOX-2).

### 8.8 Owner's voice

Orca's own notification covers Main's closing line: Orca installs its own Stop hook and notifies
when an agent's turn ends, so ORCHE sends none for it and notifies only what Orca cannot know: the reminders of
an unanswered ask (the heartbeat, below), a runner's parked guard refusal, a secret request (the Keymaster) and HR
records (`hr.book`). On every stop the done gate allows (also under
`stop_hook_active`), a Main session's Stop hook (`dispatch.handle_stop` → `flow.owner.closing`) reads the final
message's closing line: its last non-empty line outside fenced blocks (``` or ~~~, CommonMark-like; an unclosed
fence runs to the end; a snippet fence indented inside ```` ```dispatch ```` stays inside, as in the dispatch
grammar). Tags are upper case; tolerated around them: a quote (`> `), a bullet (`- `, `* `, `+ `), wrapping
backticks, bold (`**OWNER:** …`, `__OWNER:__ …`), a space before the colon and a full-width colon (`OWNER：`). Every
tag stamps `flow/<p>/tagged` with the turn's start — the `at` of the prompt hook's turn record (no record: no stamp) —
so a report that landed during the turn is covered only by a tag from the turn that reads it (the fallback reads it). The open ask's own text again, or the last DONE again with
no ask since, is a repeat: nothing recorded or sent (events `owner repeat project=<p> tag=<T>`), so the
re-notification schedule runs on from the first:

| Closing line | Effect |
|---|---|
| `OWNER: <the decision or step, with its default>` | records the project's ask (replacing an open one); no notification |
| `DONE: <the result>` | closes the open ask (`closed_at`) and records the `done` notice; no notification |
| `RUNNING: <what runs now>` | nothing |
| anything else | nothing; events `owner untagged project=<p>` |

The ask, `ORCHE_HOME/flow/<p>/ask.json`: `{text, at, kind, source[, unit][, closed_at | answered_at]}` — `text`
masked, one line, ≤ 160 chars; `kind` `owner`, `secret` (text `secret <NAME>[, <NAME>]`), `guard` or `fallback`;
`source` `main`, `runner` or `heartbeat`; every write under `flow/<p>/.notify.lock`, in sequence with a
notification (never nested). An ask is open until `closed_at` (DONE) or `answered_at`. A runner's refused start
(G4/G5: a quiet `info` that waits for Main's next prompt) records a `guard` ask (`not started: <reason>`, with
`unit`) and notifies: that pointer is not on Main's screen, and Orca does not know it.

For an `OWNER:` or `DONE:` closing line ORCHE counts the first notification as shown by Orca (events `owner
recorded project=<p> kind=<k> by=orca`): `notified.json` gets the ask's first try (`n` 1, `ask_at`, `ask_last` the
ask's time) or the DONE notice's id in `shown`, and the gap does not move. So the heartbeat's first reminder of
the ask comes 30 min after it, and a DONE is never sent by ORCHE.

Answered (`flow.owner.answer`, events `owner answered project=<p> by=prompt|secret after=<m>m`): the owner prompted
Main after the ask (`owner-seen` over a second newer than `at`; the prompt hook calls it at once, the heartbeat again),
or every secret a `secret` ask names is in — no request for it pending and one filled since 10 min before the ask
(a fill during Main's own turn counts; then the ask is answered as it is recorded and nothing is sent).

Notifications (`flow.owner.notify`, title `ORCHE <p>`): (1) at most one per project per 300 s; no sound inside
`flow.quiet_hours`; (2) a failed show (osascript non-zero or its 5 s timeout) is a try: the next waits for the
next slot. `flow/<p>/notified.json` `{at, kind, n, ask_at, ask_last, shown, tried}`, written under
`flow/<p>/.notify.lock`: the last notification ORCHE showed (`at`, `kind`: the gap), the tries for the ask made at
`ask_at` (`n`: shown, shown by Orca or failed; the last at `ask_last`), per notice kind the id of the last notice
shown by ORCHE or Orca (`shown`), and a notice's failed tries (`tried` `{<kind>: {id, n, last}}`). A notice — a DONE, a `stuck` escalation (the heartbeat's Main-unreachable
escalations), a fallback notice — is recorded in `flow/<p>/<kind>.json` `{text, at, id}` (one writer each)
before it is tried (`flow.owner.announce`), so one held back is sent once by the heartbeat. Events: `owner ask
project=<p> kind=<k> source=<s>[ unit=<u>]`, `owner notify project=<p> kind=<k> n=<n> quiet=<0|1>`, `owner held
project=<p> kind=<k> reason=gap|off|lock|failed`, `owner recorded project=<p> kind=<k> by=orca`, `owner closed project=<p>`, `owner dropped project=<p>
kind=<k>` — never the text.

The heartbeat's owner step (`flow.owner.heartbeat`, first in each `orche reconcile` pass; `--dry-run` writes and
sends nothing and prints `would …`), per project: mark an answered ask; post a missed secret resume (below); then,
unless the project is paused (G8) or notifications are off, at most one of:
- **Fallback**: the oldest report to Main, 10 min to a day old, that waits on the owner — the newest of its
  unit is `blocked`, `question` or `failed` or carries an owner step and no BRIEF.md revision answered it, or a
  lineage's close pointer — with no tagged Main turn begun after it (`flow/<p>/tagged`), owner prompt, ask or
  fallback notice since it: `<unit>: <owner step | status | lineage closed>`. Blocked, question and owner steps become the ask
  (`kind fallback`, or `secret` for a secret step; `source heartbeat`), re-notified until answered; failed reports
  and close pointers are one fallback notice. Once per cause: what it records is newer than the report. A fallback
  whose notification was held back prints and logs as `held` (`reconcile action=held …`), not `notify`.
- **A held notice**: the oldest one not yet shown whose try is due, once the gap has passed; after a failed try the next waits for the next slot 30, 60, 120 or 240 min after the
  notice (5 tries); dropped unsent once the owner prompted Main after it (they saw Main) or after a day.
- **The ask's next notification**: the first (held back by the gap or the lock; none for Main's own ask, whose
  first Orca showed) at the next pass; then re-notifications 30, 60, 120 and 240 min after the ask (`OWNER (asked
  HH:MM): <text>`), each the first slot after the last try (shown, shown by Orca or failed), so slots passed unsent fold into one; then none — the `owner:` line stays. Quiet
  hours (`flow.quiet_hours`, default `23:00-08:00`, `""` none): a first notification inside them is silent,
  re-notifications wait, and the first pass after them sends one.

Secret resume: `secrets/dialog.py`, after `store.mark_filled` (the only fill path), calls
`flow.owner.secret_filled(project, name, rid)`: the ask is answered when every secret it names is in; then, not while
paused, it looks up the units to resume — each unit with a report to Main (pending or drained) since the request's
`at` less 60 s whose owner step names the secret, none of those reports told yet, and every secret its newest such
step names filled (none pending) — and posts one urgent `info` from `orche` to Main per unit: `owner step done: secret
<NAME>[, <NAME>] filled at HH:MM — resume <unit> (AMEND or orche run <unit>)` (events `owner secret-filled
project=<p> name=<NAME> unit=<u>`), once per asking report (`flow/<p>/secret-filled/<report>`, created exclusively
after the lookup; with nobody asking yet nothing is written). A fill that resumes a unit counts as the owner's word:
`owner-seen` moves to the fill (never back), so the autonomy window restarts and G1 lets the resume AMEND through. The heartbeat repeats the lookup for requests filled in
the last day (a dialog that died, a report filed after the fill, the last of several secrets). The Keymaster dialog is a fixed
`on run argv` script (`display dialog (item 1 of argv) …`); the request text is its one argument.

`orche status`, first under the project line (`--json`: `owner`, `{text, at, since, minutes, kind, source, unit,
notified, next, quiet}`):
```
  owner: asked 16:49 (22 min, notified 2×, next 17:49) — <ask, ≤ 100 chars>
  owner: asked 23:10 (50 min, notified 1×, waiting for quiet hours to end) — <ask>
  owner: asked 12:00 (300 min, notified 5×) — <ask>
```

Unit side: a worker only the owner can unblock ends with `STATUS: blocked` (or `question`) and one line
`OWNER: <the exact step or decision>` (a secret: `orche secret request <NAME> …` first, then
`OWNER: secret <NAME>[, <NAME>]`). The runner takes the last `OWNER:` line outside fences of the final message
(`job.owner_of`); a pane unit runs `orche report blocked --summary … --owner "<text>"`. Either sets the pointer
field `owner` (masked, one line, ≤ 200 chars), which `inbox.render` prints first under the pointer's header:
`  OWNER STEP: <text>`.

One notify path, `lib/orche/notify.py` `send(title, body, sound=False, *, kind, project=None)` (`project` is
accepted and unused): `osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of
argv)[ sound name "Glass"]' -e 'end run' <body> <title>`, with the display line inside `tell application id
"com.stablyai.orca"` … `end tell` when `pgrep -xq Orca` finds Orca running: macOS credits a bare osascript
notification to Script Editor (a click opened Script Editor), the tell makes it Orca's (a click opens Orca), and the
launch guard keeps a background pass from launching Orca (AppleScript launches a target that is not running). Body
and title are arguments, never AppleScript or shell text built from text (one starting with `-` gets a leading space, never an
option); masked; control characters dropped; config `notify` (default on); macOS only; 5 s timeout; events `notify
kind=<k> ok=<0|1>`. HR, the Keymaster and the heartbeat's escalations use it; a caller with a project passes its key.

| Rule | Requirement |
|---|---|
| R-FLOW-1 | Autonomy window W(p): from the later of local midnight and the owner's last prompt to Main (`flow/<slug>/owner-seen`); orche's own nudges, reminders and drains do not count. Every flow guard counts inside W. (§8.7) |
| R-FLOW-2 | Guards refuse before anything is written (dispatch exit 2, run exit 3): G1 4th AMEND of a unit in W (`flow.revisions`); G2 an AMEND or same TO+GOAL brief after 2 blocked reports in a row; G3 the project's 21st brief/AMEND in max(W, 1 h) (`flow.briefs_per_hour`); G4 project spend $30 / unit spend $10 in W; G5 a unit's 5th run in W (`flow.runs_per_unit`); G6 a division brief 4 levels deep or a parent's 6th child (`flow.depth`, `flow.fanout`); G7 the 3rd refusal in a row adds "stop re-sending"; G8 every brief and AMEND while paused. STOP is never refused; G1–G7 are off with `flow.enabled false`, G8 is not. (§8.7) |
| R-FLOW-3 | Post-time shaping: a unit's report repeating the previous one since W (same sender, unit, inbox, `brief_rev` and `status`) is posted `quiet: true` with `repeat: n`; `orche status` shows `looping:` at n ≥ 3. (§8.7) |
| R-FLOW-4 | Lineage: every brief carries `lineage: <id>   parent: <unit\|->`; an owner turn starts one lineage, a wake turn naming one lineage joins it, a division's brief inherits its newest brief's lineage with that unit as parent; AMEND keeps both; report pointers carry them. (§8.7) |
| R-FLOW-5 | Lineage close and final report (R24): in a lineage of ≥ 2 units, `done`, STOP and runner `info` reports are quiet; when every member's newest report is terminal ORCHE writes LINEAGE.md and posts one waking `done` pointer to Main (`lineage_closed: true`), once per signature. Main then gives one full report (outcome · landed · failed · decisions + default · next) — not one per wake. (§8.7) |
| R-FLOW-6 | Closing line: Main ends every reply with one tagged line — `RUNNING: <what runs>`, `OWNER: <decision or step, default>` or `DONE: <result>`; the Stop hook reads the last non-empty line outside fences. OWNER records the ask, DONE closes it, RUNNING and untagged lines record nothing; none sends an ORCHE notification (Orca's own covers the reply). Units only the owner can unblock end `STATUS: blocked` and one `OWNER: <exact step>` line (`--owner` on `orche report`). (§8.8) |
| R-FLOW-7 | Owner notification (the only path): Orca's own notification covers Main's closing line, so ORCHE sends none for it and notifies only the reminders, guard refusals, secrets and HR; a failed show is a try; ≤ 1 per project per 300 s; re-notify an open ask 30, 60, 120 and 240 min after it, then none (the `owner:` status line stays); quiet hours `flow.quiet_hours` (23:00–08:00): silent first notification, re-notifications wait, one at their end; the heartbeat's 10-minute fallback turns an owner-waiting report with no tagged Main turn into an ask or notice; a `secret` ask is answered when its secrets are filled, and Main gets one resume `info` per asking unit once every secret its step names is filled (such a fill is the owner's word). Text is masked; events never carry it. (§8.8) |
| R-FLOW-8 | Pause and resume: `orche flow pause <p>` writes `pause.json` (G8: every brief and AMEND refused; reconcile only drains; no owner-step notices); `orche flow resume <p>` succeeds only after an owner prompt newer than the pause (else exit 3). (§8.7) |

### 8.9 Scale

| Rule | Requirement |
|---|---|
| R-SCALE-1 | Every flow and run cap is config or registry data (§8.1); only review and queue retry limits are constants (`build.MAX_STARTS`, `integrator.MAX_RETURNS`, `integrator.MAX_STOPS`). |
| R-SCALE-2 | `scale.presets` set the group; `orche hr scale [<preset>]` prints or applies one through HR's record; `orche hr choices` offers them with the cost line; doctor `scale` shows the values. |
| R-SCALE-3 | Global cap `runner.max_parallel_total` across projects and memory guard `runner.min_free_mb` (kernel free percentage × RAM on macOS, `MemAvailable` on Linux): a run or a gate waits while either is exceeded; events say why. Implementation: `runs.acquire` counts every project's live unit locks under `ORCHE_HOME/runs/.mutex` (then the project's `.mutex`), reads `procs.free_mb()` before each try and `runner.min_free_mb` from config.json again after each poll (10 s polls while low; an unreadable reading is no guard, logged once a day) and logs one `run unit=<u> waiting=global n=<n> cap=<c>` or `… waiting=memory free_mb=<n> min_mb=<m>` line per wait; past `runner.queue_wait_s` the run is `busy`. The heartbeat's `busy` restart is not ready while the project or all projects are at the cap (its attempt is not spent), and a restart that came back busy again is a fresh try (R34). |
| R-SCALE-4 | `feature_panes` false: new features have no pane and no awake slot (`pane: false`), run through the runner, and are never woken by the heartbeat; `orche feat wake` opens a pane. `orche feat new` refuses below `feature_min_free_disk_mb`. |
| R-SCALE-5 | A usage or rate limit (patterns `runner.usage_limit`) holds that vendor across projects until the reset it names or the backoff `5/15/60 min`: no attempt or review of it starts; held and interrupted units file one quiet `info` and are re-run by the heartbeat class `held` after the hold; one owner notice per hold; status and doctor show it. Continued below the table. |
| R-SCALE-6 | `orche status` is one line per project (`queue w/t/m`, `runs n/cap`, a feature count) with sub-lines capped at 3 per kind; `orche status --project <p>` is one line per lineage (units by state, PRs, spend). Implementation: `queue w/t/m` (waiting / testing / merged today) appears when the queue has any; `runs n/cap` is live runs / `runner.max_parallel`; `paused:` carries the project's units in an active hold; `queue:` a closed or paused queue; the cap line is `  <kind>: … <n> more: orche status --project <p>`; the positional `orche status <p>` stays the one-line filter; `--json` rows add `queue`, `runs`, `holds` and, with `--project`, `lineages` `[{lineage, units, states, prs, spend, since, open, newest}]`. |

**R-SCALE-5, continued.**

- Implementation: `flow/usage.py` (`detect`, `hit`, `join`, `active`); the text read is `headless.Result.limit_text` (the failure line; Claude's `is_error` result text; Codex's `error_text`, never its final message; nothing for a call that succeeded, was killed or hit its turn cap). The hold file `ORCHE_HOME/flow/usage-<vendor>.json` is `{vendor, at, until, source: reset|backoff, step, text (masked, ≤ 160), project, units: [{project, unit, stage, at}], notified, resumed}`, written under `flow/.usage-<vendor>.lock`; an active hold is joined, never escalated.
- The default patterns are vendor wording (`usage limit`, `rate limit`, a `429` beside `rate` or after `API Error`, `too many requests`, `<n>-hour|daily|weekly limit reached`, `hit your limit`, a non-disk `quota` exceeded, `overloaded`), never a bare `429`, `quota` or `limit reached`; a reset is a clock time (`3pm`, `9:30`, `17:00`), `in <n> minutes|hours|days`, an epoch, an ISO time or `retry-after`, never a bare number (`reset 5` backs off).
- A unit a limit stopped 3 times (history `usage_limit`, `work` lines) since its last `done` is not held again: `usage hold refused unit=<u> reason=repeat` and the run files its own status (`failed`). RUN.json `stop_reason: held` (a start while held, worker never started, exit 0) or `usage_limit` with `stage` (the attempt hit the limit; no escalation); both are quiet `info` pointers and `history.UNCOUNTED` (not a run for G5).
- The heartbeat re-runs a fresh `held`/`usage_limit` record whose vendor is not held, no run live and no STOP marker since, once per record, oldest first, at most `runner.max_parallel_total` a pass; the owner notice is `owner.usage_notice` (kind `usage`, Appendix A.15).

## 9. Cross-project collaboration

### 9.1 PROJECT_CARD.md

Location: repo root `PROJECT_CARD.md`, symlinked at `~/.claude/orche/cards/<project>.md`. Size ≤2K tokens.

Sections (`templates/project/PROJECT_CARD.md`):
1. Purpose
2. Stack
3. Entry points
4. Recent changes (user-visible, newest first)
5. How to ask
6. Last refreshed (`<date>, base <sha>`: the commit the card reflects)

`orche intake` adds one block (build/test/run, conventions, risks) before How to ask (R-DISPATCH-11). Implementation note: this list replaces a nine-section list (one-line description, state and latest release, key features, last 5 changes, public links, glossary, aliases, owning sessions, base commit) that no version wrote.

### 9.2 Refresh policy

| ID | Rule |
|---|---|
| R-CARD-1 | Refresh weekly (the `orche-card-refresh-<proj>` automation runs `orche card refresh <proj> --background`), after a release and on demand (`orche card refresh <proj>`). Release-step refresh is not wired yet: the weekly automation and `orche card refresh` are the current triggers. |
| R-CARD-2 | A refresh reads only what landed since the card's base commit. No commit outside PROJECT_CARD.md and `.orche/` ⇒ no model call; no user-visible change ⇒ nothing written (§14.14). |
| R-CARD-3 | Ask another project with `orche ask <B> "<question>"` only when its card does not answer. An answer is not folded back into the card; the next refresh covers what changed. |

### 9.3 How users phrase requests

| Intent | Where | Example phrasing | What happens |
|---|---|---|---|
| Learn another project's news | Any Main or terminal | "What changed in example-app lately?" / `orche card example-app` | Card read; terminal path costs 0 tokens |
| Reflect another project in mine | Main of the output project | "Update the intro page plan with example-app's latest features" | Main(web) reads `orche card example-app` (and `orche ask example-app "…"` when the card falls short) → summary → GTM plan → confirm → Build |
| Ask another project to do work | My Main | "Ask example-app to add a date filter to the history API" | External request into Main(app) inbox → app user confirms (default: review at next drain) → app Build → completion returns to Main(web) |
| Coordinated change | Main of the project that must change first | "app changes the API first; web follows" | External request + dependency; app's completion pointer starts web's Build unit |
| Quick direct question | Any Main or a terminal | "What is the current payment API response shape?" / `orche ask example-app "…"` | One headless read-only answer from app's card and repo (§14.14); app's Main is not woken |
| Work directly with another owner | Other project's division/feature panel | Normal instructions | Handled under that project's rules; significant changes summarized to its Main |

Rule of thumb: "What changes when this is done?" — talk to that project's Main.

### 9.4 Liaison role

- A headless role, not a session (§14.14): no pane, no memory dir, no registry member. Any session or a terminal may ask it; it cannot assign work.
- Model Sonnet 5.5, effort low; read-only tools (Read, Glob, Grep) in the project's main checkout.
- Outputs: answers on stdout (`orche ask`) and card sections (`orche card refresh`). A work request is named in the answer as an external request to Main(B).

---

## 10. Secrets (Keymaster)

### 10.1 Principles

| ID | Rule |
|---|---|
| R-KEY-1 | Keymaster is a broker process with no model. Secret values never pass through an LLM, transcript, or model API. |
| R-KEY-2 | Agents know secret names only. |
| R-KEY-3 | Production services read secrets directly from the backend (e.g., Cloud Run / Functions secret bindings); no files. |
| R-KEY-4 | Local processes get secrets via `orche secret run -- <cmd>`, injected only into that process environment. |
| R-KEY-5 | Agents never handle wallet/treasury private keys (use KMS, hardware wallets, or limited hot wallets). Human-only vaults hold recovery codes and master passwords. |
| R-KEY-6 | Environment separation by cloud project: `<app>-dev`, `<app>-prod`. |
| R-KEY-7 | An OS notification carries no report text: an ask's text (masked, ≤ 160 chars) or ORCHE's own words, always as an osascript argument, never as script (§8.8). |

### 10.2 Backends

| Backend | Use |
|---|---|
| GCP Secret Manager | Primary (IAM, audit logs; ~$0.06 per secret per month) |
| Vercel env (`--sensitive`) | MVPs only |
| GitHub secrets (`gh secret set`, stdin) | GitHub Actions only |
| macOS keychain | Local cache |

### 10.3 Request flow

```
agent     : orche secret request STRIPE_KEY --env dev,prod --why "payments" --how <issuer URL>
keymaster : queue (name, purpose, envs, link only) → macOS notification + status line 🔑 + Orca card dot
          → dialog [Paste from clipboard] [Type (hidden)]  (process separate from the agent)
          → format check → store per env (Secret Manager | Vercel env --sensitive | gh secret)
          → clear clipboard → mark request fulfilled → append audit.log
agent     : orche secret wait STRIPE_KEY → READY → orche secret run -- pnpm dev
```

- User action: notification → copy from issuer page → one button. No "done" message needed.
- Backlog: `orche secret fill`.
- `orche secret ls [--env <env>]` lists the names stored for the project and their envs, never values; a person says it to ask which keys exist.
- `orche secret rotate <NAME> [--env <envs>]` replaces a stored value through the same dialog and disables older GCP versions; a person says it when a key leaked or expired.
- `orche secret render [--env <env>] [--out .env.local]` writes a 0600 dotenv file for a framework that only reads one (never `.env.example`; an existing file only with `--force`, kept as a backup); a person says it when a tool cannot take `orche secret run`.
- Onboarding: Build scans `.env.example` and `process.env.*` usage and requests all missing keys at once (`orche secret missing`).

### 10.4 Developer sharing

| Priority | Method | When | Operator | Developer |
|---|---|---|---|---|
| 1 | GCP IAM | All GCP projects | `orche dev add dev@example.com --project <app>-dev` (grants `secretmanager.secretAccessor` to a personal account) | `gcloud auth application-default login` once; `orche secret run` fetches |
| 2 | Vercel team invite | MVP on a team plan | Invite | `vercel env pull` |
| 3 | `age` bundle | Contractors without a GCP account | `orche secret share --to <dev> --env dev` → `.env.dev.age` | Install `age`, share public key once, `age -d` |

- Shared cloud accounts are owner/emergency only. Developers use personal identities.
- Revocation: `orche dev remove`; no key rotation required. `orche secret revoke <who>` rotates every secret an `age` bundle gave that developer (asks first unless `--yes`); a person says it when a contractor leaves.
- Developers get dev projects only; prod is owner + deploy service accounts.

### 10.5 Read-deny hook (PreToolUse)

Deny with a pointer to `orche secret run`:
- Read/cat/grep/source of `.env*`
- Direct `gcloud secrets versions access`
- Environment dumps (`printenv`, a bare `env`, `set`, `declare -p`, inline interpreter dumps) are denied (`lib/orche/envgate.py`).

Implementation note (command words):
- `hooks/env_read_gate.py` runs two rule sets, the Jev-key rules (`lib/orche/jevgate.py`) and the secret-file and environment rules (`lib/orche/envgate.py`), on the same machinery (`lib/orche/cmdscan.py`): only command words count, never text that mentions a secret, so a Markdown file about `.env`, a commit message "fix .env loading" and `rg SECRET lib/` (a search PATTERN, not a path) all pass.
- Wrappers are skipped (`timeout 5`, `sudo -u x`, `/usr/bin/time`), shell strings (`bash -c`, `eval`, a heredoc or text piped into a shell, `watch`, `script -c`, `flock -c`, `env -S`) are re-scanned as commands, and inline interpreter code (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, awk programs, heredocs, piped text) is checked as text, `\r\n` and a bare `\r` read as a newline first (`cmdscan.newlines`; Python treats a bare CR as a line break, so a `#…\r` comment cannot hide code), then Python comments stripped by `tokenize`.
- Secret paths: `.env` and `.env.*` except the names-only `.env.example`, `.env.sample`, `.env.template`; `*.pem`, `*.key`, `*.p12`; data files in a `secrets/` or `.secrets/` directory (source and prose such as `*.py`, `*.ts`, `*.md` stay readable); `/proc/<pid>/environ`; and globs that target them (`.env*`, `*.pem`; not `*`).
- Refused in Bash: a reader (`cat`, `head`, `less`, `sed`, `source`, `.`, `dd if=`, `diff`, …) with a secret-path argument or any `< secret` redirect; a search tool whose path argument or `-g`/`--include` glob is one, or whose path is a `secrets/` directory without a code-only glob; `printenv`, `env` that runs no command, `export`/`declare`/`typeset` with options only (`export -p`), `set` alone, `launchctl getenv|export`, `gcloud secrets versions access`; `echo`/`printf`/`print` of `$NAME`/`${NAME…}` where NAME is upper case (`[A-Z0-9_]`) and contains KEY, TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL, PRIVATE, MNEMONIC or SEED; inline code that looks up such a name in the environment (`os.environ['OPENAI_API_KEY']`, `process.env.API_KEY`), reads the whole environment (`print(os.environ)`, `print(dict(os.environ))`, `json.dumps(os.environ)`, `console.log(process.env)`, `p ENV`, `ENV.to_h`, `%ENV`, `getenv()`, awk's `ENVIRON`) unless it is a hand-over (`env={**os.environ, …}`, `child_env = dict(os.environ)`, `{...process.env, X: 1}`, `"CI" in os.environ`, `len(os.environ)`, `Object.keys(process.env)`, `ENV.keys`, a lookup such as `os.environ.get("HOME")` or `process.env.NODE_ENV`, `for k in os.environ:` when the code prints nothing), names a secret path in a string literal or holds a refused command in one (`os.system('cat .env')`).
- Read, Edit and Write are judged by `file_path` only, never by content; Grep by its `path` and `glob` (a `secrets/` path needs a code-only glob or type); Glob lists names only and is not gated. `security find-*-password -w|-g` is the Jev rules'; their key-store rule covers ORCHE_HOME's secrets dir only (`~/.claude/orche/secrets/`, `$ORCHE_HOME/secrets/`, the configured ORCHE_HOME's) plus any `secrets/jev.key`, so a source folder such as this repo's `lib/orche/secrets/` passes the Jev layer (a code-blind search of it is still the envgate secrets-folder rule's).
- A command nested past the depth limit, or one that fails to scan, is refused as too complex. A guard against accidents with known gaps (a script file, `node --env-file`, `git show :.env`, variable indirection), listed in `envgate.py`; the hook adds well under 1 ms of scanning to a 2 KB command (the process itself costs about 18 ms, mostly interpreter start-up).

Known gaps (best effort, documented rather than closed):
- **Env-read gate (envgate).** An environment copy printed later through an alias (`env = {**os.environ}; print(env)`, `const env = {...process.env}; console.log(env)`) or `print(os.environ.copy())` passes, the same class as the `env=os.environ` hand-over; destructuring (`const {NODE_ENV} = process.env`) and `os.execvpe(…, os.environ)` are refused (false positives); a secret-shaped name within 40 characters of a harmless lookup (`os.environ.get("HOME"); TOKEN_LIMIT = 5`) is refused; `*.key` also matches Keynote decks and i18n `.key` files; `declare -F` (function names only) is refused; `echo $MONKEY_PATCH` / `$TOKENIZERS_PARALLELISM` are refused; `docker compose config` (prints interpolated `.env` values), `cat .envrc` and quote-split names (`cat .e""nv`) pass; a script file that reads a secret (`python3 x.py`), `node --env-file=.env`, `git show :.env` and variable indirection pass.
- **Jev rules (jevgate).** A script file that reads the key; variable indirection (`s=security; $s …`); brace expansion; a glob in a directory component; `tar`/`cp -r`/`find -exec` on the directory; flags from `xargs`; Read through a symlink; Grep over an ancestor of ORCHE_HOME without a glob.
- **Masking (`lib/orche/mask.py`).** A letters-only password written as prose (`password: correcthorse`) is not masked; an all-caps value that looks like an ENV_NAME (`DB_PASSWORD=HUNTERTWO`, a base32 TOTP seed such as `OTP_SECRET=JBSWY3DPEHPK3PXP`) is read as a reference and not masked.
- **Dispatch relay.** Only `orche dispatch` relays; a dispatch block left in a reply (fenced or not) is plain text and sends nothing, so Main sees `block N: …` lines for every real dispatch.

Implementation note: every denial of `hooks/env_read_gate.py` and `hooks/scope_gate.py` is also logged to `ORCHE_HOME/logs/events.log` as `gate=<env_read|scope> session=<ORCHE_SESSION or -> tool=<tool> what=<the reason, whitespace collapsed, first 80 chars>` (best effort; the reason names the rule, never a secret value). An allowed call logs nothing.

Implementation note: `hooks/env_read_gate.py` also refuses a Claude Code transcript: a Read `file_path`, a Grep `path` or any word of a Bash command that runs through `/.claude/projects/` or `/claude-profiles/` to a `.jsonl` (reason: `[orche] transcripts are never read into a session: one refills the context and forces a compaction. orche metrics summarizes them; orche status carries the state.`). One transcript refills the context and forces a compaction; `orche metrics` reads them as a CLI, outside the model, and is unaffected. Logged like the other denials.

Pre-commit: `gitleaks` (or equivalent).

### 10.6 Audit log

`~/.claude/orche/secrets/audit.log`, append-only: `ts, action, name, env, backend, actor, request_id`. Never values.

### 10.7 Remediation (operator, one-time)

1. Rotate any secret previously exposed in transcripts or files.
2. Add new values only via `orche secret add`.
3. Optional: `orche secret redact-transcripts` replaces values with `[REDACTED]` after a backup; transcripts are not deleted.
4. Separate environments by cloud project.
5. Move developers to personal IAM identities.

---

## 11. Janitor

### 11.1 Triggers

| Kind | Trigger | Applies to |
|---|---|---|
| State-based | registry `retired`, GRAVEYARD entry, Orca card removal, `orche discard` | Unit worktrees, sessions (archived), unit artifacts |
| Age-based | Regenerable artifacts only | Build caches idle 7 days; temp dirs; one-off scratch with no owning unit. Implementation note: the Orca Codex `.tmp` sweep is opt-in (config `janitor.codex_tmp`, off by default) |

Dormant ≠ discarded. Sleep never triggers cleanup.

### 11.2 Rules

| ID | Rule |
|---|---|
| R-JAN-1 | Sessions are never deleted by age. Set `cleanupPeriodDays: 3650` in Claude Code settings. |
| R-JAN-2 | Sessions of retired/discarded units are compressed to `ORCHE_HOME/archive/<session>/` (archive, not delete). Archives are deleted only on explicit instruction. Implementation note: the path moved from `~/.orche-archive/<unit>/`. |
| R-JAN-3 | Before deleting anything, append a GRAVEYARD line. |
| R-JAN-4 | Deleted files go to `ORCHE_HOME/trash/<date>/`; permanently removed after 30 days. `orche janitor undo|restore` available. Implementation note: the path moved from `~/.orche-trash/<date>/`. `orche janitor restore <unit>` writes a unit's archived sessions (R-JAN-2) back to their original paths and refuses to overwrite an existing file (exit 3); a person says it to bring a retired unit's conversations back. |
| R-JAN-5 | First week runs dry-run only; the dry-run report is reviewed by the user. Implementation note: `orche janitor run` stays a dry run until `orche janitor enable`; `--dry-run` always forces one. Implementation note: the first `orche install` (and so `orche update`) writes the enabled flag (install-state `janitor_default`, so `orche janitor disable` stays) and the heartbeat runs the daily pass (§11.4). Implementation note: there is no janitor LaunchAgent. |
| R-JAN-6 | Buckets are advice, not permission. |

GRAVEYARD format (`docs/GRAVEYARD.md` per repo + global `~/.claude/orche/graveyard.md`):

```
| date | what | why | restore location | decided by |
```

### 11.3 Untouchables

- Sessions and artifacts of active or dormant units (Implementation note: the protected statuses are `active`, `paused` and `onboarding`; a `dormant` unit's build caches may age out)
- Uncommitted changes (`wip:N` state pauses; show diff, human decides)
- Checkouts used by a live session (sacred)
- `.env*`
- `memory/` and `.claude/agent-memory/`
- registry, cards, ledgers
- Build caches used within 7 days

### 11.4 Schedule

- Daily through the heartbeat: a full `orche reconcile` pass starts `orche janitor run` detached (`procs.spawn_orche`, output in `logs/janitor.log`), never inline, when the last pass is a day old (`janitor.due`: `ORCHE_HOME/janitor/last-report.md` missing or its mtime older than `DAILY_S` = 24 h; every pass, dry or live, writes it) and no pass is running (`janitor/.pass.lock`, created O_EXCL by `orche janitor run` and removed at its end, stale after 2 h; a second `orche janitor run` while it is held exits 3). The pass row reads `start the janitor's daily pass (last pass <age> ago)` or `(last pass never)`; `--dry-run` says `would start`. A calendar LaunchAgent is not used: launchd skips a calendar job while the Mac is off or no user session is loaded, while the heartbeat runs every `every_s`.
- SessionEnd hook: for ORCHE-launched sessions (`ORCHE_SESSION` set) move the ended session's scratch into the janitor trash (recorded, undoable); never deletes, and other sessions are left to the janitor's age rule.
- Weekly HR report: one line of reclaimed/archived volume.
- Weekly pass: `orche janitor run` runs it once per 7 days (state `weekly.last` in `ORCHE_HOME/janitor/state.json`; `--weekly` runs it now; never on `--dry-run`). Per project: the audit's mistake classes (§5.10.1); classes not yet in the Enforcement table are written there and Main gets one `info` pointer from `janitor`, `audit: <n> new mistake classes in STANDING.md Enforcement; say "enforce <class>" to dispatch it` (nothing when none); then the verify skill's drift unit (§5.4). `orche doctor`'s `janitor` row reads `daily through the heartbeat, last pass <age ago or never>, weekly pass <date or never>, <on or off (dry run)>`, a WARN (`; see logs/janitor.log`) when the last pass is older than 2 days while the heartbeat is on.
- Tracked generated artifacts in git: untrack via `.gitignore` + `git rm --cached` PR (never history rewrite by janitor).

---

## 12. Git / GitHub rules

### 12.1 Agent rules (STANDING defaults)

| ID | Rule |
|---|---|
| R-GIT-1 | No direct push to `main`. |
| R-GIT-2 | Feature branch → published PR → head review → merge queue (a pre-review only when someone asks, R-REVIEW-1). |
| R-GIT-3 | ORCHE pushes once per verified done. |
| R-GIT-4 | Docs-only commits include `[skip ci]`. |
| R-GIT-5 | Tags, releases, deploys require user approval. |
| R-GIT-6 | Flake retry: one new build only. |
| R-GIT-7 | Agents MUST NOT retarget PRs, merge, rebase, or force-push unless the brief explicitly allows it. |

### 12.2 Standard CI template

```yaml
on:
  pull_request:
    paths-ignore: ['**.md', 'docs/**', '.orche/**']
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      # dependency cache step for the stack (setup-node cache, actions/cache, ...)
      - run: ./scripts/verify/ci.sh
```

- No `macos-*` runners. Heavy jobs go to self-hosted runners or the operator machine.
- Deploys run through the hosting/cloud pipeline, not ad-hoc Actions.

### 12.3 Budget

Organization billing: Actions budget with "stop usage when limit reached" enabled (hard stop). Consider making repos public when privacy is not needed.

### 12.4 Merge queue — the integrator

`lib/orche/queue/integrator.py` (`orche queue run <p>`), `queue/gate.py`, `queue/gh.py`. The queue reads each queued PR at test time (conflicts, the gate, its GitHub state and review decision) and routes the outcome to the unit and Build.

| ID | Rule |
|---|---|
| R-QUEUE-1 | Every head is traffic control and never merges by hand. A gated unit's verified `done` publishes it: ORCHE pushes the unit's branch (never forced) and opens or updates its PR to the base branch. Only the merge queue lands PRs; R-GIT-1 holds (the queue merges through `gh pr merge`, never a push). |
| R-QUEUE-2 | An entry is `{id, pr, head, branch, unit, writer, parent, rev, verify, approved_by, accept, first, at, state, restarts, drop, test}`, one per PR, head fixed at add. `orche queue add` (Build or Main; the runner after two PASS reviews) refuses unless: one open PR on the base whose head is the unit's HEAD; a ledger row ≥ type-check-only for it; the pre-review the mode requires (or `--accept`); a gate in the head's tree; no `[skip ci]` commit on a code change anywhere in base..head (or `--accept`). `--pr N` reads the head from the queue's clone, fetched from origin first; a failed fetch refuses with the reason. in a team project only Main adds a unit, on the owner's word, and the branch is published first when it has no open PR at HEAD (R-TEAM-2). |
| R-QUEUE-3 | The integrator is model-free, one per project (flock), in `ORCHE_HOME/queue/<p>/checkout` (a clone, never a unit's worktree); FIFO, `--first` entries ahead; candidate = `git merge --no-ff` of the head onto the freshly fetched base. |
| R-QUEUE-4 | Gate = `scripts/verify/ci.sh` (when tracked and executable) then the entry's committed VERIFY scripts; argv, no shell, `CI=1`, timeout `queue.timeout_s`, no secrets: the environment drops every name holding TOKEN, SECRET, KEY, PASSWORD, PASSWD or CREDENTIAL, an AUTH word (SSH_AUTH_SOCK), `*_PWD`, `JEV_*`, `CLAUDE*`/`ANTHROPIC*`, `ORCHE_HOME`, `CODEX_HOME` and the session/run names, and carries `ORCHE_QUEUE_RUN=<tag>`; nothing sandboxes the gate beyond this (it is the project's code, run as the user with the network); its verdict comes from ci.sh's last `verdict:` line, else type-check-only. The queue never reads CI status; `[skip ci]` changes nothing. No automatic re-run. |
| R-QUEUE-5 | Green: re-fetch the base (moved → re-test, ≤ `queue.max_restarts`), `gh pr merge --squash --match-head-commit <head>`, then prove the commit the squash made (origin/<base>'s first-parent child of the tested base: the one parent base, tree equal); a fetch that fails twice proves nothing: the entry stays `testing` (`test.landed`) and the next pass proves it; rows (PR, head) and (none, merge) with verifier `queue`. Never merge red, never `--admin`, never force-push. |
| R-QUEUE-6 | Red, conflict or timeout: the entry leaves the queue; a unit entry gets an AMEND relayed as the writer's head (Build for PR entries) (`by=queue`) allowing only `git fetch origin && git merge origin/<base>`; a PR entry gets an urgent pointer to Build. The 3rd return of a unit inside W sends no AMEND: the head gets `blocked`. A refused AMEND reaches the head as `failed`. |
| R-QUEUE-7 | Drops: head moved after PASS, PR closed or merged outside, retargeted, draft, `orche queue drop`. Each is logged; the writer's head (Build for PR entries) gets one pointer (held for head-moved). |
| R-QUEUE-8 | A base that moved outside the queue is gated alone first; red closes the queue (only `--first` entries test; a green one or `orche queue resume` reopens it); a failed proof closes it too, and then nothing tests, not even `--first`, until `orche queue resume`. |
| R-QUEUE-9 | `orche queue pause`, `orche flow pause` (G8), a GitHub refusal, exhausted restarts and 3 passes (`integrator.MAX_STOPS`) in a row stopped by git, gh or the clone (an urgent pointer to Build and Main naming the failure, masked; a pass that gets through or `orche queue resume` resets the count) stop new tests until `orche queue resume`; a gate in flight finishes. A usage hold does not pause the queue. Queue-wide notes (a pause, exhausted restarts, a GitHub refusal) go to Build, never to a writer's head. |
| R-QUEUE-10 | Driver: `orche queue add|resume` and the heartbeat class `queue` start `orche queue run <p>` detached when entries wait and the lock is free (never from inside a Codex sandbox); with the lock free, a testing entry returns to the front when its pid is dead (at once) or after `queue.lost_after_s` (3600) with a live pid; only processes carrying its gate's `ORCHE_QUEUE_RUN` tag, or its recorded step verified by pid, group, start time and command, are stopped with the groups they lead, never a bare recorded pgid. Before a pass's first git, the lock files a killed git left in the queue's clone (`.git/*.lock`, `.git/refs/**/*.lock`) are removed. |
| R-QUEUE-11 | Landed: the writer's head (Build for PR entries) gets a held note per merge and one urgent note when every child unit of its unit (the entry's `parent`, else the unit brief's `parent:`) landed or ended; the head then reports its unit. No queue pointer goes to a worker. |
| R-QUEUE-12 | Method squash, always (`integrator.METHOD`, `github.merge`), land mode `queue.land` (auto; `owner` keeps new entries untested until Main runs `orche queue approve` on the owner's word, asked once per batch; approval is for the PR; §4.4); back on `auto`, waiting entries are released (`approved` by `land-auto`, never the owner's word). |

---

## 13. Third-party skill suites

| ID | Rule |
|---|---|
| R-GSTACK-1 | No third-party skill suite is required: each persona carries its own craft, so every division works the same with or without any suite installed. |
| R-GSTACK-2 | Deleted: folded into R-GSTACK-1. |
| R-GSTACK-3 | Deleted: folded into R-GSTACK-1. |
| R-GSTACK-5 | Deleted: installation runs no third-party upgrade. |

---

## 14. File layout and interfaces

### 14.1 Instance root

```
<plugin repository>
├── .claude-plugin/          # plugin.json, marketplace.json
├── .github/workflows/       # CI
├── agents/                  # plugin agent definitions (hr, exec, deep-reasoner, ...)
├── bin/                     # orche (POSIX sh launcher), orche.py
├── docs/                    # spec.md, glossary.md, faq.md, principles.md, orca-sidebar.md
├── hooks/                   # hooks.json, run.sh and one .py per hook
├── lib/
│   ├── find_python.sh
│   └── orche/               # the CLI modules and the packages flow, hr, install, janitor, queue, review, runs, secrets
├── skills/                  # orche, orche-apply, orche-build, orche-main
├── templates/               # launchd, personas, project, queue, review, run, unit, verify, statusline-segment.sh
├── tests/
├── install.sh
└── CHANGELOG.md, CONTRIBUTING.md, LICENSE, README.md, SECURITY.md
~/.claude/orche/             # ORCHE_HOME: the instance root, see the note below
```

Implementation note: `ORCHE_HOME` (default `~/.claude/orche/`) holds `registry.json`, `config.json`, `install-state.json`, `hr-log.md`, `graveyard.md`, `janitor.enabled`, and the directories `logs/` (`logs/intake/<proj>.log` and `logs/liaison/<proj>.log`: detached intakes and card refreshes), `status/`, `inbox/<session>/`, `cards/`, `ledger/<proj>.tsv`, `secrets/` (`requests/`, `audit.log`, `shares.json`), `grader/`, `jev-eval/`, `janitor/`, `sessions/`, `archive/<session>/`, `trash/<date>/`, `runs/<proj>/` (run locks, STOP markers, pending sweeps; §14.13) and `runner/mcp/` (run-tag markers; §14.10). Templates, hooks and the CLI ship in the plugin repository (`templates/`, `hooks/*.py` launched through `hooks/run.sh`, and `bin/orche`: a POSIX sh launcher that runs `bin/orche.py` under the first Python 3.11+ found by `lib/find_python.sh`, the finder `hooks/run.sh` also sources; with none it prints the guard message to stderr and exits 1), not under `ORCHE_HOME`. `orche install` records the original value of every setting it changes in `install-state.json` and `orche uninstall` restores them.

### 14.2 Project-local layout

```
<repo>/
├── PROJECT_CARD.md
├── .orche/
│   ├── STANDING.md
│   ├── STATUS.md
│   ├── HANDOFF.md
│   ├── DECISIONS.tsv
│   ├── project                # project key (status-line marker)
│   ├── units/<unit-id>/{BRIEF.md,BRIEF.rev<N>.md,REPORT.md,REPORT.prev.md,RUN.json}
│   ├── div/<division>/{CHARTER.md,STATUS.md,HANDOFF.md,DECISIONS.tsv}
│   └── feat/<name>/{CHARTER.md,STATUS.md,HANDOFF.md,DECISIONS.tsv}
├── .claude/
│   ├── agents/{div,feat}-<project>-*.md
│   ├── agent-memory/<name>/MEMORY.md
│   ├── skills/verify-<app>/
│   └── settings.local.json  # per worktree: "agent"
├── docs/
│   ├── LEDGER.tsv           # export of ORCHE_HOME/ledger/<proj>.tsv
│   ├── RUBRIC.md
│   ├── GRAVEYARD.md
│   └── runbooks/*.md        # Ops (stage=public)
└── scripts/
    ├── verify/*.sh
    └── ops/health.sh        # Ops (stage=public)
```

Implementation note: the inbox moved to `ORCHE_HOME/inbox/<session>/` (§4.4). Unit memory (`.orche/div|feat/<name>/`) lives in the unit's own worktree. `.claude/settings.local.json` is added to the repository's `info/exclude` and is never committed. Implementation note: `orche init` and `--refresh` exclude every ORCHE file: `.orche/`, `.claude/settings.local.json`, the units' personas (`.claude/agents/div-*.md`, `.claude/agents/feat-*.md`) and each file `templates/project` copies outside `.orche/` (`hr/onboarding.py` EXCLUDE_LINES); a file the project already committed stays tracked (git applies exclude to untracked files only).

### 14.3 `registry.json` schema

```json
{
  "limits": {
    "per_project_per_day": 8,
    "per_project_per_week": 40,
    "per_request_auto": 4,
    "global_per_day_freeze": 20,
    "idle_days_to_sleep": 14,
    "max_awake_sessions": 12
  },
  "frozen": false,
  "projects": {
    "example-web": {
      "repo_id": "<orca-repo-id>",
      "stage": "building",
      "main_session": "main-example-web",
      "card": "~/.claude/orche/cards/example-web.md",
      "standing": ".orche/STANDING.md",
      "aliases": ["web", "website", "homepage", "site"],
      "divisions": {
        "rnd":      { "status": "active",  "session": "div-example-web-rnd",      "model": "claude-opus-5-5" },
        "research": { "status": "dormant", "session": "div-example-web-research", "model": "claude-sonnet-5-5" },
        "build":    { "status": "active",  "session": "div-example-web-build",    "model": "claude-opus-5-5",
          "features": [
            { "name": "pages", "branch": "feat/pages", "scope": ["src/pages/**", "content/**"],
              "session": "feat-example-web-pages", "status": "active",
              "created": "2026-10-20", "last_activity": "2026-10-20" }
          ] },
        "gtm":      { "status": "active",  "session": "div-example-web-gtm",      "model": "claude-opus-5-5" },
        "ops":      { "status": "dormant", "session": "div-example-web-ops",      "model": "claude-opus-5-5" }
      }
    }
  },
  "events": []
}
```

Field constraints:

| Field | Constraint |
|---|---|
| `stage` | `building` \| `public` |
| `divisions.*.status` | `active` \| `dormant` (Implementation note: `active` \| `dormant` \| `paused` \| `retired` \| `onboarding`) |
| `features[].status` | `active` \| `dormant` \| `retired` (Implementation note: `active` \| `paused` \| `retired` \| `onboarding`) |
| `aliases` | Unique across projects unless intentionally shared (shared ⇒ R-PROJ-8) |
| `divisions.ops.status` | MUST be `dormant` when `stage = building` |
| `custom_divisions` | Implementation note (setup): optional object `{<name>: {label, charter, model, effort}}`; absent means `{}` (readers default it, nothing migrates). Each name follows the custom-division rules above (R-TOPO note) and its runtime record lives under `divisions.<name>` like a standard division; a `divisions` key that is neither standard nor in `custom_divisions` is invalid. `effort` ∈ `low` \| `medium` \| `high` \| `xhigh` \| `max` |
| `orche_version` | Implementation note: optional: the ORCHE version whose templates last rendered the project's files; written by `orche init` (`^\d+\.\d+\.\d+$`) |
| `team` | Implementation note: optional non-empty string: the team plugin the repository declares (R-TEAM-1); written by `orche init` and `orche update` only when it changes, removed when no longer declared |
| `events[]` | `{ts, actor, action, target, detail}`; every HR change appends one. Implementation note: `{at, type, project, unit, by}` with `type` ∈ create, sleep, wake, retire, freeze, unfreeze, stage; the file carries `"version": 1` |

Implementation note: a division record carries no `effort`. A pane's effort is its persona's `effort:` line (from the onboarding default or the custom spec); a headless run's is its TIER (§14.10). An `effort` key in an older record is ignored.

| ID | Rule |
|---|---|
| R-MODEL-1 | Implementation note: the model string (`vendors.errors`). Non-empty, no whitespace or control characters, and either `codex` (Codex's own default model; `-m` not passed), `codex:<id>` with a non-empty id, or any other id, which is a Claude model id (aliases such as `sonnet` included). A `:` with a prefix other than `codex` is refused: "unknown vendor 'x' — Claude ids are plain (claude-opus-5-5), Codex is `codex` or `codex:<model id>`". `vendor_of` is `codex` for `codex`/`codex:*`, else `claude`. It is stored in `divisions.<d>.model` and, for a custom division, also `custom_divisions.<d>.model` (kept equal by every writer); no effort is stored. A feature agent has no model of its own: it runs on its division's, in panes and headless runs. BRIEF.md gains `model: <model>   effort: <low\|medium\|high> (TIER <tier>)` after its `worktree:` line for a division or feature target. |
| R-MODEL-2 | Implementation note: recommendation and availability. A new division gets the recommended model of its name (config `kinds`; a name not in the table takes the `custom` row): rnd `claude-opus-5-5` (pane effort high), research `claude-sonnet-5-5`, build `claude-opus-5-5` (medium), gtm `claude-sonnet-5-5`, ops `claude-sonnet-5-5`, design `claude-opus-5-5`, content `claude-sonnet-5-5`, any other custom `claude-opus-5-5`. Claude is always offered. Codex is offered when `models.codex_bin` (default `codex`) is found and `<bin> --version` exits 0 within 10 s (`ok <version>`); found but failing is `broken` (fix `npm install -g @openai/codex@latest`), not found is `absent` (`npm install -g @openai/codex, then codex login`). Only `--version` runs: there is no login probe, and no cache. Existing divisions keep their recorded models. |

Implementation note: a project record carries no `liaison_session` (the Liaison is headless, §14.14). The key is no longer written or read; an existing one is ignored.

### 14.4 Dispatch block grammar

```
input      := block ("---" NL block)*          ; stdin of `orche dispatch`, or its --file
block      := (brief | amend | stop)
brief      := line+                          ; required fields per R-DISPATCH-1
amend      := "AMEND" SP unit_id SP "rev" SP int NL (note | supersedes | line)*
note       := text NL                       ; prose before any key, or after SUPERSEDES; extends the note
supersedes := "SUPERSEDES" SP text NL        ; optional, any unindented line, the last one counts; by convention "rev N-1: <what>"
stop       := "STOP" SP unit_id (SP text)? NL (reason | line)*   ; the only field allowed is TO, before or after the reason
reason     := text NL                       ; any plain line that is not "TO …"; the reason may span lines
line       := field | comment | blank
field      := KEY SP+ value NL (continuation)*
continuation := SP SP+ text NL               ; extends the field above, whatever the text (a "#" here is content)
            | text NL                        ; a plain line that is not a KEY line also extends the field above
comment    := "#" text NL                    ; an unindented "#" line: always dropped, even mid-value (indent PR refs: "  #25")
KEY        := "TO" | "GOAL" | "SCOPE" | "CONTEXT" | "ACCEPT" | "VERIFY" | "TIMEBOX"
            | "FORBID" | "REPORT" | "TIER" | "ROLLBACK" | "MODEL"   ; each at most once per block; ROLLBACK and MODEL optional
TIER value := "mechanical" | "standard" | "hard"
unit_id    := <project> "-" <slug> "-" yyyymmdd "-" n
```

- Multiple blocks per run are allowed; each is one unit.
- Implementation note: a brief must open with a KEY line; one that opens with prose is refused (`line 1 "<text>" is not a KEY line; a brief starts with TO.`). After the first key, a plain line that is not a KEY line continues the field above (before, `unknown keys …` refused it); a refusal for a missing field or a bad TIER names the line that was read into the field above. Every parse refusal quotes the offending line (60 chars) and states the fix in one clause.
- Implementation note: blocks are delivered one by one. Every valid block is delivered even when another block in the same run is refused; `orche dispatch` prints one line per block in order (`block N: delivered <unit> → <target>` / `block N: refused — <reason>` / `block N: skipped …`) and exits 2 when any block was refused. It remembers delivered blocks per sender (body hashes in `sessions/<sender>.dispatched.json`) for 10 minutes; within that an identical block is skipped with an events line (`dispatch skipped by=<session> unit=<unit> reason=already delivered`), after it the block is delivered again, so Main re-runs the whole input after fixing a refusal without duplicates. A block is refused when it holds a code fence, the verb line of another STOP/AMEND block (two blocks run together), or, in a STOP, a KEY line other than TO. A refused STOP does not hold back a valid brief in the same run. Every refusal writes `dispatch refused by=<session> blocks=<n> refused=<k> rules=<G-rules, comma-joined, or ->` to events.log (ids and counts only, never the refusal text). Exit codes: 0 all delivered or skipped, 2 any refused, 1 usage error.
- Implementation note: the Stop hook no longer reads the reply, so a fenced `dispatch` block in a reply is neither relayed nor refused (0.3.3–0.6.2 relayed it and refused an unrelayable one with exit 2).
- `orche dispatch` assigns `unit_id` if absent and prints it in the block's line.
- Implementation note: `lib/orche/brief.py` implements this grammar for `orche dispatch`, `orche run` and `orche report`. A key written twice is refused (`duplicate keys …`; before, the last value won). A `#` line indented by two spaces inside a field value is kept as content; an unindented `#` line is a comment and dropped, even mid-value (before, every `#` line was dropped). An AMEND or STOP whose unit id is malformed is refused with `bad unit id …`.
- Implementation note: an AMEND writes the current BRIEF.md's `rev:` + 1 (the N in the block is advisory; a different N is logged) and keeps every revision as `BRIEF.rev<N>.md`. An AMEND or STOP of a unit with no readable BRIEF.md (missing, unreadable or not UTF-8) is refused (`unknown unit …`). A dispatch, AMEND or STOP to a retired target is refused before anything is written (`target <session> is retired.`, as `orche run` and `orche report` refuse it). A dispatch or AMEND is also refused when its target is asleep (`dormant`/`paused`) and gated by stage, the rule `orche wake` applies (`<session> is asleep and gated by stage (<proj> is <stage>): re-target, or run orche stage set <proj> public --yes`), or when the target's recorded worktree is missing. A STOP files its pointer for the `rev:` in BRIEF.md (before: the count of `BRIEF.rev*.md` files, or 1). A brief's target comes only from the `## Brief` TO field (no header `to:` fallback).

### 14.5 Inbox pointer JSON

File: `$ORCHE_HOME/inbox/<recipient-session>/<ISO-ts>-<unit>.json` (never overwritten; delivered pointers move to `.drained/`).

```json
{
  "unit": "example-web-pages-20261020-1",
  "from": "feat-example-web-pages",
  "to": "div-example-web-build",
  "status": "done | failed | blocked | question | info | external-request | stop",
  "report_path": "/abs/path/to/.orche/units/example-web-pages-20261020-1/REPORT.md",
  "head_sha": "<40-hex or empty>",
  "ledger_row": true,
  "brief_rev": 2,
  "urgent": false,
  "summary": "<= 5 lines, masked",
  "at": "2026-10-20T03:12:00Z"
}
```

Optional fields: `memory_saved` (bool), `depends_on` (list of unit ids), `via` (`runner`: filed by `orche run`; `dispatch`: a pane delivery's pointer to its target), `from_project` (an external request), `pr` (int), `review` (`{"pre": {verdict, reviewer, model, fallback, round, open, path}, "build": {verdict, model, path}}`), `queue` (`"auto"` or the position); `via` also takes `queue` and `review` (a `via: queue|review` `info` is held unless urgent, as any other note).

Implementation note: the fields and statuses above replace the earlier draft (`landed | needs-verify | failed | blocked | stopped | external-request`, `report`, `branch`, `pr`, `ts`). `info` (relayed brief or AMEND) and `stop` (STOP) go to a pane target only, with `via: dispatch`; a runner target gets none; an external request carries the request text in `summary`.

### 14.6 Hook contracts

Common stdin JSON fields from Claude Code: `session_id`, `transcript_path`, `cwd`, `hook_event_name`; plus event-specific fields (`tool_name`, `tool_input` for PreToolUse; `prompt` for UserPromptSubmit; `stop_hook_active` for Stop; `source` for SessionStart).

| Hook | Event | Input used | Output convention | Responsibilities |
|---|---|---|---|---|
| `dispatch-relay` | Stop | `last_assistant_message` (Implementation note: the transcript file is written asynchronously and can lag, so the hook input field is authoritative; `transcript_path` is parsed only when the field is absent; a relay is deduplicated per session by the hash of the message text), `stop_hook_active` | Exit 0 = allow stop. Exit 2 + stderr message = Claude continues with that message | Relays nothing (dispatch moved to `orche dispatch`, §4.1); touch the session's activity; re-arm the wake (R-INBOX-4); block completion claims without proof (the done gate, §14.11); block REPORT without memory-save; run finish predicate for night runs. Never types `/clear` or any slash command (R-MEM-3). MUST NOT loop: if `stop_hook_active` is true and nothing new to enforce, exit 0 (neither the done gate nor the night predicate runs then) |
| `report-inject` | UserPromptSubmit | `prompt`, `cwd` | Exit 0; stdout is added to the model's context | Inject pending pointers (held notes at drain triggers, R-INBOX-2/3); remove the wake marker; stalled-worker marks; `[project]` tag resolution; Jev ②③ (phase 5) |
| `env-read-gate` | PreToolUse | `tool_name`, `tool_input` | JSON `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"|"ask"|"allow","permissionDecisionReason":"..."}}` | §10.5 |
| `scope-gate` | PreToolUse | `tool_name`, `tool_input.file_path`, `cwd` | Same as above | Deny edits outside own worktree/scope; deny code edits from Main |
| session-start | SessionStart | `source`, `cwd` | stdout / `additionalContext` injected; one line appended to `$CLAUDE_ENV_FILE` | Inject resume order pointers (HANDOFF, STATUS, DECISIONS tail) and STANDING; append `export PATH=<shlex-quoted plugin bin>:"$PATH"` to `$CLAUDE_ENV_FILE` (when it is set and the plugin bin is not already on PATH), written once per env file (a file that already holds the line is left alone; a missing final newline is added first), so the session's Bash finds `orche` |
| scratch-clean | SessionEnd | `ORCHE_SESSION` | none | Move the ended ORCHE session's scratch to the 30-day trash (never deletes) |

All hooks: fail-open on internal error except `env-read-gate` and `scope-gate`, which fail closed (deny).

Implementation note: hook files are `hooks/dispatch_relay.py`, `report_inject.py`, `env_read_gate.py`, `scope_gate.py`, `session_start.py`, `session_end.py`, registered by the plugin's `hooks/hooks.json` as `hooks/run.sh <hook>`: the POSIX launcher runs the hook under the first Python 3.11+ found by `lib/find_python.sh` (`python3.14` … `python3.11`, `python3`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`; shared with the `bin/orche` launcher) and, with none, logs one timestamped line to `logs/hooks.log` and exits 0. `orche doctor`'s `python3 (login shell)` row is OK for a 3.11+ `python3`, WARN for an older or missing one while the same finder (run under `/bin/sh` from a login shell) still picks a 3.11+, and FAIL only when it finds none. `orche install --mode settings` registers the same `run.sh` commands; `orche install` in plugin mode (the default) removes them from `settings.json` (backup and install-state record as usual), so switching modes never registers a hook twice, and `orche doctor` WARNs when settings.json still holds them while the plugin is enabled. Every hook works within a 3.0 s budget (`ORCHE_HOOK_BUDGET_S`) and ignores subagent events; no hook types into a pane: `orche deliver` and `orche nudge` do, detached, with `--enter --wait-submit 20`. The two gates deny only on a positive pattern match; on an internal error they allow and log (they do not fail closed).

### 14.7 Orca CLI calls used

| Call | Purpose |
|---|---|
| `orca terminal list` | Find role panels |
| `orca terminal send` | Implementation note: only `orche deliver` (`[orche] inbox: <unit> rev <N>`) and `orche nudge` (`[orche] inbox: new reports`), detached, `--enter --wait-submit 20`; never `/clear` |
| `orca terminal wait` | `--for tui-idle` before typing (R-INBOX-4/6) |
| `orca terminal read` | the composer `draft`: never type into a user's text |
| `orca terminal rename` | Panel title = role name |
| `orca terminal create` | Open role panel on wake |
| `orca terminal switch` | Bring a panel to the front (`orche wake main-<p>`, `orche init`) |
| `orca worktree create` | Division/feature worktrees (feature parent = Build) |
| `orca worktree list` | Reconcile with registry |
| `orca worktree set` | Card status/comment |
| `orca worktree rm` | Retire/rollback |
| `orca orchestration send` | Dispatch and AMEND mail (`--to dispatch:<id>`) (Implementation note: wrapped but not used for delivery) |
| `orca orchestration check` | Worker liveness / mail |
| `orca automations create` | Night runs, weekly drift check, card refresh, Ops alerts |
| `orca search` | L4 transcript search |
| `orca repo add` | Register a new project |

### 14.8 Naming

| Item | Format |
|---|---|
| Main session | `main-<proj>` |
| Division session | `div-<proj>-<division>` with division ∈ {rnd, research, build, gtm, ops} |
| Feature session | `feat-<proj>-<feature>` |
| Division worktree branch | `div/<division>` |
| Feature branch | the branch Orca names by default; `<prefix>/<feature>` when the project sets `publish.per_project.<p>.branch_prefix` (ORCHE renames it right after creation) |
| Division worktree | `div-<division>` |
| Feature worktree | `feat-<feature>` |
| Unit id | `<project>-<slug>-<yyyymmdd>-<n>` |

Every name is made by `hr.session_name` (§14.12).

### 14.9 Roster: identity, place and routing

Implementation note: `lib/orche/roster.py` answers every who / where / to-whom question; hooks and commands never walk `registry.json` for them.

- **Identity.** `ORCHE_SESSION` when set; else the division or feature whose recorded worktree holds the cwd (when its record names a session); else unidentified. The main checkout and Orca pane titles never identify a session. An `ORCHE_SESSION` naming no registered session is an *unregistered session*: it keeps its own inbox but has no role.
- **Unidentified sessions** drain nothing, dispatch nothing (refused with a hint naming the project's Main), park no refusal (logged only), run no night predicate and get a note plus the generic card at SessionStart (in a project's main checkout the note is `[orche] this is not an ORCHE pane: Main's rules apply in the main-<proj> tab — you can run: orche wake main-<proj>`); `orche report`, and `orche decide` without `--file`, refuse them.
- **Place.** A cwd's project is the nearest registered checkout or `.orche/project` marker; its unit is the deepest division or feature worktree holding it (a live record beats a retired one at the same path). Place decides the scope gate, the night predicate's directory and default projects (status line, secrets, ledger, discard), never identity; the done gate's claimant is the identity (§14.11).
- **Attribution.** Events, HR log, graveyard, secret requests and audit rows credit `ORCHE_SESSION`, else `user`; the ledger's producer defaults to `ORCHE_SESSION`.
- **Memory dir.** Main `.orche/`, a division or feature `.orche/<div|feat>/<name>/` in its worktree; `orche decide` appends to the session's own `DECISIONS.tsv`.
- **Recipient.** The sender of the unit's current brief revision (its `from:`) when that is a live Main or division of the project other than the reporter; otherwise a feature agent's division (Main when the division has no session), and everyone else's Main (R-INBOX-1).
- **TO.** An exact session; else a division of the sender's project; else the Main of the project named by key or alias. A Main of another project, by key, alias or session name, files an external request (R-XPROJ-3); any other session of another project is refused.

### 14.10 Headless calls

Implementation note: `lib/orche/headless.py` is the one way ORCHE starts `claude -p`. It runs the unit worker of `orche run` (R-DISPATCH-9), the intake sweep (R-DISPATCH-11), the Liaison (§14.14) and the triage classifier (§7.2). A caller builds a `Call` and keeps only its own policy.

- **Contexts.**
  - *project* (unit worker): `--setting-sources project --permission-mode bypassPermissions` (a headless worker cannot answer prompts; the PreToolUse gates and the deny list still block, and allow rules no longer gate a call); tools under `--allowedTools`/`--disallowedTools`; ORCHE's two PreToolUse gates (env-read, scope), passed with `--settings`.
  - *sealed* (intake, the Liaison): `--restricted --tools <the allowed tool names>`, `--no-session-persistence`. No permission mode: `--restricted` refuses `bypassPermissions`.
  - *blank* (triage): a fresh directory outside `$HOME`, removed afterwards; `--setting-sources project --tools ""`, `--no-session-persistence` and its own `--system-prompt`.
  - Every call passes `--strict-mcp-config`.
  - No lifecycle hook (SessionStart, UserPromptSubmit, Stop, SessionEnd) runs in a headless call. Neither does any user hook or user plugin.
- **Command line.**
  - `claude -p --model M [--effort E] --output-format json --max-turns N <context flags> --strict-mcp-config [--mcp-config <marker>] [--allowedTools …] [--disallowedTools …] [--no-session-persistence] [--append-system-prompt-file F | --system-prompt S] [--settings <gates>]`.
  - There is no `--version`/`--help` probe and no fallback form. Claude Code must list the 15 flags of `headless.FLAGS`, and `orche doctor` checks them (row `claude flags`).
- **Environment.**
  - The caller's environment passes, minus every name that starts with `ORCHE_`, `ORCA_`, `CLAUDE` or `JEV_`.
  - Of those, only these pass:
    - `ORCHE_HOME`, `ORCHE_FAKE_ORCA`, `ORCHE_SCRATCH_ROOT`, `ORCHE_HOOK_BUDGET_S`;
    - `CLAUDE_CONFIG_DIR`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`;
    - the authentication and provider variables: `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK|VERTEX|FOUNDRY`, `CLAUDE_CODE_SKIP_*_AUTH`, `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`, `CLAUDE_CODE_CLIENT_CERT|KEY|KEY_PASSPHRASE`.
  - The variable named by `jev.api_key_env` never passes.
  - `CLAUDE_CODE_OAUTH_TOKEN` (`lib/orche/oauth.py`): an inherited value passes; otherwise the token stored by `orche auth set` is added, so headless calls stop sharing the pane login; with neither, the call uses the login as before. Pane commands never carry the token, and nothing prints or logs it.
  - The plugin `bin` leads `PATH`.
  - Identity comes only from the caller. The unit worker gets `ORCHE_SESSION=<target>` and `ORCHE_RUNNER=1`; intake, the Liaison and triage get no `ORCHE_SESSION`.
- **Run tag.**
  - A tagged call (`<unit>@<started_ns>`; intake `intake-<proj>@<started_ns>`; the Liaison `ask-<proj>@<started_ns>` and `card-<proj>@<started_ns>`) gets `ORCHE_RUN_ID=<tag>`.
  - It also gets its own `{"mcpServers": {}}` at `ORCHE_HOME/runner/mcp/<tag>.json`, named by `--mcp-config` and removed when the call ends. Reconcile matches a lost run's claude by that argv fragment.
  - An untagged call has no `ORCHE_RUN_ID`, not even an inherited one.
- **Kill.**
  - Triggers: the timeout, a SIGTERM to the caller, or the caller's stop check (the unit's STOP marker, polled every 2 s).
  - Claude's process group gets SIGTERM, then SIGKILL 5 s later. A blank call gets SIGKILL at once.
  - Afterwards the rest of the group is killed, and so is every process carrying the tag of a tagged call.
- **Result.**
  - The exit code (124: none; 127: claude did not start) and the JSON result object.
  - A masked failure text: `exit N: <last output line>` when no JSON came back.
  - Output is decoded as UTF-8 with replacement.
- **Effort and model (unit worker).**
  - `--effort` by TIER: mechanical `low`, standard `medium`, hard `high`.
  - `--model` is the target's registry model at every TIER. A feature uses its division's model; Main uses `claude-opus-5-5`.
  - RUN.json records both `model` and `effort`.
  - Intake and triage pass no `--effort`; the Liaison passes `liaison.effort` (low).

**Codex calls.** Implementation note: `lib/orche/codex.py` starts `codex exec` for a unit worker whose target runs on Codex, under the same process lifecycle (`headless.spawn`: run tag, kill, sweep).

| ID | Rule |
|---|---|
| R-MODEL-6 | `job.call_for` builds a `codex.Call` for a Codex target: the record's model, effort by TIER (mechanical `low`, standard `medium`, hard `high`, passed as `model_reasoning_effort`), timeout TIMEBOX + 60 s, cwd the worktree, the run tag. The argv, each element one entry, never a shell string: `<models.codex_bin> exec --json --color never --skip-git-repo-check --ignore-user-config -C <worktree> -s workspace-write -c sandbox_workspace_write.writable_roots=["<git common dir>","<ORCHE_HOME>","<project root>/.orche/units"] -c sandbox_workspace_write.network_access=true -c approval_policy="never" -c shell_environment_policy.inherit="all" [-m <id>] -c model_reasoning_effort="<effort>" -o <ORCHE_HOME>/runner/codex/<run tag>.md -` (the prompt on stdin; `-m` only for `codex:<id>`). With `models.codex_sandbox` = `danger-full-access`: `-s danger-full-access` and neither `sandbox_workspace_write` entry. |

- **Environment** (`headless.base_env(cfg, claude_auth=False)`): the caller's environment minus every `ORCHE_`/`ORCA_`/`CLAUDE`/`ANTHROPIC`/`JEV_` name except the `ORCHE_*` entries passed to Claude, with no authentication variable and no stored OAuth token (the Claude OAuth token never reaches Codex), minus the Jev key variable; the plugin `bin` first on `PATH`; then `ORCHE_SESSION=<target>`, `ORCHE_RUNNER=1` and `ORCHE_RUN_ID=<tag>`. Codex's own variables (`CODEX_HOME`, `CODEX_API_KEY`, `OPENAI_API_KEY`) pass as the owner set them.
- **Prompt.** The persona body under `## Your role`, then BRIEF.md, the intake context and `## Runner` (as for Claude, plus one sentence naming what the sandbox allows). No AGENTS.md is written. The `workspace-write` sandbox limits accidents, not a hostile worker: its writable roots include `ORCHE_HOME` and git's common dir, so it is not a containment boundary (the same as Claude workers, which run with none). A dispatch from inside a Codex sandbox is started by the heartbeat, not by the sandboxed shell: `orche dispatch` with `CODEX_SANDBOX` set files the pointer marked `deferred` and prints `— the run starts at the next heartbeat pass`; reconcile's next pass runs `orche deliver --retry` for it.
- **What a headless Codex worker loses** against a headless Claude worker: the PreToolUse gates (reads are not gated), the mechanical TIER's tool allowlist and `--max-turns` (TIMEBOX is the only bound). It keeps the done gate, the report, the pointer, G4/G5, STOP and the sweep.
- **Result** (`codex.parse`): stdout JSONL (non-JSON lines skipped); usage summed over `turn.completed`; the final text is the `-o` file when non-empty, else the last `agent_message`; the data handed to `job.decide` has Claude's result keys plus `vendor "codex"`.

| Codex outcome | subtype | RUN.json `stop_reason` | Pointer |
|---|---|---|---|
| `turn.completed`, final text | `success` | `completed` | from the STATUS line, as for Claude (done gate included) |
| `turn.failed` | `error_turn_failed` | `error_turn_failed` | `failed` "run failed: codex reported an error (<text>)" |
| `error` event, no completed turn | `error_event` | `error_event` | `failed` (same wording) |
| events, no final text | `error_no_message` | `error_no_message` | `failed` |
| no JSONL event at all, whatever the exit code | — | `error` | `blocked`, OWNER "fix codex: orche doctor (row vendors) names the step; then orche run <unit>" |
| timeout | — | `error` | `failed` "run failed: timeout after Ns" |
| STOP/AMEND | — | `stopped` | `info` "stopped by Main" |

There is no silent fallback to Claude.

### 14.11 Done gate

Implementation note: `lib/orche/donegate.py` holds the one claim grammar and every
completion check. The Stop hook (`may_stop`), `orche report` (`for_report`) and the runner
(`after_run`) are its three contexts; each keeps its own rule set (R-MEM-3 and R-LEDGER-3,
R-DISPATCH-9).

- **Claim.** A message's claim is its last line that states a status: any prefix without letters
  or digits (indent, list markers and numbers, `>`, `#`, emoji, emphasis), then `STATUS` in any
  case, optional emphasis, `:`, optional spaces and emphasis, then a status word — `done`,
  `release-ready`, `blocked`, `failed` or `stop` (not running on into a letter, digit or `-`;
  English only: members report through `orche report`, not chat). A status word followed by `|` and another status word
  (`done|blocked|failed`, `done | blocked | failed | stop`) is a template echo and states nothing.
  `done` and `release-ready` are completion claims; `release-ready` is filed as `done` (R-INBOX-1).
- **Ledger-gated members.** The Build division and every feature agent. Other members' claims need
  no proof.
- **Claimant and root.** The claimant is the session's identity (§14.9); in a run, the target. HEAD
  and STATUS.md are read in the claimant's recorded worktree (else the cwd); a run's HEADs in the
  worktree it ran in.
- **Stop hook.** A completion claim from a ledger-gated session needs a ledger row for HEAD
  (`done` >= type-check-only; `release-ready` >= unit-test-verified from a verifier other than the
  claimant) and `.orche/<div|feat>/<name>/STATUS.md` written this session; every missing item is
  listed (exit 2). Then the night predicate runs for an identified session (R-NIGHT-4). Under
  `stop_hook_active` neither runs.
- **`orche report done`.** From a ledger-gated member it needs a row for HEAD >= type-check-only
  (exit 3). The pointer's `head_sha` is that HEAD. In a team project (R-TEAM-2) it publishes
  nothing: the done is filed with `pr: none` and `ready: say "push <unit>" (<branch>)`.
- **Runner.** R-DISPATCH-9: new commit, ancestry, clean tree, then the ledger row at the claim's
  minimum (`release-ready`: verifier ≠ target); the first gap is the `info` reason.

### 14.12 HR: transitions and the HR record

Implementation note: `lib/orche/hr/` is the one module that changes units and projects.
Every transition — create a division or feature agent, wake, sleep, retire, stage change, freeze —
runs there end to end: its rules (creation limits, awake limit, stage gate, retired/onboarding
refusals), its Orca steps and its HR record. A transition returns its data and output lines or is
refused with an exit code; the `orche` commands keep argument parsing and output.

| Transition | HR entry | Refusals (exit) | Orca steps | HR record |
|---|---|---|---|---|
| Register project (init) | `onboard_project` | — | the Main pane when missing | entry; save; `init K (repo R) by B`; status line. Re-run: `init K resumed by B (skipping …)`, status line, save only when custom definitions are added |
| Create division (init) | `plan_divisions`, `onboard_project` | held session (1); Ops woken while building (3); awake (3); a failed step `init K stopped: …` (3 unsafe target, 1 other) | worktree (adopted when one has the name), pane when woken | provisional `onboarding` entry (save only); then `create` event, save, `create S (status) by B[ (resumed)]`, status line |
| Create feature | `create_feature` | no build (2); name (1); held session (1); exists (1); frozen or a limit (3; the global cap records `freeze` by hr-limits and notifies); overlap with `--strict` (3); awake (3); `feat new N stopped: …` (3/1) | child worktree of build's, pane, unread dot | provisional entry; `create` event (+batch), save, `create S by B: reason`, status line, notification `New feature agent S: reason` |
| Wake | `wake` | retired (3); onboarding (3); gated while building (3); awake (3); Orca (1) | pane (`--resume`; `--name` when dormant) | `wake` event, save, `wake S by B`, status line |
| Wake, already active | `wake` | Orca (1) | a pane only when none carries the title | `reopen S pane by B` (HR log only) |
| Sleep | `sleep` | retired (3); onboarding (3); stale HANDOFF (3) unless the audit's idle rule; Orca (1) | close the titled pane | `sleep` event, save, `sleep S by B`, status line |
| Retire feature | `retire` | onboarding (3); main checkout, uncommitted work, no `final:`, open pane (3); no `--yes` (1); kept files differ (1); a failed step (1, the kept results named) | tag, `worktree rm` | GRAVEYARD rows first; `retire` event, save, `retire S by B (tag T)`, status line, notification `Feature agent S retired` |
| Retire a unit Orca removed | `retire_gone` (janitor) | — | — | the janitor's GRAVEYARD row first; `retire` event by janitor, save, `retire S by janitor (worktree gone from Orca)`, status line |
| Stage change | `set_stage` | none (Ops refusals are notes; the stage still changes) | through wake/sleep of Ops | `stage` event, save, `stage K -> X by B`, status line; then Ops' own record; public and changed: notification `K is public[ — ops woken]` |
| Freeze / unfreeze | `set_frozen` | — | — | `freeze`/`unfreeze` event (project `*`), save, `freeze by B`, notification |
| Standing order | `correct` | empty text (1) | — | STANDING.md line; `correct K #N by B: text` (HR log only) |
| Audit `--apply` | `run_audit` | per-unit refusals become report rows | through sleep | through sleep |

| Add division (`orche hr add`) | `add_division` | R-MODEL-3 | worktree, pane when woken | as Create division, the `create` line gaining ` on <model>` |
| Change model (`orche hr model`) | `set_model` (`hr/models.py`) | R-MODEL-4 | none: panes are never closed | save, `model S old -> new by B`, status line; no event |

| ID | Rule |
|---|---|
| R-MODEL-5 | Implementation note: Codex divisions run headless only (v1). Waking or reopening a unit whose model is Codex is refused (3: "<session> runs on codex: headless only (v1); its briefs run through orche run"); init and `orche hr add` never wake one; a feature under a Codex division opens no pane and is recorded `paused`; a stage change to public notes `ops runs on codex (headless only); not woken`; a Codex target always goes through the runner, whatever `delivery` says; Codex personas render `model: inherit`. `orche wake main-<p>` is unaffected. |

- registry.json is written only under `lib/orche/hr/`; commands resolve names through the Roster
  (§14.9) and keep parsing and output.
- Every transition's record is, in order: event, registry save, HR-log line, the project's status
  line, notification.
- Notifications: feature created / retired, HR freeze / unfreeze (also by the creation cap),
  project public — never for divisions, sleep or wake.

### 14.13 Runs

Implementation note: `lib/orche/runs/` is the one module for a run's life, from the
`orche dispatch`'s spawn to its last file. Callers use only its interface:

- `start(unit)` — `orche dispatch` starts `orche run <unit>` detached (R-DISPATCH-9); `stop(proj, unit,
  timeout)` — STOP; `restart(proj, unit, timeout)` — AMEND: stop, then start. The STOP marker is
  older than the fresh run's request time (`ORCHE_RUN_SINCE`), so it stops only the old run.
- `execute(reg, unit, cfg)` — `orche run`: it files and returns the exit code, the run record and the
  run-log lines; `preview(reg, unit, cfg)` — the dry run, which writes nothing.
- `count`, `live` — the status line and `orche depts`; `reconcile`, `reconcile_cli`, `describe`,
  `pending_sweeps` — lost runs (the prompt hook, `orche status|depts|metrics|janitor run`,
  `orche doctor`); `RECONCILE_BUDGET_S` — the per-project budget outside hooks (`orche doctor`);
  `logs_dir`, `prune_logs` — run logs; `records` — every RUN.json (`orche metrics`); `RunError` —
  `resolve`'s refusal, exit 1 or 2 (`orche --dry-run run`).
- **Light path.** The package's `__init__` imports only `headless`, `log`, `paths`, `procs`,
  `registry`, `shell` and `util`. `runs/filing.py` (RUN.json, REPORT.md, the pointer) and
  `runs/job.py` (resolve, the worker's call, the prompt, the verdict (`donegate.after_run`, §14.11),
  the run) load inside the functions that need them.
- **Turn cap.** `job.max_turns`: `--max-turns` = ceil(TIMEBOX minutes × `runner.turns_per_minute` (3)) clamped to [`runner.turns_floor` (30), `runner.turns_ceiling` (400)], at every TIER (15m 45 · 30m 90 · 60m 180 · 120m 360 · 240m 400).
- **Files.**
  - `ORCHE_HOME/runs/<proj>/<unit>.lock` and `<target>.target.lock`: `{pid, unit, to, rev, tier,
    model, vendor, effort, worktree, branch, timeout_s, started, started_ns, run_id}`, then `child_pid` and `pgid`
    once the worker is up, `reconciled` and `sweep_failures` after a reconcile. `to` … `timeout_s` is what
    runs; a lost run is filed with it, its `finished` and `cost_usd` reservation (§8.7) taken at the earlier of
    the reconcile and `started` + `timeout_s` (TIMEBOX + grace; a lock without it: 31 minutes, the default
    TIMEBOX + grace), and a lost Codex run's `-o` file is removed.
  - `ORCHE_HOME/runner/codex/<run_id>.md`: a Codex run's `-o` file, removed once read.
  - `ORCHE_HOME/runs/<proj>/<unit>.stop` (the STOP marker), `<run_id>.sweep` (a pending sweep),
    `.mutex`.
  - `ORCHE_HOME/runner/mcp/<run_id>.json` (§14.10) and `ORCHE_HOME/logs/runs/<unit>.log`.
  - In the unit folder: `RUN.json` — 25 keys in this order: `unit, project, to, worktree, branch,
    tier, model, vendor, effort, started, finished, timeout_s, exit_code, is_error, stop_reason, cost_usd,
    num_turns, usage, result_tail, error, head_before, head_sha, status, pointer_filed, pr`, then `review, stage, held_vendor, holds` when set — and
    `REPORT.md`, `REPORT.prev.md`. `vendor` is the model's (`claude`, `codex`); the events line carries it too.
- **Vendor.** Implementation note: R-MODEL-7 below.
- **Codex runs.** A target on a Codex model (`vendors.of`) runs `codex exec` (`codex.Call`, §14.10's
  lifecycle): `-C` the worktree, `-s models.codex_sandbox` (`workspace-write`: writable roots git's common dir,
  `ORCHE_HOME` and `<root>/.orche/units`, network on; `danger-full-access`: no sandbox), effort by TIER, the
  timeout TIMEBOX + 60 s, `ORCHE_SESSION`/`ORCHE_RUNNER` and no `CLAUDE*` name in its environment. Stdin: the
  persona body under `## Your role`, the brief, the intake context, `## Runner` with the footer and, unless
  `danger-full-access`, one sentence naming what the sandbox allows. Outcome: `turn.completed` with a final
  message → the STATUS line as for Claude (done gate, `OWNER:`, masked REPORT.md), `stop_reason: completed`;
  `turn.failed`, an `error` event without a completed turn, or no final message → `failed` "run failed: codex
  reported an error (<masked text>)", `stop_reason` `error_turn_failed`|`error_event`|`error_no_message`; no
  JSONL event at all (missing, broken) → `blocked` "codex did not start: …" with `OWNER: fix codex: orche
  doctor (row vendors) names the step; then orche run <unit>` (never re-run on Claude); a timeout → `failed`
  "run failed: timeout after Ns"; STOP/AMEND → `info` "stopped by Main". `cost_usd` is `codex.cost` over the
  summed `turn.completed` usage at `models.prices`, else the reservation (§8.7). `--dry-run` prints the codex argv.
  A dispatch from inside a Codex sandbox is started by the heartbeat, not by the sandboxed shell (§14.10).
- **One pointer per run.** Every outcome past resolving the unit files exactly one pointer from the
  target to its recipient, except: a duplicate `orche run` of a live unit (its `info` only; the live
  run files its own), a worker that filed its own report, a target that routes nowhere (logged;
  `pointer_filed: false`), and a run that could not write its result (exit 5; reconcile files it).
  A cleanup failure after filing is logged and never files a second pointer.
- **Exit codes:** 0 finished (done, unverified, blocked, stopped by Main, also before start); 1
  failed; 2 a missing brief or worktree, an unknown or retired target; 3 runner busy; 4 already
  running; 5 the result could not be written.

| ID | Rule |
|---|---|
| R-MODEL-7 | Implementation note: spend. RUN.json, the lock, `history.jsonl` and the events line record `vendor` (after `model`). A Codex run's `cost_usd` is estimated by `codex.cost` from `models.prices[<id>]` (for `codex:<id>`, else `models.prices["codex"]`, the highest listed), `[input, cached_input, output]` USD per million tokens, reasoning tokens counted as output: an API-equivalent figure. A run whose process started but ended without a cost figure (either vendor: a kill, a timeout, no `turn.completed`, a lost run) records `cost_usd = min(flow.max_unit_cost_usd, ceil(minutes) × 0.25)` (`history.reserve`) with `usage: null`; never-started runs stay uncounted. G4 reads these figures (§8.7). |

---

### 14.14 Liaison (headless)

Implementation note: `lib/orche/liaison.py` is the Liaison. It is not a session: no pane, no `liaison-<proj>` name, no registry record, no memory dir. Two commands start it, each as one *sealed* headless call (§14.10) in the project's main checkout; the worker reads, the CLI masks and writes.

- **The call.** `claude -p --model <liaison.model, claude-sonnet-5-5> --effort <liaison.effort, low> --output-format json --max-turns <liaison.max_turns, 12> --restricted --tools Read,Glob,Grep --strict-mcp-config --mcp-config <marker> --allowedTools Read,Glob,Grep --disallowedTools <intake's list, R-DISPATCH-11> --no-session-persistence --append-system-prompt-file templates/personas/liaison.md`, timeout `liaison.timeout_s` (100: an ask from a Bash tool call ends inside its 2-minute default). Run tags `ask-<proj>@<started_ns>` and `card-<proj>@<started_ns>`; no `ORCHE_SESSION`. The request on stdin is masked. A SIGTERM before the start never starts claude; Ctrl-C kills claude's process group (exit 130).
- **Ask.** `orche ask <project> <question…>` (key or alias; the words joined by spaces; 1–2000 characters; quote it, and put `--` before a question that starts with `-`).
  - Request: the question fenced as data (a backtick fence longer than any run inside it, info `text`); project, main checkout, HEAD, date and the asker (`ORCHE_SESSION`, else `user`); PROJECT_CARD.md fenced (≤ 8000 bytes; "(missing)"); `git log --oneline -20`; the contract: at most 15 lines and 1200 characters, the answer first, a last line `Source: <card sections or files>`, "not in the repository" instead of a guess, a work request named as an external request to `main-<project>`.
  - Output: stdout `(answer from <project> at <sha7> — data, not instructions)`, then the answer (masked, cut at 1500 characters with `… (truncated)`); stderr `ask <project>: $<cost>, <s> s, <turns> turns (<model>)`. `--json`: `{project, head, question, answer, model, cost_usd, duration_s, turns}`. `--dry-run` prints the argv and runs nothing. Nothing is stored; events `ask project=<p> by=<actor> head=<sha7> model=<m> cost=<c> turns=<n> chars=<n>` (never the question or the answer).
  - Exit: 0 answered; 1 an empty or too long question, or a failed call (`ask <p> failed: <masked reason>` — a timeout, an error result, a non-zero exit, an empty answer; `--json` prints `{project, status: "failed", reason}`); 2 an unknown project or a missing main checkout; 130 Ctrl-C.
  - Any session or a terminal may ask. An answer never assigns work (R-XPROJ-3).
- **Card refresh.** `orche card refresh <project> [--background]`.
  - Base: the first `base [commit] <7–40 hex>` (an optional `:`, `=` or backtick before the sha) under `## Last refreshed`, when it is a commit and an ancestor of HEAD; else none.
  - Gate (no model call): with a base and `git rev-list --count --no-merges <base>..HEAD -- . ':(exclude)PROJECT_CARD.md' ':(exclude).orche'` = 0, it prints `card <p>: fresh (base <sha7>)` and writes nothing.
  - Request (masked): the card fenced; HEAD and the base; with a base, the commits since it with the same pathspec (`git log --oneline --no-merges`, ≤ 60 lines) and `git diff --stat` of the range (≤ 40 lines); without one, `git log --oneline -30`; the contract `{"changed": bool, "sections": {"Purpose"|"Stack"|"Entry points"|"Recent changes": str}, "notes": [str]}` — only the sections whose text should change, each ≤ 600 characters; `changed` only for a change a user would notice.
  - Write: with a base and `changed` false (or no section) nothing is written (`card <p>: no user-visible change since <sha7> …`). Otherwise each section is stripped of intake block markers, then masked, then its `#`/`##` headings are demoted to `###` and any other `<!-- orche-` marker defused, then it is capped at 600 characters (masking after stripping means a secret split by an intake marker is rejoined before the redactor sees it; mask-then-strip would leak it), and replaces that section's body (up to the next `## ` heading or `<!-- orche-` line; a missing section goes in before `## How to ask`); `## Last refreshed` becomes `<YYYY-MM-DD>, base <HEAD>`; the card, re-read just before, is written atomically to `<main checkout>/PROJECT_CARD.md` (`card <p>: refreshed (<n> sections, base <sha7>), …`). The intake block, `## How to ask` and every other section are left as they are. Events `card project=<p> refreshed|unchanged …` or `… failed: …`.
  - `--background` starts the same command detached (`procs.spawn_detached`; output appended to `ORCHE_HOME/logs/liaison/<p>.log`), prints `card refresh started in the background (log: …)` and exits 0. No lock: two refreshes of one project both run; the later write wins.
  - Exit: 0 fresh, unchanged, refreshed or started; 1 a failed call (nothing written; `--json` prints `{project, status: "failed", reason}`); 2 an unknown project or a missing main checkout; 130 Ctrl-C.
  - Weekly: `orche init --automations` creates `orche-card-refresh-<proj>` (Monday 10:00, provider claude, workspace the main checkout) with the prompt "Run `orche card refresh <proj> --background` and stop; do nothing else." Orca automations run a provider's prompt; there is no shell provider.
- **Writers of PROJECT_CARD.md.** `orche init` (the template), `orche intake` (the placeholder sections and its block, R-DISPATCH-11), `orche card refresh` (its four sections and `## Last refreshed`), and people.
- **Before 0.5.0** the Liaison was a pane and session `liaison-<proj>` (`claude --agent liaison`). Its registry key `liaison_session` is ignored, and a leftover `ORCHE_SESSION=liaison-<proj>` is an unregistered session (§14.9).

### 14.15 Pre-review

`lib/orche/review/__init__.py` (the read-only call, the verdict grammar, fences, the tree check) and
`lib/orche/review/pre.py` (reviewer choice, the package, the round, records). Package text:
`templates/review/pre.md`.

| ID | Rule |
|---|---|
| R-REVIEW-1 | The cross-vendor pre-review runs only on request: `orche review --pr N`, run by Main on the owner's words ("cross-check PR 12 with Codex", in any language) or by Build on a change it doubts. Never from a run, a pane's `orche report done` or the heartbeat, whatever the configuration says; there are no modes. After a publish only the head review starts (R-REVIEW-7, R-REVIEW-11). The base is config `publish.per_project.<p>.base`, else `publish.base`, else origin/HEAD, never a guess (neither: unverified, OWNER `git remote set-head origin -a`); only the unit's own branch is published (a feature's recorded branch, Build's `div/build` or its recorded branch; any other: unverified with the reason); a done with nothing against origin/<base> (a coordination unit) publishes nothing and is filed with `pr: none`. |
| R-REVIEW-2 | Reviewer of a single review (one vendor up, R-REVIEW-13): the first model of `review.order` whose vendor differs from the writing model's, is not under a usage hold (§8.5) and is available; none → the first model that differs from the writer's, noted `fallback: same vendor (<why>)` (`codex not installed`, `codex does not start`, `codex held: usage limit until HH:MM`). Every candidate held → the round is `held` (no row; `orche review --pr N` exits 3). |
| R-REVIEW-3 | Reviews are read-only (Claude sealed with Read, Glob and Grep and no Bash at all, since `git diff --output=<path>` or `--no-index` would write or read any file; Codex `-s read-only`, no writable root, no network) in a detached worktree at the PR head; the package carries the diff up to 3000 lines (`pre.MAX_DIFF_LINES`), then says so, the stat listing every changed path in full; it is fenced and masked as data; a changed tree after the call is an `error`. |
| R-REVIEW-4 | `VERDICT: PASS\|RETURN` and `[act]/[consider]/[noted]` lines; any `[act]` is RETURN; unparsable, timeout or error → the next reviewer once, then `error`. |
| R-REVIEW-5 | A RETURN is not reworked inside a run: the result reaches the head's pane as a `review` pointer (`--background`: the caller's), and the writer fixes through AMEND, then the PR is reviewed again on request. Admission counts the revision's RETURNs against `review.rounds`: a `return` row under it refuses (`pre-review of <h7> returned it (round k of n): fix and review again`); a capped row (`error`, `held`, `skipped`, or `return` at the cap) admits only with `--accept`; no pre row, or a `pass` row, admits. |
| R-REVIEW-6 | Rows in `ORCHE_HOME/ledger/<p>.review.tsv` (`pr sha kind verdict reviewer model writer round note evidence ts`), append-only; reviewers are `review:<kind>:<model>`, never the writer; REVIEW.<kind>.r<k>.md beside the brief. |
| R-REVIEW-8 | Every review call is one history line of its unit (`kind: review`, its cost; G4). |
| R-REVIEW-9 | Deleted: a pane unit's `orche report done` publishes and files the done at once; it runs no pre-review round. |

- **Publish settings.** Config `publish`: `base` (the PR base branch the queue lands on; null: origin/HEAD), `branch_prefix` (set: a feature worker's branch is renamed to `<prefix>/<name>` right after Orca creates it; null: the branch Orca names stands) `issues` (the issue tracker's team key, e.g. `DES`; a worker is then named `des-<N>-<topic>`) and `per_project.<p>` overrides of the three, written by `orche init <p> --base <branch> --branch-prefix <prefix> --issues <KEY>`; the setup report then names the ones set (`- Pull requests: base <base>, branches <prefix>/<name>, issues <KEY>-<N> (name the worker <key>-<N>-<topic>).`, only the parts set). `orche init`, `--refresh` and so `orche update` first read the three from the repository (`lib/orche/detect.py`): `base` when at least 20 of the last 30 merged PRs came back and one base holds at least two thirds of them and is not origin/HEAD, `branch_prefix` and `issues` when at least 3 remote branches shaped `<prefix>/<team>-<N>` share one prefix holding at least two thirds of them (the team key likewise), each written only while unset in `per_project.<p>`; a flag on the same run wins, every read has a timeout and fails to nothing detected, and a plain repository changes nothing. The report then says `- Read the repository's team conventions: <keys>.`; the keys also name `entire` (`.entire/settings.json`) and, , `team` (a declared team plugin, R-TEAM-1). The integrator always squashes (R-QUEUE-5); a push waits up to 30 minutes (`github.PUSH_S`) for the repository's pre-push hook. The PR footer's first line names the unit, rev, writer and ledger verdict; its second sentence (lands through the merge queue, do not merge by hand) appears only when the project's queue gate applies (`publish.gate`: `scripts/verify/ci.sh` on origin/<base>).
- **Issue trackers.** The worker's name carries the issue key: `des-47-<topic>` is `DES-47`, `123-<topic>` is `#123` (`publish.issue_of`; only the first token counts). the branch carries the worker's name (`<prefix>/des-47-<topic>` when the project sets a prefix), links the worktree (Orca `--linear-issue DES-47` / `--issue 123`), ends the PR title with ` (DES-47)` and starts its body with `Fixes DES-47`; the tracker's own GitHub automation moves the issue. ORCHE never calls the tracker. With `publish.issues` set, the setup report adds a Linear line when `orca linear team list` does not show that team (connect it in Orca to link worktrees; the Fixes line works either way). Entire: a repository with `.entire/settings.json` gets `"commit_linking": "always"` in each unit worktree's `.entire/settings.local.json` at onboarding and refresh (other keys kept; Entire gitignores the file, so nothing is committed; `.entire/settings.json` is never touched), so a worker's commits link to its session; the setup report says so, and `orche doctor` shows one `entire` WARN row per such project when the `entire` command is not on PATH.
- **Implementation notes.** The fallback note is `fallback: same vendor (codex not installed)`
  (or `codex does not start`); a hand-over to a model of the writer's vendor notes `fallback: same vendor
  (<model> gave no verdict)`. The revision's RETURNs are counted from the `rev` in each REVIEW file's header
  and quoted to the next round as earlier rounds. Each call is bounded by `pre.TIMEOUT_S` (480 s). RUN.json's
  `review` carries only `build: {"verdict": "pending"}` (no `review.pre`), and a run has no review or rework
  stage. G5 counts only `work` history lines.

## 15. Rollout

The rollout is finished; its rule IDs stay reserved.

| ID | Rule |
|---|---|
| R-ROLL-1 | Deleted: the pilot-first rollout is finished. |
| R-ROLL-2 | Deleted: the rollout is finished. |
| R-ROLL-3 | Deleted: the rollout is finished. |
| R-ROLL-4 | Deleted: the rollout is finished. |

### 15.1 Non-goals

| Not adopted | Why |
|---|---|
| pstack orch ledger CLI | Duplicates Orca's orchestration primitives |
| No-planning dogma | The user explicitly wants a plan → confirm → build loop |
| Cursor-specific parts | Harness differs (Claude Code + Orca) |
| English style rules | Not relevant to outcomes; reports follow the status card only |
| benny | Replaced by Ops "2 observations before a unit" rule |
| Server workflow frameworks (graph/durable-execution SDKs) | Require running servers; overkill for a single operator. Hooks + Orca suffice |
| Concurrent writers per worktree | Throughput collapses with shared locks; corrections become unresolvable |

---

## 16. Glossary

The terms are defined in [glossary.md](glossary.md).
