# ORCHE FAQ

Short answers; [SPEC.md](../SPEC.md) has the rules behind each one.

## Is ORCHE a harness or an orchestrator?

An orchestration layer. Claude Code is the harness: one agent's loop, tools, hooks and context.
Orca manages repos, worktrees and terminal panes. ORCHE sits on top of both and decides who does
what, how work is handed over (`orche dispatch` briefs, inbox pointers), and when it counts as done (a
verification-ledger row). It ships as a Claude Code plugin (agents, skills, hooks) plus the
`orche` CLI; it does not replace or wrap Claude Code.

## Why `/clear` at unit boundaries instead of compacting?

Compact asks a model to summarize; what it drops is uncontrolled, it costs an extra call, and the
context keeps growing. ORCHE keeps memory in files the owner writes on purpose: the persona
(`.claude/agents/<session>.md`), `.orche/STANDING.md`, the unit's `CHARTER.md`, `STATUS.md`,
`DECISIONS.tsv` and `HANDOFF.md`, inbox pointers, and transcripts that are kept (and archived on
retire, never deleted). After `/clear` or a resume, the SessionStart hook re-injects them. The
Stop hook refuses a `STATUS: done` claim until `STATUS.md` was updated in the session.

## How do corrections work?

Tell Main, in plain words and in your language ("pages unit: don't change fonts"). Main issues
`AMEND <unit> rev N` with `SUPERSEDES rev N-1: <what>`; the hook writes `BRIEF.md` rev N (history
in `BRIEF.rev<N>.md`), files an inbox pointer and nudges the worker's pane. A report carrying an
older `brief_rev` never approves the newer revision. "Stop that" becomes `STOP <unit>`. For a
rule that should hold from now on, tell Main "from now on always …" (it runs
`orche hr correct <proj> --text "..."`, a numbered standing order).

## How do I address another project?

Put its name or alias in square brackets, e.g. "What changed in [app] lately?". Candidates are
a closed list: registered projects and their aliases (`orche init --aliases`). Main echoes
`Understood: source = <project>, output = <project>` and asks one line when it is ambiguous.
Questions go through the project cards (`orche card <proj>`) and `orche ask <proj> "…"`; work requests become
an `external-request` pointer in the other Main's inbox, which its user confirms. Do not use
`@`: in Claude Code it mentions a session, so ORCHE never uses it for projects.

## What wakes Main?

Hooks never start a Main turn on their own for reports. When Main next takes a prompt, the
UserPromptSubmit hook injects urgent pointers right away (flag `urgent`, or status `failed`,
`blocked`, `question`, `stop`, `external-request`) and holds the rest until a drain point: a
prompt starting with `[drain]`, a status question, 4 or more pending pointers, or one older than
90 minutes. A dispatch addressed to Main (for example from another project) also types an
`[orche] inbox: <unit> rev <N>` nudge into Main's pane. With Jev enabled, it may mark a pointer
urgent, never the reverse.

## Why is Ops dormant?

Ops handles monitoring, incidents, releases and backups, which only matter once real users
exist. The setup leaves Ops unchecked by default; check it in the `orche init` checklist (or add it later
with `orche init <proj> --divisions <current>,ops`). New projects start at stage `building`, and Ops
cannot be woken while a project is in that stage (`orche init --wake ops` is refused). To go live, tell Main (it asks once, then runs
`orche stage set <proj> public --yes`): it records the stage, wakes Ops and prints the Ops
checklist. "Back to building" runs `orche stage set <proj> building --yes`; Ops' files stay.

## What does the Janitor never touch?

Active, paused and onboarding units; `.env*` files; `memory/` and agent-memory directories; the
registry and cards; ledger, GRAVEYARD, DECISIONS, STATUS, HANDOFF and CHARTER files. Dirty or
live checkouts are reported as skips, not cleaned. Only state retires things (`retired`, a
GRAVEYARD row, `orche discard`); age touches only regenerable artifacts. Sessions are archived,
never deleted; files go to a 30-day trash with `orche janitor undo`. It stays a dry run until
you tell Main to turn the janitor on (it runs `orche janitor enable`).

## How does Keymaster keep secrets out of transcripts?

Agents only know secret names. An agent runs `orche secret request NAME --env dev --why "..."`, which
notifies you; tell Main to hand it over (it runs `orche secret fill`, never the value in chat) and you paste the value into a dialog that runs outside the agent, and Keymaster stores it in GCP
Secret Manager, Vercel, GitHub or the keychain, passing it on stdin, never argv, then clears the
clipboard. Processes get values via `orche secret run -- <cmd>`, injected into that process only.
A PreToolUse gate denies `.env*` reads and environment dumps; `orche secret redact-transcripts`
cleans up old leaks after a backup.

## What is Jev, and why is it off by default?

Jev is an external, fast model for typed decisions (choice, score, true/false). ORCHE can use it
to decide whether a report should interrupt Main. It is never used for code. It is off because
it sends (masked, code-stripped) text off the machine, has no published independent evaluation
or SLA, and must first pass offline validation on your own data: `orche jev eval extract`,
label in a separate session, `orche jev eval run`, `orche jev eval report`. Then tell Main
to turn Jev on (it runs `orche jev enable`). Calls time out after 1 s and fail open.

## What does `orche init` leave in my repo, and what should I commit?

Commit: `.orche/STANDING.md`, `.orche/project`, `PROJECT_CARD.md`, `docs/RUBRIC.md`,
`docs/GRAVEYARD.md`, `docs/LEDGER.tsv` (an export of the canonical ledger under
`~/.claude/orche/ledger/`), `.env.example` (names only) and `.claude/agents/<session>.md`.
Never commit `.claude/settings.local.json`; ORCHE adds it to the repository's `info/exclude`.
Briefs and reports under `.orche/units/` are excluded automatically the same way (`orche init`
adds the line), so they never keep the main checkout dirty.
Unit memory in each worktree (`.orche/div/<name>/`, `.orche/feat/<name>/`) is the unit's choice;
it is archived when the unit retires. Existing files are never overwritten.

## Why did my PR merge without me?

Every PR passes three checks before it lands. First a pre-review by a different vendor, then the head review, then the
merge queue gate: `scripts/verify/ci.sh` plus the brief's VERIFY, run on top of the current main. When all are green the
queue merges it. Set `queue.land: owner` and each PR waits for your approval instead: tell Main to merge the waiting PRs (it runs
`orche queue approve <p> --all`).
A merge deploys only if your host deploys main. ORCHE never merges with `--admin` and never force-pushes.

## How many agents can run at once?

Tell Main in your language ("up to 20 agents", "30"): it asks the scale question and runs `orche hr scale <preset>`.

| Preset | max_parallel | total | min_free_mb | feature_min_free_disk_mb | feature panes | about |
|---|---|---|---|---|---|---|
| default | 4 | 8 | 2048 | 2048 | off | — |
| 20 | 12 | 16 | 2048 | 8192 | off | $40/h |
| 30 | 20 | 24 | 3072 | 10240 | off | $60/h |

A run waits while free memory is below `min_free_mb` (RAM), and `orche feat new` refuses while free disk is below
`feature_min_free_disk_mb`. Workers run headless without panes in every preset; `orche feat wake` opens one.

With a Claude subscription (Max), many agents share one usage window and soon reach it. ORCHE then holds that vendor
until the window resets and re-runs the held work. An API key or Codex keeps going meanwhile.
