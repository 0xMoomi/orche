---
name: orche-apply
description: Apply ORCHE to one project on request. Use when the user names a project and asks to put it under ORCHE ("put X under ORCHE", "/orche-apply X"), or says `orche init` (or `/orche-apply`) with no project named, which means the project of the current directory; to choose or customise its divisions (including a custom division of their own), to add a division to an already-registered project, or asks what applying ORCHE to a project does. Never apply to every repo at once.
---

# Apply ORCHE to one project

ORCHE is installed once per machine (plugin + `orche` CLI + hooks). A project joins ORCHE only when the
user names it. `orche apply-all` is never used: some repos are dormant and some will never use ORCHE.

## What applying does (say this before doing it)

`orche init <project>` makes, for that project only:

1. Files in the project's main checkout: `.orche/project`, `PROJECT_CARD.md`, `AGENTS.md`, `.agents/`,
   `.claude/agents/div-<proj>-*.md` (untracked; `.orche/units/` and `.claude/settings.local.json` go into
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
no checklist, no dry-run, no question. Pass timeout 600000 (the intake runs last). Then report three
lines from its output: the `Summary:` line, the intake's one-line result (or `intake skipped: …`), and
the last line, `Main is open in the main-<proj> tab; type a request there.` If it fails with `not inside
a git repository`, ask for the project path. A path or a project name means that project.

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
2. Show the setup as a checklist, defaults filled in, and ask one question:
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
   prints nothing until it ends). Tell the user the intake's one-line result, e.g.
   `intake <proj>: 4 divisions, $0.42, 38 s (claude-sonnet-5-5)`, or the `intake skipped: …` line and
   that `orche intake <proj>` can be re-run. Verify with `orche status <proj>` (the project appears with
   its division names, custom ones included, `○` = dormant) and `orche depts <proj>` (one row per
   division, also by name). Only the status-line cell uses the label (`Design` → `DES○`).
6. Hand over in three lines: Orca has switched to the Main tab (`main-<proj>`; if init warned that it could
   not switch, `orche wake main-<proj>` opens or switches to it), that the user types the first request
   there in plain language (Main echoes `Understood: source = <proj>, output = <proj>` and ends with a
   dispatch block), and that `[drain]` shows finished work.

## Later changes

- Add a division: print the setup (`orche init --print-setup <proj>`), add the standard name to
  `divisions` or the new entry to `custom`, preview and apply with `--setup-file` as above (or
  `orche init <proj> --divisions <list> --custom name=label:charter`; `--custom` splits on `:`, so a charter
  or model id containing `:` goes in the setup file). Only the new division's worktree and
  files are created; existing ones, and the registry entries, are left as they are.
- Removing a division, or changing a custom division's definition, is out of scope for `orche init`: sleep
  it (`orche sleep div-<proj>-<division>`) or remove it by hand.
- Wake / sleep a division: `orche wake|sleep div-<proj>-<division>`; feature agents under Build:
  `orche feat new|sleep|wake|retire` (or ask the `hr` agent).
- Leave ORCHE: retire the feature agents (`orche feat retire … --yes`), remove the division worktrees with
  `orca worktree rm`, delete the project's ORCHE files from the main checkout and its registry entry; the
  user decides this, never a hook or an agent.

## Do not

- Do not run `orche apply-all`, and do not apply to a project the user did not name in this request (a
  bare `orche init` names the repository holding the current directory).
- Do not wake divisions or create feature agents during apply unless asked.
- Do not commit the generated files; the user decides whether they belong in the repo.
