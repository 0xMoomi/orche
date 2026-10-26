# Changelog

All notable changes to ORCHE are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic versioning.

## 0.12.0 — 2026-10-26

### Added

- The quick start: one three-line paste installs ORCHE and sets up the project; the installer prints the next steps.
- `.orche/SETUP.md`, the setup report Main gives the owner after `orche init` ("ORCHE is set up", what was made, what to switch on any time); `orche init --refresh` rewrites it and `--json` names it (`setup_report`).
- `orche doctor` ends with "Next steps (optional, any time)": Jev, the Codex cross-check, the background-run token, the janitor, the merge-queue gate, more agents, and the always-present lines; its `--json` is `{"checks", "next_steps"}`.
- Main's "what else can ORCHE do" row: the next steps in plain words.
- `orche focus <project|session>`: switch Orca to a pane (a Main is reopened when closed); terminal-notifier is optional (opt-in: say 'make notification clicks open the tab'); doctor and the setup report say what a click does.

### Changed

- The installer's closing lines say that ORCHE is installed and what to do next.
- README is a quick start plus an "Any time" table.
- Templates, generated file headers and the unit rules use plain punctuation (no en or em dashes), and the intake persona and the unit card forbid them.

### Fixed

- A click on an ORCHE notification opens Orca (it used to open Script Editor); with terminal-notifier installed it opens the project's Main tab.
- Panes no longer pass `--autocompact 200000`: with the installer's 50 % setting a pane compacted at about 100K tokens, under the fixed prefix a session with many plugins carries, so Main compacted every turn ("Autocompact is thrashing"); the model's window decides again. Reading a Claude Code transcript into a session is refused (the env_read_gate).
- When terminal-notifier cannot show a notification (its macOS permission refused or not yet granted), ORCHE falls back to Orca's own notification instead of showing nothing.

## 0.11.0 — 2026-10-26

### Changed

- Main resumes its conversation on every reopen (`orche wake main-<p>`, the closed-Main repair, a disconnected pane); only `orche init` starts it fresh.
- Every pane session compacts at 200K tokens (`--autocompact 200000`) and gets the project card again after a compaction.
- A reopened Main says in five lines what was done last, what runs and what waits on the owner.
- After `orche init`, Main reports the setup result in its own tab (divisions and models, the intake line, workspaces) and asks for the first request; init closes the window it was run from, so the applying session never writes a report, dispatches or acts as Main.
- A pane `done` leaves quiet `verifying`/`returned` notes while its pre-review runs; the card shows the unit `verifying`.
- Main matches the owner's intent in any language, never the wording, and runs the matching `orche` command itself; the rulebook's `## Owner's plain words` is one table that covers every owner action.
- README, FAQ and the help card lead with words: you tell Main in your language, and commands are shown as what Main runs (and what scripts may call).
- The repository is English-only: docs, examples, card labels and triage heuristics; Korean choice texts and the `--lang` flag are gone (Main translates for the owner); triage candidates are found by structure (a named target, not a question) in any language.
- `orche update` on a development checkout (another branch, local changes or local commits) pulls nothing and aligns the install and every project with the checkout when the recorded version differs; it used to refuse with "update by hand".

### Removed

- The install's "restart any main-/div-/feat- panes" notice (a 0.6 migration line) and the "restart the pane or /clear" line after a persona refresh: panes read new files at their next session start, and no ORCHE message asks anyone to close a Main.

## 0.10.0 — 2026-10-26

### Added
- Teams: workers (feature agents) under any head but Research, R&D and Ops; each head staffs a worker per slice.
- Status: every division's cell shows its active workers (`BUILD●4 DES●2`).
- `orche feat new` wakes a dormant head (R6), refuses a worker staffing workers, checks overlap across divisions.

### Changed
- The writer's head reviews and queues its own workers; queue AMENDs, pointers and landed notes go to that head;
  queue-wide notes (a pause, exhausted restarts, a GitHub refusal) stay with Build. Only `infra` under Build stands.
- Default preset: headless workers, up to 8 a day, 40 a week, 4 per request (was 3/10/2); a landed worker retires.
  Upgrade: an existing registry keeps 3/10/2 until `orche hr scale default` is run once.
- A head's child unit that is another division's counts as landed once it reports done.
- Personas, orche-build ("Your team"), orche-main, SPEC and the glossary say heads; SPEC R-REVIEW-8/9 are now 10/11.
- `orche status <p>` prints the project card (`--json`: an object), `orche status --project <p>` the detail view; `[skip ci]`
  admission refuses a code commit anywhere in base..head (`--accept` as before).
- A review attempt without a cost figure counts its wall time like a worker's (§7.3); head-review wording and a
  `no-head` status; scale presets list only their differences.
- Merge-queue admission, GitHub calls, the base branch, the queue record, the run record and the reviewer call each live in one module.

### Removed
- `orche resume`; `config/orche.example.json`; config keys `review.effort/max_diff_lines/build_timeout_s/timeout_s/
  max_starts/tier_audit`, `queue.method/max_returns/max_stops/docs_paths`, `jev.min_confidence` (now constants;
  landing is always a merge commit; the Jev tier audit runs whenever Jev is on).
- RUN.json keys `kind, attempts, attempt, resumed, pin_note, escalated_from` (history lines keep every call).

### Fixed
- The merge queue removes the lock files a killed git left in its clone before a pass (one used to stop every
  later pass until the queue paused); a failed git step names git's `error:` line instead of `From <url>`.

## 0.9.1 — 2026-10-25

### Fixed
- A pane whose Orca terminal is disconnected (the agent inside exited) is closed and reopened through the
  wake path before a brief or report is delivered to it; the heartbeat's first retry does the same, and the
  stall line says `pane reopened` instead of `retrying`. A reopen types nothing and costs no model call.
- A disconnected Main pane goes through the closed-Main repair even with nothing to deliver, and a missing
  one as soon as a report waits (same daily cap, quiet hours and one escalation as before).
- The held-reviewer test no longer depends on the time of day.

## 0.9.0 — 2026-10-25

### Added
- Scale: every flow cap is config (`flow.revisions`, `flow.briefs_per_hour`, `flow.runs_per_unit`, `flow.depth`,
  `flow.fanout`); `runner.max_parallel_total` (8) caps live runs across projects and `runner.min_free_mb` (2048)
  makes a run wait while free memory is lower. Presets default/20/30 (`scale.presets`) set them at once:
  `orche hr scale [<preset>]` (HR event `scale`; `orche hr choices` offers them). `feature_panes: false` makes
  features pane-less (`orche feat wake` opens one); `feature_min_free_disk_mb` guards `orche feat new`.
- Publish: after a gated unit's verified done, ORCHE (not the model) pushes the unit's own branch (`feat/*`,
  `div/build`; never forced) and opens or reuses one ready PR to the base: config `publish.base` (null), else
  origin/HEAD, else an OWNER step (`git remote set-head origin -a`), never a guess. A failed push or PR, or a
  feature done with nothing to publish, leaves the done unverified, with an OWNER step when only a person can fix
  it; a Build done that changes nothing publishes nothing (`pr: none`). RUN.json, REPORT.md and the pointer carry
  the PR. A feature run starts by merging the base into its branch (a conflict is aborted and noted in the prompt).
- The merge queue: an approved PR becomes an entry pinned to its head (`orche queue add <unit> [--first]` or
  `--pr N`; `drop`, `pause`, `resume`, `approve`; `orche queue <p>` lists them); `queue.land: owner` keeps entries
  untested until `orche queue approve`. The integrator, model-free, one per project, in its own clone, merges the
  first entry's head onto the fetched base, runs the gate (`scripts/verify/ci.sh`, then the unit's committed
  VERIFY scripts; no secrets in its environment), lands a green entry with `gh pr merge --match-head-commit` and
  proves the merge commit (parents and tree). A red, conflicting or timed-out entry goes back to its writer as an
  AMEND, the third in a window to Build. A red main closes the queue to all but `--first` entries until main is
  green again; a failed proof closes it until `orche queue resume`. GitHub refusals, a base that keeps moving and
  `queue.max_stops` (3) passes in a row stopped by git or gh pause it with a notice. The heartbeat starts the
  integrator and puts a lost entry back at the front.
- Reviews: a reviewer of another vendor (`review.order`; with none, the other Claude model, noted) pre-reviews a
  published done read-only inside the unit's run (`VERDICT: PASS|RETURN`); the writer reworks a RETURN in the same
  run, and after `review.rounds` (2) RETURNs the done goes to Build with the open findings. Then a headless,
  read-only Build review (Build's persona and model) answers the eight-line checklist: a PASS queues the PR;
  RETURN, high risk, size or an error wake the Build pane (`review.build: false` turns it off). The heartbeat
  starts pending Build reviews and tells Build after `review.max_starts` (3) starts. `orche review --pr N` runs
  both for Build's own PRs; a pane member's `orche report done` runs one pre-review round itself.
- The tier ladder: TIER picks the model and effort (`ladder.<vendor>.<TIER>`; mechanical starts on Haiku, the
  division's model is the ceiling, the `MODEL` brief key pins one). A failed or unverified mechanical attempt
  escalates once to the standard rung in the same run; every call is an attempt in RUN.json `attempts`.
- Usage holds and spend caps: an attempt that hits a usage or rate limit holds that vendor across projects until
  the reset it names, else a 5/15/60-minute backoff (`lib/orche/flow/usage.py`). The unit files one quiet `info`,
  never `failed`; the owner is told once and the heartbeat re-runs held units after the reset at the held stage
  (a review or rework on the same HEAD resumes there); a unit held 3 times since its last done is not held again.
  G4 is checked before every paid call (escalation, review, rework, Build review), skipping it over a cap.
- `orche status`: `queue w/t/m` and `runs n/cap` (was `RUNNING n`) on the project line, `paused: claude usage limit
  until 15:00 (7 units held)` and `queue:` lines, at most 3 sub-lines per kind (`--project <p>` prints all, and a
  line per lineage). `orche doctor` rows `gh`, `queue gate`, `deploy on merge`, `queue`, `review`, `scale`,
  `ladder`, `usage` and `memory`.
- The resume card: `orche resume [<project>]` prints each open lineage (goal, units, PRs, newest report, what it
  waits on), what waits on the owner and the queue; SessionStart shows it to Main after a `/clear` or a restart.

### Changed
- G5 (`flow.runs_per_unit`) counts only a run's `work` line; escalation, rework and review are calls inside it.
- The heartbeat's `busy` restart waits without spending its attempt while the slot cap is full (R34).
- Rulebooks: Build is traffic control and never merges by hand; the merge queue lands every PR (`orche-build`,
  `orche-main`, the div-build and feature personas); `templates/verify/ci.sh` is the gate's starting script.
- Docs: SPEC §4.13 Model ladder, §8.9 Scale, §12.4 Merge queue, §14.15 Pre-review; GLOSSARY's queue, review,
  ladder and scale terms; a short README (what it is, install, set up, use, update); FAQ on landing and scale.

### Removed
- `vendors.EFFORT` (the TIER→`--effort` table); the ladder replaces it.
- SPEC's watch-pr and the "integration worktree"/"Main approves integration" rules: the merge queue lands PRs.

## 0.8.0 — 2026-10-23

### Added
- Per-division models as data (`lib/orche/vendors.py`). A division records one model string: a Claude id
  (`claude-opus-5-5`; aliases such as `sonnet` too), `codex` (Codex's default model) or `codex:<model id>`;
  anything else with a `:` is refused (`unknown vendor`), and a plain id that is neither `claude-…`, an
  alias nor a catalog id is refused (`unknown model 'gpt-4' — …`). A feature agent runs on its division's model.
- `kinds` in config: one row per division kind (rnd, research, build, gtm, ops, design, content, `custom`
  for any other name) with its model and pane effort (rnd opus high, research sonnet medium, build opus
  medium, gtm sonnet medium, ops sonnet medium, design opus high, content sonnet medium, custom opus high),
  `label`, `desc` and `why` in Korean and English, and a `charter` for design and content; override one
  entry in `config.json`. `models.catalog` names the models a chooser offers (label, cost tier, description);
  also `models.codex_bin`, `models.codex_sandbox`, `models.prices`, and `lang` (default `en`).
- A Codex division's persona says `model: inherit`. Waking a Codex division is refused (exit 3: "codex
  divisions run headless only (v1); briefs reach them through orche run"); a feature under a Codex Build
  opens no pane and is recorded `paused`.
- `orche hr add <proj> <division> [--model M] [--label L --charter C] [--wake] [--no-intake]`: adds one
  division to an onboarded project. It prints `recommended for <division>: <model> — <reason>`, the `choices:`
  line (Claude ids, and Codex only when it runs) and, when Codex is missing or broken, `codex: <state> — <fix>`,
  then `created div-<proj>-<division> (dormant) on <model>` and starts the intake in the background
  (`intake: started in the background (orche intake status <proj>)`; `--no-intake` skips it). Exit 1 for an
  existing division or a custom one without label/charter, 2 for an unknown project, 3 for a Codex model when
  Codex is unavailable or with `--wake`.
- `orche hr model <proj> <division> <model>`: records a division's model and prints
  `<session>: <old> → <new>` plus how it switches (`(the next run uses it)`, `(headless only; the next run uses it)`
  for Codex, or `restart its pane to switch: …` for an awake pane); `is already on <model>` when nothing changes.
  Moving an awake division to Codex is refused (exit 3, `sleep its pane first`). One hr-log `model` line.
- `orche hr choices [<project>] [--lang ko|en]` (global `--json`): the owner's setup questions, built from data
  and written nowhere — keys `project`, `lang`, `stage`, `kinds`, `custom`, `models` and `ask` (`add`, `change`,
  `model` per kind plus `*` for a new one, `setup` and `pick` for a project not under ORCHE yet). Each question
  is an AskUserQuestion `tool` (question, header, 2–4 options) plus `values` (label → value); the recommended
  option comes first with "(recommended)" (in the owner's language), Codex is offered only when it runs (otherwise its state is
  in the question). An unknown project exits 2. A `--setup-file` may carry each division's model under `models`.
- Division and model setup prompts (the words for division, department or model, opus, sonnet, haiku, codex) bypass
  triage with reason `setup`: Main asks the owner instead of sending a brief.
- The model shows in `orche depts` (MODEL column), `orche status`, a brief's `model:` line and `orche doctor`
  (a `vendors` row: OK, or WARN naming the Codex divisions and the fix when Codex does not run).
- Codex divisions run headless through `orche run`: `codex exec --json` in the worktree under the
  `workspace-write` sandbox (git's common dir, `ORCHE_HOME` and the units folder writable, network on;
  `models.codex_sandbox: danger-full-access` drops it; the sandbox limits accidents, not a hostile worker: `ORCHE_HOME` and git's common dir are writable, so it is no containment boundary, as for Claude workers), effort by TIER, the persona body and a sandbox sentence on
  stdin. The final message is filed as a Claude worker's (STATUS line, `OWNER:`, done gate, masked REPORT.md); a
  `turn.failed` or `error` event or no final message files `failed` with codex's masked text; a codex that prints
  no event (missing, broken) files `blocked` with `OWNER: fix codex: orche doctor (row vendors) names the step;
  then orche run <unit>` — never a silent re-run on Claude. `cost_usd` is the estimate at `models.prices`;
  `orche --dry-run run` prints the codex argv.

- `orche update [--dry-run]`: fast-forwards the ORCHE checkout to `origin/main`, reinstalls with the new code,
  refreshes every project (`orche init <project> --refresh`, new: ORCHE-owned files only, no Orca call) and reports
  `ORCHE <old> → <new>` and whether sessions need a restart; a development checkout is refused, nothing changed.

### Changed
- Every run's spend reaches G4: a run whose worker started but left no cost figure (stopped, timed out, crashed,
  Codex without a completed turn, lost) records a reservation — whole minutes rounded up (at least one) × $0.25,
  at most `flow.max_unit_cost_usd`, `usage: null` — instead of nothing. A lost run's wall time ends at its
  timeout (the lock records `timeout_s`), not at the reconcile that finds it, and a timed-out run's
  `stop_reason` is `error` whatever its worker printed. Runs that never start their worker (busy,
  duplicate, stopped before start, not installed) stay uncounted.
- `vendor` (`claude` or `codex`) follows `model` in RUN.json (now 24 keys), the run lock, `runs/<p>/history.jsonl`
  and the events line (`model=… vendor=…`).
- A dispatch from inside a Codex sandbox (`CODEX_SANDBOX` set) is started by the heartbeat, not by the sandboxed
  shell: the pointer is marked `deferred`, the delivered line ends `— the run starts at the next heartbeat pass`,
  and reconcile's next pass delivers it (`deliver … try 1/3`) instead of waiting 10 minutes.
- `orche update` updates the clone install recorded (else its own checkout; no `.git` → `not a git checkout:
  <root>; reinstall with the README prompt`, exit 2), fetches with `GIT_TERMINAL_PROMPT=0` and a 120 s bound
  (the error text masked), resumes an install whose recorded version differs, and `--json` answers every outcome.
  Its refresh re-renders each pane's effort from the `kinds` table, so a 0.7.x build, gtm or ops pane moves from
  high to medium; recorded models are kept.
- A STOP or AMEND reaches a deferred run: a stopped pointer is drained (`stopped`) instead of started, and an
  AMEND stops the live older revision before the heartbeat starts the new one. `orche run` and a delivery retry
  inside a sandbox defer to the heartbeat too (`deferred <unit>: …`, exit 0).
- `orche init` on an onboarded project refreshes only ORCHE's own files: a project template the owner deleted
  (`.env.example`, `docs/RUBRIC.md`, `.orche/STANDING.md`) is not recreated. A folder macOS privacy blocks is
  named `unreadable (privacy): <path>` (exit 3; any other denial `unreadable (permission): <path>`). A `kinds`
  default model that is not valid refuses init before anything is written (`config kinds.<kind>.model: …`, exit 1).

### Docs
- Main's card routes division/model setup to the `orche-apply` skill and names the two-model pattern (a hard
  design call → one brief to two divisions on different models, a third reconciles A and B); the rulebook gains
  `## Divisions and models`. The `orche-apply` skill asks in plain words with a choice dialog and never creates
  a division or a project without an answer; the HR agent lists `orche hr add|model|choices`.
- SPEC numbers the model rules R-MODEL-1 to R-MODEL-11 (model string, recommendation and availability,
  `hr add`, `hr model`, headless-only Codex, the `codex exec` call, vendor and spend, the two-model reconcile,
  `hr choices`, setup in plain words, the `models` setup key), plus the triage `setup` bypass and the G4 note.
- README: divisions and models in plain words, enabling Codex and what a headless Codex worker gives up.
  GLOSSARY: Model string, Kind, Choice dialog, Two-model reconcile.
- Plain words everywhere (R-PLAIN-1 to R-PLAIN-4): Main's rulebook gains `## Owner's plain words` (status,
  check, update, divisions, halt → the `orche` command Main runs; the owner is never asked to type an orche
  command — a step only the owner can do, such as installing Codex or granting disk access, is shown as that one
  step — and never sees raw output) and its card says so. The README install prompt asks the first setup with choices ("The default 4 (recommended)"
  first) in the owner's language (`--lang ko|en`) and passes the answers as `--setup-file` (a picked design or
  content under `"custom"`); the quick start is three plain steps; new `## Update` section.
  GLOSSARY: Update.

## 0.7.3 — 2026-10-23

### Fixed
- `orche reconcile`: a project folder the pass cannot list (`.orche`, `.orche/units`, … — macOS privacy or a chmod)
  is `unreadable` on every pass, not only the LaunchAgent's: one events line `reconcile unreadable project=<key>
  path=<path>`, that project is skipped for the rest of the pass (runs, observe, repairs), the others still run,
  and the pass exits 0 and writes the heartbeat. On Python 3.11/3.12 the glob under a denied folder raised
  PermissionError and failed the hook-kicked pass (CI red on 0.7.2).

## 0.7.2 — 2026-10-23

### Added
- Pass watchdog (`orche reconcile`): a pass still running 2 × BUDGET_S (120 s) after it took the lock is aborted — SIGALRM in
  the main thread, and 15 s later a last-resort thread when the signal cannot interrupt a call blocked in the
  kernel (a macOS privacy check; it prints the abort line to reconcile.log before exiting). `ORCHE_HOME/reconcile/
  last-abort.json` records `{at, phase, path, hint}`: `path` is the innermost path the stuck pass held under a
  project's root, a protected folder or `/Volumes` (never ORCHE's own files), `hint` is `privacy` for a path under
  Desktop, Documents, Downloads, iCloud Drive, `~/Library/CloudStorage` or `/Volumes`, else `slow`; one events
  line `reconcile abort phase=<phase> hint=<hint>`, the lock is released, exit 3. `heartbeat.json` gains
  `job_at` (the last pass the LaunchAgent itself ran with reconcile on; its plist sets `ORCHE_RECONCILE_JOB=1`),
  `interpreter` (that pass's Python) and `unreadable` (protected projects it could not list: a denial answers
  EPERM while a glob there returns nothing); hook-kicked passes carry them over.
- `orche doctor`: the `heartbeat` row names a blocked job — `heartbeat job cannot read <keys or path> (macOS
  privacy) — run: orche install --grant`, or `heartbeat job loaded, no pass yet — …` — and a new `privacy` row
  checks the projects under protected folders against the job's own passes. `orche status` says `heartbeat:
  blocked by macOS privacy — run orche install --grant`.
- `orche install` waits up to 45 s for a pass of the job itself ("checking the heartbeat's first pass… (up to 45 s)", in Korean; a plain
  `launchctl kickstart` only when none came yet): `reconcile job: first pass OK (<took_s> s)`. When the job
  cannot read a project it opens Finder at the item to add (the job's Python.app, or a uv/pyenv/conda
  `python3.x`) and System Settings at Full Disk Access and prints what to do, Korean then English, after every
  other line; the install still succeeds. `orche init` of a new project under a protected folder does the same
  check. `orche install --grant` installs nothing: it walks through the grant in three steps and checks for up
  to 5 minutes ("granted" / "still blocked", in Korean then English:
  `… / granted — heartbeat OK` / `… / still blocked — …`; `nothing to grant — …` with no protected project). Ctrl-C during a wait: one line, exit 130. Re-run
  `orche install` (same --mode as before) to get the check; after a Python upgrade the grant may not carry
  over (the doctor says so; run `--grant` again).
- `orche setup [--project PATH ...] [--no-auth]`: one command for a new machine — the install (`--no-jev`, in
  the mode the last install used: plugin when the ORCHE plugin is enabled, else settings), the OAuth token
  status and the two commands to store one, `orche init PATH --no-intake` per project (an unknown repo is
  registered), the heartbeat job's first pass, `orche doctor`. Safe to repeat; exit 1 when a step failed or the
  doctor has a FAIL; when macOS privacy is all that is left the last line is "setup: one step left — …" (in Korean).

### Fixed
- `orche wake` right after a pane was closed: Orca can still list the closed pane, so wake printed
  `main-<p> pane switched to` or `<div> is already active` and opened nothing. Wake now verifies the pane by
  its handle (`orca terminal read`, then a fresh list) and opens it again when it is gone; a read that timed
  out (Orca's `bad_output` "timeout after 10s") keeps the pane, never a second one.
- A reconcile repair is recorded before it runs, so a pass aborted mid-repair still spends the try (the daily
  cap holds).

## 0.7.1 — 2026-10-23

### Fixed
- Owner notifications judge `flow.quiet_hours` on the reconcile pass's own clock, not re-projected onto the
  host's time zone: a pass's escalation no longer goes silent (or loud) because the machine's zone differs
  (CI red on UTC runners at 00:24 UTC).

## 0.7.0 — 2026-10-23

### Added
- `orche reconcile` (C10): one pass repairs stuck flow — an unread waking pointer gets a nudge, an undelivered
  dispatch is delivered again — with backoff 10/20/40 min (cap 3) and an urgent escalation after that; `--dry-run`
  prints what it would do. Each pass writes `ORCHE_HOME/reconcile/heartbeat.json`; `orche status` prints a
  `heartbeat:` line (last pass, interval, repairs today, or STALE / never / off) and `orche doctor` a `heartbeat`
  row. Config: `"reconcile": {"enabled": true, "every_s": 300}`; the backoff (STALL_S 600 s), attempt cap (CAP 3)
  and pass budget (BUDGET_S 60 s) are constants in `lib/orche/reconcile.py`.
- `orche install` writes the LaunchAgent `~/Library/LaunchAgents/com.orche.reconcile.plist` (`orche reconcile`
  every `reconcile.every_s`, and at load; output to `ORCHE_HOME/logs/reconcile.log`) and loads it with
  `launchctl bootout` + `bootstrap gui/<uid>` (only for the real home; `--no-reconcile` skips it). `orche uninstall`
  removes it; `orche doctor` row `reconcile job` checks it is loaded and its interval matches.
- Hook kick: the prompt and Stop hooks start one detached `orche reconcile` when the heartbeat is missing or
  older than 3 × `every_s` (at most once per `every_s`), so flow repairs without launchd too.
- Reconcile repairs (C10 T3/T4): a dispatch pointer whose revision already ran or was reported on is drained; a
  run stopped `busy` is restarted once; a closed Main with waiting reports is reopened without a focus switch and
  an active division or feature is woken (2 a day, not in `flow.quiet_hours`, then one owner notification; a dormant
  or paused unit only gets an urgent info to Main); a silent pane unit idle 20 min or 1.5 × TIMEBOX old gets one
  report-due reminder, then 30 min later (or at 2 × TIMEBOX) one urgent info to the brief's sender with a default.
  A still-loaded pre-0.6.0 `com.orche.main-waker` job gets one owner notification a day.
- Config `flow`: `{"enabled": true, "quiet_hours": "23:00-08:00", "max_unit_cost_usd": 10,
  "max_project_cost_usd": 30}`. `orche status` shows `stalled:` lines
  (`--json`: `stalled`).
- Flow guards (C10 T5): `orche dispatch` refuses, inside the project's autonomy window (since the later of local
  midnight and the owner's last prompt to Main), a fourth AMEND to one unit (three revisions pass, G1), an AMEND or same-GOAL brief to a
  unit that reported blocked twice in a row (G2), and the 21st brief or AMEND in an hour (G3); each refusal asks
  for `OWNER: <the decision>` and logs `flow refused rule=<G> by=<s> unit=<u> n=<count>`. The third refusal in a
  row (G7) adds "stop re-sending; tell the user what you tried". STOP always passes; `flow.enabled false` turns
  G1–G7 off (the pause, G8, stays). `orche flow pause <p> [--reason]` / `orche flow resume <p>` hold all relays (G8; resume needs
  an owner prompt newer than the pause, else exit 3); reconcile only drains a paused project. `orche status`
  shows `looping:` and `paused:` lines.
- Run history and caps (C10 T6): every finished, unrun or lost run appends one line to
  `ORCHE_HOME/runs/<p>/history.jsonl` (`{unit, rev, to, model, effort, status, stop_reason, cost_usd, num_turns,
  started, finished}`). Inside the autonomy window, a project that spent `flow.max_project_cost_usd` (30) on
  headless runs refuses every brief, AMEND and run, and a unit that spent `flow.max_unit_cost_usd` (10) refuses
  its AMENDs and runs (G4); a unit's 5th run refuses (G5). G5 also refuses, at the relay, an AMEND the runner would run, and both caps are
  checked again once a run holds its slot. A malformed cap falls back to its default; a guard that raises fails open
  (`flow guard-error by=<s> unit=<u> <Type>`). A brief to another project's Main passes the sender's guards (G8
  pause included); G3 counts the blocks ahead in the same dispatch; resume needs a prompt over a second after the
  pause; a quiet repeat is not a second blocked report (G2); `flow.enabled false` also drops the G7 suffix. A refused run exits 3, starts no claude, writes no
  RUN.json and files one quiet info pointer "not started: …; ask the owner". `orche status` shows
  `paused: autonomous spend $X since HH:MM (cap $30) — until the owner answers` (`--json`: a `paused` entry with `by: spend`; `looping` and `paused` entries
  have one shape each).
- Repeated reports stay quiet (C10 T6): a unit session's report with the same `brief_rev` and `status` as its
  previous one to the same inbox since W gets `quiet: true` and `repeat: <n>` (no wake), urgent or not; runner
  filings, external requests and brief-less (`adhoc-`/`xreq-`) units are never shaped.
  `orche status` shows `looping: <unit> — same report n× (<status>, rev <rev>): repeats did not wake Main` from n 3.
- Lineage and chain caps (C10 T7): every brief carries `lineage: <id>   parent: <unit|->` under its `to:` line
  (AMEND keeps both; a pre-C10 brief reads as its own lineage, no parent). A division's brief inherits the lineage
  of the division's newest brief and names it as parent. Main's briefs from one owner turn share one new lineage;
  a wake turn with exactly one injected lineage joins it; triage starts a new one. Report pointers (`orche
  report`, runner filings, unrun and lost) carry `lineage` and `parent`. G6: a division's brief is refused when
  its parent is already 3 briefs deep or already has 5 child briefs; Main is not capped; STOP passes.
- Lineage close (C10 T8): in a lineage of two or more units, done and stopped reports arrive quiet; failed,
  blocked and question still wake Main. When every unit has ended, ORCHE writes `.orche/units/<L>/LINEAGE.md`
  (one row per unit) and posts one waking close pointer from `orche` (`lineage L closed: n units — …`), once per
  lineage state; reconcile reposts a close a crash missed (≤ 24 h). Main's inbox block gains one
  `[orche] lineage L: closed|open — …` line per lineage.
- Owner's voice (C10 T9): Main's closing line is the owner's notification. A Main session's Stop hook (also under
  `stop_hook_active`) reads the last non-empty line outside fences: `OWNER: …` records the project's ask
  (`ORCHE_HOME/flow/<p>/ask.json` `{text, at, kind: owner|secret|guard, source[, unit][, closed_at]}`) and
  notifies; `DONE: …` closes the open ask and notifies once; `RUNNING: …` or no tag notifies nothing (no tag logs
  `owner untagged`). A first notification while the owner prompted Main under 2 min ago is skipped, not delayed (an ask is still
  re-notified from it; a DONE is not sent: the owner watched it land); at most one per project
  per 5 min (`flow/<p>/notified.json`). A runner's refused start (G4/G5) records a `guard` ask and notifies. A
  worker's last `OWNER:` line (runner) or `orche report --owner "<text>"` (pane) is the pointer field `owner`;
  Main's inbox shows it first as `OWNER STEP:`. One notify path, `lib/orche/notify.py` (`on run argv`: body and
  title are osascript arguments, masked, one line; config `notify`; macOS only), also carries HR, Keymaster and
  heartbeat notifications. Events name ids and kinds, never the text.
- Owner re-notification (C10 T10): the heartbeat re-notifies an open ask 30, 60, 120 and 240 min after it, then
  never (the `owner:` line stays); inside `flow.quiet_hours` a first notification is silent, re-notifications wait
  and the first pass after them sends one. The ask is answered (`answered_at`; events `owner answered project=<p>
  by=prompt|secret after=<m>m`) by the owner's next prompt in Main (the prompt hook) or once every secret a
  `secret …` ask names is filled; a filled secret also posts one urgent `info` to Main per asking unit — a unit whose
  report since the request names the secret, once every secret its owner step names is filled — `owner step done:
  secret <NAME>[, <NAME>] filled at HH:MM — resume <unit> (AMEND or orche run <unit>)` — from the Keymaster dialog
  after `store.mark_filled`, or from the heartbeat for a day (a dialog that died, a report filed after the fill);
  such a fill counts as the owner's word (`owner-seen`), so G1 lets the resume AMEND through. Fallback (R26): a report to Main that
  waits on the owner (blocked, question, failed, an owner step, a lineage close) for 10 min with no tagged Main
  turn begun after it (a tag stamps the turn's start), owner prompt or ask since notifies `<unit>: <owner step or status>` once — an ask for blocked, question and
  owner steps, a one-off notice otherwise. A DONE, an escalation (`stuck`) and a fallback notice are recorded in
  `flow/<p>/<kind>.json` before they are tried, so one held back (gap, lock) goes once at a later pass; a failed show
  (osascript exit or timeout) is a try — the next waits for the next 30/60/120/240-min slot, at most 5 for an ask or
  a notice — and a fallback held back prints as `held`. The open ask's text again, or the last DONE again with no ask since, is a repeat (nothing sent).
  `orche status` prints `owner: asked HH:MM (<m> min, notified n×[, next HH:MM | waiting for quiet hours to end])
  — <ask>` (`--json`: `owner`). The closing line also tolerates a quote, a bullet, backticks, `__bold__`, a space
  before the colon and a full-width colon, and a snippet fence indented inside ```` ```dispatch ```` no longer ends
  the block early. The Keymaster dialog is a fixed `on run argv` script with the request text as its argument; a
  notification argument starting with `-` gets a leading space.
### Changed
- `orche run` drains the dispatch pointers it replaces after the spawn succeeded (a failed spawn leaves the brief
  pending), and a foreground run drains again once it holds its slot; a held target lock logs `run unit=<u> lock-held`.
- Main's report line (and skill orche-main): a closed lineage with nothing more to dispatch gets the full report
  (outcome · landed · failed · decisions + default · next step); otherwise Main dispatches and writes one progress
  line; every reply ends with exactly one `RUNNING:`, `OWNER:` or `DONE:` line. This supersedes the C9 ruling A
  ("report after every wake"): one full report per lineage, a progress line otherwise.
- Cards and rulebooks (skills orche-main, orche, orche-build; division and feature personas; the runner footer;
  the Fable block): a unit only the owner can unblock ends `STATUS: blocked` with one `OWNER: <exact step>` line
  (`orche report blocked --owner "<step>"` in a pane; a secret: `orche secret request <NAME>`, then
  `OWNER: secret <NAME>`) instead of filing `orche report question`; Main's waker check reads the `heartbeat:` line
  of `orche status`. SPEC rows R-FLOW-1…8 and R-RECONCILE-1…6.
- `orche janitor install-schedule|uninstall-schedule` use `launchctl bootstrap|bootout gui/<uid>` instead of
  the deprecated `load -w` / `unload -w`.

### Removed
- The 0.6.2 per-pointer owner notification (R26): `deliver.notify_owner`, `inbox/<s>/.notified` and config
  `inbox.notify` are gone (a stale `inbox.notify` key is ignored; the top-level `notify` gates every notification),
  and `inbox.wake` no longer starts a nudger for an urgent Main pointer while a wake is fresh (9aafc26). Urgent
  reports still wake Main at once; the owner hears Main's `OWNER:` / `DONE:` line instead.
- Config keys `reconcile.stall_s`, `reconcile.cap` and `reconcile.budget_s` (now constants).
- The Main card line "An instruction you would write twice goes into STANDING.md first." (the card had to make
  room for the closing-line rule; STANDING.md itself is unchanged).

### Upgrading
- Run `orche install --mode settings` to write and load the `com.orche.reconcile` LaunchAgent (until then the
  hooks kick a pass when the heartbeat is stale).
- Restart Main and the division panes so they load the new cards.
- `inbox.notify`, the per-pointer owner notification and the 9aafc26 exception are gone; drop `inbox.notify` from
  `config.json` (it is ignored).
- Drop `reconcile.stall_s`, `reconcile.cap` and `reconcile.budget_s` from `config.json` (ignored; constants now).
- Main must end every reply with `RUNNING:`, `OWNER:` or `DONE:` — only OWNER and DONE notify the owner.
- The C9 ruling A "report after every wake" is superseded by one full report per lineage.

## 0.6.3 — 2026-10-22

### Removed
- `orche install` no longer registers gstack's careful PreToolUse hook (and no longer mentions it): its `ask`
  dialogs stalled unattended panes. ORCHE's own gates (`env-read-gate`, `scope-gate`) and each brief's FORBID
  lines are the safety net. An existing careful entry in `settings.json` is left alone by install and uninstall.
- The Stop hook no longer relays dispatch blocks from the reply (a fenced block there is plain text); `brief.scan` is removed.

### Added
- `orche doctor` WARNs when the careful hook (`check-careful.sh`) is registered in `settings.json`: its approval
  dialogs stall unattended ORCHE panes; remove it or keep it only for your own sessions.

### Changed
- Main hands work out with `orche dispatch` (blocks on stdin or `--file`, `---` between blocks; `orche --dry-run dispatch` checks only); each block's outcome prints in the same turn and a refusal exits 2, so Main fixes and re-runs without an extra Stop-hook turn.
- Personas name the report format a status card (was ketchup style); `orche init` re-renders them.
- Main's report to the owner is a status card: a fenced block of at most 6 lines with Korean labels and fixed glyphs (⛔ owner step first when the owner must act, then 🟢 done · 🟡 verifying · 🔴 failed · ⏸ held counts, ▶ running, ❓ decision, ➡ next, 🔗 refs); it replaces the ketchup counts.
- A dispatch or AMEND to a target that cannot be reached is refused by `orche dispatch`: an asleep division gated by stage while its project is building (`… is asleep and gated by stage (<proj> is building): re-target, or run orche stage set <proj> public --yes`), or a division/feature whose worktree is missing; before, such a brief sat `undelivered`.
- The nudger and the deliverer take separate locks (`.wake.lock`, `.deliver.lock`), so a brief is no longer held up behind a wake's quiet window; the nudger's lock wait follows `inbox.quiet_max_s` (360 s + the cap) instead of a fixed 600 s, and urgent notifications are recorded under their own `.notify.lock` so two nudgers never show one twice.

## 0.6.2 — 2026-10-22

### Added
- A macOS notification when the owner is needed: one per urgent report to Main (`blocked`, `question`, `failed`
  or `urgent: true`), titled `ORCHE <project>`, holding only the unit's short id and status. `inbox.notify`
  (default on) turns it off.
- `orche status` lists `blocked: <unit> (<status>, since HH:MM, N min) — waiting on the owner` for a unit whose
  newest report to Main is `blocked` or `question` (under 24 h, not yet answered by a later pointer or brief).
- `orche doctor` warns about a loaded `com.orche.main-waker` launchd job and prints the `launchctl bootout` fix.
- Gate denials (`env_read_gate`, `scope_gate`) are logged to events.log: `gate=<name> session=… tool=… what=…`.

### Changed
- One wake per quiet batch: the nudger waits until the newest waking report is `inbox.quiet_s` old (90 s, at most
  `inbox.quiet_max_s`, 240 s) before it wakes Main; an urgent report wakes at once.
- The wake line asks Main for one consolidated report that puts what the owner must do first.
- The Main rulebook forbids installing a waker, launchd job or polling script; ORCHE wakes Main itself.
- Main's reply ends with one line to the owner (state, then the decision needed with its default or the question
  what to do next); dispatch blocks go before that line and are relayed wherever they sit in the final reply.

## 0.6.1 — 2026-10-22

### Added
- Headless runs can use their own Claude OAuth token instead of the pane login: run `claude setup-token`, then
  `orche auth set` (hidden prompt, or stdin from a script; never argv). Stored in the macOS keychain (account
  `CLAUDE_CODE_OAUTH_TOKEN`), else `~/.claude/orche/secrets/oauth.token` (0600). Every `claude -p` ORCHE starts
  gets it; an inherited `CLAUDE_CODE_OAUTH_TOKEN` wins; panes keep the login. `orche auth status|rm`, and an
  `oauth token` row in `orche doctor` (WARN when not set).

### Fixed
- `orche wake` reopened a unit pane with `claude --resume <session name>`, which Claude Code cannot find (the
  name matches the transcript's title, not its id: a name shared by an earlier session of the same unit opens
  the interactive picker, and a cleaned-up transcript fails), so the woken pane never started its conversation.
  It now resumes the newest transcript titled with the session name in the unit's worktree project dir by its
  id (`--resume <id>`). When no transcript carries the name (or the project dir is missing) the pane starts a
  fresh session under `--name <session>` and stderr says so.
- A plain line that is not a KEY line continues the field above; it no longer refuses the whole reply as `unknown keys …`.
- A STOP reason may span lines, and its TO may come before or after the reason.
- In an AMEND, prose before any key or after SUPERSEDES extends the note.
- A brief that opens with prose is refused: `line 1 "…" is not a KEY line; a brief starts with TO.`
- Every parse refusal quotes the offending line (60 chars) and states the fix in one clause.
- One refused block no longer drops the reply: every valid block is delivered, and one refusal lists each block (`block N: delivered …` / `block N: refused — …`) and asks Main to re-send only the refused block(s).
- The same reply seen again (including under `stop_hook_active`, where the refusal is parked) never delivers a block twice.
- Every refusal logs `dispatch refused by=<session> blocks=<n> reason=…` to events.log.
- When every block of a reply is refused, the refusal says `no block was delivered` (it used to say the delivered
  ones went out).
- A dispatch block holding a code fence, the verb line of another STOP/AMEND block, or a KEY line other than TO
  inside a STOP is refused (two blocks run together or a pasted code sample); code samples go indented.
- A block identical to one already delivered from the same session is skipped only within 10 minutes of that
  delivery (`delivered_at` in the relayed marker), and the skip is no longer silent: an events line
  `dispatch skipped …` and an `info` pointer to the sender name it. A legitimate repeat after that is delivered.
- A relayed marker that cannot be written is logged to hooks.log instead of swallowing the refusal.
- A stored OAuth token that cannot be read (keychain error, timeout after 15 s, unreadable file) writes one line
  to hooks.log (`oauth: stored token unreadable (…); headless run uses the login`), never the value.
- A usage error masks a token passed as `--token=<value>` (`[REDACTED]`).
- The test suite never runs the real `security` binary, so it cannot read or prompt for the login keychain.

## 0.6.0 — 2026-10-22

### Added
- Main hears about every finished unit without being asked: when a report lands, ORCHE types `[orche] inbox:
  new reports` into the recipient's Orca pane once it is idle (`orche nudge <session>`), never into a draft or a
  dialog, once per batch. The Stop hook re-arms it when reports landed during a turn. Config `inbox.wake` (on).
- `orche deliver <unit>`: delivers a pane unit (detached, started by the Stop hook) or starts a runner unit that
  never ran; the recovery command. `orche status` lists `undelivered:` units and `unread:` reports (10 min).
- `orche init` on a registered project upgrades it in place: it re-renders ORCHE's own files (division and
  feature personas in the main checkout and the worktrees, worktree settings, marker, exclude lines, card link),
  copies each overwritten persona to `~/.claude/orche/backups/<project>/<time>/`, never touches your files
  (PROJECT_CARD.md sections, STANDING.md, LEDGER.tsv, GRAVEYARD.md, RUBRIC.md, `.env.example`, unit memory), and
  prints what changed and which panes to restart. The registry records `orche_version` per project; `orche status`
  and `orche doctor` say `upgrade: <project> rendered by …, now … — run orche init <project>`.

### Changed
- Every done/failed/blocked/question/stop/external request reaches the next prompt; only notes (`info`) wait for
  `[drain]`.
- No hook types into a pane any more: a failed delivery is an urgent pointer to the sender naming the fix
  (no more `budget-exceeded`). Runner units get no pointer in the target's inbox.
- A unit reports to whoever sent its brief (a feature unit Main dispatched reports to Main).
- The runner's turn cap is TIMEBOX minutes × 3 (30–400) at every TIER; a capped run says "turn cap reached
  (<n> turns, $<cost>)", RUN.json records `error_max_turns`, the events line ends `stop=<reason>`.
- A pane may write its own `REPORT.md` under `<project root>/.orche/units/<unit>/`.
- Personas: divisions never ask the owner in their pane; decisions go in REPORT.md as `decision → default
  applied`; `orche report question` when blocked. Main reports every completion unasked.
- A report is an order: every inbox block injected into Main ends with "Act on the inbox above now: read each
  report named, end this turn with your report to the user (outcome · landed · failed · decisions · next step),
  then the next dispatch or the question. Never only acknowledge." Held notes say "FYI from ORCHE, no action needed".
- The Main pane starts with `--permission-mode bypassPermissions`, like division panes (a woken Main must not
  stop at a prompt nobody watches). An open Main keeps its mode until reopened.
- Personas carry a "generated by ORCHE" header line: put project rules in `.orche/STANDING.md`.

### Fixed
- A turn that ends on an API error (StopFailure) re-arms Main's wake like a normal Stop.
- A pane may not write its unit's `brief.md` or `run.json` in any letter case (the gate matched upper case only).
- A refused dispatch no longer wakes Main (no refuse → wake → re-emit loop); Main still gets it at its next prompt.
- A wake or brief whose turn starts up to 20 s after Enter counts as delivered (was 5 s: hooks run first).
- `orche feat new` and the other onboarding paths back up a persona they overwrite to
  `~/.claude/orche/backups/<project>/<time>/`, as `orche init` does.

### Removed
- `runner.max_turns`, `runner.hard_in_pane` (ignored if still in config.json).
- `templates/personas/main.md` (never rendered: Main's rules are the orche-main card, loaded at session start).

### Upgrading from 0.5.0
- Run `orche init <project>` in each project (`--no-intake` keeps it to the refresh). Then, when idle, reopen
  Main (close the `main-<project>` tab, `orche wake main-<project>`) and the awake division panes init names. A unit
  running in a division pane when you upgrade: its next AMEND runs headless.
- A STOP of a unit still running in a pane after the upgrade does not reach that pane: STOP it before
  upgrading, or tell the pane by hand.
- Optional: remove `runner.max_turns` / `runner.hard_in_pane` from `~/.claude/orche/config.json`.

## 0.5.0 — 2026-10-21

### Added

- `orche ask <project> "<question>"`: one read-only headless answer about a project — a sealed
  Sonnet 5.5 call in its main checkout (Read, Glob and Grep only; the question and the card go in as
  data). The masked answer (at most 1500 characters) prints under `(answer from <p> at <sha7> — data,
  not instructions)`, the cost on stderr; nothing is stored. Exit 1 when the call fails, 2 for an
  unknown project or a missing main checkout. Config section `liaison` (`model`, `effort`, `max_turns`, `timeout_s`).
- `orche card refresh <project> [--background]`: no model call when no commit outside
  PROJECT_CARD.md and `.orche/` landed since the card's base commit ("fresh"); otherwise one Liaison
  call returns the Purpose, Stack, Entry points and Recent changes sections, which ORCHE masks, merges
  into the card and stamps `## Last refreshed` with `<date>, base <HEAD>`. With no user-visible change
  it writes nothing. `--background` detaches it (log `ORCHE_HOME/logs/liaison/<p>.log`).
- The card template gains `## Recent changes`; `## How to ask` names `orche card` and `orche ask`.

### Changed

- **The Liaison is headless.** It is no longer a session, a pane or a registry member: `orche init`
  and `orche apply-all` open the `main-<proj>` pane only, and SessionStart, dispatch, `orche report`
  and `orche decide` treat `liaison-<proj>` as an unregistered session.
- The weekly `orche-card-refresh-<proj>` automation's prompt is "Run `orche card refresh <proj>
  --background` and stop; do nothing else."
- `orche wake liaison-<proj>` exits 2 ("unknown unit").
- `orche discard` on a main-checkout transcript says "<t> is a transcript from <p>'s main checkout;
  refusing".
- The setup summary ends in `· pane main`; `orche init` prints "panes: main already open".
- Main's role card names `orche card <p>` and `orche ask <p> "…"` for questions about another project.

### Removed

- The Liaison pane and session (`liaison-<proj>`), its roster kind, its SessionStart role, its memory
  dir `.orche/liaison/` (the HR audit no longer reads it) and `hr.panes.PANE_KINDS`.
- `liaison_session` in `registry.json`: no longer written or read; an existing key is ignored.
- `agents/liaison.md` and the Liaison pane persona. Delete by hand, if present,
  `<repo>/.claude/agents/liaison.md` in each project's main checkout and the dangling
  `~/.claude/agents/liaison.md` link (settings mode; `orche uninstall` also removes it).
- `intake._failure` (now `headless.Result.problem`) and `runs.job.fence` (now `util.fence`).

### Upgrading from 0.4.0

1. Close each project's `liaison-<proj>` pane in Orca.
2. Only for projects initialised with `--automations`: change the prompt of each
   `orche-card-refresh-<proj>` automation to "Run `orche card refresh <proj> --background` and stop; do
   nothing else." Use Orca's automation settings, or take the id from `orca automations list --json` and
   run (single quotes, so the shell does not run the backticks):

   ```sh
   orca automations edit <id> --prompt 'Run `orche card refresh <proj> --background` and stop; do nothing else.' --json
   ```

3. Delete `<repo>/.claude/agents/liaison.md` and `~/.claude/agents/liaison.md`, and optionally
   `<repo>/.orche/liaison/`. A `liaison_session` key in the registry stays and is ignored.

Rolling back to 0.4.0: a project registered under 0.5.0 has no `liaison_session` key, and 0.4.0's
registry validator refuses a registry with such an entry; add the key to it by hand (any string) before
downgrading. Entries registered before 0.5.0 keep the key, so they stay downgradable.

### Internal

- `lib/orche/liaison.py` (`ask`, `refresh`, `gate`, `merge`) is the Liaison: one sealed headless call
  per command with intake's read-only tool and deny rules (SPEC §14.14); `cmd_ask` and `cmd_card` are
  its CLI and import it lazily.

## 0.4.0 — 2026-10-20

### Added

- `orche init` with no argument installs on the repository holding the current directory (exit 1
  "not inside a git repository: pass the project path (orche init <path>)" outside one); the orche-apply
  skill runs it at once on a bare `orche init`.
- `orche wake main-<proj>` / `liaison-<proj>` opens the project pane in the main checkout, or switches
  Orca to it when it is already open.
- `orche init <path>` adds a git repository Orca does not list yet (`orca repo add --path`) once the
  checklist is confirmed, right before its first write (an abort or a dry run never adds; a linked git
  worktree is refused with the main checkout's path), and ends by switching Orca to the `main-<proj>` tab ("Main is open in the main-<proj>
  tab; type a request there."); `--json` reports `switched`, dry-run prints what it would add and switch.
- A settings-mode install links each plugin skill into `~/.claude/skills/<name>`; plugin mode and
  `orche uninstall` remove those links, and foreign entries with the same name are skipped and reported.

### Changed

- **One Roster answers who, where and to whom.** Identity, place, attribution, recipients and `TO`
  resolution now follow SPEC §14.9 everywhere; the user-visible effects are listed below.
- Orca pane titles no longer identify a session; only `ORCHE_SESSION` or a member's worktree does.
- An `ORCHE_SESSION` naming no registered session gets a SessionStart note and keeps its own inbox;
  the cwd is no longer used as a fallback.
- A session with no identity writes its dispatch refusals to `hooks.log` only and skips the night check.
- Secret requests made without a session are credited to `user`.
- `orche decide` writes the session's own `DECISIONS.tsv` and refuses without a session unless `--file` is given.
- A live division or feature record beats a retired one at the same worktree path.
- `TO main-<other>` files an external request to that project's Main.
- In `TO`, a division name of the sender's project beats another project's key or alias.
- `bin/log.sh` is removed; `orche decide` is the only `DECISIONS.tsv` writer.
- The SessionStart note in a project's main checkout reads `[orche] this is not an ORCHE pane: Main's
  rules apply in the main-<proj> tab — you can run: orche wake main-<proj>`.
- The SessionStart hook appends `export PATH=<shlex-quoted plugin bin>:"$PATH"` to `$CLAUDE_ENV_FILE`
  (when the plugin bin is not already on PATH and the file does not already hold that line, adding a
  newline first when the file lacks one) so a session's Bash finds `orche`.
- Division and feature panes start with `--permission-mode bypassPermissions`; Main and Liaison panes
  are unchanged.
- When a live `orche init` cannot find or switch to the `main-<proj>` tab it warns `could not switch to
  main-<proj>; open that tab in Orca` and does not print the "Main is open" line.
- A settings-mode install links only `skills/` folders that hold a `SKILL.md`.
- **One module starts every headless `claude -p`** (`lib/orche/headless.py`: the unit worker, the
  intake sweep and the triage classifier; SPEC §14.10).
- **Unit workers take `--effort` from TIER** (mechanical low, standard medium, hard high) **and run
  the target's registry model at every TIER.** Mechanical units on the built-in divisions move from
  Sonnet to Opus at low effort; Research's standard and hard units move from Opus to Sonnet.
  RUN.json records `effort`.
- **Unit workers run ORCHE's secret-read and scope gates** (PreToolUse, through `--settings`).
  Before, no ORCHE hook ran in a headless call.
- **`orche doctor` checks every flag ORCHE passes** (row `claude flags` from `headless.check`,
  replacing `claude --setting-sources`). Claude Code must list all 15: there is no fallback for an
  older claude.
- **Headless unit workers start with `--permission-mode bypassPermissions`**; the PreToolUse gates
  and the deny list still block; the sealed intake call is unchanged (`--restricted` refuses bypass).
- **Headless calls no longer inherit the caller's identity or session state.** The intake worker no
  longer gets the caller's `ORCHE_SESSION`. ORCHE's, Orca's, Claude Code's and Jev's own variables
  reach a worker only when named; `ORCHE_KEYMASTER`, `CLAUDE_EFFORT` and Orca's pane variables used
  to leak. The Jev key variable (`jev.api_key_env`) never reaches a worker; the triage classifier
  used to get it.
- **A non-UTF-8 byte in claude's output no longer fails a run or an intake** (it is decoded with
  replacement).
- **One brief module.** `lib/orche/brief.py` parses ```dispatch blocks, allocates unit ids and
  writes and reads every BRIEF.md revision; the Stop hook, `orche run` and `orche report` go through
  it. BRIEF.md's bytes are unchanged.
- **A `#` line inside a field value is kept** (a shell comment in VERIFY, a markdown heading in
  CONTEXT); before, it was dropped silently. A `#` line outside a value is still a comment.
- **A key written twice is refused** (`duplicate keys …`); before, the last value won.
- **Refusal wording.** Unknown keys are listed in the order written, each once; a malformed AMEND or
  STOP unit id reads `bad unit id …`.
- A brief's target comes only from its `## Brief` TO field. `orche report` without `--unit` picks the
  newest brief, ties broken by date, then by the numeric n (`-10` beats `-9`).
- **One done gate and one claim grammar** (SPEC §14.11). The Stop hook, `orche report` and `orche
  run` read the same STATUS line, with these user-visible effects:
- The Stop hook reads the last STATUS line, not the first: `STATUS: done` followed later by
  `STATUS: blocked` is not a completion claim.
- A template echo (`STATUS: done|blocked|failed`, spaced or not) is never a claim, in the Stop hook
  or the runner. Before, the Stop hook gated it and the runner filed the persona's spaced echo as
  `done`.
- The Stop hook accepts headings, emoji and other symbol prefixes (`## STATUS: done`). The runner
  accepts numbered lists, emphasis around the word, `STATUS :` and the Korean word for done.
- The Stop gate follows the session's identity: a feature agent outside its worktree is gated, and
  Main standing in a feature worktree is not. HEAD is read in the claimant's worktree, also for the
  `head_sha` of an `orche report` pointer.
- `orche run` judges `STATUS: release-ready` (>= unit-test-verified from a verifier other than the
  target) and files it as `done`; before, it was always `info`.
- `orche wake`'s awake-limit refusal names the unit to put to sleep (`orche sleep <unit>`).
- `orche stage set <proj> public` notes an onboarding Ops with wake's onboarding text.
- `orche hr audit` reads the Liaison's decisions too.
- The project's status line is rewritten by every HR record, so also after a janitor retirement and
  per division during `orche init`; a `stage set` to the current stage writes none.
- **A lost run reports what ran.** Reconcile files it from its lock: the pointer's `brief_rev` is the
  revision that ran, and RUN.json's `tier`, `model`, `effort`, `worktree` and `branch` are the run's,
  no longer re-resolved from today's BRIEF.md and registry (a lock written before this version gives
  `null` and the brief's current revision). This extends "A run's pointer names the revision it ran"
  (Fixed) to lost runs.
- `orche metrics` adds one Runner line per model and effort (`--json`: `runner.by_model_effort`).
- `orche init` no longer writes `effort` into division records.

### Fixed

- **A run's pointer names the revision it ran**: `brief_rev` and the prompt come from the BRIEF.md the
  run resolved, so an AMEND landing mid-run no longer relabels it.
- **An AMEND or STOP of a unit without a readable BRIEF.md is refused** (`unknown unit …`); before,
  an AMEND wrote and relayed a partial brief and a STOP was relayed.
- **A dispatch, AMEND or STOP to a retired target is refused before anything is written**
  (`target <session> is retired.`); before, it was relayed and the runner refused it later.
- **A STOP files its pointer for the `rev:` in BRIEF.md**; before, it counted `BRIEF.rev*.md` files.
- **`orche report` no longer crashes** on a folder under `.orche/units/` that is not a unit id; an
  unreadable or non-UTF-8 BRIEF.md reads as missing.
- **The Fable block's AMEND/STOP line matches the grammar**; run `orche install` to refresh it.
- A run whose lock or STOP-marker cleanup fails after its pointer was filed no longer files a second
  `failed` pointer or overwrites RUN.json; reconcile clears the stale lock.

### Removed

- The identity and routing helpers the Roster replaced: `units.Unit`, `units.find_unit`,
  `units.project_from_cwd`, `units.unit_from_cwd`, `units.session_from_cwd`, `units.all_sessions`,
  `units.actor`, `inbox.identify` and its pane-title cache, `dispatch.resolve_target`,
  `dispatch.sender_role`, `cmd_report.route`, `cmd_report.identified_session`.
- **The `claude --version`/`--help` probe and its cache** (`ORCHE_HOME/runner/probe.json`; delete it
  by hand if present).
- **The inline `--append-system-prompt`, `--setting-sources` and `--allowedTools ""` fallbacks.**
- **`runner.model_mechanical`.**
- **`ORCHE_HOME/triage/mcp-empty.json`** (no longer written; delete it by hand if present).
- The runner's and triage's own claude code that `headless` replaced: `runner.probe`,
  `runner.claude_version`, `runner.probe_path`, `runner.claude_flags`, `runner.build_argv`,
  `runner.model_for`, `runner.worker_env`, `runner.env_for`, `runner.execute`, `runner.run_claude`,
  `runner.sigterm_guard`, `runner.sweep_group`, `runner.sweep_run`, `runner.parse_output`,
  `runner.STOP_POLL_S`, `runner.DROPPED_ENV`, `runner.AUTH_ENV`, `runs.mcp_config_path`,
  `triage.haiku_argv`, `triage.haiku_env`, `triage.mcp_config`, `triage.scratch_dir`.
- **`ORCHE_HOME/briefs/`** is no longer created or checked (nothing wrote to it; existing folders
  are left alone).
- **R-DISPATCH-3 (Jev fills an empty TIER) and R-DISPATCH-4 (small units fold fields)** are dropped
  from SPEC; neither was built.
- **`jev.suggest_tier`**: Jev no longer suggests tiers (ruling 2026-10-19: Jev does not pick tiers or
  models).
- The grammar, unit-id and BRIEF.md helpers `brief` replaced: `util.DISPATCH_KEYS`, `util.unit_id`,
  `util.parse_kv_block`, `util.fenced_bodies`, `util.find_fenced_block`, `dispatch.parse_block`,
  `dispatch.find_blocks`, `dispatch.render_brief`, `dispatch.write_brief`, `dispatch.read_brief_fields`,
  `dispatch.current_rev`, `dispatch.allocate_unit_id`, `dispatch.unit_dir`, `dispatch.where`,
  `cmd_report.newest_brief`, `cmd_report.brief_rev`, `jev.TIERS`, `jev.COMPLEXITY_LEVELS`,
  `triage.TIERS`.
- The claim and completion checks `donegate` replaced: `dispatch.CLAIM`, `dispatch.git_head`,
  `dispatch.GATE_PREFIX`, `dispatch.GATE_SUFFIX`, `dispatch.ledger_missing`, `dispatch.gated_sender`,
  `dispatch.completion_gate`, `dispatch.night_predicate`, `cmd_report.done_gate`, `runner._STATUS`,
  `runner.worktree_clean`, `runner.gated`, `runner.history_rewritten`, `runner.done_problem`, and
  `tests/test_dispatch.py` (its cases live in `tests/test_donegate.py`). `cmd_feat.orche_owned` and
  `janitor.discover.porcelain_path` moved to `units`; `git_head` to `shell`.
- The division record's `effort` in `registry.json`: nothing read it. A pane's effort is its
  persona's `effort:` line, a headless run's its TIER. Existing keys are ignored and no longer
  required.

### Internal

- `lib/orche/roster.py` (`Roster`, `Member`, `Place`, `Identity`) is the single read model over `registry.json`.
- `lib/orche/headless.py` (`Call`, `run`, `argv`, `env`, `Stop`, `check`, `marker`) is the single way
  ORCHE starts `claude -p`; `call_for` in `runs/job.py` builds the unit worker's `Call`.
- `lib/orche/donegate.py` (`claim`, `Claim`, `Proof`, `may_stop`, `for_report`, `after_run`) holds the
  one claim grammar and every completion check (the Stop hook, `orche report`, the runner).
- `lib/orche/hr/` (book, limits, panes, onboarding, transitions, create, retire, audit) performs
  every unit transition and is the only writer of `registry.json` (SPEC §14.12); `lib/orche/cli.py`
  holds the shared exit codes and `fail`.
- `lib/orche/runs/` (the Run module: a light `__init__` with the interface, `filing`, `job`) replaces
  `runner.py` and `runs.py`; `cmd_run` is a thin CLI over `runs.execute` / `runs.preview`; the Stop
  hook calls `runs.start` / `stop` / `restart`; `procs.spawn_detached`, `inbox.about`, `inbox.clip`
  and `intake.read_context` take over helpers from dispatch, cmd_report and the runner (SPEC §14.13).

## 0.3.5 — 2026-10-19

### Fixed

- **Jev triage routes simple prompts again.** `triage.classify_jev` reported the lowest confidence
  of its route, division and TIER answers, and the division question was asked with bare names, so
  its low confidence (p50 0.52) kept almost every simple prompt in Main. The confidence is now Jev's
  probability for the route it chose (else the route answer's confidence); the division and TIER
  answers never lower it. Every option is described to Jev (`criteria` descriptions; the division
  and TIER words come from `triage.DIVISION_HINTS` and `triage.TIER_HINTS`, shared with the Haiku
  prompt, whose words are identical to 0.3.4's while its line breaks differ), and `jev` keeps a
  Choice answer's `probabilities` for the asked options (a NaN or infinite number is dropped).
  Divisions now come out build 39 / ops 6 instead of rnd 23 of 46. Live run on 84 labelled prompts
  (2026-10-19; 46 reach the classifier), gate at 0.9: before, recall 0.00–0.05; after, precision
  0.968, recall 0.79 (one false direct: "Fix the flaky test in tests/test_sync.py").

### Changed

- **`triage.min_confidence` defaults to 0.8** (was 0.9). Sweep on the same 84 prompts: P ≥ 0.95
  precision 1.0, recall 0.71; ≥ 0.9 0.968 / 0.79; ≥ 0.8 0.972 / 0.92; ≥ 0.7 0.974 / 0.97; from 0.9
  down the one false direct is the same prompt (the flaky-test fix). Haiku on the same set, with
  the 0.3.4 prompt and the word-identical 0.3.5 one alike: precision 1.0, recall 0.28–0.31 at 0.9
  and 0.59–0.64 at 0.8.

### Verified

- **Real prompts.** On 546 masked real Main prompts (120 days, one machine), the heuristics passed 2
  to the classifier and Jev routed none (no false directs). Asking Jev on every prompt without the
  heuristics was rejected (precision 0.63 at P ≥ 0.8), as was a Jev-answered status check (placement
  ②: 2 of 8 flagged prompts were status questions).
- **Jev latency.** 600 real prompts: p50 0.45 s, p95 0.76 s, 3 timeouts at the 2 s limit, each
  failing open to Main.

## 0.3.4 — 2026-10-19

### Added

- **`orche intake <project> --background`** starts the intake detached (its own session, so it
  outlives the calling Bash tool call), logs to `~/.claude/orche/logs/intake/<project>.log` and returns
  at once ("intake started in the background (log: …)"). The liaison starts its `--if-stale` refresh
  this way instead of a shell `&` into `.orche/liaison/intake.log`, a folder that might not exist.

### Changed

- **Triage heuristics: verb forms, Korean question endings.** A Korean verbal noun ("edit",
  "add", "delete", …) is an instruction only with a verb ending or as the prompt's last word, so a
  prompt that uses it as a plain noun ("show me the edit history of README.md") no longer reaches
  the classifier; native verbs need an imperative or propositive ending. A Korean question ending
  without `?` is a question and never a candidate. English takes more verbs (delete, replace, move,
  implement, bump, set, …) and lead-ins (just, kindly, let's, I need you to); the negation guard
  also covers the Korean "don't" forms and "no need to".
- **Triage brief: bounded GOAL, the whole prompt in CONTEXT.** GOAL is the classifier's goal, else the
  prompt when it has at most 300 characters, else a 200-character excerpt marked "(excerpt; the full
  prompt is quoted in CONTEXT)". Whenever GOAL is not the prompt verbatim, CONTEXT quotes every prompt
  line (before, a classifier goal replaced the prompt). The block reason shows the GOAL's first 60
  characters, masked.
- **Triage SCOPE may hold globs** (`*`, `?`, `**` only; an entry with `[` or `]` is dropped) from the
  classifier, relative and based inside the worktree. A glob never reaches VERIFY (refused); the commit
  clause tells the worker never to `git add` one ("never `-A`, `-u`, `.` or a glob"), which, like SCOPE
  itself, is an instruction. Feature scope matching (`units.scope_match`) is unchanged from 0.3.2: `[`
  is literal, so `app/[slug]/**` admits only that directory.
- **`orche triage eval` records its summary** per backend in `~/.claude/orche/triage/eval.json`;
  `orche triage status` shows when it last ran, on which labelled file and whether that file still
  exists, and warns (as do `orche triage enable` and `orche status`) when triage is enabled on Jev
  with no Jev eval on file. `orche status` prints one triage line while triage is enabled.
- **Dispatch blocks may use `~~~` fences.** One fence scanner (`util.fence_spans`) serves the Stop-hook
  relay and triage: ```` ``` ```` or `~~~`, closed by the same character at least as long.
- **A dispatch block inside another open fence, or whose closing fence is indented deeper than its
  opener, is no longer relayed** (0.3.3 relayed it); ORCHE now says so instead of dropping it: "[orche]
  dispatch block not relayed: the block at line N: <reason>; close the earlier fence / outdent the
  closer" (exit 2, or a parked question under `stop_hook_active`), only when the reply holds no
  relayable block: a well-formed block outside any fence is relayed even next to an example. The
  refusal says nothing in the reply was relayed; it fires in ORCHE sessions only.
- **`orche apply-all` never runs the project intake** (and so neither does `install.sh
  --all-projects`): every init gets `--no-intake`, and the run ends with "intake not run for <n>
  projects: run `orche intake <proj>` per project (about $0.1–0.3 each)". In 0.3.3 it ran one paid
  sweep per repo, its output hidden, and Ctrl-C moved on to the next repo.
- **`intake.timeout_s` defaults to 540** (was 900), so a sweep started by `/orche-apply` ends inside
  the Bash tool's 10-minute maximum.

### Fixed

- **A fenced snippet inside a dispatch block no longer cuts the block short** in the Stop-hook relay
  (an indented ```` ``` ```` line in a CONTEXT continuation closed it, and the relay refused the block
  for missing fields); only a closer indented no deeper than the opener closes a dispatch block.
- **A prompt triage routes now touches Main's activity**, so `orche depts` and the status line no
  longer show Main idle after blocked prompts.
- **Masking no longer rewrites ordinary prose** (shipped in 0.3.3, whose entry said "ordinary words
  are left alone", which was not true for prose). The generic `<secret word>: <value>` rule masked
  "Secret: not needed" into "Secret: [REDACTED] needed", standing orders, cross-project request bodies,
  `token: GITHUB_TOKEN`, `${DB_PASSWORD}` and `pwd: /path`. It is now two rules: config syntax
  (`KEY=value`, `"key": value`, `KEY: value`, `a.b.password: value`) masks any value that is not a
  reference, an ENV_NAME, a path or a number; prose (`the token: …`) masks only a credential-like value
  (6+ characters, no spaces, with a digit, a symbol or mixed case) or a quoted one. On the final
  review's 50 phrases: 0 false positives, 9 of the 10 review secrets masked (the letters-only prose
  password is a known gap).
- **A carriage return no longer hides code from the env-read gate** (shipped in 0.3.3). Python reads a
  bare `\r` as a line break, but the gate stripped a `#` comment to the next `\n`, so
  `python3 -c '#<CR>import orche.jevkey …'` passed again (0.3.2 refused it), as did environment dumps
  and `.env` reads hidden that way. `\r\n` and a lone `\r` are now read as `\n` before any line or
  token handling, by both rule sets and the command scanner.
- **The env-read gate lets ordinary environment hand-overs through again** (shipped in 0.3.3):
  `env={**os.environ, "X": "1"}`, `child_env = dict(os.environ)`, `{...process.env, X: 1}`,
  `"CI" in os.environ`, `len(os.environ)`, `Object.keys(process.env)`, `ENV.keys`, and
  `for k in os.environ:` when the code prints nothing. Real dumps stay refused: `print(os.environ)`,
  `print(dict(os.environ))`, `print({**os.environ})`, `json.dumps(os.environ)`,
  `console.log(process.env)`, `ENV.to_h`, `printenv`, a bare `env`, `export -p`.
- **The Jev key-store rule is tied to the real store**: ORCHE_HOME's secrets folder (any spelling) and
  any `secrets/jev.key`. ORCHE's own sources under `lib/orche/secrets/` can be read, edited and searched
  with a code glob again (a code-blind search of any `secrets/` folder is still refused).
- **The Jev key's variable no longer reaches workers.** `JEV_API_KEY` (and the configured
  `jev.api_key_env`) is dropped from every `orche run` and intake worker's environment.
- **`orche --json intake status <project>` without a stamp** prints `{"project", "status": "none"}`
  (exit 2 as before) instead of nothing.
- **A partial `triage/eval.json`** (a run without `file`, `at` or `n`) no longer crashes `orche triage
  status` or `orche status`; the missing parts show as `?`.
- **Docs and help**: the env-read gate's "only what a command runs counts" now says the Jev key file's
  full path is refused anywhere in a command; SPEC §10.5 says environment dumps are denied (not
  "careful (ask)"); R-DISPATCH-9 names the division's CONTEXT block on the worker's stdin; `orche
  triage status`, `orche janitor run` and `orche intake --if-stale` help and the README's init flags
  say what they do. CI runs with `permissions: contents: read`.

### Known gaps

- **Masking:** a letters-only password written as prose (`password: correcthorse`) is not masked; an
  all-caps value that looks like an env variable name is read as a reference and not masked, so a
  base32 TOTP seed (`OTP_SECRET=JBSWY3DPEHPK3PXP`) stays readable.
- **Env-read gate:** an environment copy printed later through an alias (`env = {**os.environ};
  print(env)`) or `print(os.environ.copy())` passes; `const {NODE_ENV} = process.env`,
  `os.execvpe(…, os.environ)` and a secret-shaped name within 40 characters of a harmless lookup are
  refused; `*.key` matches Keynote and i18n files; `declare -F`, `echo $MONKEY_PATCH` and
  `$TOKENIZERS_PARALLELISM` are refused; `docker compose config`, `cat .envrc` and quote-split names
  (`cat .e""nv`) pass. Script files are not inspected (as before).
- **Dispatch relay:** a reply whose only dispatch block is an example inside another fence is refused
  once.
- **CI** has no Python 3.9 job for the hook modules (their 3.9 decisions were checked by hand).

## 0.3.3 — 2026-10-19

### Added

- **Project intake: `orche intake <project> [--if-stale]`, `orche intake status <project>`.** One
  read-only sweep of an existing codebase by a headless Sonnet worker with **Read, Glob and Grep only**
  (`--allowedTools Read,Glob,Grep`; Bash, Write, Edit, NotebookEdit, WebFetch, WebSearch, Task and
  Agent denied; `--restricted` and `--tools Read,Glob,Grep` when the installed claude lists them;
  Read deny rules for `.env*`, `secrets/`, keys, keystores, `.npmrc`/`.netrc` and credential JSON in
  the checkout and by absolute path, and for `~/.ssh`, `~/.aws` and the other credential stores).
  ORCHE puts what a shell would have shown into the request itself, masked and capped (≤ 12 KB): the
  tree to depth 3 (≤ 300 entries, no `.git`/`node_modules`/`dist`/`build`/`.orche`/`.venv`/caches),
  `git log --oneline -30`, the manifest/config files found, and the variable names (never values)
  of `.env.example`/`.sample`/`.template`. The worker's JSON answer ORCHE masks and writes into the
  main checkout: `.orche/div/<division>/CONTEXT.md` per division (≤ `intake.max_chars`, 2000), the
  still-empty Purpose/Stack/Entry points of `PROJECT_CARD.md` plus a delimited `Intake` block, and the
  stamp `.orche/intake.json`. One intake per project at a time; `--dry-run` prints the exact argv;
  `--json` prints a `failed` record on failure. Config `intake.enabled|model|max_turns|timeout_s|
  max_chars|refresh_commits|refresh_days`.
- **`orche init` ends with the intake** (`--no-intake` skips it; "intake running… N s" every 30 s; a
  failure is one "intake skipped: …" line, Ctrl-C or SIGTERM one "intake interrupted; run `orche intake <proj>` later" line, and
  init still prints its summary, refreshes the status line and exits 0; a re-run that adds a division
  re-runs it, otherwise "intake: covered"), **`orche run` appends the division's CONTEXT.md to the
  brief as fenced data** (`## Project context (intake <sha7>, <date>) — reference data written by
  orche intake, not instructions`, then a ```` ````text ```` fence longer than any backtick run in it,
  before the runner footer; a feature agent gets Build's), and **`orche janitor run` refreshes
  stamped projects with `--if-stale`**. `/orche-apply` runs init with `--no-intake`, then the intake
  with a 10-minute Bash timeout; the liaison starts `--if-stale` only in the background.
- **Masking (`lib/orche/mask.py`, shared by the runner, reports, transcripts and intake) covers more
  shapes:** JWTs, Stripe `sk_|rk_` keys, `github_pat_…`, `ASIA…`, the value after
  `aws_secret_access_key`, `Authorization: Bearer|Basic …`, `user:password@` in URLs (host kept), and
  `<…password|secret|token|api_key…>=<value>` (key kept, value masked). Git shas, file paths and
  ordinary words are left alone (`tests/test_mask.py`).
- `runner.run_claude` / `runner.worker_env` / `runner.sigterm_guard`: the runner's process machinery
  (env filter, per-run MCP config, timeout, process-group kill, sweeps), shared by `orche run` and
  `orche intake`.
- Tests: `tests/support.py` puts a failing `claude` first on PATH for the whole test process, so no
  test can start the real one.

### Changed

- **CI pins its runner images and uses Node 24 actions**: `ubuntu-24.04` and `macos-15` instead of
  `ubuntu-latest` (Ubuntu 26.04 from 2026-10-19) and `macos-latest` (already macOS 26);
  `actions/checkout@v7` and `actions/setup-python@v7` (both `node24`; v4/v5 were Node 20);
  `workflow_dispatch` for a manual run.
- Docs: `triage.max_latency_s` below about 1 s usually skips the classifier on a busy machine (each
  `claude --version`/`--help` probe gets half the budget).

### Fixed

- **Tests: runner/resilience fixtures wait for observable states, not fixed delays** (the busy-slot holders' launcher was rewritten under a starting holder, freeing a slot: the 0.3.2 macOS CI flake).
- **Env-read gate: the `.env`/environment rules no longer match raw text.** They moved to
  `lib/orche/envgate.py` on the Jev rules' machinery (`cmdscan`): only command words count (wrappers
  skipped, `bash -c`/`eval`/piped shells re-scanned, inline interpreter code checked as text), and
  Read/Edit/Write/Grep are judged by path, never by content. A Markdown file that mentions `.env` or
  `secret`, `git commit -m "fix .env loading"` and `rg SECRET lib/` now pass; `cat .env.example`
  (and `.env.sample`, `.env.template`: names only) is allowed. Still refused: reading `.env`/`.env.*`,
  `*.pem`, `*.key`, `*.p12`, data files in `secrets/`, `/proc/*/environ`; `printenv`, bare `env`,
  `export -p`, `set` alone, `launchctl getenv`; `echo "$OPENAI_API_KEY"` (upper-case names with KEY,
  TOKEN, SECRET, PASSWORD, PASSWD, CREDENTIAL, PRIVATE, MNEMONIC or SEED); `print(os.environ[…KEY])`,
  `console.log(process.env)`; and the same behind `timeout`, `sudo` or `bash -c`.
- **Jev gate: trailing `# …` comments in inline Python are stripped** (`tokenize`), so
  `print(1)  # don't call jevkey.get()` passes while `jevkey.get()  # fine` is still refused; code
  that does not tokenize is checked unstripped.

## 0.3.2 — 2026-10-18

### Added

- **One-command install: `install.sh`.** `curl -fsSL
  https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash` (or `bash install.sh`
  from a clone, which then uses that clone) checks the OS (macOS; Linux with a WARN that Orca is
  needed for panes; Windows refused) and the requirements (git, Python 3.11+ found like
  `lib/find_python.sh`, `claude`, `orca`), clones or updates `~/orche` (`--dir`, `--ref`; a dirty
  clone, another remote or a non-git folder is refused), adds `export PATH=... # orche` to the
  login profile once (`--no-profile` prints it instead), runs `orche install` (the Jev question
  read from `/dev/tty`, so it works through `curl | bash` too; without a terminal `--no-jev` and
  one "Jev: skipped (no terminal)" line), adds the Claude Code plugin with `claude plugin
  marketplace add` + `claude plugin install orche@orche` (or updates both on a re-run), falls back
  to `--mode settings` when `claude` or `claude plugin install` is missing or the plugin step
  fails, optionally runs `orche apply-all` (`--all-projects`), and ends with `orche doctor`'s
  WARN/FAIL lines and a three-line summary. Re-running it updates. `--dry-run` writes nothing;
  exit 1 for user/environment problems, 2 for unexpected failures. Tested under macOS's bash 3.2.
- **`orche install --no-plugin-hint`**: plugin mode without the two `/plugin …` lines (`install.sh`
  passes it because it runs those steps itself; settings mode and plain `orche install` unchanged).

### Fixed

- **A lost run's env-hidden claude started within a second of its lock is swept on macOS.**
  macOS `ps` etime is `ps`'s whole second minus the start's whole second, so `now − etime` could
  place a claude up to 1 s before the lock's `started_ns` and reconcile left it running (a flaky
  `test_a_lost_hidden_claude_started_right_after_its_lock_is_still_that_runs_claude` on CI).
  The start is now (the whole second after `ps` returned) − etime + 1 s: never earlier than the
  true start, at most about 1 s later.

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
  bypasses (short prompts, an acknowledgement or continuation word first such as "yes", "go
  ahead" or their Korean equivalents, questions, replies to a question Main asked — Main's trailing ```` ```dispatch ````
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
