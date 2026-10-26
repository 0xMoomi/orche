The queue could not merge your branch onto origin/{{BASE}}. Conflicting files: {{FILES}}.
This revision allows one extra git step: `git fetch origin && git merge origin/{{BASE}}` in your worktree (never
rebase, never force-push). Resolve the conflicts inside your SCOPE, run VERIFY, record the ledger row for the new HEAD
and end with STATUS: done; ORCHE publishes and reviews again. A conflict outside your SCOPE: end STATUS: blocked and
name the files.
