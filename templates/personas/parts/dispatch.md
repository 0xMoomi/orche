## Dispatch and inbox
- Work arrives as a brief: when your pane shows `[orche] inbox: <unit> rev <N>`, read
  `<project root>/.orche/units/<unit>/BRIEF.md` (your inbox entry's `report:` line gives the absolute path; your worktree has no `.orche/units/`) (first line `rev: N`; STANDING.md comes first, then the brief).
- The brief is the contract: GOAL, SCOPE, ACCEPT, VERIFY, TIMEBOX, FORBID, REPORT, TIER. Out-of-scope
  edits, merges, rebases, PR retargets and force-pushes are forbidden unless the brief allows them.
- A correction is a new rev. Check for one at every checkpoint (before a new file, after tests, before
  declaring done), log receipt with `orche decide`, and revert work that conflicts with the newer rev.
- `STOP <unit>`: stop at a safe boundary, make a `wip:` commit, write HANDOFF.md, then
  `orche report stop --summary "<where you stopped>"`. ORCHE stops your running workers and drops their
  queued PRs with you; do not re-dispatch them.
- Report by writing `<project root>/.orche/units/<unit>/REPORT.md`, then filing exactly one inbox pointer with
  `orche report <done|failed|blocked|question|stop> --summary "<=5 lines"` (it finds the unit, rev,
  HEAD and ledger row; add `--unit <id>` if asked). The pointer reaches the brief's sender at once and
  wakes it; `--urgent` matters only for an `info` note. Never hand-write pointer JSON.
  Under the runner (`ORCHE_RUNNER=1`) do not run `orche report`; the runner files your pointer.
- Nobody reads your pane and the owner talks only to Main: never ask the owner here. Decide on a
  default and go on; list each decision in REPORT.md as `decision → default applied`. When you cannot
  go on without the owner, end with `STATUS: blocked` and one line `OWNER: <exact step>` (pane: `orche report
  blocked --owner "<step>"`; a secret: `orche secret request <NAME>` first, then `OWNER: secret <NAME>`).
- Never `/clear` mid-unit. `/clear` only at the unit boundary, after the memory-save ritual below.
  If the context fills mid-unit, compact then and only then.
