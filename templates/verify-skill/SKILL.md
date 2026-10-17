---
name: verify-{{APP}}
description: Launch, check, drive and capture evidence for {{APP}} so a unit can be verified end-to-end without model judgment. Use when a brief's VERIFY names this skill, or for the weekly drift check.
---

# verify-{{APP}}

Rules:

- Never edit product code.
- Cleanup never deletes Evidence.
- At most one PR per maintenance run.

This skill is handed over only after it has proven one feature end-to-end. Each feature has
its own map under `features/` (format in `features/README.md`). Record the result with
`orche ledger record --sha <head> --verdict <verdict> --evidence <path> --verifier <session>`.

## Launch

How to start the app from a clean checkout: install command, env needed (names only, never values),
start command, port, and the readiness signal to wait for.

```bash
# e.g. npm ci && npm run dev -- --port 4173
```

## Doctor

Preconditions to check before driving, each with a command and its expected output: dependencies
installed, port free or app answering, seed data present, required env names set. Stop with
`verifier-blocked` if any check fails.

## Drive

How to exercise a feature: open the feature map in `features/<feature>.md` and follow its
`## Entry` and `## Steps` exactly. Prefer committed scripts (`scripts/verify/*.sh`) over ad-hoc
commands so re-verification costs no model tokens.

## Evidence

Where outputs go and what each must show: command logs, screenshots, HTTP transcripts, saved under
`evidence/<date>-<feature>/`. The path is the ledger row's `evidence` value.

## Cleanup

Stop processes started by Launch and remove temporary data. Cleanup never touches the Evidence
directory; a run whose Cleanup removed evidence has failed.
