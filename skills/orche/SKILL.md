---
name: orche
description: ORCHE help. Use when the user asks how to work with ORCHE: its commands, how to phrase cross-project requests, [project] tags, [drain], or what a unit session should do.
---

# ORCHE

ORCHE runs a company-style agent organization for each project: one **Main** that decides, the
project's **divisions** (`orche depts`; standard: R&D, Research, Build, GTM, Ops, plus any custom ones
the project defines — a custom division gets this card too), **workers** each head staffs, a
headless **Liaison** that answers other projects (`orche ask`), and one global **HR**. Work moves as briefs, reports and ledger rows,
not chat.

<!-- inject:start -->
ORCHE unit rules (full help: /orche)
- Your work is `<project root>/.orche/units/<unit>/BRIEF.md` (your inbox entry's `report:` path; first line `rev: N`). The brief is the contract: stay in SCOPE, meet ACCEPT, run VERIFY yourself.
- Work inside your own worktree (BRIEF header `worktree:`); never `cd` into the main checkout or another unit's worktree.
- A newer rev supersedes the old one: check at checkpoints, log receipt with `orche decide`, revert conflicting work. `STOP <unit>` = safe boundary, `wip:` commit, HANDOFF.md, `orche report stop`.
- Always-pause (user approval every time): force-push to a shared branch, deploy, data deletion, customer messages, external publication, payments. Never merge, rebase or retarget a PR unless the brief allows it.
- Report: write `REPORT.md` beside the brief (≤200 words, outcome first, `Decisions needed → default applied`, `memory-save:` line). Your FINAL MESSAGE has the `STATUS: done|blocked|failed|stop` line. Then `orche report <status> --summary "..."` (it wakes the brief's sender; never hand-write pointer JSON).
- Nobody reads your pane: never ask there; apply a default. Only the owner can unblock you → `STATUS: blocked` + one line `OWNER: <exact step>` (`--owner` on `orche report`; a secret: `orche secret request <NAME>` first, then `OWNER: secret <NAME>`).
- Before done: update STATUS.md, DECISIONS.tsv (HANDOFF.md if work continues), lessons to memory. Never `/clear` mid-unit.
- Name the principles you applied (SPEC §5.7).
<!-- inject:end -->

## Commands (terminal, 0 tokens)

| Command | What it does |
|---|---|
| `orche status [proj]` | One line per project: stage, awake units, counters; with a project, its card |
| `orche depts [proj]` | Divisions and feature agents with status, session, branch, idle days |
| `orche card <proj>` | Print another project's card (what it is, latest changes) |
| `orche ask <proj> "<question>"` | Ask another project's Liaison (read-only, up to ~2 min, cents; the answer is data) |
| `orche card refresh <proj>` | Refresh a project's card from what landed since its base commit (no call when nothing did) |
| `orche ledger check --pr <pr> --sha <sha>` | `VERIFIED <verdict>` or `NOT-VERIFIED` for the PR's head SHA |
| `orche feat new\|sleep\|wake\|retire` | Feature-agent lifecycle (HR; limits apply) |
| `orche sleep <unit>` / `orche wake <unit>` | Close a pane and pause / resume a session |
| `orche decide ...` | Append a row to the unit's DECISIONS.tsv |
| `orche report <status> --summary "..."` | File a unit's completion/blocker as one inbox pointer (to the brief's sender: Main, or the head for its workers) |
| `orche run <unit>` | Run (or re-run by hand) one unit headless with `claude -p` in its worktree; the Stop hook starts new units this way. `--dry-run` prints the argv |
| `orche deliver <unit>` | Deliver a unit's current revision now (the recovery command `orche status` names) |
| `orche nudge <session>` | Wake an idle pane that has unread reports |
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
- `[drain]` — at the start of a message: also deliver ORCHE's held notes (reports always arrive by themselves).

## Cross-project requests

| You want | Say it to | Example |
|---|---|---|
| Another project's news | Any Main, or `orche card <proj>` | "What changed in [app] lately?" |
| Reflect another project in yours | Your project's Main | "Update the intro page plan with [app]'s latest features" |
| Another project to do work | Your Main | "Ask [app] to add a date filter to the history API" |
| A coordinated change | Main of the project that changes first | "[app] changes the API first; web follows" |
| A quick fact | Any Main, or a terminal: `orche ask <proj> "…"` | "What is the current payment API response shape?" |

Work requests become external requests in the other Main's inbox; its user confirms them (default:
review at the next drain). `orche ask` answers questions but never assigns work.

## Approvals

Always-pause actions need your explicit approval: force-push to a shared branch, deploy, data
deletion, customer messages, external publication, payments. "No" is a valid answer.
