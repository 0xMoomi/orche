# ORCHE

ORCHE runs a small team of Claude Code sessions on each of your git projects. You talk to one of them, **Main**,
in plain words and in any language. Main plans and hands the work to divisions (Build, Research, R&D, GTM, Ops),
each working in its own git worktree. Build, GTM and your own divisions split larger work into workers, one per slice.

For developers on macOS who use Claude Code and want several agents on one repository without watching each one.
ORCHE is a Claude Code plugin plus an `orche` command, and runs inside Orca, the terminal app that holds the panes.

## How it works

1. You tell Main what you want. Main writes a brief and hands it to a division with `orche dispatch`.
2. The division works in its worktree; code work ends as one pull request per worker.
3. The head reviews each pull request; the merge queue runs `scripts/verify/ci.sh` on the base branch and lands it when green (team mode: on your word).
4. Main answers with one short status card. Nothing waits on you except decisions.

## Quick start

Needs macOS, Claude Code, Orca, git and Python 3.11+ (the installer checks them and stops with a hint when one is missing). Open Claude Code inside the project you want to set up, and paste:

```text
Install ORCHE on this Mac and set it up on my project. Run: curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash -s -- --yes
Then follow the "Next" lines it prints: ask me the setup questions with the choice dialog, in my language, and run the commands yourself.
Stop at the first failure and show me its text; no sudo, no workarounds, never print a key.
```

Then restart Claude Code once so the plugin loads.

**One-time macOS step.** When a project lives in Documents, Desktop, Downloads, iCloud Drive or `/Volumes`, the background heartbeat needs Full Disk Access.
The installer opens System Settings and shows the Python app: switch it on under Privacy & Security, Full Disk Access, then run `orche install --grant`.

Already installed? Inside a project, say "set up ORCHE here" to Claude Code, in any language.

## First three things to say

1. "set up ORCHE here": Main runs `orche init`, asks the setup questions and reports what it made.
2. A real request, such as "add a dark mode toggle": Main briefs the right division and reports back.
3. "what changed in <project>?": Main runs `orche status <project>` and answers from its card.

## What you get

- A `main-<project>` tab in Orca where you talk to Main. Divisions stay dormant (no pane, no cost) until work arrives.
- A setup report from Main: what was made and what you can switch on any time (also in `<project>/.orche/SETUP.md`).
- Nothing committed to your repository: the project's ORCHE files (`.orche/`, `PROJECT_CARD.md`, `AGENTS.md`) stay untracked.
- Every pull request is reviewed by its head, then the merge queue tests it on top of the base branch and merges it when green. Main hears back once per request, when everything it started has finished (a question or a decision for you reaches it at once), ends every reply with `RUNNING`, `OWNER` or `DONE`, and ORCHE reminds you after 30 minutes when a decision still waits.
- The status line in Main's pane shows what waits on you and what today cost (`waiting2 · … · $4.20 today`, or `PAUSED …`), and a notice says what finished and what it cost when a request's work ends.

## Any time

Say it to Main in your own words; Main runs the command.

| Say | What happens |
|---|---|
| "turn on Jev" | faster routing of simple requests: `orche jev setup` asks for the key |
| "cross-check PR 12 with Codex" | you install Codex once; Main runs `orche review --pr 12`: Codex and Claude read the diff at once. Nothing runs unless you ask |
| "set the token for background runs" | you run `claude setup-token`; Main stores it with `orche auth set` |
| "let the janitor clean up" | on by default (the heartbeat runs it daily); this turns it back on after `orche janitor disable` |
| "set up the merge queue for <project>" | `orche init --refresh <project>` opens one PR adding `scripts/verify/ci.sh`; merging it turns the queue on (team repository: on your word only) |
| "run up to 30 agents" | `orche hr scale 30`; on a Claude subscription ORCHE waits for the usage window to reset |
| "add a design division", "switch research to Sonnet", "set up again" | a choice dialog, then `orche hr add`, `orche hr model` or `orche init --refresh` |
| "pull requests go to develop and branches start with dev/" | `orche init --refresh <project> --base develop --branch-prefix dev` |
| "push DES-57" (team mode) | `orche queue add <unit>`: the branch is pushed, the pull request opened, the head review run |
| "merge PR 12" (team mode) | `orche queue approve <project> #12` |
| "update" | `orche update`: the newest ORCHE, reinstalled, every project's ORCHE files refreshed (never yours) |
| "what changed in <project>?" | `orche card <project>`, then `orche ask <project> "…"` |
| "how much did this cost?" | `orche metrics` |
| "check the setup", "what else can ORCHE do?" | `orche doctor`, ending with the next steps |
| "continue" | after a cut-off or /clear, Main picks the work up from `orche status <project>` |
| "enforce <class>" | Main sends Build that mistake class's row of the Enforcement table, from the weekly review of repeated mistakes |

Every action is also an `orche` command, for scripts (`orche --help`). Models, team mode and empty folders: [docs/faq.md](docs/faq.md).

## Cost

Every division is a Claude Code session on your own plan; `orche metrics` shows the spend and `orche hr scale` caps how many run at once.

## Files

| Path | What |
|---|---|
| `~/orche` | the install |
| `~/.claude/orche` | config, registry and logs |
| `<project>/.orche` | units and briefs |
| `~/Library/LaunchAgents/com.orche.reconcile.plist` | the heartbeat |

## Remove

Say "uninstall ORCHE" to Main; it runs `orche uninstall`, which restores every setting the install changed.

## Docs

- [docs/faq.md](docs/faq.md): short answers, models per division, team mode, empty folders
- [docs/spec.md](docs/spec.md): the full rules (hooks, runs, reconcile, everything internal)
- [docs/glossary.md](docs/glossary.md) · [docs/principles.md](docs/principles.md) · [docs/orca-sidebar.md](docs/orca-sidebar.md)
- [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [LICENSE](LICENSE) (MIT)

Several conventions are adapted from Cursor's **pstack** plugin (MIT, by Lauren Tan); see docs/spec.md.
