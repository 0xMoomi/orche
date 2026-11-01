# Orche

Orca + orchestra: a pod of agents working as one, inside the Orca terminal app.

Orche runs a small team of Claude Code sessions on each of your git projects. You talk to one of them, **Main**, in plain words and in any language; the others plan, build, review and land the work.
For developers on macOS who want several agents on one repository without watching each one. Orche is a Claude Code plugin plus an `orche` command.

## How it works

```mermaid
flowchart TB
  you([You])
  staff(["CEO Staff<br/>one for all projects"])
  subgraph B ["Another project"]
    mainB[Main]
  end
  you -->|any language| mainA
  staff -.-|"reads every Main<br/>leaves notes"| mainA
  mainB <-->|"ask · work request"| mainA
  subgraph A ["Your project"]
    mainA[Main] <-->|"brief ⇄ report"| divs
    subgraph divs ["Divisions also talk to each other"]
      direction LR
      research[Research] <-->|"brief ⇄ report"| build[Build] --> queue["Workers → PRs → merge queue"]
    end
  end
```

- **Main**, one per project, turns what you say into briefs and hands them to divisions with `orche dispatch`. It also talks to other projects' Mains: `orche ask` gets a read-only answer without waking them, and a work request goes to that Main, which asks you before taking it on.
- **Divisions** (Build, Research, R&D, GTM, Ops, or your own) brief each other too (Research can hand Build a task), and every report goes back to whoever sent the brief. They split larger work into workers, one per slice, each in its own git worktree; workers run headless, so no pane waits on you.
- **Agents open their own pull requests.** Build reviews each one, and the merge queue runs `scripts/verify/ci.sh` on the base branch and lands it when green. In team mode (a repository that declares its team plugin) push, pull request and merge wait for your word; in an issue-tracked project (Linear) each worker's name carries the issue key and its PR says `Fixes DES-57`.
- **One wake per finished batch.** Main hears back once, when everything a request started has ended; a question or a decision for you reaches it at once.
- **The CEO Staff**, one window for all your projects, opens by default: it knows what you said to every Main and what each answered, answers everyday questions, changes nothing, and "tell Main: …" leaves that Main a note.
- **The heartbeat**, a LaunchAgent pass every 5 minutes that makes no model call itself, reopens closed panes (also a Main that Orca reopened without Orche), re-delivers stuck briefs and keeps the merge queue moving.

## Quick start

Needs macOS, Claude Code, Orca, git and Python 3.11+ (the installer checks them and stops with a hint when one is missing). Open Claude Code inside the project you want to set up, and paste:

```text
Install Orche on this Mac and set it up on my project. Run: curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash -s -- --yes
Then follow the "Next" lines it prints: ask me the setup questions with the choice dialog, in my language, and run the commands yourself.
Stop at the first failure and show me its text; no sudo, no workarounds, never print a key.
```

Then restart Claude Code once so the plugin loads. Already installed? Inside a project, say "set up Orche here", in any language.

**One-time macOS step.** When a project lives in Documents, Desktop, Downloads, iCloud Drive or `/Volumes`, the heartbeat needs Full Disk Access. The installer opens System Settings at the Python app; switch it on under Privacy & Security, Full Disk Access, then run `orche install --grant`.

## What you get

- A `main-<project>` tab in Orca with the Staff tab beside it. Divisions stay dormant (no pane, no cost) until work arrives.
- A setup report from Main: what was made and what you can switch on any time (also in `<project>/.orche/SETUP.md`).
- Nothing committed to your repository: the project's Orche files (`.orche/`, `PROJECT_CARD.md`, `AGENTS.md`) stay untracked.
- A status line in Main's pane, in your language (English or Korean) and in colour: what waits on you and what runs first, then each division's running/assigned workers and today's spend (`⚠ waiting 2 · ▶ running 3 · BUILD 3/8 · … · $4.20 today`).
- Notifications: Orca shows one when Main ends a reply, Orche reminds you after 30 minutes when a decision still waits, and one notice says what finished and what it cost when a request's work ends. Missed one? Main's workspace stays marked unread in Orca's sidebar after every turn Orche started there, until you open it.
- Files: the install in `~/orche`, config, registry and logs in `~/.claude/orche`, units and briefs in `<project>/.orche`, the heartbeat in `~/Library/LaunchAgents/com.orche.reconcile.plist`.
- Spend per day: every division is a Claude Code session on your own plan; `orche status` shows each project's spend today, `orche metrics` the detail.

## Any time

Say it to Main in your own words; Main runs the command.

| Say | What happens |
|---|---|
| "turn on Jev" | faster routing of simple requests: `orche jev setup` asks for the key |
| "cross-check PR 12 with Codex" | `orche review --pr 12 --codex`: Codex and Claude read the diff at once; without `--codex` a review is Claude only |
| "set the token for background runs" | you run `claude setup-token`; Main stores it with `orche auth set` |
| "let the janitor clean up" | on by default (the heartbeat runs it daily); this turns it back on after `orche janitor disable` |
| "set up the merge queue for <project>" | `orche init --refresh <project>` opens one PR adding `scripts/verify/ci.sh`; merging it turns the queue on |
| "run up to 30 agents" | `orche hr scale 30`; on a Claude subscription Orche waits for the usage window to reset |
| "add a design division", "switch research to Sonnet", "set up again" | a choice dialog, then `orche hr add`, `orche hr model` or `orche init --refresh` |
| "push DES-57", "merge PR 12" (team mode) | `orche queue add <unit>` pushes and opens the PR; `orche queue approve <project> #12` lands it |
| "update" | `orche update`: the newest Orche, reinstalled, every project's Orche files refreshed (never yours) |
| "what changed in <project>?" | `orche card <project>`, then `orche ask <project> "…"` |
| "how much did this cost?" | `orche metrics` |
| "close the staff window", "open the staff window" | `orche sleep staff` keeps it closed until you ask again; `orche wake staff` opens it |
| "check the setup", "what else can Orche do?" | `orche doctor`, ending with the next steps |
| "continue" | after a cut-off or /clear, Main picks the work up from `orche status <project>` |
| "uninstall Orche" | `orche uninstall`, after one confirmation: every setting the install changed is restored |

Every action is also an `orche` command, for scripts (`orche --help`). Models, team mode and empty folders: [docs/faq.md](docs/faq.md).

## Docs

- [docs/faq.md](docs/faq.md): short answers, models per division, team mode, empty folders
- [docs/spec.md](docs/spec.md): the full rules (hooks, runs, reconcile, everything internal)
- [docs/glossary.md](docs/glossary.md) · [docs/principles.md](docs/principles.md) · [docs/orca-sidebar.md](docs/orca-sidebar.md) · [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [LICENSE](LICENSE) (MIT)

Several conventions are adapted from Cursor's **pstack** plugin (MIT, by Lauren Tan); see docs/spec.md.
