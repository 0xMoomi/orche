---
name: orche-build
description: Build division rulebook. Use in the Build session when assigning feature work, integrating branches, recording ledger verdicts, running review, or opening PRs.
---

# ORCHE Build rules

Build owns integration: its worktree (`div/build`) is the integration worktree, it owns the ledger,
and it opens PRs. Feature code is written by feature agents; Build integrates and verifies.

<!-- inject:start -->
ORCHE Build rules (full rulebook: skill orche-build)
- Your worktree is the integration worktree. Feature agents write feature code; you assign, integrate, verify and open PRs.
- Ledger (`orche ledger record|check`): append-only; key (PR, head SHA); a new head SHA voids older rows; no row = unverified. CI green is never a verdict. The verifier is never the writer.
- Verify order: the committed verify skill/script first, then review in a fresh session that sees only diff + ACCEPT + VERIFY results.
- `STATUS: done` needs a ledger row for HEAD (>= type-check-only); `STATUS: release-ready` (a claim in your final message, filed with `orche report done`) needs >= unit-test-verified from an independent verifier. The Stop hook and the runner enforce it (your last STATUS line is the claim).
- Ask HR for a feature agent when >= 2 of: >= 2 weeks, independent file boundary, repeated requests.
- No direct push to main; one push per unit after local verification; `[skip ci]` for docs-only commits. Tags, releases and deploys need user approval (STANDING 5); Main approves integration. Always-pause: force-push to a shared branch, deploy, data deletion, customer messages, external publication, payments.
- Before done: STATUS.md + DECISIONS.tsv updated, lessons saved to memory; report via REPORT.md (`Decisions needed → default applied`) + `orche report <status> --summary "..."`. Nobody reads your pane: never ask the owner there; blocked on the owner → `orche report question`.
<!-- inject:end -->

## Integration worktree

- Feature agents work in child worktrees of Build (`feat/<name>`, git base `main`). You merge their
  branches into the integration branch, run the full verification, and open the PR to `main`.
- One writer per worktree: never edit inside a feature agent's worktree; send it a brief instead.
- Create a persistent feature agent (via HR, `orche feat new`) only when at least two hold: expected
  duration ≥2 weeks, an independent file boundary, repeated requests in the same area. Otherwise use
  an ephemeral exec subagent for the unit.
- Git: no direct push to `main`; feature branch → PR → you integrate; one push per unit after local
  verification; docs-only commits carry `[skip ci]`; flake retry is one new build only. Tags,
  releases and deploys need user approval.

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

## Review in a fresh session

1. Scripted verification first: the project's verify skill (`verify-<app>`, made from the ORCHE
   verify-skill template) or committed `scripts/verify/*`. A unit whose VERIFY is not a committed script is not verified.
2. Then a model judge in a **fresh session** that receives only the diff, ACCEPT and the VERIFY
   results — never the writer's conversation. Normal units: `/code-review`, `/simplify`,
   `security-review`. High-risk units (payments, auth, migrations, deploy scripts, keys) get the
   strongest available judge.
3. Classify findings Act on / Consider / Noted / Dismissed; filter nit inflation. Act-on findings
   become a new brief revision for the writer, not a chat message.

## Verify skill

The project's verify skill lists, per feature, the exact commands and known pitfalls. Point every
brief's VERIFY at it. When a check is missing, add it as a committed script and reference it; a
check written twice in briefs belongs in the verify skill.

## Reports

REPORT.md first line `STATUS: done | release-ready | blocked | failed | stop`; then outcome,
branch, head SHA, PR, verdict + ledger row, commands actually run, deviations, follow-ups, and the
`memory-save:` line. Then file it with `orche report <status> --summary "..."` (one pointer to the brief's
sender, usually Main, who is woken by it; `--urgent` matters only for `info`). Never hand-write pointer JSON.

`release-ready` is a claim, not a pointer status: write `STATUS: release-ready` in REPORT.md and your
final message (the Stop hook checks it against the ledger), then file it with `orche report done`.
Pointer statuses are exactly: done, failed, blocked, question, info, external-request, stop. Update STATUS.md and DECISIONS.tsv and save lessons to memory before claiming done.
