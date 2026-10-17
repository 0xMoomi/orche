# Changelog

All notable changes to ORCHE are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic versioning.

## 0.1.0 — 2026-10-17

First public release.

### Added

- **CLI and registry.** `orche` command with auto-discovered subcommands, global `--json` and
  `--dry-run`, and fixed exit codes (0 ok, 1 user error, 2 not found / unverified, 3 blocked by
  policy). One registry (`~/.claude/orche/registry.json`) holds projects, aliases, divisions,
  feature agents, limits and an append-only event log; every write is atomic and schema-checked.
- **Sleep, wake and visibility.** `orche sleep` / `orche wake` close a unit's pane and resume its
  session later. `orche depts`, `orche status`, `orche projects` and `orche card` answer status
  questions without a model. A status-line segment shows divisions, awake feature agents, new
  agents, pending secret requests and unverified completions.
- **Hooks and the dispatch grammar.** Main assigns work with fenced `dispatch` blocks (`TO GOAL
  SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER`, optional `ROLLBACK`; `AMEND <unit> rev
  N` and `STOP <unit>`). The Stop hook relays them as versioned briefs and gates completion
  claims; the UserPromptSubmit hook delivers inbox pointers at drain points; SessionStart
  re-injects standing orders and unit memory; two PreToolUse gates block secret reads and
  out-of-scope edits. `orche report` files a completion or blocker as one inbox pointer.
- **Verification ledger, decision log and verify skill.** `orche ledger record/check/summary/
  export/grade` keyed by PR and commit; writer and verifier must differ. `orche decide` appends
  to a unit's `DECISIONS.tsv`. A `verify-<app>` skill template turns verification into committed
  scripts.
- **Keymaster.** `orche secret request/wait/fill/add/ls/missing/rotate/run/render/share/revoke/
  redact-transcripts` and `orche dev add/remove`: agents know secret names only; values go from
  a dialog straight to GCP Secret Manager, Vercel, GitHub or the macOS keychain and reach a
  process only through `orche secret run`.
- **Janitor.** `orche janitor` and `orche discard`: state-based cleanup that archives sessions
  (never deletes them), moves files to a 30-day trash with undo/restore, writes a GRAVEYARD row
  before anything moves, and stays in dry-run until enabled. Optional daily schedule.
- **HR, onboarding and lifecycle.** `orche init` registers an Orca repo and onboards its five
  divisions; `orche feat new/sleep/wake/retire` manages feature agents with daily/weekly creation
  limits, a global freeze, scope-overlap checks and an archive on retire. `orche hr audit`
  proposes sleeps; `orche hr correct` adds numbered standing orders. `orche stage set` moves a
  project between `building` and `public` and wakes Ops.
- **Agents, skills, personas and templates.** Plugin agents (exec, runner, deep-reasoner, hr,
  liaison), the `orche`, `orche-main` and `orche-build` skills, persona templates for every role,
  and project templates (STANDING.md, PROJECT_CARD.md, RUBRIC.md, GRAVEYARD.md, unit memory files,
  CI workflow, overnight contract, large-job workflow).
- **Jev gate.** Optional typed-decision client for drain urgency (it can add urgency, never
  remove it), off by default, with a 1 s fail-open timeout, code stripping and secret masking
  before anything is sent; `orche jev eval` validates it offline first.
- **Installer.** `orche install`, `orche uninstall`, `orche doctor` and `orche apply-all`:
  patch user settings, the status line and agent links with backups and recorded originals, check
  the installation, and onboard every Orca repo at once.
- **Documentation.** README, SPEC, principles, FAQ, Orca sidebar guide, CONTRIBUTING and SECURITY.

### Known limitations

- Unfenced code in a pointer or prompt can still reach Jev; code stripping is a heuristic.
- The Keymaster Vercel backend is MVP-grade: `rotate` removes the old value before adding the new one.
- The janitor has no run lock; do not start two `orche janitor run` passes at once.
- The env-read gate is best-effort: backtick forms, `bash -c env` and `os.environ` dumps are not caught.
- Sessions are identified only via `ORCHE_SESSION`, their unit worktree, or an exactly matching Orca pane title; other panes in a main checkout are left alone.
- A child `claude -p` started from an ORCHE pane inherits `ORCHE_SESSION` and is treated as that session.
- SessionEnd on `/clear` moves the session's scratch to the janitor trash (recoverable with `orche janitor undo`).
- On Python < 3.11 the env-read gate logs a guard line on every call.
- Per-secret GCP IAM is not implemented: `orche dev add` refuses when one GCP project holds every env.
- A relay that fails part-way can leave an empty reserved unit directory; an AMEND with the wrong rev becomes max+1.
- `feat new` can leave an orphan Orca worktree after a partial failure; re-running it resumes the entry.
- The janitor ages scratch by mtime only and records trash purges in the global GRAVEYARD only.
- `orche ledger check --pr N` without `--sha` checks the cwd HEAD, not the PR head.
- The UserPromptSubmit hook marks stalled workers (`STALLED`) but does not nudge them.
- Dotenv values containing `$` are written verbatim; frameworks that expand variables may change them.
