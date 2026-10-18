# ORCHE

ORCHE runs a small company of Claude Code sessions inside Orca, the desktop
multi-agent manager. Each project gets one **Main** (decides, never codes), a
**Liaison** (answers other projects), and five long-lived **divisions** (R&D, Research, Build, GTM,
Ops), each in its own git worktree; Build owns per-feature **feature agents**.
Main assigns work with structured `dispatch` blocks; hooks, not model turns, deliver them.
Workers report with a small inbox pointer, and Main reads those in batches.
A unit is done only when a verification-ledger row exists for its commit.
Memory lives in files, so sessions `/clear` at unit boundaries instead of growing.
Status costs zero tokens: a status line, the Orca sidebar and the `orche` CLI.
Why: in practice most token spend is a big orchestrator context re-read on every wake-up.

ORCHE is a Claude Code **plugin** (agents, skills, hooks) with a companion CLI, `orche`.
It is neither a CLAUDE.md snippet nor a single skill.

## Requirements

- macOS (the pure-Python parts also run on Linux), Python 3.11+ (standard library only), git
- Claude Code, and Orca with its `orca` CLI on `PATH`
- Optional: `gh` (GitHub secrets), `gcloud` (GCP Secret Manager), `vercel`, `age` (secret sharing)

## Install

```sh
git clone https://github.com/0xmoomi/orche ~/orche
export PATH="$HOME/orche/bin:$PATH"     # add to your shell profile

orche --dry-run install                 # show what would change; writes nothing
orche install                           # patch user settings, status line, agent symlinks
```

Then, inside Claude Code, install the hooks, agents and skills as a plugin (`orche install`
prints these two lines with your clone's path):

```
/plugin marketplace add ~/orche
/plugin install orche@orche
```

(`/plugin marketplace add https://github.com/0xmoomi/orche` works too; the CLI still needs the clone.)

`orche doctor` checks the result (one OK/WARN/FAIL line each), including whether a login shell
finds `orche` and a Python 3.11+ `python3` (macOS's `/usr/bin/python3` is 3.9: `brew install python`
and put `/opt/homebrew/bin` before `/usr/bin` in `PATH`; an old `python3` is only a WARN while the
launchers can still pick a 3.11+ themselves, and a FAIL when they cannot). `orche install` backs up every
file before changing it (`<file>.bak-<ts>`) and records the original values in
`~/.claude/orche/install-state.json`; `orche uninstall` restores them. Use
`orche install --mode settings` instead of the plugin if you want the hooks registered directly
in `~/.claude/settings.json`; a later plain `orche install` (plugin mode) takes those entries out again,
so the hooks never run twice. `orche install` also patches two operator-specific files when they
are present (`~/.claude/fable/*` and `~/.codex/AGENTS.md`) and skips them when they are absent;
`orche install --dry-run` previews every change, these included.

### Jev (optional)

ORCHE works fully without Jev. Jev is a small typed-decision model (yes/no, pick one, score) from
TypeSafe that ORCHE can use for quick routing questions. With Jev, triage's classifier answers in
about 1 s instead of about 3 s with Haiku, and those calls no longer use your Claude subscription's
Haiku. Without it, triage uses Haiku and the other Jev checks are skipped.

The first `orche install` in a terminal asks once:

1. **"Jev API key (optional, Enter to skip)"**. On macOS a small ORCHE dialog with a hidden field
   opens (the default); type or paste the key and press Save. Over SSH, or without a desktop
   session, a hidden terminal prompt is used instead (`--no-dialog` forces it). Cancel, an empty
   field, Ctrl-D or Ctrl-C skips Jev: install prints "Jev skipped — ORCHE works without it …" and
   finishes normally. Without a terminal (a script, a pipe) nothing is asked and nothing is read
   from stdin.
2. **"Key type: 1) Vercel AI Gateway (default) 2) TypeSafe direct"**. Press Enter for an AI
   Gateway key (`https://ai-gateway.vercel.sh/typesafe`, model `typesafe-ai/jev`) or `2` for a
   key from console.typesafe.ai (`https://api.typesafe.ai`, model `jev-1.13.0`).
3. ORCHE sends **one** tiny test question with the key (well under $0.01). Only if it answers
   does ORCHE store the key and turn Jev on: "Jev on (gateway, 0.4 s)". If it fails, you see the
   error (with the key hidden) and nothing is changed: no key is stored, your config stays as it
   was, and ORCHE keeps working without Jev.

Where the key lives: on macOS only in your login keychain (service `orche`, account
`JEV_API_KEY`); on Linux in `~/.claude/orche/secrets/jev.key` (readable only by you). ORCHE never
prints or logs the key: it is not written to your shell profile, logs, events or `--json` output,
and it is sent only to an `https` endpoint (a redirect is treated as an error, never followed).
ORCHE's env-read gate refuses an agent's normal ways to read or pass it: the environment, a
keychain value lookup (`security find-generic-password … -w`, also behind `timeout`, `sudo`,
`bash -c` and the like), the key file (its full path anywhere in a command, any read of the
secrets folder, and inline `python -c`/`node -e`/heredoc code that names it or calls orche's key
API) and the stdin flags (`--stdin`, `--key-stdin`, `--jev-key-stdin`). A command too nested to
check is refused too. The gate is a guard against accidents, not a sandbox: inline interpreter
code is checked as text, but a script file that reads the key is not, and a determined program
running as you can still reach the key, so the keychain's own access control and your operating
system remain the boundary. An exported `JEV_API_KEY` variable
still works and takes precedence. `orche uninstall` keeps the stored key; `orche jev key rm`
deletes it.

| Command | What it does |
|---|---|
| `orche jev setup` | the same questions later (or `--key-stdin --kind gateway\|direct` in scripts) |
| `orche jev probe` | one live test call with the stored key; prints the time or the (masked) error |
| `orche jev key set` | replace the key (dialog on macOS, else a hidden prompt; `--no-dialog`; `--stdin` only from a trusted script — without it a pipe is refused: "no terminal: use the dialog, or --stdin from a trusted script") |
| `orche jev key status` | says only where the key comes from: `env`, `keychain`, `file` or `none` |
| `orche jev key rm` | delete the stored key |
| `orche jev enable` / `disable` | turn Jev on or off without touching the key (`disable` also forgets the last failed probe) |

Non-interactive installs: `orche install --no-jev` never asks; `orche install --jev-key-stdin
--jev-kind gateway` reads the key from stdin. `orche doctor` shows a `jev` row: `OK jev gateway
(key: keychain)`, or a WARN — never a FAIL, because ORCHE works without Jev: `not configured
(Haiku fallback)`, `disabled (…)`, or
``last probe failed <masked reason>; ORCHE works without Jev; run `orche jev probe` ``.

## Apply ORCHE to a project

ORCHE joins one project at a time, when you name it (`orche apply-all` exists but is not the way to
start). `orche init <project>` on a terminal asks which divisions the project gets:

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
Summary: build, gtm, design(custom) · stage building · 3 worktrees · panes main + liaison
Apply? [Y/n]
```

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
  any other setup flag.
- **Adding later.** Re-run `orche init <project>` with more divisions or custom ones (on a terminal,
  the checklist marks what the project has as `(onboarded)`). Only the new divisions get worktrees and
  files; existing worktrees, files and registry entries are left untouched.
- **Out of scope.** `orche init` never removes a division, changes a custom division's definition, or
  changes a registered project's stage or aliases. Sleep a division you no longer want
  (`orche sleep div-<proj>-<division>`) or remove it by hand; the stage changes with `orche stage set`.

In Claude Code, `/orche-apply <project>` walks through the same choices in chat and applies them with
`--setup-file` after a dry-run preview.

## Five-minute quick start

1. Onboard a repository that Orca already knows (display name, id or path). On a terminal this
   first shows the division checklist (see [Apply ORCHE to a project](#apply-orche-to-a-project));
   it then creates one worktree per chosen division, opens `main-<proj>` and `liaison-<proj>` panes
   in the main checkout, and starts Build and GTM; the other divisions stay `dormant` (no pane, no
   cost):

   ```sh
   orche init my-app --wake build,gtm
   orche depts my-app        # divisions and feature agents with status, session, branch, idle days
   orche status              # one line per project
   ```

2. Talk to `main-my-app` in plain language. Main echoes what it understood, gets a plan
   confirmed, and ends its reply with a dispatch block. The Stop hook writes the brief to
   `.orche/units/<unit>/BRIEF.md` and starts the unit; Main spends no turn on delivery:

   ````
   ```dispatch
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
   ```
   ````

3. The hook runs the unit; watch `orche status` (`RUNNING n`) or the status line (`run2`).
   `orche run <unit>` starts a headless `claude -p` worker in the target's worktree with the
   brief as its prompt; the worker runs VERIFY, ends with `STATUS: done|blocked|failed`, and the
   runner files one pointer to Main (a feature reports to Build) plus `RUN.json` and `REPORT.md`
   next to the brief (the worker itself never runs `orche report` under the runner). A worker
   never records its own verdict (writer ≠ judge). From Build or a feature agent (the targets
   `orche report done` gates) the pointer is `done` only when the final `STATUS: done` comes
   with a new commit made during the run, a clean worktree, and a ledger row for that commit;
   otherwise it arrives as `info` "unverified: STATUS: done (no commit)", "(uncommitted
   changes)" or "(no ledger row for <sha>)" — a committed unit stays `info` "(no ledger row
   for <sha>)" until a verifier records the row, and Main decides. Writer ≠ judge is enforced when the row is
   recorded: `orche ledger record` refuses a `--verifier` equal to the producer (`--producer`,
   else the recording session's `ORCHE_SESSION`); the runner only checks that a row exists and
   does not re-check who recorded it. From any other target (Research, R&D, GTM, Ops, Main) a
   final `STATUS: done` files `done`, as `orche report done` would. Among runner runs there is
   one run per target at a time, so "a new commit made during the run" is this unit's; a pane
   worker (`delivery: pane`, `runner.hard_in_pane`) or a person working in that worktree is not
   counted.

   ```sh
   # in the reviewer's session (here a verification feature agent), not the producer's
   orche ledger record --pr 42 --sha <head-sha> --verdict unit-test-verified \
     --evidence logs/verify-api.txt --verifier feat-my-app-review
   orche run my-app-add-a-date-filter-20261018-1   # re-run a unit by hand (--dry-run prints the argv)
   # pane mode only (delivery: pane): the worker files its own pointer
   orche report done --summary "date filter added; tests pass"
   ```

4. Main sees urgent pointers at its next turn and the rest at a drain point. Type `[drain]` to
   get everything now plus a three-line status.

Every command takes `--json`; commands that change state take `--dry-run` (`orche --help`).

## How units run

By default (config `delivery: runner`) every new brief to a division or feature agent runs as a
lean headless worker: the Stop hook spawns `orche run <unit>` detached and returns at once, and
the runner calls `claude -p --output-format json` in the unit's worktree with project settings
only (`--setting-sources project`), no MCP servers (its own empty `--mcp-config`,
`~/.claude/orche/runner/mcp/<unit>@<started_ns>.json`, written at run start and removed on every
exit path, by reconcile and by log pruning; `--dry-run` prints the placeholder path), and a
TIER-sized budget:

| TIER | Model | Max turns | Allowed tools |
|---|---|---|---|
| mechanical | `runner.model_mechanical` (default `claude-sonnet-5-5`) | `runner.max_turns` (20) | Read, Write, Edit, Glob, Grep, `ls`, `test`, `git status`, `git diff`, `git add`, `git commit`; denied (best-effort prefix rules): `git commit --amend`, `-n`, `--no-verify`, `git push` |
| standard | `claude-opus-5-5` | `runner.max_turns` (20) | the same allowed tools plus Bash (no deny list) |
| hard | `claude-opus-5-5` | 40 | the same allowed tools plus Bash (no deny list) |

A mechanical worker may commit, but the runner footer still says "do not commit, push, merge or
rebase unless the brief allows it", so it is told to commit only when the brief says so (a triage
brief does). The deny list is a best-effort first filter: prefix rules miss abbreviated or combined
flags (`git commit -am x --amen`, `-anm`, `--no-veri`). The guarantee against an amend, reset or
rebase is the done gate's ancestry check, at every tier: when the HEAD from before the run is not an
ancestor of the new HEAD (or is unknown), the pointer is `info` "unverified: STATUS: done (history
rewritten …)", never `done`. Skipping the hooks (`--no-verify`) is a documented gap no post-hoc
check sees: it is forbidden by the brief and the footer, and the ledger verifier reviews the diff.

The persona goes in as `--append-system-prompt-file` (inline `--append-system-prompt` only
for a `claude` without the file flag; the `--help` probe is cached per `claude --version`). The
worker gets Main's environment minus `CLAUDE_PROJECT_DIR`, Orca's `ORCA_TERMINAL_HANDLE` and
Claude Code's own `CLAUDE_CODE_*` session variables (authentication and mTLS variables stay). The timeout is the brief's TIMEBOX plus 60 s
(30 min when it has none, `1h30m` adds up, at most 4 h). At most `runner.max_parallel` (4) units
run per project and one per target session: a target's units share its worktree, so parallelism
is across targets (and feature agents, each with its own worktree), never inside one worktree. A
run over either limit waits up to `runner.queue_wait_s` (30 minutes), then files `info` "runner
busy: <n> live runs" (or "<target> is running <unit>"), "retry with `orche run <unit>`". Every
run ends with one pointer — a runner error or SIGTERM files `failed`, a duplicate `orche run` of
a live unit files `info` "already running (pid N, started HH:MM)" and exits 4 while the live run
still files its own. Measured on
2026-10-18 with 12 small units: pane workers cost $9.77 and took 22 minutes; runner workers cost
$3.75 (about $0.31 per unit) and took 2 minutes. `orche metrics` sums the runs it recorded.

`delivery: pane` restores the older path (the hook types `[orche] inbox: <unit> rev <N>` into the
worker's Orca pane). Main targets always use the pane, because a person reads them, and so do
`hard` units when `runner.hard_in_pane` is true. For a runner unit, `STOP <unit>` kills a live
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
`runner.model_mechanical`, `runner.max_turns`, `runner.max_parallel`, `runner.hard_in_pane`,
`runner.queue_wait_s`, `runner.log_keep_days` (14: older `logs/runs/*.log` are deleted at each
`orche run` and by `orche janitor run`; RUN.json records stay). `orche doctor` checks that
`claude` is found and supports `--setting-sources`, warns under 1 GiB free disk for
`~/.claude/orche` and over 200 MiB of run logs, warns when the process table (`ps`) cannot be
read and for every lost run whose leftovers are not swept yet, and reconciles lost runs (one
WARN row each).

**What if the runner dies.** If `orche run` itself is killed (kill -9, the OS running out of
memory, a shutdown, a power cut) or cannot write its result (a full disk), the run does not
vanish silently: ORCHE notices the dead run the next time anyone looks — the next prompt in any
of the project's sessions, the next `orche run` in that project, or `orche status` (also `depts`,
`doctor`, `metrics`, `janitor run`). It then writes `RUN.json` with `stop_reason: lost` (a
RUN.json the run already wrote is kept), files `failed` "runner died: <unit> (pid N, started
HH:MM); re-run with `orche run <unit>`" to Main, stops the claude that run left behind, and frees
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
   acknowledgement or continuation word (응, 네, 예, 그래, 좋아, 아니, 그걸로, 그렇게, 그럼; yes,
   yeah, ok, okay, sure, no, nope, go ahead, do it, that one); questions (ending in `?` or
   starting with a question word, Korean or English); and replies to a question Main just asked
   go to Main. For the last one, Main's last message is read from the transcript, its trailing
   fenced blocks (Main's ```` ```dispatch ```` block) and `STATUS:` lines are dropped, and what is
   left must end with `?` or a confirmation request ("Shall I …", "진행할까요", …).
3. **Heuristics** (no model). Only a request that is an instruction ("add", "fix", "rename", …;
   "추가해줘", "수정", "정리", …) AND names a concrete target (`docs/guide.md`, `README.md`,
   or a quoted identifier such as `` `fooBar` ``) AND has no negation ("don't", "하지 말고", …)
   is a candidate. Everything else goes to Main.
4. **Classifier** (candidates only). It returns a route, a division, a TIER, a confidence and a
   draft GOAL/ACCEPT/VERIFY/SCOPE. The backend is Jev (about 1 s) when Jev is enabled and
   configured, otherwise `claude -p` with Haiku (about 3 s measured on 2026-10-18, 5–9 s
   expected under load; capped by `triage.max_latency_s`, default 12 s). Haiku runs with no
   tools (`--tools ""`), from a fresh empty directory under the system temp dir, outside your
   home folder, so no CLAUDE.md is loaded (about 960 input tokens per call, about $0.0003), with
   the runner's filtered environment; on timeout its whole process group is killed. The prompt
   goes direct only when the route is `direct`, the confidence is at least
   `triage.min_confidence` (0.9) and the TIER is not `hard`.
5. **Action.** ORCHE writes the brief (CONTEXT "routed by triage from a Main prompt"; ACCEPT
   ends with "`git add` only the files you changed, by name (never `-A`, `-u` or `.`); exactly
   one commit with a conventional message";
   TIMEBOX 30m; FORBID "push, amend, --no-verify, files outside SCOPE") and starts the runner
   through the same path as a Main dispatch. SCOPE keeps only plain relative paths inside the
   division's worktree (letters, digits, `_`, `.`, `/` and `-` only, no leading `-`, no `/…`,
   `~…` or `..`), else `.`. VERIFY is the classifier's only when it is one `ls`, `test`,
   `git status` or `git diff` command a mechanical worker can run (`git` with plain paths and
   only `--short`, `--stat`, `--name-only`, `--name-status`, `--cached` or `--porcelain`), else
   `ls -l <scope files>` when that passes the same check, else `git status --short` (also with
   no files). The `claude --version`/`--help` probe counts against `triage.max_latency_s` (each
   call capped at 2 s); a failed probe means no classifier this time. The prompt is then blocked with "[orche] routed to <division>
   as <unit> (runner-started, TIER <t>) … (to cancel: [main] STOP <unit>)". Once the runner
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
slug is made from the prompt's first words (masked). `orche triage status` shows the backend, latency p50/p95 and the
last 20 decisions; `orche triage eval <file>` scores labelled prompts (`{"prompt": …, "label":
"direct"|"main"}` per line; `--backend heuristics` scores the no-model checks alone); `orche
metrics` has a Triage section. Config: `triage.enabled`, `triage.backend` (`auto`, `jev`,
`claude-haiku`), `triage.min_confidence`, `triage.max_latency_s`, `triage.keep_prompts`.
Triage needs `delivery: runner`, and only Main sessions are triaged.

## The organization

```
User ──(talks to Main; may open any panel)──▶ Main (per project)
                                               │ ```dispatch block → [Stop hook] → relay
   ┌───────────┬──────────┬───────────┬────────┼────────┬──────────┐
   ▼           ▼          ▼           ▼        ▼        ▼
 Liaison      R&D      Research     Build     GTM      Ops (dormant while stage=building)
                                      │
                         ┌────────────┼────────────┐
                    feature agent  feature agent  feature agent …
                    (child worktrees of Build; created by HR)
HR (one global) ── registry.json ── create · sleep · retire · rate limits · weekly audit
```

| Piece | What it does |
|---|---|
| Dispatch relay (Stop hook) | Parses `dispatch` blocks, refuses incomplete ones, writes `BRIEF.md` revisions, starts the runner (or nudges the target pane); gates `STATUS: done` claims on a ledger row |
| Runner | `orche run <unit>`: one headless `claude -p` worker per unit in its worktree; records `RUN.json`, files the pointer through the ledger gate |
| Report inject (UserPromptSubmit hook) | Injects inbox pointers at drain points; urgent ones first |
| Verification ledger | `orche ledger record/check`; verdicts `live-ui-verified`, `unit-test-verified`, `type-check-only`, `verifier-blocked`, `verifier-failed`. CI green is not a verdict |
| HR | `orche feat new/sleep/wake/retire`, rate limits, creation freeze, weekly audit, `orche hr correct` for standing orders |
| Keymaster | `orche secret …`: agents see secret names only; you paste values into a dialog |
| Janitor | `orche janitor …`: state-based cleanup; archives sessions, never deletes them |
| Jev gate | Optional fast typed-decision model for drain urgency; off by default |

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
structure: hooks relay dispatches and reports (0 Main turns), reports are batched at drain points,
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

## Documentation

- [SPEC.md](SPEC.md): the full specification (rule IDs, schemas, hook contracts)
- [docs/principles.md](docs/principles.md): the six system and ten working principles
- [docs/faq.md](docs/faq.md): common questions
- [docs/orca-sidebar.md](docs/orca-sidebar.md): how ORCHE looks in Orca
- [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

## Attribution

Several conventions are adapted from Cursor's **pstack** plugin (MIT, by Lauren Tan): the
verification ledger, the decision log, the Always-pause list and the "show me your work"
reporting conventions, among others listed at the top of [SPEC.md](SPEC.md).

## License

MIT. See [LICENSE](LICENSE).
