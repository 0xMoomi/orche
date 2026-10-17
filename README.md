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

`orche doctor` checks the result (one OK/WARN/FAIL line each). `orche install` backs up every
file before changing it (`<file>.bak-<ts>`) and records the original values in
`~/.claude/orche/install-state.json`; `orche uninstall` restores them. Use
`orche install --mode settings` instead of the plugin if you want the hooks registered directly
in `~/.claude/settings.json`. `orche install` also patches two operator-specific files when they
are present (`~/.claude/fable/*` and `~/.codex/AGENTS.md`) and skips them when they are absent;
`orche install --dry-run` previews every change, these included.

## Five-minute quick start

1. Onboard a repository that Orca already knows (display name, id or path). This creates the five
   division worktrees, opens `main-<proj>` and `liaison-<proj>` panes in the main checkout, and
   starts Build and GTM; the other divisions stay `dormant` (no pane, no cost):

   ```sh
   orche init my-app --wake build,gtm
   orche depts my-app        # divisions and feature agents with status, session, branch, idle days
   orche status              # one line per project
   ```

2. Talk to `main-my-app` in plain language. Main echoes what it understood, gets a plan
   confirmed, and ends its reply with a dispatch block. The Stop hook writes the brief to
   `.orche/units/<unit>/BRIEF.md` and nudges the target pane; Main spends no turn on delivery:

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

3. The worker does the job and runs VERIFY; it never records its own verdict (writer ≠ judge).
   A verification unit or reviewer — a different session from the one that wrote the code —
   records the verdict, and then the worker files one pointer (`done` from Build or a feature
   agent needs that ledger row):

   ```sh
   # in the reviewer's session (here a verification feature agent), not the producer's
   orche ledger record --pr 42 --sha <head-sha> --verdict unit-test-verified \
     --evidence logs/verify-api.txt --verifier feat-my-app-review
   # back in the worker's session
   orche report done --summary "date filter landed; PR 42; unit-test-verified"
   ```

4. Main sees urgent pointers at its next turn and the rest at a drain point. Type `[drain]` to
   get everything now plus a three-line status.

Every command takes `--json`; commands that change state take `--dry-run` (`orche --help`).

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
| Dispatch relay (Stop hook) | Parses `dispatch` blocks, refuses incomplete ones, writes `BRIEF.md` revisions, nudges the target pane; gates `STATUS: done` claims on a ledger row |
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
