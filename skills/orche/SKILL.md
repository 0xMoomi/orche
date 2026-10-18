---
name: orche
description: ORCHE help. Use when the user asks how to work with ORCHE: its commands, how to phrase cross-project requests, [project] tags, [drain], or what a unit session should do.
---

# ORCHE

ORCHE runs a company-style agent organization for each project: one **Main** that decides, the
project's **divisions** (`orche depts`; standard: R&D, Research, Build, GTM, Ops, plus any custom ones
the project defines — a custom division gets this card too), **feature agents** under Build, a
**Liaison** for other projects, and one global **HR**. Work moves as briefs, reports and ledger rows,
not chat.

<!-- inject:start -->
ORCHE unit rules (full help: /orche)
- Your work is `.orche/units/<unit>/BRIEF.md` (first line `rev: N`). The brief is the contract: stay in SCOPE, meet ACCEPT, run VERIFY yourself.
- Work inside your own worktree (BRIEF header `worktree:`); never `cd` into the main checkout or another unit's worktree.
- A newer rev supersedes the old one: check at checkpoints (new file, after tests, before done), log receipt with `orche decide`, revert conflicting work. `STOP <unit>` = safe boundary, `wip:` commit, HANDOFF.md, `orche report stop`.
- Always-pause (user approval every time): force-push to a shared branch, deploy, data deletion, customer messages, external publication, payments. Never merge, rebase or retarget a PR unless the brief allows it.
- Report: details go in `.orche/units/<unit>/REPORT.md` (≤200 words, outcome first, commands actually run, deviations, `memory-save:` line). Your FINAL MESSAGE carries the `STATUS: done|blocked|failed|stop` line (the Stop gate reads it there). Then `orche report <status> --summary "..."` (one pointer; `--urgent` for failure, blocker, security, deploy impact). Never hand-write pointer JSON.
- Before claiming done: update STATUS.md and DECISIONS.tsv (HANDOFF.md if work continues) and save lessons to agent memory.
- Never `/clear` mid-unit; only at the unit boundary after the memory save.
- Name the principles you applied (SPEC §5.7) and the decision each changed.
<!-- inject:end -->

## Commands (terminal, 0 tokens)

| Command | What it does |
|---|---|
| `orche status` | One line per project: stage, awake units, counters |
| `orche depts [proj]` | Divisions and feature agents with status, session, branch, idle days |
| `orche card <proj>` | Print another project's card (what it is, latest changes) |
| `orche ledger check --pr <pr> --sha <sha>` | `VERIFIED <verdict>` or `NOT-VERIFIED` for the PR's head SHA |
| `orche feat new\|sleep\|wake\|retire` | Feature-agent lifecycle (HR; limits apply) |
| `orche sleep <unit>` / `orche wake <unit>` | Close a pane and pause / resume a session |
| `orche decide ...` | Append a row to the unit's DECISIONS.tsv |
| `orche report <status> --summary "..."` | File a unit's completion/blocker as one inbox pointer (to Main; features to Build) |
| `orche run <unit>` | Run (or re-run by hand) one unit headless with `claude -p` in its worktree; the Stop hook starts new units this way. `--dry-run` prints the argv |
| `orche stage set <proj> building\|public --yes` | Project stage (public needs user approval) |
| `orche hr correct <proj> --text "..."` | Add a numbered standing order |
| `orche metrics`, `orche janitor report` | Usage metrics; last cleanup report |

Every command takes `--json`; mutating ones take `--dry-run`.

## Talking to Main

- Main's first line is an echo-back: `Understood: source = <project>, output = <project>`. If it
  is wrong, say so in one line.
- Talk to the Main of the project whose output will change ("What changes when this is done?").
- Main answers questions with a recommendation and a default; silence means the default applies.
- To correct running work, just say it ("pages unit: don't change fonts"); Main issues a new brief
  revision. "stop that" becomes `STOP <unit>`.

## Tags

- `[project]` — name or alias in square brackets (e.g. `[shop]`) marks the source project. Main
  resolves `[project]` tags against the registry (closed list + aliases); the hook resolves only the
  dispatch `TO` field. Never use the at-sign for projects: it mentions a session.
- `[drain]` — at the start of a message: deliver every pending report to Main now and get the
  3-line status (counts · what changed · open gates).

## Cross-project requests

| You want | Say it to | Example |
|---|---|---|
| Another project's news | Any Main, or `orche card <proj>` | "What changed in [app] lately?" |
| Reflect another project in yours | Your project's Main | "Update the intro page plan with [app]'s latest features" |
| Another project to do work | Your Main | "Ask [app] to add a date filter to the history API" |
| A coordinated change | Main of the project that changes first | "[app] changes the API first; web follows" |
| A quick fact | The other project's Liaison panel | "What is the current payment API response shape?" |

Work requests become external requests in the other Main's inbox; its user confirms them (default:
review at the next drain). Liaisons answer questions but never assign work.

## Approvals

Always-pause actions need your explicit approval: force-push to a shared branch, deploy, data
deletion, customer messages, external publication, payments. "No" is a valid answer.
