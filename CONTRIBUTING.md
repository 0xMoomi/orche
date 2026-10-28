# Contributing to ORCHE

Thanks for helping. ORCHE is small on purpose; please keep it that way.

## Ground rules

- **Python 3.11+, standard library only.** No pip packages in hooks, the CLI or `lib/`.
- **Shell-outs go through `lib/orche/shell.py`** (`run(cmd, *, timeout, dry_run, input)`), only to
  the tools the SPEC allows (`orca`, `gh`, `gcloud`, `vercel`, `claude`, `git`, `osascript`,
  `pbcopy`/`pbpaste`, `security`, `launchctl`, `age`, `tar`, `zstd`). Tests stub it.
- **Conventional commits:** `feat:`, `fix:`, `test:`, `docs:`, `chore:`. One logical change per commit.
- **No secrets, no personal data.** No keys, tokens, real email addresses or absolute
  home-directory paths in code, tests, fixtures or docs. Use `~`, `$HOME` or `example.com`.
  Before you push, both of these must print nothing:

  ```sh
  grep -rnE "/(Users|home)/[A-Za-z0-9_-]+" --exclude-dir=.git --exclude-dir=internal .
  grep -rnE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}" --exclude-dir=.git --exclude-dir=internal . \
    | grep -vE "actions/|@v[0-9]|noreply@anthropic.com|example\.(com|org)"
  ```

## Tests

```sh
python3 -m unittest discover -s tests
```

The suite must pass with no network and no real `orca`, `gh` or `gcloud`. Tests run against a
temporary `ORCHE_HOME` (`tests/support.py`) and a fake Orca: stub `shell.run` (or the Orca
adapter) to return canned JSON for `orca terminal list`, `orca worktree create`, and so on.
Hook tests feed fixture JSON on stdin and assert the exit code and stdout/stderr. Keep test
output clean: no stray prints or warnings.

## Where things live

```
bin/                 orche (sh launcher) and orche.py, which discovers lib/orche/cmd_*.py
lib/orche/cmd_*.py   one module per CLI command, found by file name
lib/orche/           top-level modules by theme: dispatch, brief, inbox, deliver (work flow);
                     registry, roster, units, project (who is where); headless, codex, vendors, ladder
                     (model calls); triage, jev*, envgate, cmdscan, mask (gates); orca, github, shell (tools)
lib/orche/<package>/ flow, hr, install, janitor, queue, review, runs, secrets: one domain each
hooks/               hooks.json, run.sh and one .py per Claude Code hook; logic lives in lib/
skills/, agents/     the plugin's skills and agent definitions
templates/           files ORCHE writes into projects and prompts it gives to agents
tests/               unittest suite, support.py and fixtures; no network, no real orca or gh
```

## Adding a command

1. Create `lib/orche/cmd_<name>.py` with `register(subparsers)`; `bin/orche.py` (started by the
   `bin/orche` sh launcher) discovers it by file name, so no other file changes.
2. Do **not** declare `--json` or `--dry-run` in your subparser. The root parser owns them and
   sets `args.json` / `args.dry_run`; a subparser default would silently overwrite the root value.
3. Mutating commands honor `args.dry_run` (print the actions, change nothing). Human output is
   English, one line per fact. Exit codes: 0 ok, 1 user error, 2 not found / unverified,
   3 blocked by policy.
4. Only `lib/orche/hr/` writes the registry; every other module reads it (`lib/orche/registry.py`,
   `lib/orche/roster.py`). Mask anything that may leave the machine with `lib/orche/mask.py`.

## Hooks

- Each hook finishes within the **1.8 s budget** (`ORCHE_HOOK_BUDGET_S`); hooks share one
  deadline for every Orca call they make.
- **Fail open:** on any internal exception a hook exits 0 and appends a line to
  `~/.claude/orche/logs/hooks.log`. The two security gates (`env_read_gate`, `scope_gate`) deny
  only on a positive pattern match, and allow and log on an internal error.
- Ignore events from subagents (`agent_id` present) unless the hook's purpose requires them.
- Hooks never type `/clear` or any other slash command into a pane.

## Pull requests

Describe the behavior change and link the SPEC rule (e.g. `R-INBOX-3`) it implements or changes.
When code and spec diverge, change the rule in docs/spec.md in place; no implementation notes.
