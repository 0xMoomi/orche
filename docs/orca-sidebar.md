# ORCHE in the Orca sidebar

Orca shows **repo → worktree card → agents** (terminal panes). ORCHE maps its roles onto that
hierarchy with fixed names, so you can see the organization at a glance without asking a model.

## Layout

```
my-app                                   (Orca repo)
 ├─ main checkout                        2 panes: main-my-app, liaison-my-app
 ├─ div-rnd                              div-my-app-rnd       (branch div/rnd)
 ├─ div-research                         div-my-app-research  (branch div/research)
 ├─ div-build                            div-my-app-build     (branch div/build)
 │   ├─ feat-pages                       feat-my-app-pages    (branch feat/pages)
 │   └─ feat-billing                     feat-my-app-billing  (branch feat/billing)
 ├─ div-gtm                              div-my-app-gtm       (branch div/gtm)
 └─ div-ops                              div-my-app-ops       (branch div/ops)
```

| Item | Name | Where |
|---|---|---|
| Main pane | `main-<proj>` | the project's main checkout |
| Liaison pane | `liaison-<proj>` | the same main checkout, separate pane |
| Division card / pane | worktree `div-<division>`, pane `div-<proj>-<division>` | its own worktree, branch `div/<division>` |
| Feature card / pane | worktree `feat-<feature>`, pane `feat-<proj>-<feature>` | child worktree of Build, branch `feat/<feature>` |

Pane titles equal Claude Code session names. ORCHE starts each pane with
`PATH=<plugin>/bin:$PATH ORCHE_SESSION=<session> claude --name <session>` (or `--resume <session>`
on wake), so hooks know the pane's role and `orche` is on the pane's PATH. `orche init` creates the division worktrees; `orche feat new` creates
feature worktrees.

## Lineage

Division worktrees have no Orca parent. Feature worktrees are created with the **Build worktree
as their Orca parent**, so they nest under `div-build` in the sidebar: everything a feature agent
produces is integrated and verified by Build.

## Unit states

| Status | In the sidebar | Meaning |
|---|---|---|
| `active` | card with a running pane | awake and working |
| `dormant` | card, no pane | onboarded but never started; costs nothing. Ops stays here while the stage is `building` |
| `paused` | card, no pane | put to sleep (`orche sleep`, or the HR audit after 14 idle days); `orche wake` resumes the same session |
| `onboarding` | card, maybe no pane | the worktree exists but setup did not finish; re-run `orche init` or `orche feat new` to resume |
| `retired` | gone | worktree removed with `orca worktree rm`; memory files and transcripts archived under `~/.claude/orche/archive/<session>/` |

A new feature agent's card gets Orca's **unread dot**. `orche depts` prints the same table in the
terminal.

## Status line

`orche install` adds an ORCHE segment to `~/.claude/statusline-command.sh`:

```
R&D● RS○ BUILD●3 GTM● OPS○ · new1 · 🔑2 · unverified1
```

| Token | Meaning |
|---|---|
| `●` / `○` / `◐` | division active / asleep (dormant, paused, retired) / onboarding |
| `BUILD●N` | N active feature agents |
| `DES○` | a custom division: the first three letters of its label (four or five, then a digit, when that cell is taken; one cell per division the project has) |
| `newN` | feature agents created in the last 24 hours |
| `🔑N` | pending secret requests |
| `unverifiedN` | `done` reports without a ledger row |

Zero counters are omitted. The snippet walks up from the session directory to the first
`.orche/project` file (one line: the project key, written by `orche init`) and prints the cached
`~/.claude/orche/status/<proj>.line` using shell builtins only. `orche feat` and `orche stage`
refresh that file; run `orche statusline` inside the project to refresh it by hand (for example
right after `orche init`). Directories outside a project
show nothing.
