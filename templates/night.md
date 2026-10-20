# Overnight run — {{PROJECT}}

<!--
Autonomous-run contract. Duration is never a completion condition: the run ends when the finish
predicate exits 0 or the escape hatch fires. Launch runs through an Orca automation.
The Stop hook runs `.orche/night/<session>.predicate.sh` after each turn: non-zero -> keep going,
zero -> stop and report. Never weaken the predicate during the run.
The run must be identified (ORCHE_SESSION=<session>, or the member's own worktree); an unidentified
session runs no predicate.
-->

## GOAL
<one-sentence outcome>

## FINISH-PREDICATE
Script: `.orche/night/<session>.predicate.sh` (committed, executable; exits 0 only when the goal is met)
<what the script checks, e.g. the verify command passes and a ledger row exists for HEAD>

## PERMISSIONS
- Allowed: <paths, commands, branches>
- Never: Always-pause actions (force-push to a shared branch, deploy, data deletion, customer messages,
  external publication, payments), merges, out-of-scope edits.
- At 70% of the budget stop spawning new work; land only verified work.

## ESCAPE
If blocked for <N> hours (or <condition>): stop, write HANDOFF.md, send an urgent inbox pointer saying why.
