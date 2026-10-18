# Changelog

All notable changes to ORCHE are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic versioning.

## 0.3.1 — 2026-10-18

Jev onboarding: set Jev up once, or skip it and everything works without it.

### Added

- **First-run Jev question.** `orche install` in a terminal asks once for an optional Jev key
  (Enter skips; on macOS a native hidden-answer dialog, else a hidden terminal prompt) and the key
  type (Vercel AI Gateway, the default, or TypeSafe direct), sends one tiny probe, and only when it
  answers stores the key and turns Jev on ("Jev on (gateway, 0.4 s)"). A failed probe or key store
  changes nothing (no key stored, config.json byte-for-byte as before) and never fails the
  install; Ctrl-D or Ctrl-C at the prompt skips Jev like Enter. Without a terminal nothing is
  asked and stdin is never read. `--no-jev` skips the question; `--jev-key-stdin --jev-kind
  gateway|direct` configures without prompts (`--jev-kind` alone answers the key-type question).
- **`orche jev setup`, `orche jev probe`, `orche jev key set|rm|status`.** The key lives in the
  macOS keychain (service `orche`, account `JEV_API_KEY`; written over stdin, never in argv) or,
  elsewhere, `~/.claude/orche/secrets/jev.key` (0600). The Jev client resolves the env var
  `JEV_API_KEY` first, then the stored key. `key status` says only where the key is. `key set`
  reads stdin only with `--stdin`; with no dialog and no terminal it refuses ("no terminal: use
  the dialog, or --stdin from a trusted script"). The key is sent only to an `https` endpoint and
  a redirect is an error, never followed. `orche uninstall` keeps the key and says how to delete it.
- **`orche doctor` `jev` row**: OK with the kind and key source, otherwise WARN, never FAIL (Jev is
  optional): not configured (Haiku fallback), disabled, or "last probe failed <masked reason>;
  ORCHE works without Jev; run `orche jev probe`" (no network call). `orche jev disable` forgets
  the failed probe.
- **Env-read gate** (`lib/orche/jevgate.py`): agents may not read a keychain value (`security
  find-generic-password|find-internet-password` with `-w`/`-g`, `dump-keychain -d`, `security -i`,
  also behind wrappers such as `timeout`, `/usr/bin/time`, `sudo`, `env -i`, `stdbuf`, `xargs` and
  inside `bash -c`, `eval`, `$( )`), name the key file (the ORCHE_HOME path anywhere in the text,
  normalised; `secrets/jev.key` as a command word or redirect), read anything in the secrets
  folder (`cat`, `head`, `cp`, `< file`, …), search ORCHE_HOME or the secrets folder, run inline
  interpreter code (`python -c`, heredocs, `node -e`, `perl -e`, `osascript -e`, piped code) that
  names the key file, shells out to `security … -w` or calls orche's key API, or hand a key to
  `orche` through `--stdin`, `--key-stdin` or `--jev-key-stdin`. Commit messages, search patterns
  and Python comment lines that only mention these pass. A command too nested to check is refused.
  ORCHE never prints or logs the key; the gate refuses agents' normal read paths, but it is not a
  sandbox: inline interpreter code is checked as text, a script file that reads the key is not,
  and the keychain's access control and the OS remain the boundary.
- **Setup rollback**: if config.json cannot be written after the key was stored, config.json is put
  back and the previous key restored (or the new one removed); the message says which key is
  stored if that fails. A config.json that is not valid JSON or not UTF-8 skips Jev with one line
  ("config.json unreadable (…): Jev skipped, nothing changed; …"): install exit 0, `jev setup` exit 1.

### Fixed

- **Jev wire shapes** now follow the vendor documentation (docs.typesafe.ai/api and Vercel AI
  Gateway's TypeSafe-compatible API): questions are a map keyed by id with `instructions` and
  `criteria`; answers are a map with `noul`, `choice` + `confidence`, or `score` (0-based) +
  `confidence`. The earlier list-based shapes were never called live and would have been
  rejected. Gateway requests go to `https://ai-gateway.vercel.sh/typesafe/v1/systemone` with
  model `typesafe-ai/jev`.

## 0.3.0 — 2026-10-18

Setup: choose and customise divisions. Triage: simple Main prompts go straight to a division.

### Added

- **Triage of Main prompts** (off by default; `orche triage enable`). The Main pane's prompt hook
  decides whether a prompt needs orchestration before Main spends a turn: tags (`[main]`,
  `[direct]`, `[build]`, `[research]`, `[rnd]`, `[gtm]`, `[ops]`; the whole first token),
  bypasses (short prompts, an acknowledgement or continuation word first such as "응", "그걸로",
  "yes", "go ahead", questions, replies to a question Main asked — Main's trailing ```` ```dispatch ````
  block and `STATUS:` lines are ignored), heuristics (an instruction naming a file or identifier,
  with no negation) and, for candidates only, a classifier (Jev, or `claude -p` with Haiku, no
  tools, from a fresh temp dir outside `$HOME` so no CLAUDE.md loads — about 960 input tokens and
  $0.0003 per call — with the runner's filtered environment, its process group killed on timeout;
  the `claude --version`/`--help` probe counts against `triage.max_latency_s`, each call capped
  at 2 s, and a failed probe means no classifier this time). A trailing ```` ```dispatch ```` block
  holding a fenced snippet is still stripped whole.
  A direct decision becomes a brief relayed like a Main dispatch (`by=triage`, runner-started;
  SCOPE only plain relative paths inside the worktree (no spaces, shell characters or leading
  `-`), VERIFY only a command a mechanical worker can run (`git` with read-only options and plain
  paths; the `ls -l` fallback is checked too, else `git status --short`), FORBID "push, amend,
  --no-verify, files outside SCOPE", ACCEPT asks for exactly one commit that `git add`s only the
  changed files by name, never `-A`, `-u` or `.`) and the prompt is blocked with "[orche] routed to <division> as <unit> … (to
  cancel: [main] STOP <unit>)"; once the runner started the block is final. Anything else reaches
  Main unchanged. Config `triage.enabled|backend|min_confidence|max_latency_s|keep_prompts`.
- `orche triage enable|disable|status` and `orche triage eval <file>` (precision/recall of
  `direct` on labelled prompts); a Triage section in `orche metrics`; decisions in
  `~/.claude/orche/triage/samples.jsonl` (hashed prompts unless `triage.keep_prompts`; a routed
  prompt's row also carries its unit id, whose slug comes from the prompt's first words, masked).

- **Division setup in `orche init`.** On a terminal (no `--divisions`, `--custom` or `--setup-file`,
  no `--json`) `orche init <project>` shows a plain-stdin checklist of the standard divisions —
  toggle by number, `a` adds a custom division, then stage, aliases, a `Summary:` line and
  `Apply? [Y/n]`. Defaults: every standard division except `ops`, nothing woken. `n` or end of input
  aborts with exit 0 and nothing written; `--dry-run` runs the checklist and prints the plan.
- **Custom divisions.** A project can have its own divisions (`design`, `qa`, …): a slug name, a label,
  a one-paragraph charter, a model (default `claude-opus-5-5`) and an effort (default `high`). Each gets
  a session `div-<proj>-<name>`, a worktree `div-<name>`, unit files under `.orche/div/<name>/` and a
  persona rendered from the new generic `templates/personas/div-custom.md` (charter, guards,
  Always-pause, the runner note). Registry: optional per-project `custom_divisions`.
- `orche init --custom name=label:charter[:model[:effort]]` (repeatable) and `--setup-file <json>`
  (`{"divisions", "custom", "stage", "aliases"}`); `orche init --print-setup <project>` prints the
  current setup (the defaults for an unregistered repo) in that JSON.
- `/orche-apply` asks the same questions in chat, writes a temp `setup.json`, previews with
  `orche --dry-run init <project> --setup-file …` and applies on a yes.

### Changed

- **Mechanical units may commit.** TIER `mechanical` workers get `git add` and `git commit`, with
  `git commit --amend`, `-n`, `--no-verify` and `git push` denied (`--disallowedTools`, a
  best-effort filter: abbreviated or combined flags such as `--amen` or `-anm` pass it); the runner
  footer still forbids committing unless the brief allows it. The guarantee against an amend, reset
  or rebase is the done gate's ancestry check, at every tier: when the HEAD from before the run is
  not an ancestor of the new HEAD (or is unknown), the pointer is `info` "unverified: STATUS: done
  (history rewritten …)", never `done`. `--no-verify` remains a documented gap, covered by the
  brief text and the ledger verifier's review of the diff. A Build unit that committed reports
  `info` "unverified: STATUS: done (no ledger row for <sha7>)" until a verifier records the row,
  instead of "(no commit)" with the change left uncommitted in the shared Build worktree.
- Without a terminal and without flags, `orche init` onboards the setup defaults (no `ops`) instead of
  all five divisions; a registered project keeps its own setup. Pass
  `--divisions rnd,research,build,gtm,ops` for the old set. `orche apply-all` still onboards all five.
- Re-running `orche init` adds only new divisions. It never removes one, never changes a custom
  division's definition (refused) and never changes a registered project's stage or aliases (warned).
- `orche status`, `orche depts`, the status line and the dispatch `TO <division>` shorthand list the
  project's own divisions (`registry.divisions_of`): standard ones it onboarded, then custom ones. The
  status line no longer shows placeholder cells for standard divisions a project never onboarded; a
  custom division's cell is its label's first three letters (`DES○`), or four or five, then a digit,
  when that cell is taken, so cells are unique within a project and never equal a standard cell.
- Setup input rules: a custom name is a slug of 2-16 characters with single hyphens between parts
  (`ab-` and `a--b` are refused); a label may not equal a standard division's name or label (`Ops`,
  `GTM`); labels and model ids refuse line breaks; a single line break in a charter is folded into a
  space and a blank line (a second paragraph) is refused; none may contain control characters.
  Session names are unique across projects: `orche init` refuses a division whose `div-<proj>-<name>`
  session another project has or would have for a standard division or custom definition, and
  `orche feat new` refuses a feature whose `feat-<proj>-<name>` session another project has (exit 1,
  naming that project). A setup given wrongly through flags or `--setup-file`, a wrong field type
  included, is one `orche:` line with exit 1, a user error (never a traceback; 2 stays "not found");
  `--print-setup` refuses any other setup flag. On a terminal an unticked `--wake` division is named
  and the checklist shown again before `Apply?`, and a row whose session another project holds is
  shown unticked and unavailable from the start. Checklist item numbers
  are ASCII digits only. A re-run that adds nothing leaves `registry.json` byte-identical.
- Runner resilience shipped today with this release; the details are under 0.2.0: reconcile of
  runs whose `orche run` died without filing, the `ORCHE_RUN_ID` environment sweep, a per-run
  `--mcp-config`, pruning of `logs/runs/*.log` (`runner.log_keep_days`) and the `orche doctor`
  rows (disk, run logs, process table, pending sweeps).

## 0.2.0 — 2026-10-18

Units run headless by default.

### Added

- **Runner.** `orche run <unit>` runs one dispatched unit as a lean headless worker: `claude -p
  --output-format json` in the target's worktree with the brief (plus a fixed runner footer) on
  stdin, project settings only (`--setting-sources project`), no MCP servers (an empty per-run
  `--mcp-config`), the persona via
  `--append-system-prompt-file` (inline only for a `claude` without it; the `--help` probe is
  cached per `claude --version`), Main's environment minus `CLAUDE_PROJECT_DIR` and Claude Code's
  `CLAUDE_CODE_*` session variables (authentication kept), and a TIER-sized budget (mechanical:
  `runner.model_mechanical`, default `claude-sonnet-5-5`, read/write/search tools and
  `ls`/`test`/`git status`/`git diff`; standard:
  `claude-opus-5-5` plus Bash; hard: the same with 40 turns). The timeout is TIMEBOX + 60 s
  (`1h30m` adds up; capped at 4 h). Each run records `RUN.json` (model, cost, turns, usage, stop
  reason, exit code, HEAD before/after, result tail; masked) and `REPORT.md` next to the brief, and
  files one pointer from the target (tagged `via: runner`) — every run does; when `orche run`
  itself dies without filing (SIGKILL, OOM, power loss, a full disk) reconcile files it later
  (see Resilience). From the Build division or a feature agent (the senders `orche report done`
  gates) the pointer is `done` only when the final `STATUS: done` (case-insensitive; the echoed
  template does not count) comes with a new commit, a clean worktree and a passing `orche report`
  ledger gate for that commit, otherwise `info` "unverified: STATUS: done (no commit)" /
  "(uncommitted changes)" / "(no ledger row for <sha>)" — a mechanical Build unit, which cannot
  commit, reports `info` unverified and Main decides after a verifier records the row. From every
  other target (Research, R&D, GTM, Ops, Main) `STATUS: done` files `done`. `blocked`/`failed`
  pass through; a crash, timeout, non-zero exit, runner error (also a bad `runner.max_turns`) or
  SIGTERM files `failed`; noise around claude's JSON on stdout is skipped. The worker gets
  `ORCHE_RUNNER=1`, under which `orche report` refuses (exit 2); the footer and the division and
  feature personas say so. The ledger row's writer ≠ judge rule is enforced when the row is
  recorded (`orche ledger record`); the runner checks only that the row exists. At most
  `runner.max_parallel` (4) runs per project and, among runner runs, one per target session (a
  target's units share its worktree; a pane worker in that worktree is not counted); a run that waited `runner.queue_wait_s` (30
  minutes) files `info` "runner busy" instead of vanishing, and a duplicate `orche run` of a live
  unit files `info` "already running (pid N, started HH:MM)" and exits 4 while the live run still
  files its own result. After every run the rest of claude's process group is killed, and every
  process carrying the run's `ORCHE_RUN_ID` in its environment (never in its arguments), with
  their descendants and the groups they lead — daemons a worker started (a tmux server,
  gpg-agent, ssh-agent, a language server) end with the run. `--dry-run` prints the exact
  `claude` argv.
  Measured on 12 small units: $3.75 and 2 minutes as runner workers vs $9.77 and 22 minutes as
  pane workers.
- **Visibility.** `orche status` shows `RUNNING n` per project, `orche depts` a RUNNING column,
  the status line a `runN` cell; `orche metrics` adds a Runner section (units, cost, median cost
  per unit, failures); `orche doctor` checks the `claude` binary, its version and
  `--setting-sources` support, and warns under 1 GiB free on ORCHE_HOME's filesystem and over
  200 MiB of run logs.
- **Resilience.** A run whose `orche run` died without filing, or whose lock pid now belongs to
  another program, is reconciled: a minimal RUN.json (`stop_reason: lost`) and `failed` "runner
  died: <unit> (pid N, started HH:MM); re-run with `orche run <unit>`" to Main (a feature: Build),
  events `runner-reconciled`; what it left running is killed and its locks go. A pointer the
  run or its worker had already filed means only the locks go; a target that routes nowhere is
  reported "NOT filed" (`pointer_filed: false`). A filed lost run never holds a slot: when its
  leftovers cannot be swept yet (no time in the hook, `ps` failing or hanging) it waits as a
  pending sweep (`runs/<proj>/<run_id>.sweep`) that later reconciles retry, settled by `kill -0`
  after 3 unreadable process tables in a row; `orche doctor` shows a `process table` row and the
  pending sweeps. Reconcile is pull-based: the next prompt in any session of the project (cheap:
  `kill -0`, `ps` only within the hook budget), the next `orche run` of the project, STOP of
  that unit, `orche status|depts|doctor|metrics|janitor run`. A run that cannot write its
  RUN.json, REPORT.md or pointer (ENOSPC, EACCES) exits 5 and keeps its lock, so reconcile files
  it once writes work again (as `failed` "runner died", whatever its RUN.json says). `runner.log_keep_days` (14): run
  logs older than that are deleted at `orche run` start and by `orche janitor run`.

### Changed

- **Delivery.** New config `delivery` (default `runner`): the Stop hook spawns `orche run <unit>`
  detached for every new brief to a division or feature agent and never waits for it; a failed
  spawn falls back to the pane nudge. For runner units `STOP` kills a live run (the runner files
  `info` "stopped by Main"; SIGKILL after 5 s), makes a run still waiting for a slot give up
  ("stopped by Main before start") and leaves a finished run's result alone ("already finished");
  `AMEND` stops a live run and starts a fresh one for the new rev (`runner-stop-requested`,
  `runner-restarted`, `runner-started`). The STOP mechanism is a marker, polled every 2 s by the
  run while claude works, plus SIGTERM as the fast path for a verified pid: STOP/AMEND signal a
  lock's pid only after `ps -ww` (1 s at most) shows it still is that `orche run <unit>` (whole
  unit id, `--json`/`--dry-run`/`--` allowed before it); when `ps` fails or times out the run
  counts as live, keeps its lock and gets no signal (`runner-pid-check-failed`), and the poll
  stops it. A dead run whose own pointer was already filed only loses its lock (events
  `runner-stale-lock`). The spawn is `<the hook's python> bin/orche.py run <unit>`, so no launcher
  has to find Python after it. Main targets keep the pane nudge, as
  do `delivery: pane` and hard units with `runner.hard_in_pane: true`. The sender's feedback
  pointer reads "relayed <unit> → <to> rev N (runner-started)" and never suggests `orche wake`
  for a runner unit.

### Fixed

- A SIGKILLed `orche run` (kill -9, jetsam/OOM, shutdown, power loss, a Python crash) no longer
  files nothing and leaves claude running: reconcile files `failed` and kills its claude and
  leftovers.
- A recycled PID no longer keeps a crashed run's lock alive or holds its target's queue (review
  M-3): the next reconcile (any `orche run` start among them) settles the lock without signalling
  that pid.
- Processes a worker detached into another process group (`setsid`, `nohup … &`) no longer
  survive the timeout/STOP kill (review M-5): the worker carries `ORCHE_RUN_ID=<unit>@<ns>` and
  every kill path ends with a sweep of that exact tag in the environment (on macOS `ps -axww`
  and `ps -axwwE`, the environment being the `-E` line after the exact arguments; /proc on
  Linux), including the tagged processes' descendants and process groups. On macOS Apple's own
  binaries, and programs that rename their process title (node's `npm exec`/`npx` MCP servers),
  hide their environment and are found only through a tagged parent or the process group.
- Resilience review fixes: a non-UTF-8 byte anywhere in the process table no longer disables
  every sweep and pins a project's slots (C-1); the tag inside a command's arguments (`grep
  ORCHE_RUN_ID=…`) no longer counts (I-1); a lost run's claude is no longer recognised by the
  argument every runner claude shares — its tag decides, or, with its environment hidden, its
  pgid, that argument and a start time after the lock's (I-2); the prompt hook's reconcile never
  starts a `ps` it cannot finish inside its budget (M-1); a nested `orche run` drops the tag it
  inherited (M-3); an unroutable target is never reported filed (M-5); a worker's own pointer
  counts as the run's (M-6).
- `logs/runs/*.log` are pruned (review M-10; `runner.log_keep_days`).
- Each run gets its own empty `--mcp-config`, `runner/mcp/<unit>@<started_ns>.json`, instead of
  the shared `runner/empty-mcp.json` rewritten every run: it is written at run start and removed
  on every exit path, by reconcile and by log pruning (`--dry-run` prints the placeholder path).
  An env-hidden claude is now taken for a lost run's claude only with that run's own path, its
  pgid and a start time after the lock's, so a reused pid running another run's claude is safe
  (resilience follow-up m-2).
- A pending sweep is dropped after three real failures to read the process table only: a `ps`
  that fails or times out at its full `PS_TIMEOUT_S`. A read cut short by the hook's remaining
  budget is "no time" and never counts, so a slow `ps` no longer abandons a sweep with its
  leftovers alive; inside the prompt hook every read is budget-bounded, so a hung `ps` is only
  counted by full-budget reconciles (`orche run` start, status, doctor, janitor) (m-1).
- A launcher (`bin/orche`) that finds no Python 3.11+ after the Stop hook's spawn can no longer
  lose a run: the hook spawns `bin/orche.py` with its own interpreter.

### Deferred

- The discarded spawn `Popen` can emit a silent `ResourceWarning` at hook exit (review M-10).
- Known gaps of the runner's resilience: (a) surfacing is pull-based: nothing happens until the
  next prompt in the project or `orche` command; (b) a filesystem that stays unwritable keeps a result
  unfiled (`orche doctor` warns); (c) with `ps` unreadable, a queued run on the same target can
  start next to a lost run's still-running claude, and a pending sweep dropped after three real
  failures leaves its leftovers running (`orche doctor` warns about pending sweeps); (d) a pid
  that exits and is reused between the sweep's table read and its SIGKILL (up to 2 s) could be
  signalled (resilience review M-4; theoretical, needs a pid wrap in that window); (e) on macOS
  an environment value containing ` ORCHE_RUN_ID=<id> ` matches, a nested `orche run` is spared
  only once its starting shell has left the outer run, and title-renaming programs are found
  only through a tagged parent or the process group (see SPEC R-DISPATCH-9).

## 0.1.0 — 2026-10-17

First public release.

### Added

- **CLI and registry.** `orche` command with auto-discovered subcommands, global `--json` and
  `--dry-run`, and fixed exit codes (0 ok, 1 user error, 2 not found / unverified, 3 blocked by
  policy). One registry (`~/.claude/orche/registry.json`) holds projects, aliases, divisions,
  feature agents, limits and an append-only event log; every write is atomic and schema-checked.
- **Sleep, wake and visibility.** `orche sleep` / `orche wake` close a unit's pane and resume its
  session later. `orche depts`, `orche status`, `orche projects` and `orche card` answer status
  questions without a model. A status-line segment shows divisions, awake feature agents, new
  agents, pending secret requests and unverified completions.
- **Hooks and the dispatch grammar.** Main assigns work with fenced `dispatch` blocks (`TO GOAL
  SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER`, optional `ROLLBACK`; `AMEND <unit> rev
  N` and `STOP <unit>`). The Stop hook relays them as versioned briefs and gates completion
  claims; the UserPromptSubmit hook delivers inbox pointers at drain points; SessionStart
  re-injects standing orders and unit memory; two PreToolUse gates block secret reads and
  out-of-scope edits. `orche report` files a completion or blocker as one inbox pointer.
- **Verification ledger, decision log and verify skill.** `orche ledger record/check/summary/
  export/grade` keyed by PR and commit; writer and verifier must differ. `orche decide` appends
  to a unit's `DECISIONS.tsv`. A `verify-<app>` skill template turns verification into committed
  scripts.
- **Keymaster.** `orche secret request/wait/fill/add/ls/missing/rotate/run/render/share/revoke/
  redact-transcripts` and `orche dev add/remove`: agents know secret names only; values go from
  a dialog straight to GCP Secret Manager, Vercel, GitHub or the macOS keychain and reach a
  process only through `orche secret run`.
- **Janitor.** `orche janitor` and `orche discard`: state-based cleanup that archives sessions
  (never deletes them), moves files to a 30-day trash with undo/restore, writes a GRAVEYARD row
  before anything moves, and stays in dry-run until enabled. Optional daily schedule.
- **HR, onboarding and lifecycle.** `orche init` registers an Orca repo and onboards its five
  divisions; `orche feat new/sleep/wake/retire` manages feature agents with daily/weekly creation
  limits, a global freeze, scope-overlap checks and an archive on retire. `orche hr audit`
  proposes sleeps; `orche hr correct` adds numbered standing orders. `orche stage set` moves a
  project between `building` and `public` and wakes Ops.
- **Agents, skills, personas and templates.** Plugin agents (exec, runner, deep-reasoner, hr,
  liaison), the `orche`, `orche-main` and `orche-build` skills, persona templates for every role,
  and project templates (STANDING.md, PROJECT_CARD.md, RUBRIC.md, GRAVEYARD.md, unit memory files,
  CI workflow, overnight contract, large-job workflow).
- **Jev gate.** Optional typed-decision client for drain urgency (it can add urgency, never
  remove it), off by default, with a 1 s fail-open timeout, code stripping and secret masking
  before anything is sent; `orche jev eval` validates it offline first.
- **Installer.** `orche install`, `orche uninstall`, `orche doctor` and `orche apply-all`:
  patch user settings, the status line and agent links with backups and recorded originals, check
  the installation, and onboard every Orca repo at once.
- **Documentation.** README, SPEC, principles, FAQ, Orca sidebar guide, CONTRIBUTING and SECURITY.

### Known limitations

- Unfenced code in a pointer or prompt can still reach Jev; code stripping is a heuristic.
- The Keymaster Vercel backend is MVP-grade: `rotate` removes the old value before adding the new one.
- The janitor has no run lock; do not start two `orche janitor run` passes at once.
- The env-read gate is best-effort: backtick forms, `bash -c env` and `os.environ` dumps are not caught.
- Sessions are identified only via `ORCHE_SESSION`, their unit worktree, or an exactly matching Orca pane title; other panes in a main checkout are left alone.
- A child `claude -p` started from an ORCHE pane inherits `ORCHE_SESSION` and is treated as that session.
- SessionEnd on `/clear` moves the session's scratch to the janitor trash (recoverable with `orche janitor undo`).
- With no Python 3.11+ on the machine every hook does nothing; `hooks/run.sh` logs one line per call to `logs/hooks.log` (`orche doctor` reports it), and the `orche` command prints "ORCHE needs Python 3.11+" and exits 1. Both find a Python 3.11+ even when the login shell's `python3` is macOS's 3.9.
- Per-secret GCP IAM is not implemented: `orche dev add` refuses when one GCP project holds every env.
- A relay that fails part-way can leave an empty reserved unit directory; an AMEND with the wrong rev becomes max+1.
- `feat new` can leave an orphan Orca worktree after a partial failure; re-running it resumes the entry.
- The janitor ages scratch by mtime only and records trash purges in the global GRAVEYARD only.
- `orche ledger check --pr N` without `--sha` checks the cwd HEAD, not the PR head.
- The UserPromptSubmit hook marks stalled workers (`STALLED`) but does not nudge them.
- Dotenv values containing `$` are written verbatim; frameworks that expand variables may change them.
- Orca opens its default "Terminal 1" and "Setup" tabs for every worktree ORCHE creates (setup hooks run there, e.g. `npm install`); they are idle shells, close them with `orca terminal close`.
- `scope_gate` guards Edit/Write; a worker's shell `cd` outside its worktree is not gated.
- A ledger row is keyed by head SHA: a unit that changed nothing at an already-verified HEAD passes the done gate.
- `docs/LEDGER.tsv` in the repo is updated only by `orche ledger export`.
