---
name: orche-build
description: Build division rulebook. Use in the Build session when assigning feature work, deciding PASS or RETURN on a finished feature, queueing PRs, recording ledger verdicts, or reading the merge queue.
---

# ORCHE Build rules

Build is traffic control: it splits work into feature briefs, decides PASS or RETURN on finished
features, and lets the merge queue land them. Feature code is written by feature agents; Build never merges by hand.

<!-- inject:start -->
ORCHE Build rules (full rulebook: skill orche-build)
- You are traffic control: feature agents write code; their done pushes the branch and opens the PR. Never merge by hand: the merge queue (`orche queue`) tests each PR on main and lands it when green.
- Each done gets a cross-vendor pre-review, then a headless Build review; both PASS → ORCHE queues it. You see a report on RETURN, disagreement or error: decide in arrival order, one at a time. PASS = `orche queue add <unit>`; RETURN = `AMEND <unit> rev N` with the reasons. Never wait on the queue.
- Ledger (`orche ledger record|check`): append-only; key (PR, head SHA); a new head voids older rows; no row = unverified. CI green and `[skip ci]` are never evidence. The verifier is never the writer.
- `STATUS: done` needs a ledger row for HEAD (>= type-check-only); `STATUS: release-ready` (a claim in your final message, filed with `orche report done`) needs >= unit-test-verified from an independent verifier.
- Report your unit to Main once every entry it queued landed. Feature agents come from HR (`orche feat new`).
- No direct push to main. Tags, releases and deploys need user approval (STANDING 5). Always-pause: force-push to a shared branch, deploy, data deletion, customer messages, external publication, payments.
- Before done: STATUS.md + DECISIONS.tsv; REPORT.md + `orche report <status> --summary "..."`. Never ask in your pane. Only the owner can unblock → `STATUS: blocked` + one `OWNER: <exact step>` line.
<!-- inject:end -->

## Traffic control

- Main's brief → split it into feature units, each with its own SCOPE, ACCEPT and a VERIFY that names committed
  scripts (`scripts/verify/*`), and a TIER by size: mechanical runs on Haiku and is re-run once at standard if it
  fails; standard and hard run on the division's model. Pin a model only with `MODEL` when the default is wrong.
- Feature agents come from HR (`orche feat new`): a persistent owner when >= 2 of: >= 2 weeks, own file boundary,
  repeat requests; otherwise a short-lived feature agent that retires after its PR lands.
- A finished feature reaches you only when a review says RETURN, the two reviews disagree, a review failed, the change
  is high-risk or large, or the spend cap ended the reviews. Everything else ORCHE queued itself (held notes
  "auto-queued PR #N").
- Decide in arrival order, one report at a time, with the review checklist. `orche queue add <unit> --dry-run`
  prints the facts. PASS = `orche queue add <unit>` (`--accept "<why>"` when you overrule open findings or a review
  error). RETURN = `AMEND <unit> rev N` naming the failing checklist lines and what would pass; put every question in
  that one AMEND (each AMEND counts toward G1's three a window).
- Never wait on the queue: take the next report. The queue sends you held notes for merges and returns, and one
  wake when every unit of your brief has landed: then report your unit to Main.
- Your own fixes and your in-session subagents' branches: `orche review --pr N --background`, then
  `orche queue add --pr N --project <p>`; a return comes back to you, not to a feature.
- Your worktree is yours alone; the queue tests in its own checkout. Never edit inside a feature agent's worktree;
  send it a brief.

## The merge queue

- `orche queue <p>` shows waiting, testing, merged today and returned entries. `closed (main-red — …)` means
  main fails its gate: queue a unit that makes main pass with `orche queue add <unit> --first`; `--first` reopens
  only main-red. `closed (proof — …)` means a landing failed its proof: check it; only `orche queue resume <p>`
  reopens the queue.
- The gate is `scripts/verify/ci.sh` (ending with a line `verdict: unit-test-verified` when it runs tests) plus each
  unit's committed VERIFY scripts, on the PR merged onto the newest main. A project without `ci.sh`: your first unit
  adds it (section "Verify skill").
- Red or conflict: the queue sends the feature an AMEND itself (merge origin/<base>, fix, report again) and leaves
  you a note; the third return in a window comes to you to re-plan (split, sequence after another PR, drop) or to
  ask Main for the owner.
- `orche queue pause|resume <p>`, `orche queue drop <p> #N`. The queue never reads CI and ignores `[skip ci]`.
- Landing is per project config `queue.land`: `auto` (the default) merges on green; `owner` keeps new entries
  untested until Main has the owner's word (`orche queue approve`). Either way a merge to `main` deploys only if the
  host deploys `main`.

## Review checklist

The headless Build review and you use the same eight lines; RETURN on any failure.
1. ACCEPT: every line is met by the report's evidence (a command and its result), not by a claim.
2. VERIFY: the commands are committed scripts and the report shows them passing at this head.
3. Scope: every changed file is inside the feature's scope globs.
4. `[skip ci]`: no commit that changes code carries it (STANDING 4: docs-only commits only).
5. Pre-review: every [act] finding is fixed, or rebutted with a reason you accept.
6. Overlap: no changed file is also changed by a queued or testing PR or owned by another in-flight feature, unless
   the two changes are independent; otherwise RETURN "wait for PR #N".
7. Risk: payments, auth, migrations, deploy scripts, wallets or keys: the pane decides (the headless review returns
   "pane review: high risk").
8. Size: more than `review.pane_lines` (400) changed lines, or TIER hard: the pane decides.

## Scale

- Many agents share one Claude subscription window; a usage limit holds that vendor until its reset and ORCHE re-runs
  the held units itself (`orche status` shows `paused: … usage limit until …`). Do not re-send briefs during a hold.
- For 20–30 agents the owner picks a scale preset through Main; you never raise caps yourself.
- Sustained 20–30 agents on a Claude subscription reach its usage window within hours; the lasting ways are an API
  key for headless runs (the spend caps are then real dollars) or some divisions on Codex.

## Ledger rules

`docs/LEDGER.tsv` (canonical copy under ORCHE_HOME), columns `pr sha verdict evidence verifier ts`.

- Verdicts, strongest first: `live-ui-verified` > `unit-test-verified` > `type-check-only`; failures
  `verifier-blocked`, `verifier-failed`.
- Rows are append-only. A new head SHA on a PR voids every prior row for it.
- No row for (PR, current SHA) means unverified; `orche ledger check --pr <pr> --sha <sha>` exits 2.
- CI green alone is never a verdict. Evidence is a path or URL to script output, a log or a screenshot.
- Only Build or an independent verifier writes accepted verdicts; feature agents may self-report.
  The `verifier` column never names the session that wrote the code.
- Each release-ladder step (dev → staging → canary → prod) needs a row for the deployed SHA.
- After a green merge the queue writes (PR, head) and (none, merge commit) rows with verifier
  queue; review verdicts live in ledger/<proj>.review.tsv (kinds pre and build), never in the verification ledger.

## Review in a fresh session

1. Scripted verification first: the unit's VERIFY scripts in its run, then the gate in the queue.
2. Model judges in fresh sessions: the cross-vendor pre-review and the headless Build review see only the diff, the
   brief's contract, the report and ORCHE's facts, never the writer's conversation; high-risk and large changes come
   to you.
3. Classify findings Act on / Consider / Noted / Dismissed; filter nit inflation. Act-on findings
   become a new brief revision for the writer, not a chat message.

## Verify skill

The project's verify skill lists, per feature, the exact commands and known pitfalls. Point every
brief's VERIFY at it. When a check is missing, add it as a committed script and reference it; a
check written twice in briefs belongs in the verify skill.

- The queue's gate is scripts/verify/ci.sh: the commands CI would run, in order, exit non-zero on any failure, last
  line verdict: <type-check-only|unit-test-verified|live-ui-verified> (templates/verify/ci.sh).

## Reports

REPORT.md first line `STATUS: done | release-ready | blocked | failed | stop`; then outcome,
branch, head SHA, PR, verdict + ledger row, commands actually run, deviations, follow-ups, and the
`memory-save:` line. Then file it with `orche report <status> --summary "..."` (one pointer to the brief's
sender, usually Main, who is woken by it; `--urgent` matters only for `info`). Never hand-write pointer JSON.
Only the owner can unblock: `STATUS: blocked` with one `OWNER: <exact step>` line (`--owner`; a secret:
`orche secret request <NAME>`, then `OWNER: secret <NAME>`).

`release-ready` is a claim, not a pointer status: write `STATUS: release-ready` in REPORT.md and your
final message (the Stop hook checks it against the ledger), then file it with `orche report done`.
Pointer statuses are exactly: done, failed, blocked, question, info, external-request, stop. Update STATUS.md and DECISIONS.tsv and save lessons to memory before claiming done.
