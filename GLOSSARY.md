# ORCHE glossary

The domain terms used by ORCHE's code, docs and architecture reviews.

## Language

**Roster**:
The read model over the registry that answers who a session is, where a cwd is, and where dispatched work and pointers go (`lib/orche/roster.py`). Built once per hook run or command.
_Avoid_: registry (the stored file), units (the old helper module).

**Member**:
A registered session of a project — its Main, a division or a feature agent — with its registry record.
_Avoid_: unit (that is dispatched work), role (that is a member's kind), agent (too broad).

**Identity**:
Which member a session is: `ORCHE_SESSION` when set, else the member whose worktree holds the cwd. Never guessed from the main checkout or a pane title.
_Avoid_: whoami, sender (a sender is an identity in dispatch).

**Unidentified session**:
A session without `ORCHE_SESSION` outside every member's worktree (for example in the main checkout). It drains nothing, dispatches nothing, parks nothing and runs no night predicate.
_Avoid_: unknown session.

**Unregistered session**:
A session whose `ORCHE_SESSION` names no member. It is identified (its inbox is its own) but has no role and cannot dispatch.
_Avoid_: unknown session, ghost.

**Place**:
Where a cwd is: its project (nearest registered checkout or `.orche/project` marker) and the unit worktree holding it, if any. Place decides scope and default project, never identity.
_Avoid_: location, context.

**Target**:
The member a dispatch `TO` resolves to: an exact session, else a division of the sender's project, else the Main of the project named by key or alias. A Main of another project means an external request.
_Avoid_: recipient.

**Recipient**:
The member that hears from a unit: the sender of its current brief revision when that is a live Main or division of the project; otherwise a feature agent's division, everyone else's Main.
_Avoid_: target.

**Held note**:
A non-urgent `info` pointer that is neither a pane delivery nor a runner filing (ORCHE's dispatch feedback, a pane's `orche report info`). It waits for a drain trigger; every other pointer reaches the next prompt.
_Avoid_: non-urgent pointer.

**Wake**:
ORCHE typing `[orche] inbox: new reports` into a recipient's idle pane after a waking pointer lands, once per batch, never into a draft or a dialog; it costs the recipient one turn. The command is `orche nudge <session>`.
_Avoid_: ping; "nudge" for the concept (the brief line `[orche] inbox: <unit> rev <N>` is the nudge a deliverer types).

**Undelivered**:
An `orche status` line: a unit's newest `via: dispatch` pointer still in its recipient's inbox after 10 minutes (`inbox.LATE`). Fix with `orche deliver <unit>`.
_Avoid_: lost, stuck.

**Unread**:
An `orche status` line: a session with waking pointers (reports) in its inbox for 10 minutes or more. Fix with `orche nudge <session>`.
_Avoid_: undelivered (that is a brief, not a report).

**Queued**:
A delivery state: the line was typed into a busy pane and accepted without a turn start, so Claude Code holds it for the pane's next turn. It counts as delivered, not failed.
_Avoid_: pending, failed.

**Deliverer**:
`orche deliver <unit>`: the detached process that types a pane unit's nudge and reports a failed delivery to the sender; also the recovery command.
_Avoid_: relay (that is the Stop hook).

**Actor**:
Who gets credit in events, the HR log, graveyard and audit rows: `ORCHE_SESSION`, else `user`. Never inferred from the cwd.
_Avoid_: requester, by (those are field names).

**Memory dir**:
A member's folder for STATUS.md, HANDOFF.md and DECISIONS.tsv: `.orche/` (Main), `<worktree>/.orche/<div|feat>/<name>/` (division, feature).

**Unit**:
One dispatched piece of work with a brief, a report and (for code) a ledger row, named `<proj>-<slug>-<yyyymmdd>-<n>` (SPEC §16).
_Avoid_: unit for a division or feature agent (say member); the old `units.Unit` type meant that.

**Headless call**:
One `claude -p` process that ORCHE starts, feeds, waits for and cleans up (`lib/orche/headless.py`): a unit worker, an intake sweep, a Liaison answer or card refresh, or a triage classification.
_Avoid_: worker (say unit worker), runner (that is `orche run`).

**Headless token**:
The Claude OAuth token (`claude setup-token`) that `orche auth set` stores and every headless call gets as `CLAUDE_CODE_OAUTH_TOKEN`; panes keep the interactive login.
_Avoid_: API key (it is a subscription token), Jev key (stored separately).

**Unit worker**:
The headless call that carries out one unit in its target's worktree. `orche run` (the runner) starts it and files its result.
_Avoid_: runner, headless worker.

**Call context**:
What a headless call may see. *project*: the cwd's project settings, all tools under permission rules, ORCHE's PreToolUse gates. *sealed*: `--restricted`, only the allowed tools, files confined to the cwd. *blank*: an empty directory outside `$HOME`, no tools, its own system prompt.
_Avoid_: mode, sandbox, profile.

**Run tag**:
`<unit>@<started_ns>` (intake: `intake-<project>@<started_ns>`; the Liaison: `ask-<project>@<started_ns>`, `card-<project>@<started_ns>`). A tagged call's `ORCHE_RUN_ID` and the name of its marker file; when the call ends, every process carrying the tag is killed.
_Avoid_: run id (the lock's field name for the same value).

**Liaison**:
The headless read-only role that answers other projects' questions (`orche ask`) and refreshes PROJECT_CARD.md (`orche card refresh`). Not a member: no pane, session, memory or registry record.
_Avoid_: liaison session, liaison pane (gone since 0.5.0).

**Base commit**:
The commit a card reflects: `base <sha>` under `## Last refreshed`. A refresh reads what landed since it and moves it to HEAD when it writes.
_Avoid_: stamp (that is intake's).

**Card refresh**:
`orche card refresh <project>`: a git gate, then (only when commits outside the card and `.orche/` landed since the base commit) one Liaison call whose sections the CLI merges into the card.

**Effort**:
Claude Code's `--effort` level. A unit worker's effort comes from its TIER (mechanical low, standard medium, hard high); its model comes from the target's registry record. A pane's effort is its persona file's `effort:` line; a division record carries none (an old `effort` key is ignored).
_Avoid_: tier (TIER is the brief's field).

**Dispatch block**:
A fenced block tagged `dispatch` at the end of a Main or division reply. The Stop hook reads it as a new brief, an AMEND (a correction) or a STOP (a halt); `brief.parse` holds the grammar.
_Avoid_: brief (that is the stored file), order, ticket.

**Brief**:
A unit's instructions as stored in its unit folder: `BRIEF.md`, with a header (revision, unit, target, sender, worktree, branch, project root), the STANDING orders, the persona line, then `## Brief <unit>` and the fields. Written only by `brief.write`; read only through `brief.find` / `brief.newest`.
_Avoid_: dispatch block (what Main writes), task, spec.

**Revision**:
The number on a brief's first line (`rev: N`). A new brief is revision 1; each AMEND writes the next revision and keeps every one as `BRIEF.rev<N>.md`. A run, and the pointer it files, carry the revision the run was given (`brief_rev`).
_Avoid_: version, edit.

**Unit folder**:
`<project root>/.orche/units/<unit>/`, in the main checkout and git-excluded: `BRIEF.md` and `BRIEF.rev<N>.md` (the brief module's), `REPORT.md`, `REPORT.prev.md` and `RUN.json` (the run's).
_Avoid_: unit dir, brief dir, `ORCHE_HOME/briefs` (removed).

**Claim**:
The status a message states: its last `STATUS:` line whose word is done, release-ready, blocked, failed or stop (완료 reads as done), never a template echo such as `done|blocked|failed` (`donegate.claim`, SPEC §14.11). Done and release-ready are completion claims.
_Avoid_: status line (the runner's word), STATUS word, report status (that is the pointer's).

**Done gate**:
The checks a completion claim must pass before a session may stop, a pointer may say done, or a run is filed as done (`lib/orche/donegate.py`). There are three contexts: the Stop hook, `orche report` and the runner.
_Avoid_: completion gate (the old name), ledger gate (the ledger row is one of its checks).

**Proof**:
What the done gate read for a claim: HEAD, its ledger row and the missing items.
_Avoid_: evidence (that is the ledger's evidence column), verdict (the ledger's verdict column).

**Ledger-gated member**:
A member whose completion claims need a ledger row: the Build division and every feature agent.
_Avoid_: gated sender, gated target, gated (alone; `gated_by_stage` is a different gate).

**Run**:
One execution of a unit by `orche run`: it takes a run slot, starts the unit worker, and files exactly one pointer and one Run record (`lib/orche/runs/`). An AMEND restarts it.
_Avoid_: job (the run's resolved inputs, `runs/job.py`), task.

**Run slot**:
One of a project's `runner.max_parallel` places for a live Run, held through its Run lock. Among Runs, one per target (`<target>.target.lock`), since a target's units share one worktree.
_Avoid_: queue, worker pool.

**Run lock**:
`ORCHE_HOME/runs/<project>/<unit>.lock`: the live Run's pid, run tag, claude pid and process group, and what it runs — target, revision, TIER, model, effort, worktree, branch. A Lost run is filed from it.
_Avoid_: lock file (the project's `.mutex` is one too), pid file.

**STOP marker**:
`ORCHE_HOME/runs/<project>/<unit>.stop`, written by STOP and AMEND. A Run honours only a marker written at or after its request time (`ORCHE_RUN_SINCE`), and removes only the marker it consumed.
_Avoid_: kill file, stop flag.

**Lost run**:
A Run whose `orche run` died without filing (SIGKILL, OOM, power loss) or whose pid now belongs to another program. Reconcile files it from its Run lock: `failed` "runner died" and a Run record with `stop_reason: lost`, both naming what ran.
_Avoid_: crashed run, zombie; a stale lock is a Lost run whose pointer was already filed.

**Run record**:
`RUN.json` in the unit folder: 23 keys in a fixed order — what ran (unit, project, target, worktree, branch, TIER, model, effort), then the result. Built by one constructor; it holds the unit's last Run.
_Avoid_: run log (that is `logs/runs/<unit>.log`).

**Pending sweep**:
`ORCHE_HOME/runs/<project>/<run tag>.sweep`: a filed Lost run whose leftover processes are not killed yet. It holds no Run slot; every reconcile retries it.
_Avoid_: zombie lock.

**Urgent pointer**:
A waking pointer whose status is `blocked`, `question` or `failed`, or that carries `urgent: true`. It wakes Main without the quiet window and, on macOS, shows one notification.
_Avoid_: priority, alert.

**Quiet window**:
How long the nudger waits after the newest waking pointer before it wakes Main (`inbox.quiet_s`, default 90 s, capped by `inbox.quiet_max_s`, 240 s), so a batch of reports is one wake. Skipped when an urgent pointer is pending.
_Avoid_: debounce, delay.

## Relationships

- A project has one Main, divisions and (under Build) feature agents; each is a **Member**. The **Liaison** is a *sealed* **Headless call**, not a Member.
- An **Identity** names at most one Member; a **Place** names at most one project and one unit worktree.
- A **Target** receives **Units**; a **Recipient** receives pointers.
- A **Unit** is carried out by one **Unit worker** per run.
- A Unit worker is a **Headless call** in the *project* **Call context**. An intake sweep is *sealed*; a triage classification is *blank*.
- The target's registry record gives a Unit worker its model; the Unit's TIER gives its **Effort**.
- A **Dispatch block** becomes a **Brief** (revision 1) or the next **Revision** of one (AMEND); a STOP writes nothing.
- A **Unit** has one **Unit folder** and one **Brief** with one or more **Revisions**; a pointer's `brief_rev` names one Revision.
- A **Claim** comes from one **Member**, the identity of the session (in a run, the target).
- The **Done gate** turns a Claim into a **Proof**. Only a **Ledger-gated member**'s Proof can have missing items.
- A **Unit** has zero or more **Runs**; each holds one **Run slot** through its **Run lock** and leaves one pointer and one **Run record**.
- A **STOP marker** stops the Run of its unit that was requested before it.
- A **Lost run** is filed from its Run lock and may leave a **Pending sweep**.

## Flagged ambiguities

- "unit" meant both dispatched work (SPEC §16) and a division/feature agent (`units.Unit`): unit = dispatched work; agents are members.
- "unknown session" meant both unidentified and unregistered: two terms now.
- "runner" meant both `orche run` and the claude it starts. Now the runner is `orche run`, and the claude it starts is the unit worker.
- "run id" (a lock field) and the worker's `ORCHE_RUN_ID` are the same value: the run tag.
- "brief" meant both the fenced block and the stored file (SPEC §16): Dispatch block vs Brief.
- "rev" (`rev:`, `brief_rev`, `AMEND … rev N`) is always a Revision number; the N in an AMEND is the author's guess, the written number is prior + 1.
- "completion gate", "done gate" and "ledger gate" named one thing in three places: it is the Done gate, and the ledger row is one of its checks.
- "gated" meant both the ledger gate (`gated_sender`) and the stage gate (`gated_by_stage`): say ledger-gated member.
- "run" is the noun (one execution), `orche run` the command (the runner), `lib/orche/runs/` the module; the old runner module is gone.
- "liaison" meant a pane and session (until 0.4.0); now it is the headless role only.
