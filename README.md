# ORCHE

A small company of Claude Code sessions for each of your projects, run inside Orca: one Main you talk to in plain words, divisions that do the work in their own git worktrees, and a CLI that keeps the books.

## What it is

- You only talk to **Main**, in plain language. Main plans, decides and never codes.
- Main briefs the head of each **division** — `rnd`, `research`, `build`, `gtm`, `ops`; optionally `design`, `content` or your own — and reports the result back to you. A head (all but R&D, Research and Ops) staffs one worker per slice, each in its own worktree.
- Each division has its own model: Claude by default, Codex optional.
- Divisions run headless or in their own Orca pane.
- Nothing waits on you except decisions, and Main asks those with a choice dialog.
- Done means a ledger row, not a claim.

## Quick start

Needs macOS, Claude Code, Orca, git and Python 3.11+ (the installer checks them and stops with a hint when one is missing). Open Claude Code on your Mac, inside the project you want to set up, and paste:

```text
Install ORCHE on this Mac and set it up on my project. Run: curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash -s -- --yes
Then follow the "Next" lines it prints: ask me the setup questions with the choice dialog, in my language, and run the commands yourself.
Stop at the first failure and show me its text; no sudo, no workarounds, never print a key.
```

Then restart Claude Code once so the plugin loads.

**One-time macOS step.** The heartbeat job runs in the background and needs Full Disk Access for the Python app to read projects in Documents, Desktop, Downloads, iCloud Drive, cloud storage or `/Volumes`. When that is needed, the installer opens System Settings and shows the Python app in Finder: drag it into Privacy & Security › Full Disk Access, switch it on, then run `orche install --grant`. Once per Mac.

Already installed? Inside a project, say "orche init" to Claude Code, or "set up ORCHE here" in any language.

A new, empty folder is fine: ORCHE asks whether to create the repository there (GitHub, private by default) and which preset fits, with the recommended answers first.

## What you get

- A `main-<project>` tab in Orca where you talk to Main, in any language. Main plans and decides; divisions do the work, each in its own worktree (`div-<division>`), dormant until work arrives (no pane, no cost).
- A report from Main: "ORCHE is set up", what was made and what you can switch on any time (also in `<project>/.orche/SETUP.md`).
- Nothing committed to your repository: the project's ORCHE files (`.orche/`, `PROJECT_CARD.md`, `AGENTS.md`) stay untracked.
- A worker's PR is pre-reviewed by a second model, then its head reviews it and the merge queue tests it on top of `main` with `scripts/verify/ci.sh` and merges it when green. Main ends every reply with `RUNNING`, `OWNER` or `DONE`. Orca notifies you when Main finishes a reply; ORCHE reminds you after 30 minutes when a decision still waits.

## Any time

Say it to Main in your own words; Main runs the command.

| Say | What happens |
|---|---|
| "turn on Jev" | faster routing of simple requests: `orche jev setup` asks for the key |
| "cross-check PRs with Codex" | you install Codex once (`npm install -g @openai/codex`, `codex login`); a second vendor then pre-reviews every PR, and Codex can be a division's model (headless only) |
| "set the token for background runs" | you run `claude setup-token`; Main stores it with `orche auth set` |
| "let the janitor clean up" | on by default (`orche install` turns it on and schedules it daily); this turns it back on after `orche janitor disable` |
| "set up the merge queue for <project>" | Build's first unit adds `scripts/verify/ci.sh` from `templates/verify/ci.sh` |
| "run up to 30 agents" | `orche hr scale 30`; on a Claude subscription many agents reach the usage window and ORCHE waits for its reset |
| "add a design division", "switch research to Sonnet", "set up again" | a choice dialog, then `orche hr add`, `orche hr model` or `orche init --refresh` |
| "pull requests go to develop and branches start with dev/" | `orche init --refresh <project> --base develop --branch-prefix dev` |
| (nothing to say) | A repository that already follows a team workflow (PRs to develop, dev/des-47 branches, Entire) is read at setup; nothing to configure. |
| "update" | `orche update`: the newest ORCHE, reinstalled, every project's ORCHE files refreshed (never yours) |
| "what changed in <project>?" | `orche card <project>`, then `orche ask <project> "…"` |
| "how much did this cost?" | `orche metrics` |
| "check the setup", "what else can ORCHE do?" | `orche doctor`, ending with the next steps |
| "continue" | after a cut-off or /clear, Main picks the work up from `orche status <project>` |

Every action is also an `orche` command, for scripts (`orche --help`).

Division kinds and their recommended models:

| Kind | Model | Why |
|---|---|---|
| `rnd` | Opus 5.5 | prototypes and spikes: the strongest reasoning |
| `research` | Sonnet 5.5 | sourced summaries at half Opus's price |
| `build` | Opus 5.5 | integration, tests and the ledger: correctness first |
| `gtm` | Sonnet 5.5 | launch copy and content |
| `ops` | Sonnet 5.5 | runbooks and checklists (dormant until stage public) |
| `design` | Opus 5.5 | UI, UX and design calls |
| `content` | Sonnet 5.5 | blog posts, docs and social copy |
| your own | Opus 5.5 | an unknown charter: the strongest model |

## Files

| Path | What |
|---|---|
| `~/orche` | the install |
| `~/.claude/orche` | config, registry and logs |
| `<project>/.orche` | units and briefs |
| `~/Library/LaunchAgents/com.orche.reconcile.plist` | the heartbeat |

## More

[SPEC.md](SPEC.md) (the full rules: hooks, runs, reconcile, everything internal) · [CHANGELOG.md](CHANGELOG.md) · [GLOSSARY.md](GLOSSARY.md) · [LICENSE](LICENSE) (MIT). Several conventions are adapted from Cursor's **pstack** plugin (MIT, by Lauren Tan); see SPEC.md.
