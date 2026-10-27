---
name: orche-apply
description: Apply ORCHE to one project on request. Use when the user names a project and asks to put it under ORCHE ("put X under ORCHE", "/orche-apply X", or the same in any language), or says `orche init` (or `/orche-apply`) with no project named, which means the project of the current directory; to choose or customise its divisions (including a custom division of their own), to add a division to an already-registered project, to change a division's model or to list a project's divisions — also when asked in plain words, in any language ("add a design division", "switch research to Sonnet", "which divisions are there?"); or asks what applying ORCHE to a project does. Never apply to every repo at once.
---

# Apply ORCHE to one project

ORCHE is installed once per machine (plugin + `orche` CLI + hooks). A project joins ORCHE only when the
user names it. `orche apply-all` is never used: some repos are dormant and some will never use ORCHE.

## What applying does (say this before doing it)

`orche init <project>` makes, for that project only:

1. Files in the project's main checkout: `.orche/project`, `PROJECT_CARD.md`, `AGENTS.md`, `.agents/`,
   `.claude/agents/div-<proj>-*.md` (untracked: every ORCHE file, `.orche/` included, goes into
   `.git/info/exclude`). Nothing is committed.
2. One Orca worktree per chosen division (`div-<division>`) with its charter/status/handoff files. The
   defaults are `rnd`, `research`, `build` and `gtm`; `ops` is opt-in and stays dormant while the stage is
   `building`. A custom division (the user's own, e.g. `design`) gets a worktree `div-<name>`, a session
   `div-<proj>-<name>` and a persona written from its one-paragraph charter. Every division starts dormant.
3. One Orca pane in the main checkout: `main-<proj>` (the user talks here). Other projects ask about this
   one with `orche ask <proj>`; a weekly automation runs `orche card refresh <proj>`. No division pane opens
   unless `--wake`.
4. A registry entry in `~/.claude/orche/registry.json` and two weekly Orca automations (printed, not
   created, unless `--automations`).
5. The project intake, last: a headless, read-only Sonnet sweep of the existing codebase (`orche intake
   <project>`; the worker has Read/Glob/Grep only, no shell) that writes `.orche/div/<division>/CONTEXT.md`
   per division (what that division needs to know; appended to its units' briefs as fenced reference
   data), the empty sections of `PROJECT_CARD.md` and `.orche/intake.json`. Usually well under a dollar
   and a minute, up to 9 minutes on a large repo (`intake.timeout_s`, 540); its line shows the cost. If it fails, init still
   succeeds and says `intake skipped: …`. Skip it with `--no-intake`; re-run it any time with
   `orche intake <project>` (`--if-stale` only when 50+ commits or 14+ days passed).

Units run headless through the runner (`orche run`), so no division pane is needed for work to happen;
results arrive as pointers in Main's inbox.

## Bare `orche init` (no project named)

When the user says just `orche init` (or `/orche-apply`) and names no project, the project is the git
repository holding the current directory, with the defaults. Run `orche init` at once, with no argument:
no checklist, no dry-run, no question. Pass timeout 600000 (the intake runs last). It prints
`Main is open in the main-<proj> tab with the setup summary; from here on, talk to Main there.`
and closes this window, as in step 6 below; init writes `.orche/SETUP.md` (the setup report) and Main reports it.
Only when it printed `could not close the window that ran init` or `could not switch`, hand over as step 6 says
and stop. If it fails with `not inside a git repository`, ask for the project path.
A path or a project name means that project.

## In plain words (choices)

When the user asks in plain words, in any language (match the intent, not the wording), to set up ORCHE, add a
division, change a model or see the divisions, do it in this session with the choice dialog (AskUserQuestion).
Never hand it to the `hr` agent: a subagent cannot ask.

1. Run `orche hr choices <proj> --json` (no `<proj>` for a project not under ORCHE yet); its texts are English:
   ask them in the user's language, translating the labels and descriptions yourself. Ask only what the request leaves open: a division or a model the user named is not asked again.
2. Ask with the `ask` entries as given: each entry's `tool` is one element of AskUserQuestion's `questions` (up to
   four per call); map each answer through the entry's `values` (by the option it picked, never through label text). Text
   typed instead ("Other") is the user's own words: a name, a model id, a cancel.
3. Run the `orche` command yourself; never ask the user to type one (only owner steps such as `codex login`).
4. No answer (the dialog is missing or fails, an empty answer, a question that auto-continued): a model falls back
   to the recommendation, said in one line (`orche hr model` changes it later); never create a division or a project
   without an answer: ask the same question as numbered plain text and end with `OWNER: <question> — default:
   <recommendation>` in the user's language. Esc or a cancel in any language: say it was cancelled; nothing is written.
5. Reply with one result line (what was made or changed and its model; a `codex` division runs headless only) and
   the closing line: `RUNNING:` while the background intake runs, else `DONE:`.

- Add a division: none named → `ask.add`; for each picked kind `ask.model.<kind>` (one call), then
  `orche hr add <proj> <kind> --model <model>` each. A new name (`*`, or typed): propose an English slug
  (`custom.rules`), an English label and two charters, A and B, in the user's language from their words; in one
  call ask, in the user's language, whether to write the ‘<name>’ division's (<slug>) charter this way (header "the
  work", options "A (recommended)" and "B", the charters as descriptions) together with `ask.model["*"]`; then `orche hr add <proj> <slug> --label <label>
  --charter "<charter>" --model <model>`. A picked kind that is not standard and has `charter: null` gets the same
  charter question (its slug and label fixed). No `--dry-run` first: the questions showed what it makes, and `hr add`
  checks everything before it writes.
- Change a model: none named → `ask.change`; then `ask.model.<division>`; then `orche hr model <proj> <division>
  <model>` (a `null` value, the keep option, changes nothing). Say in plain words when a pane must restart to use it; restart one
  (`orche sleep`, then `orche wake`) only when the user asks and nothing runs there. A feature agent runs on its
  division's model: offer the division.
- Pull requests and branches: `orche init` and `orche update` already read them from the repository (merged PRs'
  base, `dev/des-47-…` branches, Entire); the command is for overriding. The user names the branch PRs go to
  (`develop`), the word branches start with (`dev`) or the issue team (`DES`) → `orche init --refresh <proj> --base
  <branch> --branch-prefix <prefix> --issues <KEY>` (any flag alone); no question unless the value is missing. The
  setup report then names them.
- Which divisions: from the same JSON, one line per existing division (`<label> (<key>) — <desc> · <model label>
  · <status_text>`) and one line naming the kinds that can be added; no question.
- First setup: step 1 below and `orche hr choices --json`; ask `ask.setup`; a divisions value
  `"pick"` → `ask.pick`; a models value `"pick"` → `ask.model.<kind>` per chosen division (four per call); write the
  setup file with `divisions`, `custom` (a picked `design`/`content` as `{}`) and `models` (none for `"recommended"`,
  the picked model id for every division) (step 3); preview (step 4), then ask, in the user's language, whether to make
  it as shown (the preview's `Summary:` line in the user's words, one line), header "confirm", options "make it
  (recommended)" — "ends by reading the project (intake): about a minute, under $1" and "change it" — "write what to
  change"; on make it, apply (step 5). The literal `orche init` keeps the no-question path above.

## Procedure (choosing divisions)

Use this only when the user names divisions, a custom division, a stage or aliases, or asks to choose or
customise. In chat there is no terminal, so the setup goes through a JSON file. (A user at a terminal can run
`orche init <project>` themselves and get the same choices as an interactive checklist.)

1. Resolve the project and read its setup: `orche init --print-setup <name>` accepts the Orca display
   name, repo id, path or a registry alias, and prints `{"divisions", "custom", "stage", "aliases"}` — the
   defaults for a new project, what it has for a registered one. A path to a git repository that Orca
   does not list yet also gets the defaults: `orche init` adds it to Orca (`orca repo add`) before it
   onboards, so do not ask the user to add it in Orca first. If the name is ambiguous or unknown (not a
   git path), list candidates with `orca repo list` and ask.
2. Ask with the choice dialog (In plain words, first setup). Without it, show the setup as a checklist, defaults
   filled in, and ask one question:
   ```
   Divisions for <proj>
    [x] rnd       R&D — prototypes and technical spikes
    [x] research  Research — market, users, competitors
    [x] build     Build — integrates features, owns the ledger, opens PRs
    [x] gtm       GTM — launch, content, growth
    [ ] ops       Ops — deploy and operate (dormant until stage public)
   Stage: building · Aliases: (none)
   Use the defaults? Tell me which divisions to drop or add, or a new one to create.
   ```
   Map the answer: "defaults" keeps it; "without ops" / "with ops" / "without research" toggles; "add a design division:
   <what it does>" adds a custom division — name = a slug (2-16 chars, a-z first, then a-z or 0-9 with
   single hyphens between parts; not a standard name, not main|liaison|feat|hr|jev; its session
   `div-<proj>-<name>` must not belong to another project), label ≤ 24 chars and not a standard
   division's name or label (`Ops`, `GTM`, …), charter = the user's words as one paragraph ≤ 400 chars
   (no control characters), model `claude-opus-5-5` and effort `high` unless the user says otherwise.
   Stage `building` (default) or `public`; aliases are the short names the user types in `[project]` tags.
   Ask only for what is missing, each with a default. Wake nothing.
3. Write the answer to a temp file outside the repo (e.g. `$(mktemp -d)/setup.json`):
   `{"divisions": ["build", "gtm"], "custom": {"design": {"label": "Design", "charter": "..."}},
   "stage": "building", "aliases": ["dw"]}`.
4. Preview: `orche --dry-run init <name> --setup-file <path>`. Show the user its `Summary:` line (divisions,
   stage, worktree count, panes) and the files, worktrees and panes it would create. Wait for a yes.
5. Apply with one call: `orche init <name> --setup-file <path>` (the same command as the preview,
   without `--dry-run`). Pass timeout 600000 — the intake can take up to 9 minutes (it runs last and
   prints nothing until it ends). Init reports the intake's one-line result to Main (step 6), e.g.
   `intake <proj>: 4 divisions, $0.42, 38 s (claude-sonnet-5-5)` or `intake skipped: …` (`orche intake <proj>`
   re-runs it). `orche status --project <proj>` (the project with its division names, custom ones included,
   `○` = dormant) and `orche depts <proj>` (one row per division) show the result later.
   Only the status-line cell uses the label (`Design` → `DES○`).
6. `orche init` ends by writing the setup report `.orche/SETUP.md` ("ORCHE is set up": what was made and what
   to switch on any time), switching Orca to the `main-<proj>` tab, sending Main the setup result with the
   report (Main reports it to the owner and asks for the first request)
   and closing this window: you do not get to answer, and nothing is lost. Only when init printed `could not close the window that ran init` or `could not
   switch`: hand over in at most four lines (registered, the intake line, the `setup report:` path it printed,
   `orche wake main-<proj>` if needed, close this window and talk to Main there) and stop.

## Later changes

- Add a division: `orche hr add <proj> <division> [--model <m>] [--label <L> --charter <C>]` (a standard kind,
  `design` or `content` needs no label or charter; ask first, In plain words). Several at once also work through
  the setup file: print the setup (`orche init --print-setup <proj>`), add the standard name to
  `divisions` or the new entry to `custom`, preview and apply with `--setup-file` as above (or
  `orche init <proj> --divisions <list> --custom name=label:charter`; `--custom` splits on `:`, so a charter
  or model id containing `:` goes in the setup file). Only the new division's worktree and
  files are created; existing ones, and the registry entries, are left as they are.
- Change a division's model: `orche hr model <proj> <division> <model>` (a Claude id, `codex` or `codex:<id>`); ask
  first, In plain words.
- Removing a division, or changing a custom division's definition, is out of scope for `orche init` and `orche hr`: sleep
  it (`orche sleep div-<proj>-<division>`) or remove it by hand.
- Wake / sleep a division: `orche wake|sleep div-<proj>-<division>`; a head's workers:
  `orche feat new|sleep|wake|retire` (or ask the `hr` agent).
- Leave ORCHE: retire the feature agents (`orche feat retire … --yes`), remove the division worktrees with
  `orca worktree rm`, delete the project's ORCHE files from the main checkout and its registry entry; the
  user decides this, never a hook or an agent.

## Do not

- Do not run `orche apply-all`, and do not apply to a project the user did not name in this request (a
  bare `orche init` names the repository holding the current directory).
- Do not wake divisions or create feature agents during apply unless asked.
- Do not commit the generated files; the user decides whether they belong in the repo.
- Do not act as Main: never set `ORCHE_SESSION`, never run `orche dispatch`, never survey or change the
  repository, never add standing orders; after the handover, stop.
