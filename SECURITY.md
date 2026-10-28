# Security policy

## Supported versions

Only the latest release on `main` is supported; older releases get no fixes.

## Reporting a vulnerability

Please report privately through GitHub: open this repository's **Security** tab and choose
**Report a vulnerability** (private vulnerability reporting / security advisories). Do not open
a public issue for a security problem.

Include:

- the ORCHE version (`orche version`) and your OS / Python version;
- the component (a hook, `orche secret`, the janitor, the installer, the Jev client, …);
- exact steps to reproduce, and what you expected versus what happened;
- the impact you see (secret exposure, data loss, privilege or scope escape);
- logs with secrets removed. Never send real secret values; describe their shape instead.

We aim to acknowledge reports within a week and to fix confirmed issues in the next release.

## What ORCHE never does

- **No network calls of its own,** except through the `orca`, `gh`, `gcloud` and `vercel` CLIs,
  `git` (fetch, pull and push for `orche update` and the merge queue), `install.sh` (which clones
  and updates ORCHE from GitHub over HTTPS), and the Jev API when you enable it (`orche jev enable`;
  off by default).
- **Never sends code or diffs to Jev.** The client replaces code blocks, diff hunks and long blobs
  with a placeholder and masks the remaining text before every request.
- **Keeps secret values out of logs, transcripts, argv and the clipboard.** Keymaster takes a
  value from a dialog that runs outside the agent, passes it to the backend CLI on stdin only, never
  prints or logs it (audit entries hold names, not values), and clears the clipboard after a paste.
  Text that ORCHE writes from agent input (report summaries, standing orders) and everything sent
  to Jev passes through one masking library (API keys, cloud and GitHub tokens, private keys, seed
  phrases, wallet keys, RPC URLs with keys, emails). `orche secret redact-transcripts` redacts
  secrets already in transcripts, after a backup.
- **Never lets agents read secrets.** A PreToolUse gate denies reads of `.env*` files, environment
  dumps and direct secret-manager access; agents use `orche secret run` and know names only.
- **Never deletes untouchables.** The janitor does not touch active, paused or onboarding units,
  `.env*`, memory directories, the registry, cards, or ledger, GRAVEYARD, DECISIONS, STATUS,
  HANDOFF and CHARTER files; dirty or live checkouts are skipped. Sessions are archived, never
  deleted, and other files go to a 30-day trash with undo.
- **Fails open, gates included.** Every hook that hits an internal error, or finds no Python 3.11+,
  lets Claude Code continue and logs the error to `ORCHE_HOME/logs/hooks.log`. The gates deny only
  on a positive match: the secret-read gate (`lib/orche/envgate.py`, `lib/orche/jevgate.py`) denies a
  read that matches a secret rule, and also denies a shell command too complex to finish checking;
  the scope gate denies an edit outside the worktree's scope; the done gate (`lib/orche/donegate.py`,
  in the Stop hook) keeps a Build or feature session running while its completion claim lacks a
  ledger row or a STATUS.md update. On an internal error each of them allows and logs rather than
  block your work, so they are guard rails, not a sandbox.

## What installing ORCHE means

- **Hooks run local code on every prompt and tool call.** The plugin registers hooks
  (`hooks/hooks.json`) on session start and end, every prompt, every Read, Bash, Grep, Edit and
  Write call, and every stop; each one runs ORCHE's Python from your checkout on your machine.
- **The merge queue runs the repository's own verify script.** Its gate (`lib/orche/queue/gate.py`)
  executes the project's verify steps in a fresh checkout of the candidate branch, with an
  environment stripped of secret variables, so only queue repositories whose scripts you trust.
