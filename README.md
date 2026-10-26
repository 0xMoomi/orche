# ORCHE

A small company of Claude Code sessions for each of your projects, run inside Orca: one Main you talk to in plain words, divisions that do the work in their own git worktrees, and a CLI that keeps the books.

## What it is

- You only talk to **Main**, in plain language. Main plans, decides and never codes.
- Main briefs the head of each **division** — `rnd`, `research`, `build`, `gtm`, `ops`; optionally `design`, `content` or your own — and reports the result back to you. A head (all but R&D, Research and Ops) staffs one worker per slice, each in its own worktree.
- Each division has its own model: Claude by default, Codex optional.
- Divisions run headless or in their own Orca pane.
- Nothing waits on you except decisions, and Main asks those with a choice dialog.
- Done means a ledger row, not a claim.

## Install

Needs macOS, Claude Code, Orca, git and Python 3.11+ (the installer checks them and stops with a hint when one is missing). Open Claude Code on your Mac (any folder) and paste the whole block as one message:

```text
Install ORCHE on this Mac and set it up on one of my projects. Do exactly these steps in order. Stop at the first failure and show me the error text; do not try workarounds, do not use sudo, do not store or print any API key, and do not change settings beyond what these commands change.

1. Run: curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash -s -- --yes
2. Run: export PATH="$HOME/orche/bin:$PATH" && orche doctor
   Every line must be OK or WARN. If any line says FAIL, show it to me and stop.
3. Ask me how to start the project, with choices, in the language I write in. Run: export PATH="$HOME/orche/bin:$PATH" && orche hr choices --lang ko --json
   (--lang ko if I write Korean, else --lang en.) Ask the `ask.setup` questions with the choice dialog (AskUserQuestion): each entry's `tool` is one question, asked as given, so "기본 4개 (권장)" stays first; map each picked label through the entry's `values` ("직접 고르기" → also ask `ask.pick`; "부서마다 고르기" → also ask `ask.model.<division>` for each division).
   Write my answers to a temp file outside the project as JSON: {"divisions": [...], "custom": {"<kind>": {}}, "models": {"<division>": "<model>"}} (a picked design or content goes in "custom" as {}, not in "divisions"; no "custom" when neither was picked; no "models" for "부서에 맞춰서"; "모두 …" puts that model on every division), then confirm in one line what will be made.
   Without a choice dialog, or with no answer, take the defaults: no file, no question.
4. If this Claude Code session is already inside the project to set up (a git repository), run: export PATH="$HOME/orche/bin:$PATH" && orche init --setup-file <that file>
   Otherwise ask me for the project's folder, then run: export PATH="$HOME/orche/bin:$PATH" && orche init <that folder> --setup-file <that file>
   (No file from step 3: the same command without --setup-file.)
   Allow it up to 10 minutes (set the command timeout to 600000 ms): it reads the codebase once at the end.
   If Orca does not list the repository yet, init adds it to Orca first; at the end it switches Orca to the main-<project> tab.
5. Run: export PATH="$HOME/orche/bin:$PATH" && orche status
6. Report in five lines: the version (orche version); whether the Claude Code plugin was installed or the hooks went into settings.json; the Orca pane that was created (main-<project>); that I should restart Claude Code once so the plugin loads; and the one next step: talk in plain words in the main-<project> tab that Orca now shows.
```

Then restart Claude Code once so the plugin loads.

**One-time macOS step.** The heartbeat job runs in the background and needs Full Disk Access for the Python app to read projects in Documents, Desktop, Downloads, iCloud Drive, cloud storage or `/Volumes`. When that is needed, the installer opens System Settings and shows the Python app in Finder: drag it into Privacy & Security › Full Disk Access, switch it on, then run `orche install --grant`. Once per Mac.

## Set up a project

The install prompt asks two or three choice questions, the recommendation first: which divisions ("기본 4개 (권장)" — rnd, research, build, gtm), and which models (fitted to each division, or one for all). Then `orche init <folder>` creates:

- the divisions, each a unit in its own worktree (`div-<division>`), dormant until work arrives (no pane, no cost);
- the project's ORCHE files (`.orche/`, `PROJECT_CARD.md`, `AGENTS.md`), left untracked;
- a `main-<project>` tab in Orca, where you talk.

Already installed? Inside a project, say `orche init` to Claude Code (or `orche init ~/path/to/repo` for another folder).

**Change it later** by telling Main: "디자인 부서 추가해줘", "연구 부서 모델 바꿔줘". Main asks what you left open with a choice dialog and runs the command for you. The commands, for the curious:

| Command | Does |
|---|---|
| `orche hr add <proj> <division> [--model M]` | add a division (default: the recommended model) |
| `orche hr model <proj> <division> <model>` | move a division to another model |
| `orche hr choices` | list kinds, models and the questions Main asks |

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

**Codex.** Enable it once with `npm install -g @openai/codex@latest` and `codex login`; then pick `codex` as a division's model. Codex divisions run headless only.

**Jev (optional).** Faster routing of simple requests: `orche jev setup`.

## Use it

- Talk in the `main-<project>` tab: the work you want done, or "상태 알려줘" / "점검해줘".
- Main ends every reply with one line: `RUNNING`, `OWNER` or `DONE`. Only `OWNER` (a decision for you) and `DONE` notify you.
- Main이 끊겨도 일은 파일에 있습니다: /clear 뒤에도 'orche status <project>'로 이어 갑니다.
- In a terminal: `orche status` (one line per project) and `orche doctor` (every line OK or WARN, never FAIL).
- A worker's PR is pre-reviewed by a second model (another vendor's when Codex is installed), then its head reviews and queues it; the merge queue tests it on top of `main` with `scripts/verify/ci.sh` and merges it when green; a merge deploys if your host deploys `main`.
- Say '에이전트 30개까지' to Main to run more agents at once; on a Claude subscription many agents reach the usage window and ORCHE waits for its reset.

## Update

Say "orche 업데이트해줘" to Main, or re-run the install prompt. `orche update` pulls the newest ORCHE, reinstalls it, refreshes every project's ORCHE files (never yours) and reports the version change.

## Files

| Path | What |
|---|---|
| `~/orche` | the install |
| `~/.claude/orche` | config, registry and logs |
| `<project>/.orche` | units and briefs |
| `~/Library/LaunchAgents/com.orche.reconcile.plist` | the heartbeat |

## More

[SPEC.md](SPEC.md) (the full rules: hooks, runs, reconcile, everything internal) · [CHANGELOG.md](CHANGELOG.md) · [GLOSSARY.md](GLOSSARY.md) · [LICENSE](LICENSE) (MIT). Several conventions are adapted from Cursor's **pstack** plugin (MIT, by Lauren Tan); see SPEC.md.
