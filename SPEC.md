# ORCHE Specification

Status: canonical · Version: 4.2 · Date: 2026-10-16
Audience: AI agents and engineers implementing or operating ORCHE.
Scope: an orchestration layer for Claude Code sessions managed by the Orca desktop multi-agent manager.

Conventions in this document:
- MUST / MUST NOT / SHOULD / MAY are normative.
- `<project>` = registry key of a project. `<app>` = a deployable app inside a project. `<org>` = a GitHub organization.
- `~/.claude/orche/` = the ORCHE instance root on the operator's machine.
- Rule IDs (e.g. `R-DISPATCH-3`) are stable; tests and hooks SHOULD reference them.
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

ORCHE runs one orchestrator session ("Main", the most capable model) per project. Main understands requests, decomposes them, and emits structured dispatch blocks; it does not write code and does not read worker output in full. Hooks — not model turns — relay dispatches to long-lived division sessions (R&D, Research, Build, GTM, Ops) and to feature agents under Build, each in its own Orca worktree. Workers report by writing a small inbox pointer; Main drains pointers in batches at four defined points. Completion is defined by a verification ledger keyed by PR and SHA. Memory lives in files (persona, agent memory, STATUS/DECISIONS/HANDOFF, preserved transcripts), so sessions are cleared at unit boundaries instead of growing. Visibility (status line, Orca sidebar, `orche` CLI, macOS notifications) costs zero tokens. A model-free secrets broker, a state-based janitor, and Git/CI rules complete the system. An optional fast typed-decision model (Jev) gates routine traffic.

### 0.3 What changes

| Area | Before ORCHE | With ORCHE |
|---|---|---|
| Command | Orchestrator both decides and reads every subagent result | One Main per project. Main decides; hooks relay; divisions do long work |
| Organization | Many long-lived topic sessions, no roles | Main + Liaison + 5 divisions + auto-created feature agents under Build + global HR |
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
                                               │ ```dispatch block → [Stop hook] → relay
   ┌───────────┬──────────┬───────────┬────────┼────────┬──────────┐
   ▼           ▼          ▼           ▼        ▼        ▼
 Liaison      R&D      Research     Build     GTM      Ops (dormant while stage=building)
                                      │
                         ┌────────────┼────────────┐
                    feature agent  feature agent  feature agent …
                    (child worktrees of Build; created by HR)
HR (one global) ── registry.json ── create · sleep · retire · rate limits · weekly audit
```

### 2.2 Role table

| Role | Model / effort | Worktree | Responsibilities | Guards |
|---|---|---|---|---|
| Main | Fable 5.1 / xhigh | project main checkout | Understand, decompose, assign, confirm loop, approve integration, prepare release | MUST NOT edit code (rulebook; see note below). Long work goes to divisions. No deep review during drain. Reports in ketchup style |
| Liaison | Sonnet 5.5 / low | main checkout (separate panel) | Maintain PROJECT_CARD.md; answer other projects' questions | Card first; exit if no change since base commit; cannot assign work |
| R&D | Opus 5.5 / high (+ deep-reasoner at max) | `div/rnd` | Product and architecture planning, design, prototypes | MUST NOT edit product code (prototype folder only). gstack only for important decisions (§13) |
| Research | Sonnet 5.5 / medium | `div/research` | Market, technical, competitive research reports | Read-only. Conclusion ≤1 page. Escalate synthesis to Opus if heavy |
| Build | Opus 5.5 / high | `div/build` = integration worktree | Assign features, integrate, test, own the ledger, review, open PRs | Feature code is written by feature agents; Build integrates and verifies |
| Feature agent | Opus 5.5 / high (exec per TIER) | `feat/<name>`, Orca parent = Build | Feature code within its scope | No edits outside own worktree (scope hook). `/clear` at unit end. 14 idle days ⇒ sleep |
| GTM | Opus 5.5 / high | `div/gtm` | Copy, landing pages, launch, marketing, customer-facing text | External publication (post, send) requires user approval |
| Ops | Opus 5.5 / high (mostly scripts) | `div/ops` | Monitoring, incident first response, release ladder, backup verification, patching, cost, report triage, runbooks | Dormant unless stage=`public`. Automatic rollback only via pre-approved runbook; everything else Always-pause |
| HR | Sonnet 5.5 / low (audit: Haiku 5.5) | ORCHE repo | registry, notifications, weekly audit, `/correct` | Rate limits and freeze; every change logged |
| exec / Explore / deep-reasoner / runner | Opus or Sonnet medium / Sonnet / Opus max / Haiku 5.5 | ephemeral | Hands | Brief ≤300 words, report ≤200 words, `maxTurns` set, auto-compact window 200K |

Rules:
- R-TOPO-1: All five divisions are created from a fixed template at project onboarding. Unused divisions are `dormant` (no panel, zero cost).
- R-TOPO-2: Feature agents exist only under Build. Other divisions use ephemeral subagents.
- R-TOPO-3: Exactly one writer per worktree. A file's ownership belongs to one division or feature agent.
- R-TOPO-4: Exactly one Main per project.
- R-TOPO-5: Model IDs are configuration, not code. Implementations MUST read them from registry/persona files.
- Implementation note (v0.1.0): the `scope-gate` hook acts only inside division and feature worktrees (edits outside the worktree or a feature's scope globs are denied; `.orche/` and `docs/` are always allowed). Main's "no code edits" rule is enforced by its persona and the `orche-main` rulebook, not by a hook.

### 2.3 Lifecycle stage

Each project has `stage ∈ {building, public}` in the registry.

| Rule | Statement |
|---|---|
| R-STAGE-1 | New projects start at `building`. |
| R-STAGE-2 | While `building`, the Ops division is `dormant` and MUST NOT be woken by automations or alerts. |
| R-STAGE-3 | Only Main may set `stage: public`, and only after explicit user approval recorded in DECISIONS.tsv. |
| R-STAGE-4 | On transition to `public`, HR wakes Ops, which installs `docs/runbooks/` and `scripts/ops/health.sh` from templates and registers monitoring automations. Implementation note (v0.1.0): `orche stage set <proj> public --yes` records the stage, wakes Ops (unless `--no-wake`; the awake limit applies) and prints the Ops checklist (monitoring, release ladder, backup verification, runbooks); Ops creates the runbooks and scripts itself as its first units. |
| R-STAGE-5 | Reverting to `building` puts Ops back to sleep (`paused` via `orche sleep`, HANDOFF check applies; a refusal is reported and the stage still flips); runbooks and scripts stay. |

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

Creation trigger: Build decides a persistent owner is needed when ≥2 of: (a) expected duration ≥2 weeks, (b) independent file boundary, (c) repeated requests in the same area. Build calls HR; HR creates without asking and emits 4 notifications (status line, Orca card dot, macOS, one line to Main).

| Limit (registry `limits`) | Value | On breach |
|---|---|---|
| `per_project_per_day` | 3 | Require user confirmation |
| `per_project_per_week` | 10 | Require user confirmation |
| `per_request_auto` | 2 | 3rd+ in one request requires confirmation |
| `global_per_day_freeze` | 20 | Set `frozen: true`; no creation until `orche hr unfreeze` |
| `idle_days_to_sleep` | 14 | Feature agent → `paused` (not deleted). Implementation note (v0.1.0): `orche hr audit` proposes the sleep; `--apply` performs it |
| `max_awake_sessions` | 12 | Per machine; new wakes queue |

Creation outputs (all MUST exist after creation):
1. Orca child worktree: parent = Build worktree, git base = `main`, branch `feat/<name>`.
2. `.orche/feat/<name>/{CHARTER.md,STATUS.md,HANDOFF.md,DECISIONS.tsv}`.
3. `.claude/agents/feat-<project>-<name>.md` persona with `memory: project`.
4. Worktree `.claude/settings.local.json` with `agent` set to that persona.
5. Session name `feat-<project>-<name>`; Orca terminal renamed to match.
6. Registry entry with `status: active`, `created`, `last_activity`, `scope` globs.

Sleep: update STATUS + HANDOFF → close panel; keep worktree and session.

Implementation note (v0.1.0): unit statuses are `active | dormant | paused | retired | onboarding` for divisions and `active | paused | retired | onboarding` for features. `dormant` = onboarded but never started; `paused` = slept (`orche sleep`, HANDOFF.md must be under 30 minutes old); `onboarding` = the worktree exists but onboarding has not finished (re-running `orche init` / `orche feat new` resumes it without a second worktree).

Wake: `claude --resume <session>`; read HANDOFF as authoritative input; MUST NOT re-verify prior work (pause/pickup).
Retire (user declaration via Main): preserve uncommitted work (`wip:` commit) → final HANDOFF → archive tag → remove worktree → compress session to archive → one GRAVEYARD line → registry `status: retired`.

Implementation note (v0.1.0): `orche feat retire <proj> <name> --yes` refuses a dirty worktree (commit or discard first), a HANDOFF.md without a `final:` line, or the main checkout. It copies the unit memory files to `ORCHE_HOME/archive/<session>/unit-files/<tag>`, tags `archive/feat-<name>-<yyyymmdd>`, runs `orca worktree rm`, archives the unit's transcripts under `ORCHE_HOME/archive/<session>/`, appends a GRAVEYARD row and sets `retired`.

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
| R-MEM-3 | The Stop hook MUST block a completion (exit 2 with message) whose REPORT lacks the memory-save confirmation. Hooks never type `/clear` (or any slash command) into a pane: once the check passes, the agent itself runs `/clear` per its persona rules, and the SessionStart hook re-injects STANDING, STATUS, HANDOFF and recent DECISIONS on `clear`/`resume`. Implementation note (v0.1.0): the Stop hook gates only `STATUS: done` / `STATUS: release-ready` claims from Build and feature sessions; it requires a ledger row for HEAD and that the unit's `.orche/<div\|feat>/<name>/STATUS.md` was written during the current session (the memory-save evidence). |
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

---

## 4. Work flow

### 4.1 Dispatch brief

Main ends a response with one or more fenced `dispatch` blocks. The Stop hook parses them and relays via Orca (`orchestration send` / worker start) or `SendMessage`. Relay costs 0 Main turns.

```dispatch
TO        div-example-web-gtm
GOAL      One-sentence outcome a stranger could execute without chat history
SCOPE     Writable paths / forbidden paths / dedicated worktree + branch
CONTEXT   File and PR pointers; paste upstream report in full if needed (workers cannot see siblings)
ACCEPT    Checkable criteria, one per line
VERIFY    Exact command or verify-skill path + known pitfalls (must be a committed script, §5.3)
TIMEBOX   Approximate cap; on overrun report partial result and stop
FORBID    Retarget PR, merge, rebase, force-push, out-of-scope edits + unit-specific bans
REPORT    status, branch, head SHA, PR, verdict, commands actually run, deviations, follow-ups, memory-save
TIER      mechanical | standard | hard
ROLLBACK  (optional; mandatory when the unit deploys) how to revert
```

Field rules:

| ID | Rule |
|---|---|
| R-DISPATCH-1 | Required fields: `TO GOAL SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER`. Optional: `ROLLBACK`. |
| R-DISPATCH-2 | An empty required field means the unit is not scoped. The hook MUST refuse to relay and return the block to Main with the list of empty fields. |
| R-DISPATCH-3 | Exception: `TIER` may be empty if the Jev TIER-suggestion gate is enabled (§7); the hook fills it. Main's value always wins. |
| R-DISPATCH-4 | Small units may fold fields into one paragraph but MUST keep GOAL, SCOPE, VERIFY, REPORT explicitly. |
| R-DISPATCH-5 | Units that deploy MUST include `ROLLBACK`. |
| R-DISPATCH-6 | Each relayed brief is written to `.orche/units/<unit-id>/BRIEF.md` with `rev: 1`. |
| R-DISPATCH-7 | Unit budget = TIMEBOX (time) + `maxTurns` (turns) + subagent auto-compact window (tokens). All three are set. |
| R-DISPATCH-8 | Implementation note (v0.1.0): only a project's Main or a division session may dispatch (Build dispatches to its feature agents); Liaison and feature sessions are refused. Session identity is the `ORCHE_SESSION` environment variable, which ORCHE's pane commands set (`ORCHE_SESSION=<session> claude --name <session>`). Inside a unit worktree the worktree names the unit; a session in the main checkout without `ORCHE_SESSION` is unidentified and cannot dispatch. Each brief is kept as `.orche/units/<unit>/BRIEF.md` plus `BRIEF.rev<N>.md` history; `TO` accepts a session name, a division shorthand of the current project, or a project key/alias (→ that project's Main). |

Control verbs (first line of a dispatch block instead of `TO`-first form):

| Verb | Syntax | Effect |
|---|---|---|
| AMEND | `AMEND <unit-id> rev N` followed by changed fields + `SUPERSEDES rev N-1: <what>` | Writes BRIEF.md rev N; delivers to worker (§4.9) |
| STOP | `STOP <unit-id>` + optional reason | Worker stops at a safe boundary, `wip:` commit, writes HANDOFF |

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
- Implementation note (v0.1.0): `orche hr correct <proj> --text "..."` appends the next numbered order to the project's STANDING.md (created from the template if missing).

### 4.4 Completions as queue events

| ID | Rule |
|---|---|
| R-INBOX-1 | On finishing (or stopping, or blocking), a worker writes exactly one inbox pointer JSON (§14.5) to `$ORCHE_HOME/inbox/<recipient-session>/`. The report body is a file; the pointer only references it. Implementation note (v0.1.0): workers file it with `orche report <status> --summary "..."` (recipient defaults to Main; a feature reports to Build). The pointer lives under `$ORCHE_HOME`, not `.orche/inbox/`. A `release-ready` claim is filed as `done`. |
| R-INBOX-2 | The UserPromptSubmit hook injects pending pointers into Main's next turn, batched. |
| R-INBOX-3 | Pointers are delivered only at four drain points: (1) a critical-path unit finished, (2) a track roll-up (all units of a wave finished), (3) immediately before Main reports to the user, (4) an urgent flag. Implementation note (v0.1.0): the UserPromptSubmit hook injects urgent pointers at the recipient's next prompt and holds the rest until a drain trigger: a prompt starting with `[drain]` or `/drain`, the `[orche] inbox:` nudge, a status/progress question, 4 or more pending pointers, or a pointer older than 90 minutes. |
| R-INBOX-4 | Urgent = failure, blocker, security, deploy impact. Urgent pointers wake Main immediately. Without Jev, urgency is set by the worker's `urgent` field; with Jev, gate ① also scores it. Implementation note (v0.1.0): a pointer is urgent when `urgent` is true or its status is `failed`, `blocked`, `question`, `stop` or `external-request`. Hooks never type into Main's pane for a report; the urgent pointer is shown at Main's next turn. |
| R-INBOX-5 | The hook marks stalled workers using Orca worker liveness. Nudging a stalled worker is done by the hook, not by the user. |

### 4.5 Drain discipline (Main)

1. Classify each pointer: `landed` / `needs-verify` / `failed` / `zombie` / `noise`.
2. Update ledger and registry.
3. Dispatch the next wave in one message.
4. End with a 3-line status: counts by class · what changed · open gates.

R-DRAIN-1: During drain, Main MUST NOT read diffs. If review is needed, Main creates a verification unit and dispatches it to Build or a verifier.

### 4.6 Confirm loop before build

1. A request that needs a plan goes to R&D (product/architecture) or GTM (content/launch) for a draft.
2. Main shows the draft to the user (ketchup style, with defaults).
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

### 4.8 TIER routing

| TIER | Examples | Executor | Effort |
|---|---|---|---|
| mechanical | Rename, dependency bump, copy edit, one-line config | exec (Sonnet 5.5) or Opus medium | low–medium |
| standard | Feature implementation, fix, tests | Feature agent (Opus 5.5) | high |
| hard | Cross-cutting design, concurrency, root cause, subtle algorithms, DB migrations | deep-reasoner (Opus 5.5) | max |

R-TIER-1: Use high effort only where needed. Main writes TIER; if empty and Jev is enabled, gate ④ suggests one.

### 4.9 Overnight / autonomous contract

Template (`~/.claude/orche/templates/night.md`): `GOAL` / `FINISH-PREDICATE` (a script exiting 0 when done) / `PERMISSIONS` / `ESCAPE` ("if blocked N hours, stop and report why").

| ID | Rule |
|---|---|
| R-NIGHT-1 | Duration is not a completion condition. |
| R-NIGHT-2 | The predicate MUST NOT be weakened during the run. |
| R-NIGHT-3 | At 70% of budget, stop spawning new work; land only verified work. |
| R-NIGHT-4 | The Stop hook runs the predicate: non-zero ⇒ exit 2 (continue); zero ⇒ allow stop and report. |
| R-NIGHT-5 | Escape hatch fires ⇒ stop, write HANDOFF, urgent pointer. |
| R-NIGHT-6 | Runs are launched via Orca automations. |

### 4.10 Mid-flight corrections

Rule R-AMEND-0: A correction is a new brief revision, not a chat message.

| Case | User says (example) | Main does | Hook / worker does |
|---|---|---|---|
| Correction (conflicts with brief) | "pages unit: don't change fonts" | `AMEND <unit> rev 2`, `SUPERSEDES rev 1: font change`; add to STANDING if permanent (`orche hr correct`) | Hook writes BRIEF.md rev 2, sends Orca mail to the worker (+ `terminal send` if urgent). Implementation note (v0.1.0): delivery is an inbox pointer plus an `[orche] inbox: <unit> rev <N>` nudge typed into the worker's pane; Orca mail is not used. Worker reads mail at checkpoints (before new file, after tests, before declaring done), logs receipt in DECISIONS.tsv, reverts work that conflicts with rev 2 |
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
| R-XPROJ-2 | Information requests flow Main(A) → Liaison(A) → Liaison(B) → back. Card first. |
| R-XPROJ-3 | Work requests are written as an external-request pointer into Main(B)'s inbox. Liaison cannot assign work. Implementation note (v0.1.0): the pointer is filed with `orche report external-request --to main-<B> --summary "..."`. |
| R-XPROJ-4 | Main(B) asks its user: "1 external request: accept? (default: review at next drain)". |
| R-XPROJ-5 | Classes listed in B's STANDING as auto-accept (docs, copy, config) skip confirmation. Requests that change code always require confirmation. |
| R-XPROJ-6 | Dependencies (B first, then A) are registered on the request; B's completion pointer triggers A's Build unit. |
| R-XPROJ-7 | If Main(B) is asleep, `orche wake main-<B>` runs `claude --resume main-<B>` in B's worktree, then delivers. Waking costs one Main turn by design. Implementation note (v0.1.0): the relay never opens panes: with no pane open it logs `wake-requested` (Main and Build) or `needs-wake` (dormant/paused units); the pointer waits in the inbox. Main panes are opened by `orche init`. |
| R-XPROJ-8 | If the user picked the wrong Main, Main forwards as an external request and says so in one line. |

### 4.12 Project disambiguation

| ID | Rule |
|---|---|
| R-PROJ-1 | Candidates are a closed list: projects in the registry only. Unregistered folders are never candidates. |
| R-PROJ-2 | Each project has `aliases` in the registry and an `Aliases:` line in its card. HR adds an alias after observing the same user term twice. |
| R-PROJ-3 | Main's first line on every request is an echo-back: `Understood: source = <project>, output = <project>`. |
| R-PROJ-4 | Confidence rule: exactly one candidate (exact alias match, or Jev Choice ≥0.9) ⇒ proceed. Two+ candidates or 0.6–0.9 ⇒ one-line clarification with default; information lookups proceed on the default, work requests wait. Zero candidates ⇒ do not guess; offer to register (`orca repo add` + HR onboarding). |
| R-PROJ-5 | A `[project]` bracket tag (name or alias) in the user's text is resolved by the hook without a model. Implementation note (v0.1.0): Main resolves the tag against the registry (projects + aliases) per its rulebook; the hook resolves a dispatch `TO <project or alias>` to that project's Main without a model. |
| R-PROJ-6 | Never use `@` for project tags. `@` is Claude Code's session-mention (direct message to a named session). |
| R-PROJ-7 | The panel where the user typed fixes the output project; only the source is ambiguous. |
| R-PROJ-8 | An alias that maps to 2+ projects always triggers clarification. |

---

## 5. Quality assurance

### 5.1 Verification ledger

File: `docs/LEDGER.tsv` per repo. CLI: `orche ledger record|check`.

Implementation note (v0.1.0): the canonical ledger is `ORCHE_HOME/ledger/<proj>.tsv` (one per project); `<repo>/docs/LEDGER.tsv` is an export written by `orche ledger export`. `pr` accepts a number or `none`; `orche ledger check [--sha SHA] [--pr PR] [--min VERDICT]` (sha defaults to `git rev-parse HEAD`). The recorder refuses a verifier equal to the producer session.

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
| R-LEDGER-3 | The Stop hook blocks any "release-ready" or completion claim for a unit with a PR but no valid row. Implementation note (v0.1.0): keyed by HEAD sha: `STATUS: done` needs a row of at least `type-check-only`; `STATUS: release-ready` needs at least `unit-test-verified` from a verifier other than the claimant. |
| R-LEDGER-4 | CI green alone is never a verdict. |
| R-LEDGER-5 | Build owns the ledger; feature agents may self-report, but only Build or an independent verifier writes accepted verdicts. |
| R-LEDGER-6 | Rows are append-only. |
| R-LEDGER-7 | Release ladder steps (§2.4) each require a row for the deployed SHA. |

### 5.2 Rubric and grader

- Each repo has a rubric file (`docs/RUBRIC.md`) at the top of the ledger process.
- The grader runs in a separate context (fresh session) with: diff + ACCEPT + VERIFY output + rubric. It never sees the writer's conversation.
- R-GRADE-1: Maximum 3 grader iterations per unit. After 3 failures, mark `verifier-failed` and send an urgent pointer to Main.

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

### 5.5 Decision log

- File: `DECISIONS.tsv` (project and per-feature). Writer: `log.sh`. Implementation note (v0.1.0): the writer is `orche decide <phase> <decision> <why> <evidence> <result>` (cells single-line; cells starting with `= + - @` are prefixed with `'`).
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

### 5.9 careful hook

Registered globally (gstack `careful` hook script):

| Pattern | Decision |
|---|---|
| `rm -rf`, `DROP`, `git reset --hard`, `git checkout .`, force-push | ask |
| Deleting `/` or `~`; force-push to default branch | deny |
| Environment dumps (`env`, `printenv`, `set` piped/redirected) | ask |

### 5.10 `/correct`

Part of HR weekly audit. A mistake seen twice is promoted to enforcement, preferring in order: architecture > types/lint > tests > hooks. Map of rule → enforcement lives in `~/.claude/orche/rules.md`. A rule without enforcement that recurs is marked `repeat`.

### 5.11 TDD when cheap

Failing minimal test → fix → pass. If impractical, use the nearest executable verification.

---

## 6. Token economy

### 6.1 Levers (ordered by effect)

| # | Lever | Change | Expected effect |
|---|---|---|---|
| 1 | Unit-end `/clear` + file memory | Clear instead of compact; keep session only with uncommitted work or running process | Worker context 243K→120K; Main 518K→200K |
| 2 | Hook relay + drain discipline | Relay and report cost 0 Main turns; batched notifications | Main turns per request 8–12 → 3–4 |
| 3 | TIER routing | mechanical → Sonnet/medium; max only for hard | Fewer Opus-high calls |
| 4 | Main never reads results | Long work in divisions; review as verification units | Main context protected |
| 5 | Verification as scripts | 0-token re-verification | Less rework and re-asking |
| 6 | Model per role | Research/Liaison/HR Sonnet; runner Haiku; Fable only for Main | Fewer Fable calls |
| 7 | Jev gates (§7) | Routine messages, urgency, TIER suggestion | Routine 13–15% handled with 0 Main turns |
| 8 | Cache-friendly ordering | STANDING + persona as prefix; brief last. Evaluate `subagentPromptCacheTtl: 1h` after one week of measurement | Fewer cache writes |
| 9 | Parallel width | Concurrent exec 3; awake sessions 12; in-flight workers ≈10 rolling | Prevents runaway |
| 10 | Visibility | Status line, `orche`, sidebar | 0 lookup tokens |
| 11 | Large one-off jobs as Workflow scripts | Fan-out and collection in a Claude Code Workflow script; Main receives final result only | Main context protected |

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
| ① | Drain urgency (report injection) | Pointer summary + Main's pending work | Noul "Main must act now", Noul "user must see now", Choice affected division | Urgent ⇒ inject now; else batch to next drain point | Main wake-ups | 5 |
| ② | Routine-message handling (UserPromptSubmit) | User message + in-flight units + "plan draft pending" flag | Choice {status, resume-nudge, confirm, revision, new request} | status ⇒ answer from STATUS + ledger (0 Main turns); resume ⇒ nudge stalled worker; confirm (≥0.9) ⇒ relay confirmed plan to Build | Main turns | 5 |
| ③ | Intake tag + risk flag (UserPromptSubmit) | User message + division/feature list + registry projects | Choice target unit/project, Noul needs plan, Noul hard to reverse, Score urgency | Inject 1 tag line; risk ≥0.7 ⇒ add Always-pause confirmation flag (block direction only) | Main classification | 5 |
| ④ | TIER suggestion (dispatch hook) | Brief text | Score complexity 1–5, Noul needs design/root cause | Fill empty TIER only | Wasted Opus-high | 5 |
| ⑤ | HR scope matching | Request + existing feature scopes | Choice best unit (+other), Noul new agent justified | other + justified ≥0.8 ⇒ create; else assign existing | HR turns | 7 (optional) |
| ⑥ | Card refresh gate | Commit messages since base commit | Noul user-visible change | None ⇒ skip Liaison call | Liaison turns | 7 (optional) |

Not placed (solved deterministically): brief completeness (field check), report quality (REPORT field check), question classification (rule), Liaison response gating (low frequency).

### 7.3 Adoption conditions (all MUST hold)

1. Masking before send: keys, seeds, addresses, emails masked; code, diffs, and customer data are never sent. Same regex library as the pre-commit secret scan.
2. Version pinned; thresholds stored per version.
3. 1-second timeout, fail-open (on error or timeout, behave as if Jev is absent and route to Main).
4. Never decides irreversible actions; gate ③ may only add a confirmation, never remove one.
5. Offline validation first: ≥250 logged samples (e.g., 150 user messages + 100 reports), masked, labeled by Opus 5.5, accuracy and calibration measured per placement. Hooks are connected only after passing.

Implementation note (v0.1.0): Jev is off by default (`jev.enabled: false`; `orche jev enable|disable`). `orche jev eval extract|run|report` works without `enabled`, so validation can run before any hook uses Jev; `label` is deliberately not implemented (a separate Claude session writes `labels.jsonl`). The client replaces code, diffs and blobs with `[code omitted]` and passes every string through `mask.redact` before sending. Only placement ① (drain urgency, add-only) is wired in v0.1.0.

### 7.4 Confidence bands

| Band | Action |
|---|---|
| ≥0.9 | Act |
| 0.6–0.9 | Act with confirmation (one-line echo/default) |
| <0.6 | Defer to Main |

### 7.5 Scope statement

Jev never touches code quality. Quality is decided by the ledger and scripts. A wrong Jev answer can cost a turn or a delay, never a bad merge.

Expected volume: ~120–150 calls/day, a few cents/day.

---

## 8. Visibility (0 tokens)

### 8.1 Status line

HR writes `~/.claude/orche/status/<project>.line`; the status line script appends it with `cat`.

Implementation note (v0.1.0): `orche statusline`, `orche feat` and `orche stage` write `ORCHE_HOME/status/<project>.line`. The snippet `orche install` adds to `~/.claude/statusline-command.sh` walks up from the session directory to the first `.orche/project` marker (one line: the project key, written by `orche init`) and prints the cached line using shell builtins only. Actual segment: `R&D● RS○ BUILD●3 GTM● OPS○ · new1 · 🔑2 · unverified1` (`◐` = onboarding; zero counters omitted; no `svc` cell yet).

Segment format:

```
R&D● Research○ Build●3 GTM● Ops○ · new1 · 🔑2 · unverified1 · svc●
```

| Token | Meaning |
|---|---|
| `●` / `○` | Division awake / dormant |
| `Build●N` | N awake feature agents |
| `newN` | Feature agents created today |
| `🔑N` | Pending secret requests |
| `unverifiedN` | Release-ready units without a ledger row |
| `svc●/○` | Service health (only when stage = public) |

### 8.2 Orca sidebar mapping

Hierarchy: repo → worktree card → agents (terminal tabs). One card per role.

```
<project>
 ├─ main [default]          2 agents: main-<project>, liaison-<project>
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
| R-VIS-6 | Session names: `main-<proj>`, `liaison-<proj>`, `div-<proj>-<division>`, `feat-<proj>-<feature>`. Set with `claude --name` or `/rename`. |

Implementation note (v0.1.0): worktrees are named `div-<division>` (no Orca parent) and `feat-<feature>` (Orca parent = the Build worktree); pane titles equal the session names. R-VIS-2 and R-VIS-3 (card comment and status) are not set by ORCHE yet; the unread dot is set when a feature agent is created. See `docs/orca-sidebar.md`.

### 8.3 macOS notifications

Events: feature agent created / slept / retired; HR freeze; secret request; urgent report; confirmation awaiting; Ops incident.

### 8.4 `orche` CLI

| Command | Purpose |
|---|---|
| `orche projects` | List registered projects + aliases |
| `orche depts [project]` | Divisions and feature agents with state |
| `orche status` | All projects, one line each |
| `orche hr log` / `freeze` / `unfreeze` | HR events and freeze control |
| `orche sleep <unit>` / `orche wake <unit>` | Sleep/wake a role session |
| `orche card <project>` | Print PROJECT_CARD.md |
| `orche ledger record ...` / `orche ledger check --pr <pr> --sha <sha>` | Ledger write/check (check exits 2 if unverified) |
| `orche secret request|wait|fill|ls|missing|run|render|rotate|share|revoke|add|redact-transcripts` | Keymaster (§10) |
| `orche dev add|remove` | Developer IAM access (§10) |
| `orche janitor run|undo|restore` / `orche discard <unit>` | Janitor (§11) |
| `orche metrics` | Weekly metrics (§6.2) |

Users need only `orche depts` and `orche status`; everything else is requested from Main in natural language.

Implementation note (v0.1.0): `orche --help` is the authoritative list. Additions: `orche report`, `orche decide`, `orche feat new|sleep|wake|retire`, `orche hr audit|correct`, `orche stage set <proj> building|public [--yes]`, `orche init`, `orche apply-all`, `orche install|uninstall|doctor`, `orche jev`, `orche statusline`, `orche version`. The ledger check takes `--pr`/`--sha` flags, not a positional PR.

### 8.5 Logs

`~/.claude/orche/hr-log.md`, `~/.claude/orche/graveyard.md`, `~/.claude/orche/secrets/audit.log`.

---

## 9. Cross-project collaboration

### 9.1 PROJECT_CARD.md

Location: repo root `PROJECT_CARD.md`, symlinked at `~/.claude/orche/cards/<project>.md`. Size ≤2K tokens.

Required sections:
1. One-line description
2. State and latest release
3. Key features (user language)
4. Last 5 changes
5. Public links
6. Glossary
7. `Aliases:` line
8. Owning sessions
9. Base commit (SHA the card reflects)

### 9.2 Refresh policy

| ID | Rule |
|---|---|
| R-CARD-1 | Refresh right after each release and weekly (gated by Jev ⑥ when enabled). |
| R-CARD-2 | Liaison reads only `git log <base>..HEAD`; if no user-visible change, exit without writing. |
| R-CARD-3 | Ask the other Liaison (`SendMessage`) only when the card is insufficient; fold the answer back into the card. |

### 9.3 How users phrase requests

| Intent | Where | Example phrasing | What happens |
|---|---|---|---|
| Learn another project's news | Any Main or terminal | "What changed in example-app lately?" / `orche card example-app` | Card read; terminal path costs 0 tokens |
| Reflect another project in mine | Main of the output project | "Update the intro page plan with example-app's latest features" | Main(web) → Liaison(web) → Liaison(app) → summary → GTM plan → confirm → Build |
| Ask another project to do work | My Main | "Ask example-app to add a date filter to the history API" | External request into Main(app) inbox → app user confirms (default: review at next drain) → app Build → completion returns to web Liaison |
| Coordinated change | Main of the project that must change first | "app changes the API first; web follows" | External request + dependency; app's completion pointer starts web's Build unit |
| Quick direct question | Other project's Liaison panel | "What is the current payment API response shape?" | Liaison answers from card/STATUS; Main not woken |
| Work directly with another owner | Other project's division/feature panel | Normal instructions | Handled under that project's rules; significant changes summarized to its Main |

Rule of thumb: "What changes when this is done?" — talk to that project's Main.

### 9.4 Liaison role

- Tool of Main, not of the user. The user may open a Liaison panel to ask questions, but cannot assign work through it.
- Model Sonnet, effort low, `memory: project`.
- Outputs: card updates, answers, external-request relays.

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
- Onboarding: Build scans `.env.example` and `process.env.*` usage and requests all missing keys at once (`orche secret missing`).

### 10.4 Developer sharing

| Priority | Method | When | Operator | Developer |
|---|---|---|---|---|
| 1 | GCP IAM | All GCP projects | `orche dev add dev@example.com --project <app>-dev` (grants `secretmanager.secretAccessor` to a personal account) | `gcloud auth application-default login` once; `orche secret run` fetches |
| 2 | Vercel team invite | MVP on a team plan | Invite | `vercel env pull` |
| 3 | `age` bundle | Contractors without a GCP account | `orche secret share --to <dev> --env dev` → `.env.dev.age` | Install `age`, share public key once, `age -d` |

- Shared cloud accounts are owner/emergency only. Developers use personal identities.
- Revocation: `orche dev remove`; no key rotation required.
- Developers get dev projects only; prod is owner + deploy service accounts.

### 10.5 Read-deny hook (PreToolUse)

Deny with a pointer to `orche secret run`:
- Read/cat/grep/source of `.env*`
- Direct `gcloud secrets versions access`
- Environment dumps go through careful (ask).

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
| Age-based | Regenerable artifacts only | Build caches idle 7 days; temp dirs; one-off scratch with no owning unit. Implementation note (v0.1.0): the Orca Codex `.tmp` sweep is opt-in (config `janitor.codex_tmp`, off by default) |

Dormant ≠ discarded. Sleep never triggers cleanup.

### 11.2 Rules

| ID | Rule |
|---|---|
| R-JAN-1 | Sessions are never deleted by age. Set `cleanupPeriodDays: 3650` in Claude Code settings. |
| R-JAN-2 | Sessions of retired/discarded units are compressed to `ORCHE_HOME/archive/<session>/` (archive, not delete). Archives are deleted only on explicit instruction. Implementation note (v0.1.0): the path moved from `~/.orche-archive/<unit>/`. |
| R-JAN-3 | Before deleting anything, append a GRAVEYARD line. |
| R-JAN-4 | Deleted files go to `ORCHE_HOME/trash/<date>/`; permanently removed after 30 days. `orche janitor undo|restore` available. Implementation note (v0.1.0): the path moved from `~/.orche-trash/<date>/`. |
| R-JAN-5 | First week runs dry-run only; the dry-run report is reviewed by the user. Implementation note (v0.1.0): `orche janitor run` stays a dry run until `orche janitor enable`; `--dry-run` always forces one. |
| R-JAN-6 | Buckets are advice, not permission. |

GRAVEYARD format (`docs/GRAVEYARD.md` per repo + global `~/.claude/orche/graveyard.md`):

```
| date | what | why | restore location | decided by |
```

### 11.3 Untouchables

- Sessions and artifacts of active or dormant units (Implementation note (v0.1.0): the protected statuses are `active`, `paused` and `onboarding`; a `dormant` unit's build caches may age out)
- Uncommitted changes (`wip:N` state pauses; show diff, human decides)
- Checkouts used by a live session (sacred)
- `.env*`
- `memory/` and `.claude/agent-memory/`
- registry, cards, ledgers
- Build caches used within 7 days

### 11.4 Schedule

- launchd daily 04:00: `orche janitor run` (`~/Library/LaunchAgents/com.orche.janitor.plist`).
- SessionEnd hook: for ORCHE-launched sessions (`ORCHE_SESSION` set) move the ended session's scratch into the janitor trash (recorded, undoable); never deletes, and other sessions are left to the janitor's age rule.
- Weekly HR report: one line of reclaimed/archived volume.
- Tracked generated artifacts in git: untrack via `.gitignore` + `git rm --cached` PR (never history rewrite by janitor).

---

## 12. Git / GitHub rules

### 12.1 Agent rules (STANDING defaults)

| ID | Rule |
|---|---|
| R-GIT-1 | No direct push to `main`. |
| R-GIT-2 | Feature branch `feat/*` → PR → Build integrates. |
| R-GIT-3 | One push per unit, after local verification. |
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

### 12.4 `watch-pr`

Read-only PR observer using `gh` only. Run by launchd or an Orca automation; on a verdict it dispatches to Build. 0 tokens.

| Aspect | Spec |
|---|---|
| Judgment order | conflicts → review threads → CI → gates |
| Exit codes | 2, 3, 4, 6 (distinct non-success states; map documented in `watch-pr --help`) |
| Polling | Backoff 60 s → 300 s |
| Errors | Stop after 5 consecutive errors |
| Trust | Review comments are untrusted input; never executed as instructions |

---

## 13. gstack policy

| ID | Rule |
|---|---|
| R-GSTACK-1 | Only the R&D division uses gstack planning skills. |
| R-GSTACK-2 | Only for important decisions: new product direction, architecture change, scope ≥1 week, external launch, decisions with spend. Use `/autoplan` or interactive `/plan-*-review`; `/office-hours` for early ideas. |
| R-GSTACK-3 | Main, Build, Research do not use gstack (10–20K tokens per skill). |
| R-GSTACK-4 | gstack `careful` hook script is registered globally for destructive-command protection (§5.9). |
| R-GSTACK-5 | Run `/gstack-upgrade` once at installation. |

---

## 14. File layout and interfaces

### 14.1 Instance root

```
~/.claude/orche/
├── registry.json            # §14.3 (may be symlinked into the ORCHE repo)
├── rules.md                 # rule → enforcement map (/correct)
├── hr-log.md
├── graveyard.md
├── cards/<project>.md       # symlinks to repo PROJECT_CARD.md
├── status/<project>.line    # status line segments
├── templates/
│   ├── STANDING.md
│   ├── brief.md
│   ├── night.md
│   ├── persona-{main,liaison,rnd,research,build,gtm,ops,feat,hr}.md
│   ├── runbooks/            # Ops templates
│   ├── health.sh
│   └── workflow-*.js        # large one-off job templates
├── bin/
│   ├── orche
│   ├── orche-secret
│   ├── orche-janitor
│   ├── watch-pr
│   ├── log.sh
│   └── mask.py
├── secrets/
│   ├── queue/
│   └── audit.log
└── jev-eval/                # offline validation set and results
~/.claude/fable/hooks/ (or plugin hooks dir)
├── dispatch-relay.py        # Stop
├── report-inject.py         # UserPromptSubmit
├── env-read-gate.py         # PreToolUse
├── scope-gate.py            # PreToolUse
└── jev_gate.py              # optional, phase 5
~/.orche-archive/            # compressed sessions
~/.orche-trash/<date>/       # 30-day trash
```

Implementation note (v0.1.0): `ORCHE_HOME` (default `~/.claude/orche/`) holds `registry.json`, `config.json`, `install-state.json`, `hr-log.md`, `graveyard.md`, `janitor.enabled`, and the directories `logs/`, `status/`, `inbox/<session>/`, `cards/`, `ledger/<proj>.tsv`, `secrets/` (`requests/`, `audit.log`, `shares.json`), `briefs/`, `grader/`, `jev-eval/`, `janitor/`, `sessions/`, `archive/<session>/` and `trash/<date>/`. Templates, hooks and the CLI ship in the plugin repository (`templates/`, `hooks/*.py`, `bin/orche`), not under `ORCHE_HOME`. `orche install` records the original value of every setting it changes in `install-state.json` and `orche uninstall` restores them.

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
│   ├── units/<unit-id>/{BRIEF.md,BRIEF.rev<N>.md,REPORT.md}
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

Implementation note (v0.1.0): the inbox moved to `ORCHE_HOME/inbox/<session>/` (§4.4). Unit memory (`.orche/div|feat/<name>/`) lives in the unit's own worktree. `.claude/settings.local.json` is added to the repository's `info/exclude` and is never committed.

### 14.3 `registry.json` schema

```json
{
  "limits": {
    "per_project_per_day": 3,
    "per_project_per_week": 10,
    "per_request_auto": 2,
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
      "liaison_session": "liaison-example-web",
      "card": "~/.claude/orche/cards/example-web.md",
      "standing": ".orche/STANDING.md",
      "aliases": ["web", "website", "homepage", "site"],
      "divisions": {
        "rnd":      { "status": "active",  "session": "div-example-web-rnd",      "model": "claude-opus-5-5",   "effort": "high" },
        "research": { "status": "dormant", "session": "div-example-web-research", "model": "claude-sonnet-5-5", "effort": "medium" },
        "build":    { "status": "active",  "session": "div-example-web-build",    "model": "claude-opus-5-5",   "effort": "high",
          "features": [
            { "name": "pages", "branch": "feat/pages", "scope": ["src/pages/**", "content/**"],
              "session": "feat-example-web-pages", "status": "active",
              "created": "2026-10-20", "last_activity": "2026-10-20" }
          ] },
        "gtm":      { "status": "active",  "session": "div-example-web-gtm",      "model": "claude-opus-5-5",   "effort": "high" },
        "ops":      { "status": "dormant", "session": "div-example-web-ops",      "model": "claude-opus-5-5",   "effort": "high" }
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
| `divisions.*.status` | `active` \| `dormant` (Implementation note (v0.1.0): `active` \| `dormant` \| `paused` \| `retired` \| `onboarding`) |
| `features[].status` | `active` \| `dormant` \| `retired` (Implementation note (v0.1.0): `active` \| `paused` \| `retired` \| `onboarding`) |
| `aliases` | Unique across projects unless intentionally shared (shared ⇒ R-PROJ-8) |
| `divisions.ops.status` | MUST be `dormant` when `stage = building` |
| `events[]` | `{ts, actor, action, target, detail}`; every HR change appends one. Implementation note (v0.1.0): `{at, type, project, unit, by}` with `type` ∈ create, sleep, wake, retire, freeze, unfreeze, stage; the file carries `"version": 1` |

### 14.4 Dispatch block grammar

```
block      := "```dispatch" NL (brief | amend | stop) "```"
brief      := field+                         ; required fields per R-DISPATCH-1
amend      := "AMEND" SP unit_id SP "rev" SP int NL "SUPERSEDES" SP "rev" SP int ":" SP text NL field*
stop       := "STOP" SP unit_id (SP text)? NL
field      := KEY SP+ value NL (continuation)*
continuation := SP SP+ text NL               ; indented lines extend the previous field
KEY        := "TO" | "GOAL" | "SCOPE" | "CONTEXT" | "ACCEPT" | "VERIFY" | "TIMEBOX"
            | "FORBID" | "REPORT" | "TIER" | "ROLLBACK"
TIER value := "mechanical" | "standard" | "hard"
comment    := "#" text                       ; stripped
unit_id    := <project> "-" <slug> "-" yyyymmdd "-" n
```

- Multiple blocks per response are allowed; each is one unit.
- The hook assigns `unit_id` if absent and echoes it in the relay confirmation.

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

Optional fields: `memory_saved` (bool), `depends_on` (list of unit ids).

Implementation note (v0.1.0): the fields and statuses above replace the earlier draft (`landed | needs-verify | failed | blocked | stopped | external-request`, `report`, `branch`, `pr`, `ts`). `info` is used for relayed briefs, `stop` for STOP blocks; an external request carries the request text in `summary`.

### 14.6 Hook contracts

Common stdin JSON fields from Claude Code: `session_id`, `transcript_path`, `cwd`, `hook_event_name`; plus event-specific fields (`tool_name`, `tool_input` for PreToolUse; `prompt` for UserPromptSubmit; `stop_hook_active` for Stop; `source` for SessionStart).

| Hook | Event | Input used | Output convention | Responsibilities |
|---|---|---|---|---|
| `dispatch-relay` | Stop | `transcript_path` (last assistant message), `stop_hook_active` | Exit 0 = allow stop. Exit 2 + stderr message = Claude continues with that message | Parse dispatch blocks; refuse on empty fields (exit 2 listing fields); prepend STANDING + persona; write BRIEF.md; relay via Orca; block completion claims without ledger row; block REPORT without memory-save; run finish predicate for night runs. Never types `/clear` or any slash command (R-MEM-3). MUST NOT loop: if `stop_hook_active` is true and nothing new to enforce, exit 0 |
| `report-inject` | UserPromptSubmit | `prompt`, `cwd` | Exit 0; stdout is added to the model's context | Drain inbox at drain points; stalled-worker marks; `[project]` tag resolution; Jev ②③ (phase 5) |
| `env-read-gate` | PreToolUse | `tool_name`, `tool_input` | JSON `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"|"ask"|"allow","permissionDecisionReason":"..."}}` | §10.5 |
| `scope-gate` | PreToolUse | `tool_name`, `tool_input.file_path`, `cwd` | Same as above | Deny edits outside own worktree/scope; deny code edits from Main |
| careful | PreToolUse (Bash) | `tool_input.command` | Same as above | §5.9 |
| session-start | SessionStart | `source`, `cwd` | stdout / `additionalContext` injected | Inject resume order pointers (HANDOFF, STATUS, DECISIONS tail) and STANDING |
| scratch-clean | SessionEnd | `ORCHE_SESSION` | none | Move the ended ORCHE session's scratch to the 30-day trash (never deletes) |

All hooks: fail-open on internal error except `env-read-gate` and `scope-gate`, which fail closed (deny).

Implementation note (v0.1.0): hook files are `hooks/dispatch_relay.py`, `report_inject.py`, `env_read_gate.py`, `scope_gate.py`, `session_start.py`, `session_end.py`, registered by the plugin's `hooks/hooks.json`. Every hook works within a 1.8 s budget (`ORCHE_HOOK_BUDGET_S`) and ignores subagent events. The two gates deny only on a positive pattern match; on an internal error they allow and log (they do not fail closed). The careful hook is the operator's gstack script, registered by `orche install` only when present.

### 14.7 Orca CLI calls used

| Call | Purpose |
|---|---|
| `orca terminal list` | Find role panels |
| `orca terminal send` | Urgent delivery, nudge (Implementation note (v0.1.0): only the `[orche] inbox: <unit> rev <N>` nudge; never `/clear`) |
| `orca terminal rename` | Panel title = role name |
| `orca terminal create` | Open role panel on wake |
| `orca worktree create` | Division/feature worktrees (feature parent = Build) |
| `orca worktree list` | Reconcile with registry |
| `orca worktree set` | Card status/comment |
| `orca worktree rm` | Retire/rollback |
| `orca orchestration send` | Dispatch and AMEND mail (`--to dispatch:<id>`) (Implementation note (v0.1.0): wrapped but not used for delivery) |
| `orca orchestration check` | Worker liveness / mail |
| `orca automations create` | Night runs, weekly drift check, card refresh, Ops alerts |
| `orca search` | L4 transcript search |
| `orca repo add` | Register a new project |

### 14.8 Naming

| Item | Format |
|---|---|
| Main session | `main-<proj>` |
| Liaison session | `liaison-<proj>` |
| Division session | `div-<proj>-<division>` with division ∈ {rnd, research, build, gtm, ops} |
| Feature session | `feat-<proj>-<feature>` |
| Division worktree branch | `div/<division>` |
| Feature branch | `feat/<feature>` |
| Unit id | `<project>-<slug>-<yyyymmdd>-<n>` |

---

## 15. Rollout plan

Apply phases in order (C is independent). For each phase, list changed files and verify rollback before applying.

| Phase | Content | Scope |
|---|---|---|
| 0 | Pending global fixes: agent-definition corrections, runner model, orchestrator rules wording | Global |
| 1 | Rules and personas: ten principles, brief/TIER/STANDING conventions, drain discipline, question discipline, Always-pause, ketchup reports, night contract, session lifecycle (`/clear` at boundary after memory-save), `memory: project` on division/feature personas, `exec` definition (Sonnet/medium, auto-compact 200K, `maxTurns`), `cleanupPeriodDays: 3650`, compaction threshold 50% | Global |
| 2 | Hooks, visibility, Keymaster: `dispatch-relay`, `report-inject`, `env-read-gate`, `scope-gate`, careful global registration, SessionEnd scratch hook, `orche` CLI, status line segment, macOS notifications, `orche secret` + dialog + backends, `orche dev add/remove` | Global |
| J | Jev validation (parallel, as soon as a key exists): masking filter → ≥250 logged samples → Opus labeling → accuracy/calibration → per-placement thresholds; pin version | None (offline) |
| 3 | Cards and Liaison: `liaison` agent, card template + symlinks, weekly refresh automation | Global + pilot |
| 4 | Divisions, features, HR, quality: `hr` agent, `registry.json`, 5 division persona templates (incl. Ops runbook and health templates), rubric + max 3 iterations, `ROLLBACK` field, feature-agent generator, weekly audit + `/correct`, ledger CLI + `LEDGER.tsv`, `DECISIONS.tsv` + `log.sh`, `verify-<app>` skill + weekly check, HANDOFF/resume rules, independent-context judge, pilot wake-up | Global + pilot + IAM |
| 5 | Jev hooks ①–④ (only if J passed): 1 s fail-open, masked | Global |
| 6 | GitHub and large jobs: budget hard stop, CI template PRs, STANDING git rules, `watch-pr`, optional self-hosted runner, Workflow script templates for large one-off jobs | Per repo |
| 7 | Jev ⑤⑥ (optional): HR scope matching, card refresh gate | Global |
| C | Janitor: `orche janitor` (state-based, archive, trash, ledger, undo/restore), launchd 04:00, `orche discard`, GRAVEYARD template, untrack generated artifacts PR; first week dry-run | Global + repo |

Guidance:
- R-ROLL-1: Pilot first. For the first 2 weeks, run one flow on one project pair (e.g., `example-app` ↔ `example-web`: card → GTM plan → Build with 2 feature agents → ledger → release). Measure §6.2. Expand only if metrics approach targets.
- R-ROLL-2: Every phase is additive (files + settings keys). Rollback = delete files + remove keys; worktrees via `orca worktree rm`.
- R-ROLL-3: Operator-only items (key rotation, IAM migration, budget settings) are a separate checklist.
- R-ROLL-4: Long-running sessions do not pick up env/settings changes; restart role sessions after phases 1, 2, 5.

### 15.1 Non-goals

| Not adopted | Why |
|---|---|
| pstack orch ledger CLI | Duplicates Orca's orchestration primitives |
| Multi-model panels (arena/swarm), cross-family verification | Claude-only policy; independence comes from fresh context + scripts |
| No-planning dogma | The user explicitly wants a plan → confirm → build loop |
| Cursor-specific parts | Harness differs (Claude Code + Orca) |
| English style rules | Not relevant to outcomes; reports follow ketchup format only |
| benny | Replaced by Ops "2 observations before a unit" rule |
| Codex | Claude-only policy; existing Codex sessions are archived, not used |
| Server workflow frameworks (graph/durable-execution SDKs) | Require running servers; overkill for a single operator. Hooks + Orca suffice |
| Concurrent writers per worktree | Throughput collapses with shared locks; corrections become unresolvable |

---

## 16. Glossary

| Term | Definition |
|---|---|
| Main | Per-project orchestrator session; decides, never codes |
| Liaison | Per-project session that maintains the project card and answers other projects |
| Division | Long-lived role session with its own worktree: R&D, Research, Build, GTM, Ops |
| Feature agent | Long-lived session under Build owning one feature scope in a child worktree |
| HR | Global session/scripts managing the registry, limits, audits |
| Unit | One dispatched piece of work with a brief, a report, and (if code) a ledger row |
| Brief | The fenced `dispatch` block / `BRIEF.md` with required fields |
| Rev | Version number of a brief; corrections create a new rev |
| STANDING.md | Numbered standing orders auto-prepended to every brief |
| Inbox pointer | Small JSON written by a worker on completion; references the report file |
| Drain | Main's batched processing of inbox pointers at one of four drain points |
| Ledger | `ORCHE_HOME/ledger/<proj>.tsv` (exported to `docs/LEDGER.tsv`); PR + SHA + verdict rows defining completion |
| Verdict | `live-ui-verified`, `unit-test-verified`, `type-check-only`, `verifier-blocked`, `verifier-failed` |
| Rubric | Per-repo grading criteria used by the separate-context grader |
| Memory-save ritual | Updating STATUS.md and DECISIONS.tsv (and HANDOFF.md) before a unit-end `/clear` |
| HANDOFF.md | Fixed-section note that a resumed session treats as authoritative |
| TIER | `mechanical` / `standard` / `hard`; routes model and effort |
| Finish predicate | Script that exits 0 when an autonomous run is done |
| Escape hatch | Condition under which an autonomous run stops and reports |
| Always-pause | Actions that always need user approval |
| Ketchup-style report | Short user report: outcome first, then counts, decisions needed with defaults, then pointers |
| Stage | `building` or `public`; gates the Ops division |
| Project card | `PROJECT_CARD.md`, ≤2K-token summary for other projects |
| Keymaster | Model-free secrets broker (`orche secret`) |
| Janitor | State-based cleanup tool (`orche janitor`) |
| GRAVEYARD | Ledger of removed things with restore locations |
| Jev | External typed-decision model (Choice/Score/Noul) used only for gating |
| Orca | Desktop multi-agent manager: repos, worktrees, terminals, orchestration mail, automations |
| Dormant | Role exists in registry with no running panel; costs nothing (never started) |
| Paused | Role was put to sleep; its session resumes with `orche wake` |
| Onboarding | Worktree exists but onboarding has not finished; re-running the command resumes it |
| Retired | Role removed; worktree gone; session archived |
