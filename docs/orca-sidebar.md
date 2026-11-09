# Orche in the Orca sidebar

Orca shows **repo → worktree card → agents** (terminal panes). Orche maps its roles onto that
hierarchy with fixed names, so you can see the organization at a glance without asking a model.

## Layout

```
my-app                                   (Orca repo)
 └─ main checkout                        1 pane: main-my-app
     ├─ staff                            staff-my-app         (the CEO Staff, read-only)
     ├─ div-rnd                          div-my-app-rnd       (branch div/rnd)
     ├─ div-research                     div-my-app-research  (branch div/research)
     ├─ div-build                        div-my-app-build     (branch div/build)
     │   ├─ feat-pages                   feat-my-app-pages    (branch feat/pages)
     │   └─ feat-billing                 feat-my-app-billing  (branch feat/billing)
     ├─ div-gtm                          div-my-app-gtm       (branch div/gtm)
     └─ div-ops                          div-my-app-ops       (branch div/ops)
```

| Item | Name | Where |
|---|---|---|
| Main pane | `main-<proj>` | the project's main checkout |
| CEO Staff card / pane | worktree `staff`, pane `staff-<proj>` | its own worktree, a child of the main checkout's row, created the first time the project's Staff opens; never worked in (Staff reads the main checkout) |
| Division card / pane | worktree `div-<division>`, pane `div-<proj>-<division>` | its own worktree, branch `div/<division>`, a child of the main checkout's row |
| Feature card / pane | worktree `feat-<feature>`, pane `feat-<proj>-<feature>` | child worktree of its head's division, branch: the branch Orca names by default; `<prefix>/<feature>` when the project sets `publish.per_project.<p>.branch_prefix` (Orche renames it right after creation) |

Pane titles equal Claude Code session names. Orche starts a division or feature pane with
`PATH=<plugin>/bin:$PATH ORCHE_SESSION=<session> claude --permission-mode bypassPermissions --name <session>`
(or `--resume <session id>` on wake), and the Main pane with the same command plus
`--model claude-opus-5-5 --effort xhigh` (since 0.6.0 Main also starts in bypass, like every pane), so hooks know the pane's role and `orche` is on the pane's PATH. The Staff pane runs the same command with
`--model claude-opus-5-5 --effort medium`. `orche init` creates the division worktrees; `orche feat new` creates
feature worktrees.

## Lineage

Main's row holds the project's CEO Staff and divisions, and each division's row holds its workers. The
Staff's and a division's worktree are created with **the project's main worktree as their Orca parent**
(the checkout where Main's tab lives), and a feature's with **its head's worktree as its Orca parent**.
So the Staff and the divisions nest under Main and the workers under `div-<division>`: you talk only to
Main (and ask its Staff everyday questions), and everything a worker produces is reviewed and queued by its head.

The fold arrow on Main's row hides its Staff, its divisions and their workers; click it again to show them.
Orca remembers which rows are folded, across restarts too, so a project you folded stays folded
until you open it.

A division made by an older Orche sits beside Main. `orche update` (and
`orche init <project> --refresh`) moves each division that has no Orca parent under Main
(`orca worktree set --parent-worktree`). A division you placed under another row stays where you
put it.

## Unit states

| Status | In the sidebar | Meaning |
|---|---|---|
| `active` | card with a running pane | awake and working |
| `dormant` | card, no pane; a division's card carries the resting note | onboarded but never started; costs nothing. Ops stays here while the stage is `building` |
| `paused` | card, no pane; a division's card carries the resting note | put to sleep (`orche sleep`, or the HR audit after 14 idle days); `orche wake` resumes the same session |
| `onboarding` | card, maybe no pane | the worktree exists but setup did not finish; re-run `orche init` or `orche feat new` to resume |
| `retired` | gone | worktree removed with `orca worktree rm`; memory files and transcripts archived under `~/.claude/orche/archive/<session>/` |

**Resting note.** When Orche creates a division dormant or puts one to sleep, it writes
`Resting: starts when Main hands it work` as the workspace's Orca note (`orca worktree set --comment`),
and clears it when the division wakes. Orca's default card shows a note as a notes icon in the row
under the card's name, and its text under Notes when you hover the card; compact cards show it only
when Notes is on in Show properties. Clicking a resting card opens a plain shell tab there (Orca's
`Terminal 1`), not an agent: work Main hands the division runs in the background in its workspace,
and its agent tab opens when it staffs workers or when you say "wake <division>". Orche writes the
note only on a card whose note is empty and clears only its own text, so a note you wrote stays. A
division that was already resting gets the note at its next sleep.

A new feature agent's card gets Orca's **unread dot**. So does Main's workspace whenever a turn Orche
started there ends (a wake or a delivered report), and it stays until you open it: Orca marks a turn
only when nobody viewed the pane, and clears the mark when the workspace is on screen in a focused Orca.
A turn you typed is left to Orca. `orche depts` prints the same table in the terminal.

## Status line

`orche install` adds an Orche segment to `~/.claude/statusline-command.sh`:

```
⚠ waiting 1 · ▶ running 3 · BUILD 3/8 · RS ● · R&D ○ GTM ○ DES ○ · 🔑1 · $10.75 today
```

Most important first, in the owner's language (Korean or English, as they write to Main) and in
colour:

| Token | Meaning |
|---|---|
| `⚠ waiting N` | yellow: the owner's open ask plus the units blocked on the owner |
| `▶ running N` | green bold: live headless runs of the project |
| `BUILD 3/8` | a division with active workers: 3 of its 8 workers running now |
| `RS ●` / `GTM ◐` | an awake division without workers / one still onboarding |
| `R&D ○ GTM ○` | the asleep divisions (dormant, paused, retired), dimmed, in one group |
| `DES` | a custom division: the first three letters of its label (four or five, then a digit, when that cell is taken; one cell per division the project has) |
| `🔑N` | pending secret requests |
| `$10.75 today` | today's spend |
| `PAUSED …` | the whole line, red, while a spend cap or a pause holds |

Zero counters are omitted. The snippet walks up from the session directory to the first
`.orche/project` file (one line: the project key, written by `orche init`) and prints the cached
`~/.claude/orche/status/<proj>.line` using shell builtins only. `orche feat` and `orche stage`
refresh that file; run `orche statusline` inside the project to refresh it by hand (for example
right after `orche init`). Directories outside a project
show nothing.
