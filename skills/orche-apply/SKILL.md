---
name: orche-apply
description: Apply ORCHE to one project on request. Use when the user names a project and asks to put it under ORCHE ("X에 ORCHE 적용해줘", "/orche-apply X"), to choose or customise its divisions (including a custom division of their own), to add a division to an already-registered project, or asks what applying ORCHE to a project does. Never apply to every repo at once.
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
3. Two Orca panes in the main checkout: `main-<proj>` (the user talks here) and `liaison-<proj>` (answers
   other projects' questions, keeps PROJECT_CARD.md fresh). No division pane opens unless `--wake`.
4. A registry entry in `~/.claude/orche/registry.json` and two weekly Orca automations (printed, not
   created, unless `--automations`).

Units run headless through the runner (`orche run`), so no division pane is needed for work to happen;
results arrive as pointers in Main's inbox.

## Procedure

In chat there is no terminal, so the setup goes through a JSON file. (A user at a terminal can run
`orche init <project>` themselves and get the same choices as an interactive checklist.)

1. Resolve the project and read its setup: `orche init --print-setup <name>` accepts the Orca display
   name, repo id, path or a registry alias, and prints `{"divisions", "custom", "stage", "aliases"}` — the
   defaults for a new project, what it has for a registered one. If the name is ambiguous or unknown, list
   candidates with `orca repo list` and ask.
2. Show the setup as a checklist, defaults filled in, and ask one question:
   ```
   Divisions for <proj>
    [x] rnd       R&D — prototypes and technical spikes
    [x] research  Research — market, users, competitors
    [x] build     Build — integrates features, owns the ledger, opens PRs
    [x] gtm       GTM — launch, content, growth
    [ ] ops       Ops — deploy and operate (dormant until stage public)
   Stage: building · Aliases: (none)
   기본으로 할까요? 빼거나 넣을 부서, 추가할 부서가 있으면 알려주세요.
   ```
   Map the answer: "기본으로" keeps it; "ops 빼고" / "ops 넣어서" / "research 빼고" toggles; "design 부서 추가:
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
5. Apply: the same command without `--dry-run`. Verify with `orche status <proj>` (the project appears with
   its division names, custom ones included, `○` = dormant) and `orche depts <proj>` (one row per division,
   also by name). Only the status-line cell uses the label (`Design` → `DES○`).
6. Hand over in three lines: where the Main pane is (Orca tab `main-<proj>`), that the first request goes
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

- Do not run `orche apply-all`, and do not apply to a project the user did not name in this request.
- Do not wake divisions or create feature agents during apply unless asked.
- Do not commit the generated files; the user decides whether they belong in the repo.
