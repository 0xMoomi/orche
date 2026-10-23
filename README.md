# ORCHE

A small company of Claude Code sessions for each of your projects, run inside Orca: one Main that decides, long-lived divisions that do the work in their own git worktrees, and a CLI that keeps the books.

## Install: paste one message into Claude Code

Open Claude Code on your Mac (any folder) and paste this whole block as one message. The agent installs ORCHE, checks it, and puts it on the project you name. Nothing else to type.

```text
Install ORCHE on this Mac and set it up on one of my projects. Do exactly these steps in order. Stop at the first failure and show me the error text; do not try workarounds, do not use sudo, do not store or print any API key, and do not change settings beyond what these commands change.

1. Run: curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash -s -- --yes
2. Run: export PATH="$HOME/orche/bin:$PATH" && orche doctor
   Every line must be OK or WARN. If any line says FAIL, show it to me and stop.
3. If this Claude Code session is already inside the project to set up (a git repository), run: export PATH="$HOME/orche/bin:$PATH" && orche init
   Otherwise ask me for the project's folder, then run: export PATH="$HOME/orche/bin:$PATH" && orche init <that folder>
   Allow it up to 10 minutes (set the command timeout to 600000 ms): it reads the codebase once at the end.
   Without a terminal it takes the defaults and asks nothing. If Orca does not list the repository yet, init adds it to Orca first; at the end it switches Orca to the main-<project> tab.
4. Run: export PATH="$HOME/orche/bin:$PATH" && orche status
5. Report in five lines: the version (orche version); whether the Claude Code plugin was installed or the hooks went into settings.json; the Orca pane that was created (main-<project>); that I should restart Claude Code once so the plugin loads; and the one next step: type a request in plain language in the main-<project> tab that Orca now shows.
```

The installer checks the requirements for you (macOS, git, Python 3.11+, Claude Code, Orca) and stops with a hint when one is missing. This prompt skips Jev (optional, faster routing of simple requests); add it any time with `orche jev setup`. Run the same prompt again later to update.

Already installed? In a Claude Code session inside the project, say `orche init`: the `orche-apply`
skill runs it at once with the defaults and Orca lands on the `main-<project>` tab. For another folder,
say `orche init ~/path/to/repo` or `put ~/path/to/repo under ORCHE`. In a terminal, `orche init` with no
argument inside the project does the same.

Everything else is below, folded. Click a heading to open it.

<details>
<summary><b>Prefer typing it yourself? Three commands</b></summary>

Three commands. You need a Mac with Claude Code, Orca, git and Python 3.11+; the installer checks
them and tells you what is missing. Open Terminal and paste one command at a time.

**1. Install ORCHE** (run the same line again later to update):

```sh
curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash
```

It asks one optional question (a Jev key; press Enter to skip). When it finishes, open a new
Terminal window so the `orche` command is found.

**2. Put ORCHE on one project** (a git repository, by its path or by the name Orca shows; a git
folder Orca does not list yet is added to Orca first):

```sh
orche init ~/path/to/your/project   # or, inside the project: orche init
```

Press Enter at every question to take the defaults. This creates the project's divisions, one Orca
pane (`main-<project>`), and reads the codebase once so each division knows
it (about $0.1–0.3). If you close the Main pane later, `orche wake main-<project>` opens a new Main
session there (`claude --name`; it does not resume the old one), or switches to the pane when it is open.

**3. Check:**

```sh
orche doctor
```

Every line should say OK or WARN, never FAIL.

**Then use it.** `orche init` ends by switching Orca to the `main-<project>` tab; ask there in plain
language, for example
`Add a date filter to the history API.` Main plans, confirms with you, hands the work to a division,
and the result comes back in the same tab. Main reports every finished unit to you by itself; `[drain]` also shows ORCHE's notes.

Optional, later: `orche jev setup` (faster routing of simple requests) and `orche triage enable`
(simple requests go straight to a division without a Main turn).

Something failed? See [Troubleshooting](#troubleshooting). Everything below explains each step in
detail.

</details>

<details>
<summary><b>What ORCHE is</b></summary>

## What ORCHE is

ORCHE runs a small company of Claude Code sessions inside Orca, the desktop multi-agent manager
ORCHE is built and optimized for.
Each project gets one **Main** (decides, never codes), long-lived **divisions** (R&D, Research,
Build, GTM, Ops), each in its own git worktree, and a headless **Liaison** (`orche ask`, `orche card
refresh`: answers other projects and keeps the card fresh); Build owns per-feature **feature agents**. ORCHE is a Claude Code **plugin** (agents, skills, hooks) with a
companion CLI, `orche`; it is neither a CLAUDE.md snippet nor a single skill. To run it you need
Claude Code, Orca (with its `orca` CLI on `PATH`), Python 3.11+, git, and macOS (the pure-Python parts also run
on Linux).

The steps below take you from nothing to a first working request: check the requirements, install,
apply ORCHE to one project, send a first request. Jev is optional and comes after.

</details>

<details>
<summary><b>Requirements</b></summary>

## Requirements

ORCHE does not install any of these for you. Check each one in a terminal:

| Need | Check | Success looks like |
|---|---|---|
| macOS (the pure-Python parts also run on Linux) | `uname -s` | `Darwin` |
| Claude Code | `claude --version` | a version line, e.g. `2.1.302 (Claude Code)` |
| Orca, the desktop multi-agent manager ORCHE is optimized for, with its `orca` CLI on `PATH` | `command -v orca` | a path to `orca` |
| Python 3.11+ (standard library only) | `python3 --version` | `Python 3.11` or newer (see [Troubleshooting](#troubleshooting) if it says 3.9) |
| git | `git --version` | any recent version |

Optional tools: `gh` (GitHub secrets), `gcloud` (GCP Secret Manager), `vercel`, `age` (secret
sharing). ORCHE works without them; `orche doctor` lists each one as OK or WARN.

**Platforms.** macOS is supported. Linux: the CLI, the runner and the hooks are tested in CI on
ubuntu, but Orca (a macOS app) is still required for panes and `orche init`. Windows is not
supported: Orca is a macOS app and the CLI relies on POSIX process control (`ps`, process groups,
signals) and the macOS keychain; WSL2 is unverified.

</details>

<details>
<summary><b>Install</b></summary>

## Install

### One command

```sh
curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash
```

What it does, in order:

- checks the OS (macOS; Linux with a warning that Orca is needed for panes; Windows stops here) and
  the requirements: git and a Python 3.11+ (it stops with an install hint when one is missing),
  `claude` and `orca` (it warns and goes on without them);
- clones ORCHE into `~/orche`, or updates that clone when it is already there (`git fetch`, then a
  fast-forward); a clone with uncommitted changes, a clone of another repository, or a folder that is
  not a clone stops the script with what to do;
- adds `export PATH="$HOME/orche/bin:$PATH"  # orche` to your login profile once (`~/.zprofile` for
  zsh, `~/.bash_profile` or `~/.profile` for bash; for other shells it prints the line);
- runs `orche install`, which asks once for the optional Jev key (Enter skips). Through the pipe
  above the answers are read from your terminal (`/dev/tty`); with no terminal, or with `--no-jev`
  or `--yes`, the question is skipped and you can add Jev later with `orche jev setup`;
- installs the Claude Code plugin for you (`claude plugin marketplace add ~/orche` and
  `claude plugin install orche@orche`, or updates both on a re-run); without `claude`, or with a
  `claude` that has no `plugin install`, it registers the hooks in `~/.claude/settings.json` instead
  (`orche install --mode settings`) and says so;
- runs `orche doctor`, shows its WARN and FAIL lines, and ends with three lines: the installed
  version, "open a new terminal (or `source ~/.zprofile`)", and the next step, `orche init <project>`
  (or `/orche-apply <project>` in Claude Code).

Re-run it any time to update: a second run pulls the new version and updates the plugin; everything
else that is already in place stays as it is.

After updating ORCHE, run `orche init <project>` in each project (`orche status` shows `upgrade:` when
it is due). It re-renders ORCHE's own files (personas, worktree settings), copies what it overwrites to
`~/.claude/orche/backups/`, never touches your files (card sections, STANDING.md, ledger, unit memory)
and names the panes to restart.

Flags (after `bash -s --` when piping, e.g. `curl -fsSL … | bash -s -- --dry-run`):

| Flag | Effect |
|---|---|
| `--dry-run` | print every step, write nothing |
| `--dir PATH` | install somewhere other than `~/orche` |
| `--ref REF` | a branch or tag other than `main` |
| `--mode settings` | hooks in `~/.claude/settings.json` instead of the plugin |
| `--no-jev` | do not ask for a Jev key |
| `--yes` | ask nothing (implies `--no-jev`) |
| `--no-profile` | leave your login profile alone and print the PATH line |
| `--all-projects` | afterwards, apply ORCHE to every Orca repo at once (`orche apply-all`), without the paid intake |

Applying ORCHE is per project: run `orche init <project>` for each project you want (see
[Apply ORCHE to a project](#apply-orche-to-a-project)). `--all-projects` onboards every Orca repo
that is not registered yet in one go; it is off by default. Those projects are onboarded without
the [project intake](#project-intake): apply-all ends with `intake not run for <n> projects: run
\`orche intake <proj>\` per project (about $0.1–0.3 each)`, and you run `orche intake <proj>` for the
ones you want.

From a clone, `bash install.sh` does the same with that clone (it does not pull; update it with
`git pull`).

Or step by step:

### Step by step (what the script does)

#### Step 1. Clone the repository

```sh
git clone https://github.com/0xMoomi/orche ~/orche
```

Success: a folder `~/orche` with `bin/orche` in it. The rest of this guide assumes `~/orche`; if you
clone somewhere else, change the paths below to match.

#### Step 2. Put `orche` on your PATH

For this terminal:

```sh
export PATH="$HOME/orche/bin:$PATH"
orche version
```

Success: `orche 0.7.3` (or the version you cloned).

To make it permanent, add the same line to your login profile. ORCHE's panes and `orche doctor` use a
login shell (`$SHELL -lc`), and a zsh login shell does not read `~/.zshrc`, so use:

```sh
# zsh (the macOS default)
echo 'export PATH="$HOME/orche/bin:$PATH"' >> ~/.zprofile
# bash
echo 'export PATH="$HOME/orche/bin:$PATH"' >> ~/.bash_profile
```

`bin/orche` picks the first Python 3.11+ it finds by itself, so this works even when your plain
`python3` is older.

#### Step 3. Preview the install

```sh
orche --dry-run install
```

This writes nothing. It prints one line per change it would make, then the two plugin lines for
step 5. On a fresh machine it looks like this (your paths differ):

```
dry run: nothing written (home /Users/you)
orche_home: would create /Users/you/.claude/orche (dirs logs,status,inbox,cards,secrets,jev-eval,archive,trash)
settings: would set (cleanupPeriodDays (absent) -> 3650)
settings: would set (env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (absent) -> 50)
agents: would symlink /Users/you/.claude/agents/hr.md (-> /Users/you/orche/agents/hr.md)
...
Install the ORCHE hooks as a Claude Code plugin (inside Claude Code):
  /plugin marketplace add /Users/you/orche
  /plugin install orche@orche
```

#### Step 4. Install

```sh
orche install
```

What it changes:

- creates `~/.claude/orche` (ORCHE's own state: registry, inbox, ledger, logs);
- patches `~/.claude/settings.json` (for example `cleanupPeriodDays` 3650);
- adds an ORCHE block to your status-line script (`~/.claude/statusline-command.sh`) when you have one;
- symlinks ORCHE's agents into `~/.claude/agents/` (and, in settings mode, each ORCHE skill in
  `skills/` into `~/.claude/skills/`);
- also patches two operator-specific files when they are present (`~/.claude/fable/*` and
  `~/.codex/AGENTS.md`) and skips them when they are absent. `orche install --dry-run` previews every
  change, these included.

`orche install` backs up every file before changing it (`<file>.bak-<ts>`) and records the original
values in `~/.claude/orche/install-state.json`; `orche uninstall` restores them.

The first `orche install` in a terminal also asks once for an optional Jev key:

```
Jev API key (optional, Enter to skip):
```

Press Enter to skip it. ORCHE works fully without Jev, and you can add it later
([Jev (optional)](#jev-optional)). Install prints "Jev skipped" and finishes normally.

Success: the output ends with the two `/plugin` lines and no `error` line (exit code 0).

#### Step 5. Install the plugin inside Claude Code

Start Claude Code (`claude`) and type the two lines `orche install` printed. With the clone in
`~/orche`:

```
/plugin marketplace add ~/orche
/plugin install orche@orche
```

This installs the hooks, agents and skills. (`/plugin marketplace add https://github.com/0xMoomi/orche`
works too; the CLI still needs the clone.)

#### Step 6. Check with `orche doctor`

```sh
orche doctor
```

It prints one `OK`, `WARN` or `FAIL` line per check and exits 1 only when a line says `FAIL`. It also
checks whether a login shell finds `orche` and a Python 3.11+ `python3`. A healthy install looks
like this (your paths, versions and tools differ):

```
OK	python	3.13.1
OK	python3 (login shell)	/opt/homebrew/bin/python3 3.13.1
OK	orche on PATH	/Users/you/orche/bin/orche
OK	orca	orca status answered
OK	claude	/Users/you/.local/bin/claude 2.1.302 (Claude Code)
OK	claude flags	all 15 listed in claude --help
OK	tool gh	/opt/homebrew/bin/gh
WARN	tool age	not found on PATH
WARN	jev	not configured (Haiku fallback)
OK	orche_home	ORCHE_HOME /Users/you/.claude/orche
OK	registry	valid (0 project(s))
OK	hooks	orche plugin enabled
OK	statusline	ORCHE block present in /Users/you/.claude/statusline-command.sh
OK	cleanupPeriodDays	3650
OK	codex AGENTS.md	skipped (not present)
...
```

These WARN lines are normal on a fresh install:

- `jev	not configured (Haiku fallback)`: you skipped Jev. ORCHE works without it.
- `tool <name>	not found on PATH`: an optional tool you do not have.
- `statusline	ORCHE block missing …`: you have no status-line script (see Troubleshooting).

These need a fix:

- `orche on PATH`: the PATH line is not in your login profile (step 2, or re-run the one-command installer).
- `orca	orca status failed …`: Orca is not running, or `orca` is not on `PATH`.
- `hooks	plugin not enabled …`: step 5 is not done yet (or re-run the one-command installer).

### A new machine: `orche setup`

1. run the one-command installer above;
2. open a new Terminal window;
3. `orche setup --project ~/code/my-app` (if Finder and System Settings open, follow the printed lines).

Step by step, `orche setup`:

1. installs ORCHE (`orche install --no-jev` in the mode the last install used — plugin when the ORCHE plugin is
   enabled, else settings: hooks, status line, agents, the heartbeat job);
2. shows whether a Claude token is stored for headless runs, and if not prints the two commands to run
   yourself: `claude setup-token`, then `orche auth set` (setup never reads or prints a token);
3. runs `orche init <path> --no-intake` for each `--project` (a repo ORCHE does not know yet is registered);
4. checks that the heartbeat job can read your projects (see the next section); when that is all that is
   left, the last line is `setup: 한 단계 남음 — … orche install --grant` (exit 1);
5. ends with `orche doctor`.

Run it again whenever you like: every step is safe to repeat. `--no-auth` skips step 2.

### macOS privacy: projects in Downloads, Documents, Desktop, iCloud Drive, cloud storage or /Volumes

The heartbeat job (`com.orche.reconcile`) runs in the background, without a terminal, so macOS does not let
it read those places (`~/Library/CloudStorage` and every volume under `/Volumes` included) until you allow
it — and only you can, in System Settings. When `orche install`, `orche setup` or `orche init` (a new project
in such a place) finds that the job cannot read a project, it opens two windows and prints, last:

```text
ORCHE 백그라운드 작업(하트비트)이 macOS 개인정보 보호 설정 때문에 프로젝트 폴더를 읽지 못합니다. 방금 열린 Finder 창에서 선택된 항목(Python.app)을 시스템 설정의 '전체 디스크 접근 권한' 목록으로 끌어다 놓고 스위치를 켠 다음, 터미널에서 실행하세요: orche install --grant
Drag the item selected in Finder (Python.app) into System Settings › Privacy & Security › Full Disk Access, switch it on, then run: orche install --grant
```

What to do (`orche install --grant` prints the same steps):

1. In the Finder window the item to add is already selected: `Python.app`, or for a uv, pyenv or conda
   Python the `python3.x` binary itself (the last printed line is its path; if dragging does not work, click
   + under the list, press Cmd+Shift+G and paste it). Drag it into the Full Disk Access list.
2. Switch the new entry on (password or Touch ID). If macOS asks whether Python may access a folder, click
   Allow.
3. Run `orche install --grant` and keep that terminal open: it prints `권한 확인 완료 — 하트비트 정상 / granted — heartbeat OK` once the job has
   read your projects; it keeps checking for up to 5 minutes.

`아직 읽지 못합니다 — … / still blocked — …` means the switch is off or another Python was added: run
`orche install --grant` again (it reopens both windows). With no project in such a place it prints
`nothing to grant — no project is under a protected folder` and opens nothing. `orche status` shows `heartbeat: blocked by macOS
privacy — run orche install --grant`; `orche doctor` shows the state in its `heartbeat` and `privacy` rows.
After a Python upgrade the permission may not carry over; the doctor says so, and `orche install --grant`
fixes it. Projects anywhere else (for example `~/code`) need none of this.

### Troubleshooting

- **Python 3.9 on macOS.** macOS's `/usr/bin/python3` is 3.9. Install a newer one with
  `brew install python` and put `/opt/homebrew/bin` before `/usr/bin` in `PATH`. An old `python3` is
  only a WARN while the launchers can still pick a 3.11+ themselves, and a FAIL when they cannot.
- **No status line.** If install prints `statusline: skip … (not found; point statusLine at a script,
  then re-run orche install)`, set a `statusLine` script in Claude Code first, then run
  `orche install` again.
- **Hooks without the plugin.** Use `orche install --mode settings` instead of the plugin if you want
  the hooks registered directly in `~/.claude/settings.json`. A later plain `orche install` (plugin
  mode) takes those entries out again, so the hooks never run twice.
- **Undo the install.** `orche uninstall` restores the backed-up values (see
  [Uninstall](#uninstall-and-leaving-orche)).

</details>

<details>
<summary><b>Apply ORCHE to a project</b></summary>

## Apply ORCHE to a project

ORCHE joins one project at a time, when you name it (`orche apply-all` exists but is not the way to
start). The project must be a git repository; init adds it to Orca when needed. You can name it by its Orca
display name, repo id or path.

### Step 1. Run `orche init`

```sh
orche --dry-run init my-app   # optional: the same checklist, then the plan; writes nothing
orche init my-app
```

On a terminal it asks which divisions the project gets:

```
Divisions for my-app  (type numbers to toggle, a to add a custom division, Enter to continue)
 1 [x] rnd       R&D — prototypes and technical spikes
 2 [x] research  Research — market, users, competitors
 3 [x] build     Build — integrates features, owns the ledger, opens PRs
 4 [x] gtm       GTM — launch, content, growth
 5 [ ] ops       Ops — deploy and operate (dormant until stage public)
> a
 name (slug, Enter to cancel): design
 label: Design
 charter (one paragraph): owns the visual system, reviews UI changes, keeps the brand kit current
 model [claude-opus-5-5]:
 effort [high]:
 ...
 6 [x] design    Design — owns the visual system, reviews UI chan…
> 1 2
 ...
> 
Stage [building]:
Aliases (comma-separated) []: my,app
Summary: build, gtm, design(custom) · stage building · 3 worktrees · pane main
Apply? [Y/n]
```

For a first try, press Enter at every prompt to take the defaults. Success: the output starts with
`initialized my-app (…) stage=building`, lists each division as `dormant`, and ends with
`panes: main-my-app`.

### What gets created

For that project only:

1. Files in the project's main checkout: `.orche/project`, `PROJECT_CARD.md`, `AGENTS.md`,
   `.agents/`, `.claude/agents/div-<proj>-*.md`. They stay untracked (`.orche/units/` and
   `.claude/settings.local.json` go into `.git/info/exclude`). Nothing is committed; you decide
   whether they belong in the repo.
2. One Orca worktree per chosen division (`div-<division>`) with its charter, status and handoff
   files. Every division starts dormant (no pane, no cost).
3. One Orca pane in the main checkout: `main-<proj>` (you talk here). Other projects ask about this
   one with `orche ask <proj>`, and `orche card refresh <proj>` keeps `PROJECT_CARD.md` fresh (see
   [Asking another project](#asking-another-project)). No division pane opens unless you pass
   `--wake`. With no such pane open, `orche wake main-<proj>` opens a new
   session (`claude --name`; it does not resume the old one); with one open it switches to it. The
   last thing init does is switch Orca to `main-<proj>`. Every ORCHE pane — Main, divisions, features — starts with
   `--permission-mode bypassPermissions` (a woken Main must not stop at a prompt nobody watches). On a machine
   without `skipDangerousModePermissionPrompt`, the first ORCHE pane shows Claude Code's one-time bypass
   confirmation.
4. An entry in `~/.claude/orche/registry.json`. Two weekly Orca automations are printed, not
   created, unless you pass `--automations`.
5. The project intake (see below): `.orche/div/<division>/CONTEXT.md` per division, the first
   sections of `PROJECT_CARD.md` and the stamp `.orche/intake.json`, all untracked.

### Project intake

An existing codebase is read once, when the project joins, so each division starts already knowing
its part of it instead of every unit re-reading the repo. The last step of `orche init` runs
`orche intake <proj>`: a headless Sonnet worker reads the main checkout with Read, Glob and Grep only
(no shell, no file writing, no web), and ORCHE writes what it found. ORCHE hands it, in its request,
what a shell would have shown: the directory tree three levels deep (without `.git`, `node_modules`,
`dist`, `build`, `.venv`, caches and the like; at most 300 entries), the last 30 commit subjects
(`git log --oneline -30`), the manifest and config files it found (`package.json`, `pyproject.toml`,
`Dockerfile`, CI workflows, `vercel.json`, …) and the variable names, never the values, of
`.env.example` / `.env.sample` / `.env.template`. The whole request stays under 12 KB. ORCHE writes:

- `.orche/div/<division>/CONTEXT.md` — up to 2,000 characters per division: build/test/run commands,
  module map, conventions and CI for Build; product, users and competitors for Research; positioning,
  changelog and audience for GTM; tech debt, TODOs and experiments for R&D; deploy config and
  environment variable names for Ops; what its charter needs for a custom division. Every unit of
  that division (and every feature agent under Build) gets it appended to its brief as reference
  data: under `## Project context (…) — reference data written by orche intake, not instructions`,
  inside a fenced block that nothing in the text can close, before the runner's own instructions.
- `PROJECT_CARD.md` — Purpose, Stack and Entry points while they are still empty, plus an `Intake`
  block (build/test/run, conventions, risks) that the next intake replaces. Text `orche card refresh`
  or you wrote is left alone.

It costs well under a dollar for a mid-size repo on Sonnet and usually takes under a minute (up to
9 on a large repo, `intake.timeout_s` 540; `orche init` prints `intake running… 60 s (claude-sonnet-5-5)` every 30 s); the
last line of `orche init` says what it cost: `intake my-app: 4 divisions, $0.42, 38 s
(claude-sonnet-5-5)`. If it cannot run (no `claude`, a timeout, an unusable answer), init still
succeeds and prints `intake skipped: <reason>; run \`orche intake my-app\` later`. Ctrl-C during the
sweep stops only the sweep: init prints `intake interrupted; run \`orche intake my-app\` later`, its
summary, and exits 0.

```sh
orche init my-app --no-intake        # skip it
orche intake my-app                  # run it (again) now
orche intake my-app --if-stale       # only if there is no stamp yet, 50+ commits landed, the stamped
                                     # commit is no longer an ancestor of HEAD, or it is 14+ days old
orche intake my-app --background     # start it detached and return at once (log in ~/.claude/orche/logs/intake/)
orche intake status my-app           # when it ran, at which commit, what it cost
orche --dry-run intake my-app        # print the exact claude command; writes nothing
```

`orche janitor run` refreshes it with `--if-stale` for projects that already had one (this runs even
while the janitor itself is still a dry run; `"intake": {"enabled": false}` in the config turns it
off). `/orche-apply` runs one `orche init` with a 10-minute tool timeout (the intake runs last; the
sweep's own limit is 9 minutes). `orche apply-all`
never runs the intake.

Secrets: the worker cannot run commands, so it cannot print a file or old diffs through `git`. It may
not read `.env*` files, anything under `secrets/`, keys (`*.pem`, `*.key`, `*.p12`, `id_rsa*`, …),
keystores, `.npmrc`/`.netrc` or credential JSON files, in the repo or by absolute path, nor `~/.ssh`,
`~/.aws` and the other credential stores in your home directory (Claude Code deny rules). The
worker runs with `--restricted`: it ignores the project's settings and hooks, and its file
tools stay inside the checkout. Its instructions forbid quoting
anything that looks like a credential, and ORCHE masks keys, tokens, JWTs, passwords
(`DB_PASSWORD=…`, `postgres://user:…@`) and `Authorization` headers in the answer before writing a
single file. The same masking module (`lib/orche/mask.py`) covers everything else ORCHE writes or
sends. Config syntax (`KEY=value`, `"key": value`, `KEY: value`) has its value masked; in a sentence
(`the token: …`) only a credential-like value is (6+ characters with a digit, a symbol or mixed case,
or quoted), so prose such as "secret: not needed" stays readable. A letters-only password written as
prose (`password: correcthorse`) is not masked.

### Choices and options

- **Defaults.** Every standard division except `ops` is checked; nothing is woken. Enter at every
  prompt takes them. `n` at `Apply?` (or Ctrl-D) stops with exit 0 and writes nothing.
  `orche --dry-run init <project>` runs the same checklist, then prints the plan and writes nothing.
  A division named in `--wake` must stay ticked: Enter shows the list again, saying which one, until
  it is (or Ctrl-D stops).
- **Custom divisions.** `a` adds one: a name (slug, 2-16 characters: a-z first, then a-z or 0-9, with
  single hyphens between parts, e.g. `web-app`; not a standard name, not `main`, `liaison`, `feat`, `hr`
  or `jev`), a label (≤ 24 characters; not a standard division's name or label such as `Ops` or `GTM`),
  a charter (one paragraph, ≤ 400 characters), a model (default `claude-opus-5-5`) and an effort
  (default `high`). Labels refuse line breaks; a single line break in a charter is folded into a
  space, and a blank line (a second paragraph) is refused; neither may contain control characters.
  Session names are unique across all projects: a name whose session `div-<proj>-<name>` another
  project already has (or would have for a standard division, e.g. `shop-build` in project `acme`
  next to project `acme-shop`) is refused, and so is a feature `orche feat new` would name
  `feat-<proj>-<name>` when another project has that session. A row ticked by default whose session
  another project holds shows unticked as `(unavailable: …)` and cannot be ticked. It works like a
  standard division: session `div-<proj>-<name>`, worktree `div-<name>`,
  its own `.orche/div/<name>/` files and a persona written from the charter; it shows by name in
  `orche status` and `orche depts`, by its label's first three letters in the status line (`Design` →
  `DES○`; four or five letters, then a digit, when that cell is taken, so cells stay unique and never
  equal `R&D RS BUILD GTM OPS`), and `TO <name>` reaches it from Main.
- **Without a terminal** (scripts, agents) give the setup as flags or a file. With neither, the
  defaults apply and the `Summary:` line is printed.

  ```sh
  orche init my-app --divisions build,gtm \
    --custom "design=Design:owns the visual system and the brand kit" \
    --custom "qa=QA:tests every release:claude-sonnet-5-5:medium"   # name=label:charter[:model[:effort]]
  orche init --print-setup my-app > setup.json   # current setup (defaults for a new repo), as JSON
  orche --dry-run init my-app --setup-file setup.json
  ```

  `setup.json` is `{"divisions": [...], "custom": {"<name>": {"label", "charter", "model"?, "effort"?}},
  "stage": "building", "aliases": [...]}`. `--custom` splits on `:`, so a charter or a model id that
  contains `:` (e.g. a Bedrock model id) goes in the file. A setup given wrongly (an unknown key, a
  wrong type or value) stops with one `orche:` line and exit 1 (a user error; 2 means "not found");
  `--print-setup` only prints and refuses
  any other setup flag. Other flags: `--stage building|public`, `--aliases a,b`, `--wake build,gtm`
  (start those divisions now), `--automations` (create the weekly Orca automations), `--no-intake`
  (skip the closing [project intake](#project-intake)).
- **Adding later.** Re-run `orche init <project>` with more divisions or custom ones (on a terminal,
  the checklist marks what the project has as `(onboarded)`). Only the new divisions get worktrees and
  files; existing worktrees, files and registry entries are left untouched.
- **Out of scope.** `orche init` never removes a division, changes a custom division's definition, or
  changes a registered project's stage or aliases. Sleep a division you no longer want
  (`orche sleep div-<proj>-<division>`) or remove it by hand; the stage changes with `orche stage set`.

### In chat instead

In Claude Code, `/orche-apply <project>` walks through the same choices in chat and applies them with
`--setup-file` after a dry-run preview. A bare `orche init` said in a session inside the project skips
the choices: the skill runs `orche init` at once with the defaults.

### Check it

```sh
orche status my-app   # one line: stage, awake count, divisions (○ = dormant), counters
orche depts my-app    # divisions and feature agents with status, session, branch, idle days
```

</details>

<details>
<summary><b>First request</b></summary>

## First request

### Step 1. Open the Main pane

In Orca, open the tab `main-my-app` (in the project's main checkout).

### Step 2. Ask in plain language

Type a small, concrete request, for example:

```
Add a date filter to the history API.
```

Main's first line echoes what it understood:

```
Understood: source = my-app, output = my-app
```

Main gets a plan confirmed with you, then runs `orche dispatch` with the dispatch block on stdin
(`---` between blocks; `--file <path>` also works). The command writes
the brief to `.orche/units/<unit>/BRIEF.md` starts the unit and prints `block N: delivered …` or `block N: refused — …`, so Main fixes a refusal
in the same turn. A block written in the reply is plain text and is not sent:

```
orche dispatch <<'EOF'
TO        div-my-app-build
GOAL      Add a date filter to the history API
SCOPE     src/api/history/**, tests/api/** ; branch div/build
CONTEXT   Request from GTM plan rev 2; current handler is src/api/history/list.ts
ACCEPT    GET /history?from=&to= filters by created_at
  invalid dates return 400
VERIFY    scripts/verify/api.sh
TIMEBOX   2h; on overrun report partial result and stop
FORBID    merge, rebase, force-push, edits outside SCOPE
REPORT    status, branch, head SHA, PR, verdict, commands run, memory-save
TIER      standard
EOF
```

### Step 3. Watch it run

The unit runs as a headless worker in the division's worktree; no division pane is needed. Watch
`orche status` (`RUNNING n`) or the status line (`run2`):

```sh
orche status
orche depts my-app
```

### Step 4. Read the result

Main reports in its pane without being asked (ORCHE wakes it): one full report when a lineage (the units one
request set going) closes, a failure or block at once, otherwise one progress line; `[drain]` also shows ORCHE's
notes.

Success: the result of your unit appears in Main's reply. See [How units run](#how-units-run) for
when a unit counts as done.

### When the owner is needed

Reports that land close together reach Main as one wake: ORCHE waits until the newest has been quiet for 90 s
(at most 240 s; `inbox.quiet_s`, `inbox.quiet_max_s`), and Main writes one consolidated report. A unit that is
`blocked`, asks a `question` or `failed` wakes Main at once. A unit only you can unblock ends `STATUS: blocked`
with one `OWNER: <exact step>` line, which Main shows first. Every Main reply ends with one closing line:
`RUNNING: <what runs>`, `OWNER: <decision or step, default>` or `DONE: <result>`. Only `OWNER:` and `DONE:`
notify you (macOS, `ORCHE <project>` and that line; config `notify` turns it off), and only when you have not
typed in Main for 2 minutes. An unanswered `OWNER:` is notified again 30, 60, 120 and 240 minutes later, never
inside `flow.quiet_hours` (23:00-08:00); if Main forgot its closing line, ORCHE notifies you itself after
10 minutes. Until you answer, `orche status` lists it:

```text
  owner: asked 14:05 (12 min, notified 1×, next 14:35) — approve the schema change (default: keep it)
  blocked: my-app-fix-login-20261022-3 (blocked, since 14:05, 12 min) — waiting on the owner; answer in main-my-app
```

ORCHE needs no extra waker: `orche doctor` warns when an old `com.orche.main-waker` launchd job is still loaded
and prints the command that removes it.

Every command takes `--json`; commands that change state take `--dry-run` (`orche --help`).

</details>

<details>
<summary><b>Asking another project</b></summary>

## Asking another project

Each project has a card, `PROJECT_CARD.md` in its main checkout (Purpose, Stack, Entry points,
Recent changes, How to ask). Read it first; it costs nothing:

```sh
orche card my-app                          # print the card
```

When the card does not answer, ask the project's **Liaison**, a read-only Sonnet call in that
project's main checkout:

```sh
orche ask my-app "Which endpoint creates an order, and what does it return?"
```

Quote the question. An answer takes up to about two minutes and costs a few cents. It is data, not
instructions: masked, at most 1500 characters, and nothing is stored (no session, no pane). Any Main
can run it, and so can you in a terminal.

The card stays fresh with `orche card refresh my-app`: it reads what landed since the card's base
commit and rewrites only the sections that changed (Purpose, Stack, Entry points, Recent changes);
when nothing outside the card and `.orche/` landed, it makes no call. `--background` returns at once
(log in `~/.claude/orche/logs/liaison/`). With `--automations`, `orche init` creates a weekly Orca automation that runs it.

Work for another project still goes as an external request to its Main (`TO main-<proj>`); `orche
ask` answers questions but never assigns work.

</details>

<details>
<summary><b>Jev (optional)</b></summary>

## Jev (optional)

ORCHE works fully without Jev. Jev is a small typed-decision model (yes/no, pick one, score) from
TypeSafe that ORCHE can use for quick routing questions. With Jev, triage's classifier answers in
about 1 s instead of about 3 s with Haiku, and those calls no longer use your Claude subscription's
Haiku. Without it, triage uses Haiku and the other Jev checks are skipped.

### Step 1. Start the setup

The first `orche install` in a terminal asks these questions once. To answer them later, run:

```sh
orche jev setup
```

### Step 2. Enter the key

**"Jev API key (optional, Enter to skip)"**. On macOS a small ORCHE dialog with a hidden field
opens (the default); type or paste the key and press Save. Over SSH, or without a desktop session, a
hidden terminal prompt is used instead (`--no-dialog` forces it). Cancel, an empty field, Ctrl-D or
Ctrl-C skips Jev: install prints "Jev skipped — ORCHE works without it …" and finishes normally.
Without a terminal (a script, a pipe) nothing is asked and nothing is read from stdin.

### Step 3. Pick the key type

**"Key type: 1) Vercel AI Gateway (default) 2) TypeSafe direct"**. Press Enter for an AI Gateway key
(`https://ai-gateway.vercel.sh/typesafe`, model `typesafe-ai/jev`) or `2` for a key from
console.typesafe.ai (`https://api.typesafe.ai`, model `jev-1.13.0`).

### Step 4. One test call

ORCHE sends **one** tiny test question with the key (well under $0.01). Only if it answers does ORCHE
store the key and turn Jev on:

```
Jev on (gateway, 0.4 s)
```

If it fails, you see the error (with the key hidden) and nothing is changed: no key is stored, your
config stays as it was, and ORCHE keeps working without Jev.

### Step 5. Verify

```sh
orche jev key status   # where the key comes from: env, keychain, file or none
orche doctor           # the jev row: OK jev gateway (key: keychain)
```

`orche doctor`'s `jev` row is `OK jev gateway (key: keychain)` or a WARN, never a FAIL, because ORCHE
works without Jev: `not configured (Haiku fallback)`, `disabled (…)`, or
``last probe failed <masked reason>; ORCHE works without Jev; run `orche jev probe` ``.

### Step 6. Triage (your choice)

Jev speeds up triage, but triage stays off until you turn it on with `orche triage enable` (see
[Triage](#triage)). Everything works without Jev and without triage.

### Where the key lives and how it is protected

Where the key lives: on macOS only in your login keychain (service `orche`, account
`JEV_API_KEY`); on Linux in `~/.claude/orche/secrets/jev.key` (readable only by you). ORCHE never
prints or logs the key: it is not written to your shell profile, logs, events or `--json` output,
and it is sent only to an `https` endpoint (a redirect is treated as an error, never followed).
ORCHE's env-read gate refuses an agent's normal ways to read or pass it: the environment, a
keychain value lookup (`security find-generic-password … -w`, also behind `timeout`, `sudo`,
`bash -c` and the like), the key file (its full path anywhere in a command, any read of the
secrets folder, and inline `python -c`/`node -e`/heredoc code that names it or calls orche's key
API) and the stdin flags (`--stdin`, `--key-stdin`, `--jev-key-stdin`). A command too nested to
check is refused too. Only what a command runs counts: a commit message, a search pattern or a
file's content that merely mentions these is allowed, except the key file's full path, which is
refused anywhere in a command. The gate is a guard against accidents, not a sandbox: inline interpreter
code is checked as text, but a script file that reads the key is not, and a determined program
running as you can still reach the key, so the keychain's own access control and your operating
system remain the boundary. An exported `JEV_API_KEY` variable
still works and takes precedence. `orche uninstall` keeps the stored key; `orche jev key rm`
deletes it.

### Jev commands

| Command | What it does |
|---|---|
| `orche jev setup` | the same questions later (or `--key-stdin --kind gateway\|direct` in scripts) |
| `orche jev probe` | one live test call with the stored key; prints the time or the (masked) error |
| `orche jev key set` | replace the key (dialog on macOS, else a hidden prompt; `--no-dialog`; `--stdin` only from a trusted script — without it a pipe is refused: "no terminal: use the dialog, or --stdin from a trusted script") |
| `orche jev key status` | says only where the key comes from: `env`, `keychain`, `file` or `none` |
| `orche jev key rm` | delete the stored key |
| `orche jev enable` / `disable` | turn Jev on or off without touching the key (`disable` also forgets the last failed probe) |

Non-interactive installs: `orche install --no-jev` never asks; `orche install --jev-key-stdin
--jev-kind gateway` reads the key from stdin.

</details>

<details>
<summary><b>Headless auth (optional)</b></summary>

## Headless auth (optional)

Every unit worker, intake sweep, Liaison answer and triage call is a headless `claude -p` run. By
default those runs use the same Claude login as your panes. Give them their own long-lived token and
they stop depending on that login: a pane that logs out or switches accounts no longer breaks them.
Usage limits are per account, so a token from the same account still shares the panes' limits; separate
limits need a token made on another account.

### Step 1. Make a token

In any terminal, run `claude setup-token` and sign in. It prints a token that lasts one year. It needs
a Pro or Max plan.

### Step 2. Store it

Run `orche auth set` and paste the token at the hidden prompt (nothing is shown while you paste). From
a script, pipe it on stdin instead. The token is never taken from the command line, and ORCHE never
prints or logs it.

### Step 3. Check it

`orche auth status` (or `orche doctor`, row `oauth token`) says where the token comes from: `env`,
`keychain`, `file` or not set. It never shows the value.

What changes:

- Only headless runs get the token (as `CLAUDE_CODE_OAUTH_TOKEN`). Panes keep the interactive login.
- A `CLAUDE_CODE_OAUTH_TOKEN` already in ORCHE's environment wins; the stored token is not read then.
- Storage: on macOS the login keychain (service `orche`, account `CLAUDE_CODE_OAUTH_TOKEN`), elsewhere
  `~/.claude/orche/secrets/oauth.token`, mode 0600. The Jev key is stored separately.
- Without a token nothing changes: headless runs use the login, as before.
- `orche auth rm` deletes the stored token. When it expires, run Step 1 and Step 2 again.

</details>

<details>
<summary><b>Uninstall and leaving ORCHE</b></summary>

## Uninstall and leaving ORCHE

### Leave one project

You decide this; never a hook or an agent does it.

1. Retire the project's feature agents: `orche feat retire … --yes`.
2. Remove the division worktrees with `orca worktree rm`.
3. Delete the project's ORCHE files from the main checkout (`.orche/`, `PROJECT_CARD.md`,
   `AGENTS.md`, `.agents/`, `.claude/agents/div-<proj>-*.md`) and its entry in
   `~/.claude/orche/registry.json`.

To stop a single division instead, sleep it: `orche sleep div-<proj>-<division>`.

### Remove ORCHE from this machine

```sh
orche --dry-run uninstall   # preview; writes nothing
orche uninstall
```

`orche uninstall` removes ORCHE's block from your status-line script (and from `~/.claude/fable/*`
when present), removes ORCHE's hook entries from `~/.claude/settings.json` and restores the original
values recorded in `install-state.json`, and removes the agent symlinks and the `~/.claude/skills/` links a settings-mode install made. It keeps the backups (`*.bak-*`), ORCHE_HOME
(`~/.claude/orche`) and a stored Jev key; it prints how to delete the key (`orche jev key rm`).

Then, inside Claude Code, remove the plugin with `/plugin` (uninstall `orche@orche`). Finally delete
the clone (`~/orche`) and the PATH line from your login profile.

</details>

<details>
<summary><b>How units run</b></summary>

## How units run

By default (config `delivery: runner`) every new brief to a division or feature agent runs as a
lean headless worker: `orche dispatch` spawns `orche run <unit>` detached and returns at once, and
the runner calls `claude -p --output-format json` in the unit's worktree with project settings
only (`--setting-sources project`), ORCHE's secret-read and scope gates (passed with
`--settings`; no other ORCHE hook runs there), no MCP servers (its own empty `--mcp-config`,
`~/.claude/orche/runner/mcp/<unit>@<started_ns>.json`, written at run start and removed on every
exit path, by reconcile and by log pruning; `--dry-run` prints the placeholder path), the
target's model from the registry (a feature agent: its division's; Main:
`claude-opus-5-5`), and a TIER-sized effort and budget:

| TIER | Effort | Max turns | Allowed tools |
|---|---|---|---|
| mechanical | `low` | TIMEBOX × 3 (30–400) | Read, Write, Edit, Glob, Grep, `ls`, `test`, `git status`, `git diff`, `git add`, `git commit`; denied (best-effort prefix rules): `git commit --amend`, `-n`, `--no-verify`, `git push` |
| standard | `medium` | TIMEBOX × 3 (30–400) | the same allowed tools plus Bash (no deny list) |
| hard | `high` | TIMEBOX × 3 (30–400) | the same allowed tools plus Bash (no deny list) |

A mechanical worker may commit, but the runner footer still says "do not commit, push, merge or
rebase unless the brief allows it", so it is told to commit only when the brief says so (a triage
brief does). The deny list is a best-effort first filter: prefix rules miss abbreviated or combined
flags (`git commit -am x --amen`, `-anm`, `--no-veri`). The guarantee against an amend, reset or
rebase is the done gate's ancestry check, at every tier: when the HEAD from before the run is not an
ancestor of the new HEAD (or is unknown), the pointer is `info` "unverified: STATUS: done (history
rewritten …)", never `done`. Skipping the hooks (`--no-verify`) is a documented gap no post-hoc
check sees: it is forbidden by the brief and the footer, and the ledger verifier reviews the diff.

The persona goes in as `--append-system-prompt-file`. The worker gets your environment without
ORCHE's, Orca's, Claude Code's and Jev's own variables (names starting `ORCHE_`, `ORCA_`,
`CLAUDE`, `JEV_`). Of those only `ORCHE_HOME`, `CLAUDE_CONFIG_DIR`,
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, ORCHE's hook settings and the authentication and mTLS
variables pass, plus its own `ORCHE_SESSION=<target>` and `ORCHE_RUNNER=1` (SPEC §14.10). The timeout is the brief's TIMEBOX plus 60 s
(30 min when it has none, `1h30m` adds up, at most 4 h). At most `runner.max_parallel` (4) units
run per project and one per target session: a target's units share its worktree, so parallelism
is across targets (and feature agents, each with its own worktree), never inside one worktree. A
run over either limit waits up to `runner.queue_wait_s` (30 minutes), then files `info` "runner
busy: <n> live runs" (or "<target> is running <unit>"), "retry with `orche run <unit>`". Every
run ends with one pointer — a runner error or SIGTERM files `failed`, a duplicate `orche run` of
a live unit files `info` "already running (pid N, started HH:MM)" and exits 4 while the live run
still files its own. Measured on
2026-10-18 with 12 small units: pane workers cost $9.77 and took 22 minutes; runner workers cost
$3.75 (about $0.31 per unit) and took 2 minutes. `orche metrics` sums the runs it recorded,
also per model and effort.

`delivery: pane` restores the older path (`[orche] inbox: <unit> rev <N>` is typed into the
worker's Orca pane). Main targets always use the pane, because a person reads them; `orche deliver` types the nudge (never the hook) and tells the sender when it cannot. For a runner unit, `STOP <unit>` kills a live
run (its `claude` process group, SIGKILL after 5 s) and the runner files `info` "stopped by
Main"; a run still waiting for a slot gives up and files "stopped by Main before start"; a run
that already finished keeps its result (the feedback says "already finished"). `AMEND <unit>
rev N` stops a live run and starts a fresh one for rev N, or simply starts one when none is
live. The STOP mechanism is a marker (`~/.claude/orche/runs/<project>/<unit>.stop`), which a
running run checks every 2 s, plus SIGTERM to `orche run` as the fast path when its pid is
verified: STOP/AMEND check with `ps` (1 s at most, within the hook's 3 s budget) that the lock's
pid still is that `orche run <unit>` (the unit id as a whole word, `--json`/`--dry-run`/`--`
allowed before it). A recycled pid is never signalled (its lock is a lost run, see below); when
`ps` fails or times out the run counts as live, keeps its lock and gets no signal (events line
`run unit=<unit> to=<target>
runner-pid-check-failed pid=<pid>`): its own 2 s poll still stops it. RUN.json `error` then
reads `terminated by SIGTERM` when a verified pid got the signal first (the usual case), and
`stopped by STOP marker` only when the poll alone stopped the run. A run honours only a marker
written after the run was requested. After every run the runner kills whatever is left of
claude's process group, then every process carrying the run's `ORCHE_RUN_ID=<unit>@<ns>` in its
environment (the worker's environment tag, so a `setsid`/`nohup … &` child that left the group
dies too; the tag in a command's arguments, as in `grep ORCHE_RUN_ID=…`, never counts). This
also ends daemons a worker started that inherited the tag — a tmux server, gpg-agent for a signed
commit, ssh-agent, a language server — and everything running under them: they end with the run.
On macOS, Apple's own binaries (`/bin/sh`, `/bin/sleep`, …) hide their environment, and so do
programs that rename their process title (node's `npm exec`/`npx`-launched MCP servers, for
example): they are found only through a tagged parent or the process group, so a daemonised one
escapes the sweep. Also on macOS, an environment value that itself contains ` ORCHE_RUN_ID=<id> `
(with spaces around it) is matched as if it were the tag. An `orche run` you start inside a worker
drops the tag it inherited, but it is spared only once the shell that started it has been
reparented away from the outer run (the usual background-job case, after the outer claude
exits); while that shell still sits under the outer run, the nested run ends with it. The
hook spawns the run with its own Python (`<python> bin/orche.py run <unit>`); `bin/orche` is for
people. Config lives in
`~/.claude/orche/config.json` (see `config/orche.example.json`): `claude_bin`, `delivery`,
`runner.turns_per_minute`, `runner.turns_floor`, `runner.turns_ceiling`, `runner.max_parallel`, `inbox.wake`,
`runner.queue_wait_s`, `runner.log_keep_days` (14: older `logs/runs/*.log` are deleted at each
`orche run` and by `orche janitor run`; RUN.json records stay). `orche doctor` checks that
`claude` is found and lists the 15 flags ORCHE passes (`claude flags`), warns under 1 GiB free disk for
`~/.claude/orche` and over 200 MiB of run logs, warns when the process table (`ps`) cannot be
read and for every lost run whose leftovers are not swept yet, and reconciles lost runs (one
WARN row each).

**What if the runner dies.** If `orche run` itself is killed (kill -9, the OS running out of
memory, a shutdown, a power cut) or cannot write its result (a full disk), the run does not
vanish silently: ORCHE notices the dead run the next time anyone looks — the next prompt in any
of the project's sessions, the next `orche run` in that project, or `orche status` (also `depts`,
`doctor`, `metrics`, `janitor run`). It then writes `RUN.json` with `stop_reason: lost` (a
RUN.json the run already wrote is kept), naming what ran — the brief revision, TIER, model and
effort recorded in the run's lock — files `failed` "runner died: <unit> (pid N, started HH:MM);
re-run with `orche run <unit>`" to Main, stops the claude that run left behind, and frees
its slot (events line `run unit=<unit> to=<target> runner-reconciled pid=<pid>`). What the worker
did is not reported: Main sees `failed` "runner died" even when the work itself finished (the
run's own RUN.json may say `blocked` or `done`), so check it before re-running. When the run or
its worker had already filed a pointer before dying, only the lock goes (`runner-stale-lock`).
When the run's target no longer routes anywhere, nothing can be filed: the line says "NOT
filed" and RUN.json says `pointer_filed: false`. The slot is freed even when the leftovers
cannot be killed yet (no time in the prompt hook, or `ps` failing): they are swept by a later
reconcile, and `orche doctor` lists them meanwhile. `orche status` prints one line per such run
on stderr. After three real failures in a row to read the process table (a `ps` that fails, or
times out at its full 2 s), ORCHE stops retrying such a sweep once the lost run's claude is gone;
a read cut short by a prompt hook's remaining time never counts, so a hung `ps` is only counted
by full-time reconciles (`orche run` start, `orche status`, `doctor`, `janitor run`). To retry,
run `orche run <unit>` by hand.

Known gaps: (a) a lost run surfaces only when someone looks (the next prompt to Main or a unit,
an `orche` command); (b) a filesystem that stays unwritable keeps its `failed` pointer unfiled
(`orche doctor` warns); (c) while `ps` cannot be read, a queued run on the same target can start
next to a lost run's still-running claude, and a pending sweep dropped after three real failures
leaves its leftovers running (`orche doctor` warns about pending sweeps); (d) a pid that exits and
is reused in the up-to-2-seconds between reading the process table and the SIGKILL could be
signalled (theoretical); (e) the macOS environment-value match, the nested-run rule and the
title-renaming programs above.

### Stuck reports repair themselves

A report nobody read, or a brief that never reached its unit, is picked up by `orche reconcile`. On macOS
`orche install` sets it to run every 5 minutes (a LaunchAgent, `com.orche.reconcile`); the prompt and Stop
hooks also start a pass when the last one is more than 15 minutes old. A pass nudges the idle pane or delivers
the brief again, waits longer each time (10, 20, 40 minutes), and after three tries sends one urgent note
instead. `orche status` shows when the last pass ran, and `orche doctor` warns when the job is not loaded.
To see what a pass would do without doing it: `orche reconcile --dry-run`. To turn it off:
`"reconcile": {"enabled": false}` in the config, or `orche install --no-reconcile` for the LaunchAgent only.

### When a unit counts as done

`orche run <unit>` starts a headless `claude -p` worker in the target's worktree with the
brief as its prompt; the worker runs VERIFY, ends with `STATUS: done|blocked|failed`, and the
runner files one pointer to whoever sent the brief (Main, or Build for its feature units) plus `RUN.json` and `REPORT.md`
next to the brief (the worker itself never runs `orche report` under the runner). A worker
never records its own verdict (writer ≠ judge). From Build or a feature agent (the targets
`orche report done` gates) the pointer is `done` only when the final STATUS line (SPEC
§14.11) says `done` or `release-ready` and comes with a new commit made during the run that
keeps the old HEAD as an ancestor, a clean worktree, and a ledger row for that commit (for
`release-ready`: >= unit-test-verified from a verifier other than the target); otherwise it
arrives as `info` "unverified: STATUS: done (no commit)", "(uncommitted changes)", "(no
ledger row for <sha>)", "(ledger verdict <v> for <sha>)" or "(ledger verifier <v> is the
claimant for <sha>)" — a committed unit stays `info` "(no ledger row
for <sha>)" until a verifier records the row, and Main decides. Writer ≠ judge is enforced when the row is
recorded: `orche ledger record` refuses a `--verifier` equal to the producer (`--producer`,
else the recording session's `ORCHE_SESSION`); for `done` the runner only checks that a row
exists and does not re-check who recorded it. From any other target (Research, R&D, GTM, Ops, Main) a
final `STATUS: done` files `done`, as `orche report done` would. Among runner runs there is
one run per target at a time, so "a new commit made during the run" is this unit's; a pane
worker (`delivery: pane`) or a person working in that worktree is not
counted.

```sh
# in the reviewer's session (here a verification feature agent), not the producer's
orche ledger record --pr 42 --sha <head-sha> --verdict unit-test-verified \
  --evidence logs/verify-api.txt --verifier feat-my-app-review
orche run my-app-add-a-date-filter-20261018-1   # re-run a unit by hand (--dry-run prints the argv)
# pane mode only (delivery: pane): the worker files its own pointer
orche report done --summary "date filter added; tests pass"
```

### Triage

Off by default (`orche triage enable` turns it on). With it on, a prompt typed into a Main pane
is checked before Main spends a turn on it: a simple, fully specified request goes straight to a
division as a runner unit and Main never sees the prompt; anything that needs planning,
judgement or a conversation reaches Main as before. A Main dispatch turn costs about $0.05–0.13
and 6–20 s; a routed prompt costs a classifier call instead.

The checks run in order, and every one fails open (an error, a timeout or a low confidence sends
the prompt to Main unchanged):

1. **Tags** (the whole first word, any case: `[build]s are failing` is no tag). `[main]` always
   reaches Main. `[build]`, `[research]`, `[rnd]`, `[gtm]`, `[ops]` send the prompt to that
   division; `[direct]` lets the classifier pick the division (Build when it cannot). A tagged
   prompt runs as TIER `standard`, or `mechanical` when the classifier says so. `[drain]` and
   prompts starting with `/` or `!` are left alone.
2. **Bypasses** (no model). Prompts under 12 characters; prompts that start with an
   acknowledgement or continuation word (yes, yeah, ok, okay, sure, no, nope, go ahead, do it,
   that one, and their Korean equivalents); questions (ending in `?`, starting with a question
   word, or ending in a Korean question form even without `?`); and replies to a question Main
   just asked go to Main. For the last one, Main's last message is read from the transcript, its
   trailing fenced blocks (any ```` ``` ```` or `~~~` fence)
   and `STATUS:` lines are dropped, and what is
   left must end with `?` or a confirmation request ("Shall I …", "Want me to …", …).
3. **Heuristics** (no model). Only a request that is an instruction AND names a concrete target
   (`docs/guide.md`, `README.md`, or a quoted identifier such as `` `fooBar` ``) AND has no
   negation ("don't", "never", "no need to", and their Korean forms) AND is not a question is a
   candidate. An instruction is an English verb first ("add", "fix", "rename", "delete",
   "replace", "move", "implement", "bump", …, optionally after "please", "just", "let's" or
   "I need you to") or a Korean imperative or propositive form (a verbal noun with a verb ending
   or as the last word, or an imperative verb); a verbal noun used as a plain noun is not.
   Everything else goes to Main.
4. **Classifier** (candidates only). It returns a route, a division, a TIER, a confidence and a
   draft GOAL/ACCEPT/VERIFY/SCOPE. The backend is Jev (about 1 s) when Jev is enabled and
   configured, otherwise `claude -p` with Haiku (about 3 s measured on 2026-10-18, 5–9 s
   expected under load; capped by `triage.max_latency_s`, default 12 s). Haiku runs with no
   tools (`--tools ""`), from a fresh empty directory under the system temp dir, outside your
   home folder, so no CLAUDE.md is loaded (about 960 input tokens per call, about $0.0003), with
   the runner's filtered environment; on timeout its whole process group is killed. The prompt
   goes direct only when the route is `direct`, the confidence is at least
   `triage.min_confidence` (0.8) and the TIER is not `hard`. With Jev, the confidence is Jev's
   probability for the route it chose; the division and TIER answers do not lower it. Every
   option is described to Jev: divisions and tiers are described with the Haiku prompt's words;
   the route with a short paraphrase.
5. **Action.** ORCHE writes the brief. GOAL is the classifier's goal, else the prompt itself
   when it has at most 300 characters, else a 200-character excerpt marked "(excerpt; the full
   prompt is quoted in CONTEXT)". Whenever GOAL is not the prompt word for word, CONTEXT ("routed
   by triage from a Main prompt; the prompt, quoted below, is the whole instruction") quotes the
   whole prompt line by line, so nothing is lost (otherwise CONTEXT is "routed by triage from a
   Main prompt; the prompt is the whole instruction"). ACCEPT
   ends with "`git add` only the files you changed, by name (never `-A`, `-u`, `.` or a glob);
   exactly one commit with a conventional message";
   TIMEBOX 30m; FORBID "push, amend, --no-verify, files outside SCOPE") and starts the runner
   through the same path as a Main dispatch. SCOPE keeps only relative paths inside the
   division's worktree (letters, digits, `_`, `.`, `/` and `-` only, no leading `-`, no `/…`,
   `~…` or `..`), else `.`. The classifier's SCOPE may also hold globs (`*`, `?` and `**` only;
   an entry with `[` or `]` is dropped; the base directory inside the worktree), meaning what they
   mean in a feature scope; a glob never reaches VERIFY (refused), and the commit clause tells the
   worker never to `git add` a glob, which, like SCOPE itself, is an instruction. The scope gate enforces feature scopes
   and the worktree boundary, not a division brief's SCOPE, so SCOPE is an instruction to the
   worker. VERIFY is the classifier's only when it is one `ls`, `test`,
   `git status` or `git diff` command a mechanical worker can run (`git` with plain paths and
   only `--short`, `--stat`, `--name-only`, `--name-status`, `--cached` or `--porcelain`), else
   `ls -l <scope files>` (plain paths only) when that passes the same check, else
   `git status --short` (also with no files). The prompt is then blocked with "[orche] routed to <division>
   as <unit> (runner-started, TIER <t>): "<the GOAL's first 60 characters, masked>" … (to cancel:
   [main] STOP <unit>)", and Main's activity is touched as on any Main prompt, so `orche depts`
   and the status line do not show Main idle. Once the runner
   started the prompt is always blocked; writing the records is best-effort. The result
   arrives in Main's inbox like any other unit's. A routed Build unit commits, so the worktree
   stays clean for the next unit, and its pointer reads "unverified: STATUS: done (no ledger
   row for <sha7>)" until a verifier records the row (writer ≠ judge). If routing fails, the
   prompt reaches Main with one line saying why.

Only the extra classifier call costs time; the tags, bypasses and heuristics add under 20 ms.
Each decision writes an `events.log` line (`triage session=… decision=main|direct|bypass|tag
stage=… conf=… backend=… ms=… unit=…`) and a row in `~/.claude/orche/triage/samples.jsonl`
(a hash of the prompt, the decision, confidence, backend and latency; the masked prompt only with
`triage.keep_prompts: true`). A routed prompt's row and events line also carry its unit id, whose
slug is made from the prompt's first words (masked). `orche triage status` shows the backend,
latency p50/p95, the last eval and the last 20 decisions. `orche triage eval <file>` scores
labelled prompts (`{"prompt": …, "label": "direct"|"main"}` per line; `--backend heuristics`
scores the no-model checks alone) and records the summary per backend in
`~/.claude/orche/triage/eval.json` (when, which file, the scores; never the prompts; not with
`--dry-run`). `orche triage status` says when the last eval ran, on which file and whether that
file still exists. With triage enabled on Jev and no Jev eval on file, `orche triage status`,
`orche triage enable` and `orche status` print a warning: run an eval first. `orche status`
prints one triage line while triage is enabled. `orche metrics` has a Triage section. Config: `triage.enabled`, `triage.backend` (`auto`, `jev`,
`claude-haiku`), `triage.min_confidence` (default 0.8), `triage.max_latency_s`, `triage.keep_prompts`.
Triage needs `delivery: runner`, and only Main sessions are triaged.

</details>

<details>
<summary><b>The organization</b></summary>

## The organization

Main assigns work with structured `dispatch` blocks; hooks, not model turns, deliver them.
Workers report with a small inbox pointer, and Main reads those in batches.
A unit is done only when a verification-ledger row exists for its commit.
Memory lives in files, so sessions `/clear` at unit boundaries instead of growing.
Status costs zero tokens: a status line, the Orca sidebar and the `orche` CLI.
Why: in practice most token spend is a big orchestrator context re-read on every wake-up.

```
User ──(talks to Main; may open any panel)──▶ Main (per project)
                                               │ orche dispatch <<'EOF' … EOF → relay
               ┌──────────┬───────────┬────────┼────────┬──────────┐
               ▼          ▼           ▼        ▼        ▼
              R&D      Research     Build     GTM      Ops (dormant while stage=building)
                                      │
                         ┌────────────┼────────────┐
                    feature agent  feature agent  feature agent …
                    (child worktrees of Build; created by HR)
HR (one global) ── registry.json ── create · sleep · retire · rate limits · weekly audit
Liaison (headless) ── orche ask <proj> · orche card refresh <proj> (read-only Sonnet calls in the main checkout)
```

| Piece | What it does |
|---|---|
| Dispatch (`orche dispatch`) | Parses dispatch blocks from stdin or `--file`, refuses incomplete ones with a printed reason, writes `BRIEF.md` revisions, starts the runner (or `orche deliver` for a pane); gates completion claims (`STATUS: done` / `release-ready`) through the done gate |
| Runner | `orche run <unit>`: one headless `claude -p` worker per unit in its worktree; records `RUN.json`, files the pointer through the done gate |
| Report inject (UserPromptSubmit hook) | Injects every report at the next prompt (notes at drain triggers); ORCHE wakes an idle Main |
| Verification ledger | `orche ledger record/check`; verdicts `live-ui-verified`, `unit-test-verified`, `type-check-only`, `verifier-blocked`, `verifier-failed`. CI green is not a verdict |
| HR | `orche feat new/sleep/wake/retire`, rate limits, creation freeze, weekly audit, `orche hr correct` for standing orders |
| Keymaster | `orche secret …`: agents see secret names only; you paste values into a dialog |
| Env-read gate (PreToolUse hook) | Refuses an agent's reads of `.env`/`.env.*`, `*.pem`/`*.key`/`*.p12`, data files in `secrets/` and environment dumps (`printenv`, bare `env`, `export -p`, `echo $…_TOKEN`, `print(os.environ)`); `.env.example`/`.sample`/`.template` (names only) are allowed. It checks command words (also behind `timeout`, `sudo`, `bash -c`) and tool paths, never content: a Markdown file or commit message that mentions `.env` passes, and `rg SECRET lib/` is a search pattern, not a read |
| Janitor | `orche janitor …`: state-based cleanup; archives sessions, never deletes them |
| Jev gate | Optional fast typed-decision model for drain urgency; off by default |

</details>

<details>
<summary><b>Token economy</b></summary>

## Token economy

Numbers below come from **one deployment, Oct 2026** (a single operator's 30-day transcript
analysis). They are motivating evidence, not guarantees.

| Measurement | Value |
|---|---|
| Share of token cost that was prompt-cache read + write | 93% (read 60%, write 33%; output 7%) |
| Average context per response | Orchestrator 518K · Opus workers 243K · Sonnet workers 169K |
| Orchestrator wake-ups caused by subagent completion notifications | ~70% |
| Routine user messages (confirm / status / "continue") | 13–15% |

Cost ≈ context size × turn count. Turns were driven by notifications, not by the user, and
context grew because the orchestrator accumulated subagent output. ORCHE attacks both by
structure: hooks relay dispatches and reports (a dispatch costs 0 Main turns; a completion costs one, the wake, by design), reports are batched into Main's next prompt,
Main never reads diffs, sessions `/clear` at unit boundaries with memory in files, and TIER routes
mechanical work to cheaper models.

| Weekly target | Baseline | Target |
|---|---|---|
| Main turns/day | ≈108 | ≈50 |
| Main context per response | 518K | ≤200K |
| Worker context per response | 243K | ≤120K |

Replaying the same 30 days under ORCHE rules (anonymized simulation): Main triggers/day 106 → 52
(conservative, −49% cost) or 41 (expected, −68% cost). Caveats: token reduction correlates only
weakly with cost reduction, and savings must be tracked together with the ledger pass rate.
`orche metrics` computes the real numbers from your transcripts, without a model.

</details>

<details>
<summary><b>Documentation</b></summary>

## Documentation

- [SPEC.md](SPEC.md): the full specification (rule IDs, schemas, hook contracts)
- [docs/principles.md](docs/principles.md): the six system and ten working principles
- [docs/faq.md](docs/faq.md): common questions
- [docs/orca-sidebar.md](docs/orca-sidebar.md): how ORCHE looks in Orca
- [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

</details>

<details>
<summary><b>Attribution</b></summary>

## Attribution

Several conventions are adapted from Cursor's **pstack** plugin (MIT, by Lauren Tan): the
verification ledger, the decision log, the Always-pause list and the "show me your work"
reporting conventions, among others listed at the top of [SPEC.md](SPEC.md).

</details>

<details>
<summary><b>License</b></summary>

## License

MIT. See [LICENSE](LICENSE).

</details>
