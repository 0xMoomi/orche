# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

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

We aim to acknowledge reports within a week and to fix confirmed issues in a 0.1.x release.

## What ORCHE never does

- **No network calls of its own,** except through the `orca`, `gh`, `gcloud` and `vercel` CLIs, and
  the Jev API when you enable it (`orche jev enable`; off by default).
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
- **Fails open, except the two security gates.** Hooks that hit an internal error let Claude Code
  continue and log the error. The secret-read gate and the scope gate deny when they match a
  pattern; on an internal error they allow and log rather than block your work.
