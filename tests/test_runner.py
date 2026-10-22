"""`orche run` (the headless runner) against a fake `claude` script, and relay delivery through it.

The fake `claude` (support.write_fake_claude) lives in a temp dir and is selected through config
`claude_bin`. It answers `--help` from help.txt and `--version` from `version`, records
argv/stdin/env/cwd, runs `action.sh` (commits, noise, a self-filed report), optionally sleeps (file
`sleep`; its pid goes to `pid`), prints `reply.json` and exits with the code in `exit`.
"""
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from orche import brief, dispatch, headless, procs, registry, roster, runs
from orche.runs import job as runner
from support import write_fake_claude
from test_hooks import GIT_ENV, HookCase
from test_registry import load_cli

MECH = ("Read,Write,Edit,Glob,Grep,Bash(ls:*),Bash(test:*),Bash(git status:*),Bash(git diff:*),"
        "Bash(git add:*),Bash(git commit:*)")
MECH_DENY = ["--disallowedTools", "Bash(git commit --amend*),Bash(git commit -n*),Bash(git commit --no-verify*),"
                                  "Bash(git push*)"]
SECRET = "sk-" + "a" * 30
REAL_POPEN = subprocess.Popen
ORCHE_BIN = str(support.ROOT / "bin" / "orche")          # the sh launcher (a human's `orche run`)
SPAWN = [sys.executable, str(support.ROOT / "bin" / "orche.py"), "run"]   # the Stop hook's spawn (R-B)
DELIVER = [sys.executable, str(support.ROOT / "bin" / "orche.py"), "deliver"]   # … for a pane unit
COMMIT = ("echo work >> work.txt\ngit add work.txt\n"
          "git -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false commit -q -m work\n")
ROW = ("printf '7\\t%s\\t{verdict}\\tlog\\tverifier-1\\t2026-10-18T00:00:00Z\\n' \"$(git rev-parse HEAD)\" "
       ">> \"$ORCHE_HOME/ledger/acme.tsv\"\n")


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def write_script(path: Path, text: str) -> Path:
    """An executable script, replaced atomically: a shell that is starting it reads either the
    old file or the new one, never a truncated one (Path.write_text truncates first, and a
    just-exec'd `/bin/sh` reading it then exits at once or fails on half a loop)."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.chmod(0o755)
    os.replace(tmp, path)
    return path


class RunnerCase(HookCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.wt = self.root / "acme-build"
        self.git_init(self.wt, "README")
        self.head = self.rev(self.wt)
        self.fake = self.root / "fake"
        self.claude = write_fake_claude(self.fake)
        self.config({"claude_bin": str(self.claude), "delivery": "runner"})
        self.reg = registry.load()

    def git_init(self, folder, *files):
        git = ["git", "-C", str(folder), "-c", "commit.gpgsign=false"]
        env = {**os.environ, **GIT_ENV}
        subprocess.run(["git", "init", "-q", str(folder)], check=True, env=env)
        if not files:
            (folder / "README").write_text("x\n")
            files = ("README",)
        for name in files:
            if not (folder / name).exists():
                (folder / name).write_text("x\n")
        subprocess.run(git + ["add", *files], check=True, env=env)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True, env=env)

    def rev(self, folder):
        return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()

    def config(self, data):
        (self.home / "config.json").write_text(json.dumps(support.no_wake(data)))

    def brief(self, to="div-acme-build", tier="standard", timebox="20m", sender="main-acme"):
        fields = {"TO": to, "GOAL": "Add coupon field", "SCOPE": "src/**", "CONTEXT": "none",
                  "ACCEPT": "works", "VERIFY": "true", "TIMEBOX": timebox, "FORBID": "merge",
                  "REPORT": "status", "TIER": tier}
        target = roster.Roster(registry.load()).member(to)    # not self.reg: add_research saves a division
        return brief.write(brief.Block("brief", fields), target, sender, "1. Never force-push.",
                           timeout=0).unit

    def reply(self, result, cost=0.31, turns=4, is_error=False, subtype="success", code=0):
        (self.fake / "reply.json").write_text(json.dumps({
            "type": "result", "subtype": subtype, "is_error": is_error, "num_turns": turns,
            "result": result, "total_cost_usd": cost, "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20}}))
        (self.fake / "exit").write_text(str(code))

    def action(self, script):
        (self.fake / "action.sh").write_text(script)

    def commit_action(self, verdict="unit-test-verified", dirty=False):
        """The worker commits work.txt; a verifier row for the new HEAD when `verdict`."""
        self.ledger()
        self.action(COMMIT + (ROW.format(verdict=verdict) if verdict else "") + ("echo wip > wip.txt\n" if dirty else ""))

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def pointers(self, session):
        return [json.loads(p.read_text()) for p in self.inbox(session)]

    def run_record(self, unit):
        return json.loads((self.units_dir() / unit / "RUN.json").read_text())

    def persona(self, to="div-acme-build"):
        path = self.wt / ".claude" / "agents" / f"{to}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("You are the build division.\n")
        return path

    # -- real `orche run` processes (signals) --------------------------------
    def spawn(self, unit, env=None):
        out = open(self.root / f"{unit}.out", "ab")
        self.addCleanup(out.close)
        proc = REAL_POPEN([ORCHE_BIN, "run", unit], stdout=out, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, env=env or dict(os.environ), start_new_session=True)
        self.addCleanup(self.reap, proc)
        return proc

    def sleeper(self, argv=("sleep", "60")):
        """A live process that is not `orche run` (a busy slot holder, a recycled pid)."""
        proc = REAL_POPEN(list(argv))
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def stubborn(self):
        """A live process (not `orche run`) that survives SIGTERM: a pid a STOP may signal."""
        return self.sleeper(["/bin/sh", "-c", "trap '' TERM; while :; do sleep 1; done"])

    def fake_run(self, *args):
        """A live process whose `ps` command line is `/bin/sh …/orche run <args…>`: a live run as
        far as runs.is_run_process (and so reconcile) can tell. Returned once `ps` shows exactly
        that (the pattern runs.is_run_process matches), never before."""
        folder = self.root / "launcher"
        folder.mkdir(exist_ok=True)
        script = folder / "orche"
        if not script.exists():      # shared by every holder: never rewritten under a starting one
            write_script(script, "#!/bin/sh\nwhile :; do sleep 1; done\n")
        proc = self.sleeper([str(script), "run", *args])
        pattern = runs._run_command(args[-1])
        self.assertTrue(self.wait_for(lambda: pattern.search(subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True).stdout)),
            f"`ps` never showed {script} run {' '.join(args)} (pid {proc.pid})")
        return proc

    def lock_file(self, name, pid=None):
        """A lock of `name`, held by `pid` or else by a live process `ps` shows as `orche run <name>`."""
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        pid = self.fake_run(name).pid if pid is None else pid
        (folder / f"{name}.lock").write_text(json.dumps({"pid": pid, "unit": name}))
        return pid

    def busy_slots(self, n=4):
        """`n` live locks of other units, each held by a process `ps` shows as its `orche run`:
        the project's run slots are full. Returns the holders once every one of them still is its
        run (alive and verified as reconcile verifies it), so a run started next has to wait."""
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        holders = []
        for i in range(n):
            unit = f"acme-other-20261018-{i + 10}"
            holders.append(self.fake_run(unit))
            (folder / f"{unit}.lock").write_text(json.dumps(
                {"pid": holders[-1].pid, "unit": unit, "to": f"div-other-{i}"}))
        for i, holder in enumerate(holders):
            self.assertIsNone(holder.poll(), f"busy-slot holder {i} exited: {holder.returncode}")
            self.assertIs(runs.is_run_process(holder.pid, f"acme-other-20261018-{i + 10}"), True)
        return holders

    def git_gate(self):
        """A `git` wrapper (first on self.git_env's PATH). While the returned `gate` file exists,
        each call appends its pid to `entered` and waits until the test removes `gate` (60 s at
        most), then runs the real git. `entered` shows the moment the runner is inside a git call of
        its `starting` (before claude) or `finishing` (after claude) phase; the test signals there
        and then opens the gate. (shell.git_head gives git 2 s: the test opens the gate well
        inside that.)"""
        folder = self.root / "gitgate"
        folder.mkdir(exist_ok=True)
        gate, entered = folder / "closed", folder / "entered"
        gate.unlink(missing_ok=True)
        entered.unlink(missing_ok=True)
        g, e = shlex.quote(str(gate)), shlex.quote(str(entered))
        write_script(folder / "git", f"#!/bin/sh\nif [ -f {g} ]; then\n  echo $$ >> {e}\n  i=0\n"
                                     f"  while [ -f {g} ] && [ $i -lt 1200 ]; do sleep 0.05; i=$((i + 1)); done\n"
                                     f"fi\nexec {shlex.quote(shutil.which('git'))} \"$@\"\n")
        self.git_env = {**os.environ, "PATH": f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"}
        return gate, entered

    def claude_sleep_pid(self):
        """The pid of the fake claude's `sleep` once `ps` shows it exec'd, i.e. the fake has read
        its `sleep` file (its `pid` file is written just before that read). Call after
        claude_sleeping()."""
        parent, found = (self.fake / "pid").read_text().strip(), []

        def look():
            out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout
            found[:] = [int(f[0]) for f in (line.split(None, 2) for line in out.splitlines())
                        if len(f) == 3 and f[1] == parent and f[2].startswith("sleep ")]
            return bool(found)
        self.assertTrue(self.wait_for(look), f"no `sleep` child of the fake claude (pid {parent})")
        return found[0]

    def reap(self, proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        pid = self.fake / "pid"
        if pid.exists() and alive(pid.read_text().strip()):
            os.kill(int(pid.read_text().strip()), signal.SIGKILL)

    def wait_for(self, cond, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def lock(self, unit):
        path = self.home / "runs" / "acme" / f"{unit}.lock"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def claude_sleeping(self, unit):
        return self.wait_for(lambda: (self.fake / "pid").exists() and "pgid" in (self.lock(unit) or {}))


def digest(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*") if p.is_file()}


class DryRunArgvTest(RunnerCase):
    def base(self, unit, model, effort, turns, tools, *rest):
        """The run id exists only once the run has its slot: a dry run names a placeholder. `rest`
        (deny list, persona) sits before the gates' --settings pair, which is always last (D8)."""
        return [str(self.claude), "-p", "--model", model, "--effort", effort, "--output-format", "json",
                "--max-turns", str(turns),
                "--setting-sources", "project", "--permission-mode", "bypassPermissions",
                "--strict-mcp-config", "--mcp-config",
                str(self.home / "runner" / "mcp" / f"{unit}@<started_ns>.json"), "--allowedTools", tools,
                *rest, "--settings", headless.gates()]

    def test_standard_unit_argv_is_exact_and_nothing_is_written(self):
        unit = self.brief(tier="standard")
        persona = self.persona()
        before_home, before_repo = digest(self.home), digest(self.repo)
        code, out, err = self.orche("--dry-run", "run", unit)
        self.assertEqual(code, 0, err)
        argv = self.base(unit, "claude-opus-5-5", "medium", 60, MECH + ",Bash",
                         "--append-system-prompt-file", str(persona))
        self.assertEqual(out.splitlines(), [shlex.join(argv)])
        self.assertIn("ORCHE_SESSION=div-acme-build", err)
        self.assertIn("timeout 1260s", err)   # TIMEBOX 20m + 60 s
        self.assertEqual((digest(self.home), digest(self.repo)), (before_home, before_repo))
        self.assertFalse((self.fake / "stdin.txt").exists())   # claude itself never ran (only --help)

    def build_model(self, model):
        """Set the fixture's build division model in the registry."""
        path = self.home / "registry.json"
        reg = json.loads(path.read_text())
        reg["projects"]["acme"]["divisions"]["build"]["model"] = model
        path.write_text(json.dumps(reg))

    def test_mechanical_unit_uses_the_build_model_at_low_effort_and_the_narrow_tool_list(self):
        unit = self.brief(tier="mechanical")
        code, out, err = self.orche("--dry-run", "--json", "run", unit)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        # It may commit (git add/commit); amend, -n, --no-verify and push are denied best-effort
        # (the done gate's ancestry check is the guarantee); no persona file: no flag.
        self.assertEqual(data["argv"], self.base(unit, "claude-opus-5-5", "low", 60, MECH, *MECH_DENY))
        self.assertEqual((data["cwd"], data["tier"], data["timeout_s"]), (str(self.wt), "mechanical", 1260))
        persona = self.persona()
        _, out, _ = self.orche("--dry-run", "--json", "run", unit)
        self.assertEqual(json.loads(out)["argv"], self.base(unit, "claude-opus-5-5", "low", 60, MECH, *MECH_DENY,
                                                             "--append-system-prompt-file", str(persona)))

    def test_turns_follow_timebox_at_every_tier(self):
        self.build_model("m-x")

        def turns(unit):
            _, out, _ = self.orche("--dry-run", "--json", "run", unit)
            argv = json.loads(out)["argv"]
            return int(argv[argv.index("--max-turns") + 1])

        hard, mech = self.brief(tier="hard"), self.brief(tier="mechanical")
        _, out, _ = self.orche("--dry-run", "--json", "run", hard)
        self.assertEqual(json.loads(out)["argv"], self.base(hard, "m-x", "high", 60, MECH + ",Bash"))
        _, out, _ = self.orche("--dry-run", "--json", "run", mech)
        self.assertEqual(json.loads(out)["argv"], self.base(mech, "m-x", "low", 60, MECH, *MECH_DENY))
        got = {(tier, tb): turns(self.brief(tier=tier, timebox=tb))
               for tier in ("mechanical", "standard", "hard") for tb in ("5m", "15m", "30m", "2h", "4h")}
        self.assertEqual(got, {(tier, tb): n for tier in ("mechanical", "standard", "hard")
                               for tb, n in (("5m", 30), ("15m", 45), ("30m", 90), ("2h", 360), ("4h", 400))})
        self.config({"claude_bin": str(self.claude),
                     "runner": {"turns_per_minute": 1, "turns_floor": 10, "turns_ceiling": 50}})
        self.assertEqual([turns(self.brief(tier="hard", timebox=tb)) for tb in ("5m", "20m", "2h")], [10, 20, 50])
        self.config({"claude_bin": str(self.claude), "runner": {"turns_ceiling": 100}})   # the rest: defaults
        self.assertEqual([turns(self.brief(timebox=tb)) for tb in ("5m", "20m", "2h")], [30, 60, 100])
        self.assertEqual((runner.max_turns(1200, None), runner.max_turns(1200, {"turns_per_minute": 0.5})),
                         (60, 30))

    def test_feature_unit_uses_its_division_model(self):
        subprocess.run(["git", "init", "-q", str(self.root / "acme-checkout")], check=True)
        self.build_model("m-x")
        unit = self.brief(to="feat-acme-checkout")
        code, out, err = self.orche("--dry-run", "--json", "run", unit)
        self.assertEqual(code, 0, err)
        argv = json.loads(out)["argv"]
        at = argv.index("--model")
        self.assertEqual(argv[at:at + 4], ["--model", "m-x", "--effort", "medium"])

    def test_timebox_parsing(self):
        cases = {"30m": 1800, "2h": 7200, "~45 min": 2700, "1.5 hours": 5400, "about an hour": 1800,
                 "": 1800, "10 minutes; report partial": 600,
                 # M-4: compound values add up, unknown units fall back, the cap is 4 h
                 "1h30m": 5400, "1 h 30 min": 5400, "2h, then 30m review": 7200, "48h": 14400,
                 "1d": 1800, "45s": 1800, "30m then 2h": 1800}
        self.assertEqual({k: runner.timebox_seconds(k) for k in cases}, cases)


class StatusTest(RunnerCase):
    def test_status_line_is_case_insensitive_and_ignores_the_template_echo(self):
        """M-1."""
        self.assertEqual(runner.status_of("ok\nstatus: blocked\nneed key")[:2], ("blocked", "status: blocked"))
        self.assertEqual(runner.status_of("Status: Done.\nx")[0], "done")
        self.assertEqual(runner.status_of("End with `STATUS: done|blocked|failed`")[0], None)
        self.assertEqual(runner.status_of("STATUS: done|blocked|failed\nSTATUS: failed\nboom")[0], "failed")


class RunTest(RunnerCase):
    def test_done_with_a_new_commit_and_its_ledger_row_files_done_and_records_the_run(self):
        unit = self.brief()
        self.persona()
        self.commit_action()
        self.reply(f"Did it. token {SECRET}\nVERIFY output: ok\nSTATUS: done\ncoupon field added\ntests pass")
        code, out, err = self.orche("run", unit)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"ran {unit} -> done", out)
        new = self.rev(self.wt)
        self.assertNotEqual(new, self.head)
        rec = self.run_record(unit)
        self.assertEqual({k: rec[k] for k in ("unit", "to", "model", "cost_usd", "num_turns", "exit_code",
                                              "is_error", "stop_reason", "status", "head_before", "head_sha",
                                              "tier", "pointer_filed")},
                         {"unit": unit, "to": "div-acme-build", "model": "claude-opus-5-5", "cost_usd": 0.31,
                          "num_turns": 4, "exit_code": 0, "is_error": False, "stop_reason": "end_turn",
                          "status": "done", "head_before": self.head, "head_sha": new, "tier": "standard",
                          "pointer_filed": True})
        self.assertEqual(rec["usage"], {"input_tokens": 10, "output_tokens": 20})
        keys = list(rec)
        self.assertEqual((rec["effort"], keys[keys.index("model") + 1]), ("medium", "effort"))
        self.assertTrue(rec["started"] and rec["finished"])
        self.assertNotIn(SECRET, rec["result_tail"])
        report = (self.units_dir() / unit / "REPORT.md").read_text()
        self.assertIn("STATUS: done", report)
        self.assertNotIn(SECRET, report)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["unit"], ptr["from"], ptr["to"], ptr["status"], ptr["ledger_row"], ptr["head_sha"]),
                         (unit, "div-acme-build", "main-acme", "done", True, new))
        self.assertEqual(ptr["summary"], "coupon field added\ntests pass")
        self.assertEqual(ptr["report_path"], str(self.units_dir() / unit / "REPORT.md"))
        # The worker got the brief + footer on stdin, its session, its worktree and the plugin bin.
        stdin = (self.fake / "stdin.txt").read_text()
        self.assertTrue(stdin.startswith("rev: 1\nunit: " + unit))
        self.assertIn(f"You run headless. Work only inside {self.wt}.", stdin)
        self.assertIn("Do not commit, push, merge or rebase unless the brief allows it.", stdin)
        self.assertIn("Do not run `orche report`: the runner files your pointer from your final message.", stdin)
        self.assertIn("`STATUS: done|blocked|failed`", stdin)
        self.assertEqual((self.fake / "session.txt").read_text().strip(), "div-acme-build")
        self.assertEqual((self.fake / "cwd.txt").read_text().strip(), str(self.wt))
        self.assertTrue((self.fake / "path.txt").read_text().startswith(str(support.ROOT / "bin") + ":"))
        self.assertIn("ORCHE_RUNNER=1\n", (self.fake / "env.txt").read_text())
        self.assertEqual(list((self.home / "runner" / "mcp").iterdir()), [])   # the run's own config: gone
        self.assertIn(f"run unit={unit} to=div-acme-build model=claude-opus-5-5 cost=0.31 turns=4 status=done "
                      "stop=end_turn\n", self.events())
        self.assertTrue((self.home / "activity" / "div-acme-build").exists())
        self.assertEqual(ptr["via"], "runner")
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])

    def test_done_without_a_commit_is_unverified_even_with_a_row_for_the_old_head(self):
        """I-2: a stale ledger row for the pre-run HEAD no longer verifies a run that committed nothing."""
        unit = self.brief()
        self.ledger(("7", self.head, "unit-test-verified", "log", "verifier-1", "2026-10-18T00:00:00Z"))
        self.reply("I did nothing\nSTATUS: done\nall good")
        code, _, _ = self.orche("run", unit)
        self.assertEqual(code, 0)
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["status"], "info")
        self.assertEqual(ptr["summary"], "unverified: STATUS: done (no commit)\nall good")
        self.assertEqual(self.run_record(unit)["status"], "info")

    def test_done_with_uncommitted_changes_or_without_a_row_is_unverified(self):
        """I-2: a commit with a dirty tree, then a clean commit without (or below) a ledger row."""
        cases = ((1, dict(dirty=True), "(uncommitted changes)"),
                 (2, dict(verdict=None), "(no ledger row for {sha})"),
                 (3, dict(verdict="verifier-failed"), "(ledger verdict verifier-failed for {sha})"))
        for n, kw, why in cases:
            unit = self.brief()
            self.commit_action(**kw)
            self.reply("STATUS: done\nok")
            self.orche("run", unit)
            (self.wt / "wip.txt").unlink(missing_ok=True)
            sha = self.rev(self.wt)[:7]
            [ptr] = [p for p in self.pointers("main-acme") if p["unit"] == unit]
            self.assertEqual((ptr["status"], ptr["summary"]),
                             ("info", f"unverified: STATUS: done {why.format(sha=sha)}\nok"), n)

    def test_done_after_an_amend_is_unverified_at_every_tier(self):
        """I-B: an amend (in any spelling the deny list misses) rewrites history: `info`, never `done`,
        even with a clean tree and a verifier row for the new HEAD."""
        amend = ("echo more >> README\ngit add README\n"
                 "git -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false "
                 "commit -q --amen -m rewritten\n")
        for n, tier in enumerate(("mechanical", "standard", "hard"), 1):
            before = self.rev(self.wt)
            unit = self.brief(tier=tier)
            self.ledger()
            self.action(amend + ROW.format(verdict="unit-test-verified"))
            self.reply("STATUS: done\nok")
            code, _, _ = self.orche("run", unit)
            self.assertEqual(code, 0)
            self.assertNotEqual(self.rev(self.wt), before)
            [ptr] = [p for p in self.pointers("main-acme") if p["unit"] == unit]
            self.assertEqual((ptr["status"], ptr["summary"]),
                             ("info", f"unverified: STATUS: done (history rewritten ({before[:7]} is not an "
                                      f"ancestor of HEAD))\nok"), tier)
            self.assertEqual(self.run_record(unit)["status"], "info")

    def test_release_ready_files_done_with_a_verified_row(self):
        """Q4: release-ready from a gated target, a new commit and a unit-test-verified row from
        another verifier files `done`."""
        unit = self.brief()
        self.commit_action(verdict="unit-test-verified")
        self.reply("shipped\nSTATUS: release-ready\nok")
        self.assertEqual(self.orche("run", unit)[0], 0)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("done", "ok"))

    def test_release_ready_below_unit_test_verified_is_unverified(self):
        unit = self.brief()
        self.commit_action(verdict="type-check-only")
        self.reply("STATUS: release-ready\nok")
        self.assertEqual(self.orche("run", unit)[0], 0)
        sha = self.rev(self.wt)[:7]
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]),
                         ("info", f"unverified: STATUS: release-ready (ledger verdict type-check-only for {sha})\nok"))

    def test_release_ready_verified_by_the_claimant_is_unverified(self):
        """Q4: a unit-test-verified row whose verifier is the target itself does not prove
        release-ready; the run is filed info, not done."""
        unit = self.brief()
        claimant = roster.Roster(registry.load()).member("div-acme-build").session
        self.commit_action(verdict=None)
        self.action(COMMIT + ROW.format(verdict="unit-test-verified").replace("verifier-1", claimant))
        self.reply("STATUS: release-ready\nok")
        self.assertEqual(self.orche("run", unit)[0], 0)
        sha = self.rev(self.wt)[:7]
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]),
                         ("info", f"unverified: STATUS: release-ready (ledger verifier {claimant} is the "
                                  f"claimant for {sha})\nok"))
        self.assertEqual(self.run_record(unit)["status"], "info")

    def test_an_echoed_status_template_is_no_status_line(self):
        """Q8: the footer's echo `STATUS: done | blocked | failed | stop` is not a claim."""
        unit = self.brief()
        self.commit_action()
        self.reply("STATUS: done | blocked | failed | stop\nok")
        self.assertEqual(self.orche("run", unit)[0], 0)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]),
                         ("info", "unverified: no STATUS line\nSTATUS: done | blocked | failed | stop\nok"))

    def test_a_numbered_status_line_is_a_claim(self):
        unit = self.brief()
        self.commit_action()
        self.reply("x\n1. STATUS: done\nok")
        self.assertEqual(self.orche("run", unit)[0], 0)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("done", "ok"))

    def test_blocked_and_failed_pass_through(self):
        for n, word in ((1, "blocked"), (2, "failed")):
            unit = self.brief()
            self.reply(f"STATUS: {word}\nneed the API key")
            code, _, _ = self.orche("run", unit)
            self.assertEqual(code, 1 if word == "failed" else 0)
            ptr = [p for p in self.pointers("main-acme") if p["unit"] == unit][0]
            self.assertEqual((ptr["status"], ptr["summary"]), (word, "need the API key"))

    def test_stdin_and_pointer_carry_the_revision_the_run_was_given(self):
        """BRIEF.md is read once at resolve: a rewrite during the run changes neither the prompt
        nor the pointer's brief_rev."""
        unit = self.brief()
        self.reply("STATUS: blocked\nneed the API key")
        self.action(f"printf 'rev: 2\\n' > {shlex.quote(str(self.units_dir() / unit / 'BRIEF.md'))}\n")
        self.orche("run", unit)
        self.assertTrue((self.fake / "stdin.txt").read_text().startswith("rev: 1\n"))
        [ptr] = [p for p in self.pointers("main-acme") if p["unit"] == unit and p.get("via") == "runner"]
        self.assertEqual(ptr["brief_rev"], 1)

    def test_lowercase_status_and_template_echo(self):
        """M-1 through a run: `status: blocked` counts; an echoed template is no `done` claim."""
        unit = self.brief()
        self.reply("status: blocked\nneed key")
        self.orche("run", unit)
        unit2 = self.brief()
        self.commit_action()
        self.reply("Reminder: end with STATUS: done|blocked|failed")
        self.orche("run", unit2)
        got = {p["unit"]: (p["status"], p["summary"].splitlines()[0]) for p in self.pointers("main-acme")}
        self.assertEqual(got, {unit: ("blocked", "need key"), unit2: ("info", "unverified: no STATUS line")})

    def test_nonzero_exit_with_a_parsed_result_files_failed(self):
        """M-2."""
        unit = self.brief()
        self.reply("STATUS: blocked\nhalf way", code=2)
        code, _, _ = self.orche("run", unit)
        self.assertEqual(code, 1)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]),
                         ("failed", "run failed: claude exited 2 (STATUS: blocked)\nhalf way"))

    def test_noise_before_the_json_keeps_the_result(self):
        """I-7: an update banner on stdout no longer turns a finished run into `failed`."""
        unit = self.brief()
        self.action("echo 'Update available: 2.1.303'\n")
        self.reply("STATUS: blocked\nwaiting on design")
        code, _, _ = self.orche("run", unit)
        self.assertEqual(code, 0)
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["cost_usd"], rec["num_turns"], rec["error"]), ("blocked", 0.31, 4, None))

    def test_crash_files_failed_without_a_report(self):
        unit = self.brief()
        (self.fake / "exit").write_text("3")
        code, out, _ = self.orche("--json", "run", unit)
        self.assertEqual(code, 1)
        rec = json.loads(out)
        self.assertEqual((rec["status"], rec["exit_code"], rec["is_error"]), ("failed", 3, True))
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["status"], "failed")
        self.assertTrue(ptr["summary"].startswith("run failed: exit 3"), ptr["summary"])
        self.assertFalse((self.units_dir() / unit / "REPORT.md").exists())

    def test_worker_stderr_is_masked_in_the_run_record(self):
        """I-4."""
        unit = self.brief()
        self.action(f"echo 'auth failed key={SECRET}' >&2\n")
        (self.fake / "exit").write_text("2")
        self.orche("run", unit)
        raw = (self.units_dir() / unit / "RUN.json").read_text()
        self.assertNotIn(SECRET, raw)
        self.assertIn("auth failed key=", self.run_record(unit)["error"])
        [ptr] = self.pointers("main-acme")
        self.assertNotIn(SECRET, ptr["summary"])

    def test_claude_error_result_files_failed(self):
        unit = self.brief()
        self.reply("", is_error=True, subtype="error_max_turns", code=1, turns=61, cost=1.234)
        self.orche("run", unit)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]),
                         ("failed", "run failed: turn cap reached (61 turns, $1.23) — re-run with a larger TIMEBOX "
                                    "or split the unit"))
        self.assertEqual(self.run_record(unit)["stop_reason"], "error_max_turns")    # not the API's end_turn
        self.assertIn(f"run unit={unit} to=div-acme-build model=claude-opus-5-5 cost=1.234 turns=61 status=failed "
                      "stop=error_max_turns\n", self.events())
        other = self.brief()
        self.reply("", is_error=True, subtype="error_during_execution", code=1, cost=None)
        self.orche("run", other)
        [ptr] = [p for p in self.pointers("main-acme") if p["unit"] == other]
        self.assertEqual(ptr["summary"], "run failed: claude reported an error (error_during_execution)")

    def test_timeout_kills_the_worker_and_files_failed(self):
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        started = time.monotonic()
        with mock.patch.object(runner, "timebox_seconds", return_value=0), mock.patch.object(runner, "GRACE_S", 1):
            code, _, _ = self.orche("run", unit)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(code, 1)
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["error"], rec["timeout_s"]), ("failed", "timeout after 1s", 1))
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("failed", "run failed: timeout after 1s"))
        self.assertFalse(alive((self.fake / "pid").read_text().strip()))

    def test_worker_written_report_is_kept(self):
        unit = self.brief()
        (self.fake / "report_to").write_text(str(self.units_dir() / unit / "REPORT.md"))
        self.reply("STATUS: blocked\nx")
        self.orche("run", unit)
        self.assertEqual((self.units_dir() / unit / "REPORT.md").read_text(), "worker wrote this\n")

    def test_stale_report_of_an_earlier_run_is_set_aside(self):
        """M-9: an empty result never links the previous run's REPORT.md."""
        unit = self.brief()
        (self.units_dir() / unit / "REPORT.md").write_text("old run\n")
        self.reply("")
        self.orche("run", unit)
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["report_path"], "")
        self.assertFalse((self.units_dir() / unit / "REPORT.md").exists())
        self.assertEqual((self.units_dir() / unit / "REPORT.prev.md").read_text(), "old run\n")

    def test_each_run_gets_its_own_empty_mcp_config_removed_when_it_ends(self):
        """M-12, m-2: claude gets `--mcp-config ORCHE_HOME/runner/mcp/<run_id>.json` (its
        ORCHE_RUN_ID), holding `{"mcpServers": {}}` while it runs; the file is gone afterwards."""
        unit = self.brief()
        self.action('cp "$(sed -n \'/^--mcp-config$/{n;p;}\' "$(dirname "$0")/argv.txt")" "$(dirname "$0")/mcp.json"\n')
        self.reply("STATUS: blocked\nx")
        self.assertEqual(self.orche("run", unit)[0], 0)
        env = dict(line.split("=", 1) for line in (self.fake / "env.txt").read_text().splitlines() if "=" in line)
        mcp = self.home / "runner" / "mcp" / f"{env['ORCHE_RUN_ID']}.json"
        self.assertTrue(env["ORCHE_RUN_ID"].startswith(f"{unit}@"))
        argv = (self.fake / "argv.txt").read_text().splitlines()
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(mcp))
        self.assertEqual(json.loads((self.fake / "mcp.json").read_text()), {"mcpServers": {}})
        self.assertFalse(mcp.exists())

    def test_worker_env_is_headless_env_end_to_end(self):
        """The worker gets headless.env: Main's session, Orca and Jev variables never pass, the auth
        variable does; the call sets ORCHE_SESSION / ORCHE_RUNNER and the run's ORCHE_RUN_ID."""
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        caller = {"ORCHE_SESSION": "main-acme", "ORCHE_KEYMASTER": "1", "CLAUDE_EFFORT": "max",
                  "ORCA_PANE_KEY": "p", "JEV_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        with mock.patch.dict(os.environ, caller):
            self.assertEqual(self.orche("run", unit)[0], 0)
        env = dict(line.split("=", 1) for line in (self.fake / "env.txt").read_text().splitlines() if "=" in line)
        argv = (self.fake / "argv.txt").read_text().splitlines()
        rid = Path(argv[argv.index("--mcp-config") + 1]).stem
        self.assertTrue(rid.startswith(f"{unit}@"))
        names = ("ORCHE_SESSION", "ORCHE_RUNNER", "ORCHE_RUN_ID", "CLAUDE_CODE_OAUTH_TOKEN", "ORCHE_KEYMASTER",
                 "CLAUDE_EFFORT", "ORCA_PANE_KEY", "JEV_API_KEY")
        self.assertEqual({k: env.get(k) for k in names},
                         {"ORCHE_SESSION": "div-acme-build", "ORCHE_RUNNER": "1", "ORCHE_RUN_ID": rid,
                          "CLAUDE_CODE_OAUTH_TOKEN": "tok", "ORCHE_KEYMASTER": None, "CLAUDE_EFFORT": None,
                          "ORCA_PANE_KEY": None, "JEV_API_KEY": None})

    def test_main_target_runs_in_the_main_checkout_and_is_not_ledger_gated(self):
        self.git_init(self.repo, "docs/LEDGER.tsv")       # .orche/ stays untracked: ORCHE's own files
        unit = self.brief(to="main-acme")
        self.commit_action(verdict=None)
        self.reply("STATUS: done\nanswered")
        code, _, _ = self.orche("run", unit)
        self.assertEqual(code, 0)
        self.assertEqual((self.fake / "cwd.txt").read_text().strip(), str(self.repo))
        ptr = [p for p in self.pointers("main-acme") if p["unit"] == unit][0]
        self.assertEqual((ptr["from"], ptr["status"]), ("main-acme", "done"))

    def test_feature_target_reports_to_its_sender(self):
        subprocess.run(["git", "init", "-q", str(self.root / "acme-checkout")], check=True)
        for sender in ("main-acme", "div-acme-build"):
            with self.subTest(sender=sender):
                unit = self.brief(to="feat-acme-checkout", sender=sender)
                self.reply("STATUS: done\nfeature done")
                self.orche("run", unit)
                [ptr] = [p for p in self.pointers(sender) if p["unit"] == unit]
                self.assertEqual((ptr["from"], ptr["status"]), ("feat-acme-checkout", "info"))  # no HEAD, no commit
                self.assertEqual(ptr["summary"], "unverified: STATUS: done (no commit)\nfeature done")
                other = "div-acme-build" if sender == "main-acme" else "main-acme"
                self.assertEqual([p for p in self.pointers(other) if p["unit"] == unit], [])

    def test_refusals(self):
        code, _, err = self.orche("run", "Bad_Id")
        self.assertEqual(code, 1, err)
        code, _, err = self.orche("run", "acme-nothing-20261018-1")
        self.assertEqual(code, 2, err)
        (self.units_dir() / "acme-nobrief-20261018-1").mkdir(parents=True)
        code, _, err = self.orche("run", "acme-nobrief-20261018-1")
        self.assertEqual(code, 2)
        self.assertIn("no brief", err)
        self.assertEqual(self.pointers("main-acme"), [])     # no known target: nothing to file
        unit = self.brief(to="div-acme-rnd")
        (self.root / "acme-rnd").rmdir()
        code, _, err = self.orche("run", unit)
        self.assertEqual(code, 2)
        self.assertIn("worktree of div-acme-rnd is missing", err)
        reg = registry.load()
        reg["projects"]["acme"]["divisions"]["build"]["status"] = "retired"
        registry.save(reg)
        retired = self.brief()
        code, _, err = self.orche("run", retired)
        self.assertEqual(code, 2)
        self.assertIn("retired", err)
        self.assertFalse((self.fake / "stdin.txt").exists())
        # I-1: a spawned run whose target is known never ends silently.
        got = {p["unit"]: (p["from"], p["status"]) for p in self.pointers("main-acme")}
        self.assertEqual(got, {unit: ("div-acme-rnd", "failed"), retired: ("div-acme-build", "failed")})
        self.assertEqual(self.run_record(unit)["status"], "failed")


class RunnerFailureTest(RunnerCase):
    """I-1 / I-3: no unit ends without exactly one pointer."""

    def test_runner_exception_files_failed_and_releases_the_slot(self):
        unit = self.brief()
        self.reply("STATUS: done")
        with mock.patch.object(runner, "decide", side_effect=OSError("disk full")):
            code, _, err = self.orche("run", unit)
        self.assertEqual(code, 1)
        self.assertIn("OSError: disk full", err)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["from"], ptr["status"], ptr["summary"]),
                         ("div-acme-build", "failed", "run failed: runner error: OSError: disk full"))
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["model"]), ("failed", "error", "claude-opus-5-5"))
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])

    def test_headless_run_exception_files_failed_and_releases_the_lock(self):
        unit = self.brief()
        self.reply("STATUS: done")
        with mock.patch.object(runner.headless, "run", side_effect=RuntimeError("boom")):
            code, _, err = self.orche("run", unit)
        self.assertEqual(code, 1)
        self.assertIn("RuntimeError: boom", err)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["from"], ptr["status"], ptr["summary"]),
                         ("div-acme-build", "failed", "run failed: runner error: RuntimeError: boom"))
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"]), ("failed", "error"))
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])

    def test_sigterm_kills_claude_and_files_failed(self):
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        os.kill(self.lock(unit)["pid"], signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 1, (self.root / f"{unit}.out").read_text())
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["error"]), ("failed", "terminated by SIGTERM"))
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("failed", "run failed: terminated by SIGTERM"))
        self.assertFalse(alive((self.fake / "pid").read_text().strip()))
        self.assertIsNone(self.lock(unit))

    def test_sigterm_while_waiting_for_a_slot_files_failed(self):
        """A real `orche run` waiting for a slot (4 busy) gets SIGTERM without a STOP marker."""
        self.config({"claude_bin": str(self.claude), "runner": {"queue_wait_s": 60}})
        busy = self.busy_slots()
        unit = self.brief()
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        out = self.root / f"{unit}.out"
        # The mutex file appears on the first acquire attempt: the handler is installed by then.
        self.assertTrue(self.wait_for((self.home / "runs" / "acme" / ".mutex").exists), out.read_text())
        # Waiting, provably: no slot taken, all four holders still their runs (a gone holder frees
        # a slot and the run would start claude instead: the 0.3.2 CI flake).
        self.assertIsNone(self.lock(unit), out.read_text())
        self.assertEqual([h.poll() for h in busy], [None] * 4)
        self.assertIsNone(proc.poll(), out.read_text())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 1,
                         f"{out.read_text()}holders alive: {[alive(h.pid) for h in busy]}")
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("failed", "run failed: runner terminated by SIGTERM"))
        self.assertEqual(self.run_record(unit)["stop_reason"], "error")
        self.assertFalse((self.fake / "stdin.txt").exists())
        self.assertTrue(all(alive(holder.pid) for holder in busy))

    def test_sigterm_before_claude_starts_files_failed_or_stopped_before_start(self):
        """Round 2: SIGTERM in the `starting` phase (slot taken, the runner held in its `git` before
        claude): claude never runs; with the STOP marker (runs.stop: a verified real run) "stopped
        by Main before start"."""
        for n, stop in ((1, False), (2, True)):
            unit = self.brief()
            self.reply("STATUS: done")
            gate, entered = self.git_gate()
            gate.write_text("x")
            proc = self.spawn(unit, env=self.git_env)
            out = self.root / f"{unit}.out"
            self.assertTrue(self.wait_for(lambda: self.lock(unit) is not None and entered.exists()), out.read_text())
            if stop:
                self.assertTrue(runs.stop("acme", unit))
            else:
                proc.send_signal(signal.SIGTERM)
            gate.unlink()
            self.assertEqual(proc.wait(timeout=30), 0 if stop else 1, out.read_text())
            [ptr] = [p for p in self.pointers("main-acme") if p["unit"] == unit]
            want = (("info", "stopped by Main before start") if stop else
                    ("failed", "run failed: runner terminated by SIGTERM"))
            self.assertEqual((ptr["status"], ptr["summary"]), want)
            self.assertFalse(runs.stop_requested("acme", unit))
            self.assertIsNone(self.lock(unit))
        self.assertFalse((self.fake / "stdin.txt").exists())
        self.assertIn(f"run unit={unit} to=div-acme-build delivery=runner-stopped", self.events())

    def test_sigterm_during_popen_still_kills_the_child_once_it_exists(self):
        """Round 2: a SIGTERM that lands while claude is being started (here: inside on_start) is
        recorded and kills claude's group the moment its pid is known."""
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        real_update, children = runs.update, []

        def update(path, **fields):
            real_update(path, **fields)
            children.append(fields["child_pid"])
            os.kill(os.getpid(), signal.SIGTERM)     # the runner's handler is installed: only recorded
        started = time.monotonic()
        with mock.patch.object(runs, "update", update):
            code, _, _ = self.orche("run", unit)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(code, 1)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("failed", "run failed: terminated by SIGTERM"))
        self.assertFalse(alive(children[0]))
        if (self.fake / "pid").exists():        # the fake got as far as its sleep: dead too
            self.assertFalse(alive((self.fake / "pid").read_text().strip()))

    def test_sigterm_after_claude_finished_files_the_result_and_drops_a_late_stop(self):
        """Round 2: STOP lands while the runner records a finished run (held in its `git` after
        claude): the real result is filed, nothing else, the marker is removed (`runner-stop-late`)."""
        unit = self.brief()
        gate, entered = self.git_gate()
        self.action(f"touch {shlex.quote(str(gate))}\n")     # claude closes the gate; it calls no git after
        self.reply("STATUS: blocked\nreal result")
        proc = self.spawn(unit, env=self.git_env)
        out = self.root / f"{unit}.out"
        # A git call held at the gate is the runner's first after claude exited: `finishing`.
        self.assertTrue(self.wait_for(entered.exists), out.read_text())
        self.assertTrue(runs.stop("acme", unit))          # the lock is live: marker + SIGTERM
        gate.unlink()
        self.assertEqual(proc.wait(timeout=30), 0, out.read_text())
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("blocked", "real result"))
        self.assertEqual(self.run_record(unit)["status"], "blocked")
        self.assertFalse(runs.stop_requested("acme", unit))
        self.assertIn(f"run unit={unit} to=div-acme-build delivery=runner-stop-late", self.events())

    def test_cmd_report_refuses_under_the_runner(self):
        self.brief()
        os.environ["ORCHE_SESSION"] = "div-acme-build"
        os.environ["ORCHE_RUNNER"] = "1"
        code, out, err = self.orche("report", "done", "--summary", "x")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "orche: report refused: this unit runs under `orche run`, which files your "
                              "pointer from your final message\n")
        self.assertEqual(self.pointers("main-acme"), [])

    def test_runner_files_nothing_when_the_worker_already_reported(self):
        unit = self.brief()
        self.action(f"env -u ORCHE_RUNNER orche report blocked --unit {unit} --summary self-filed >/dev/null\n")
        self.reply("STATUS: blocked\nfrom the runner")
        code, _, _ = self.orche("run", unit)
        self.assertEqual(code, 0)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["from"], ptr["summary"]), ("div-acme-build", "self-filed"))
        self.assertFalse(self.run_record(unit)["pointer_filed"])


class ConcurrencyTest(RunnerCase):
    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_fifth_run_waits_then_files_busy(self):
        for i in range(4):
            self.lock_file(f"acme-other-20261018-{i + 10}")
        unit = self.brief()
        (self.units_dir() / unit / "REPORT.md").write_text("an earlier run\n")
        self.reply("STATUS: done")
        self.config({"claude_bin": str(self.claude), "runner": {"queue_wait_s": 0.3}})
        with mock.patch.object(runs, "POLL_S", 0.05):
            started = time.monotonic()
            code, _, err = self.orche("run", unit)
        self.assertEqual(code, 3)
        self.assertIn("runner busy: 4 live runs", err)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertFalse((self.fake / "stdin.txt").exists())
        # I-1: Main hears about it instead of waiting forever (round-2 M-9: no stale REPORT.md link).
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["from"], ptr["status"], ptr["summary"], ptr["report_path"], ptr["via"]),
                         ("div-acme-build", "info", f"runner busy: 4 live runs, retry with `orche run {unit}`",
                          "", "runner"))
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["error"]), ("info", "busy", "runner busy: 4 live runs"))

    def test_stale_locks_are_dropped_and_max_parallel_is_read_from_config(self):
        for i in range(3):
            self.lock_file(f"acme-other-20261018-{i + 10}")
        self.lock_file("acme-dead-20261018-1", self.dead_pid())
        self.assertEqual(runs.count("acme"), 3)
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertFalse((self.home / "runs" / "acme" / "acme-dead-20261018-1.lock").exists())
        self.config({"claude_bin": str(self.claude), "runner": {"max_parallel": 3, "queue_wait_s": 0}})
        with mock.patch.object(runs, "POLL_S", 0):
            self.assertEqual(self.orche("run", unit)[0], 3)

    def test_same_unit_already_running_files_info(self):
        unit = self.brief()
        pid = self.lock_file(unit)
        code, _, err = self.orche("run", unit)
        self.assertEqual(code, 4)
        self.assertIn("already running", err)
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["via"]), ("info", "runner"))
        self.assertTrue(ptr["summary"].startswith(f"not started: {unit} is already running (pid {pid}, "
                                                  "started ?)"), ptr["summary"])
        self.assertFalse((self.units_dir() / unit / "RUN.json").exists())   # the live run writes its own

    def test_a_unit_being_stopped_is_waited_for_not_refused(self):
        unit = self.brief()
        self.lock_file(unit)
        (self.home / "runs" / "acme" / f"{unit}.stop").write_text("x\n")
        with mock.patch.object(runs, "WAIT_S", 0.2), mock.patch.object(runs, "POLL_S", 0.05):
            with self.assertRaises(runs.Busy):
                runs.acquire("acme", unit, {}, 4)
        (self.home / "runs" / "acme" / f"{unit}.lock").unlink()
        # The old marker stays but never hits a fresh run: only a marker written after it started counts.
        since = time.time_ns()
        lock = runs.acquire("acme", unit, {}, 4, wait_s=0)
        self.assertTrue(runs.stop_requested("acme", unit))
        self.assertFalse(runs.stop_requested("acme", unit, since))
        runs.release(lock)
        self.reply("STATUS: blocked\nx")
        self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertEqual(self.pointers("main-acme")[0]["status"], "blocked")


BODY = ("TO        {to}\nGOAL      Add coupon field\nSCOPE     src/**\nCONTEXT   none\nACCEPT    works\n"
        "VERIFY    true\nTIMEBOX   20m\nFORBID    merge\nREPORT    status\nTIER      {tier}")


class RelayRunnerTest(RunnerCase):
    """dispatch.relay in-process; Popen is monkeypatched (the spawner), Orca is the fake switch."""

    def setUp(self):
        super().setUp()
        os.environ["ORCHE_FAKE_ORCA"] = "1"
        self.spawned = []
        self.deliverers = []

    def relay(self, to="div-acme-build", tier="standard", body=None, popen=None, deliverer=None):
        def fake_popen(argv, **kw):   # only the runner and deliverer spawns are faked (git still runs)
            if argv[:3] == DELIVER:
                self.deliverers.append((argv, kw))
                self.captured_deliverers = len(self.deliverers)     # HookCase.tearDown waits for no process
                return deliverer(argv, **kw) if deliverer is not None else mock.Mock(pid=4343)
            if argv[:3] != SPAWN:
                return REAL_POPEN(argv, **kw)
            self.spawned.append((argv, kw))
            if popen is not None:
                return popen(argv, **kw)
            return mock.Mock(pid=4242)
        with mock.patch.object(procs.subprocess, "Popen", fake_popen):
            started = time.monotonic()
            r = roster.Roster(registry.load())
            [unit] = dispatch.relay(r, [body or BODY.format(to=to, tier=tier)], r.member("main-acme"),
                                    deadline=time.monotonic() + 3)
            self.elapsed = time.monotonic() - started
        return unit

    def sends(self):
        return [a for a in self.orca_calls() if a[1:3] == ["terminal", "send"]]

    def feedback(self):
        return [p["summary"] for p in self.pointers("main-acme") if p["from"] == "orche"]

    def urgent(self):
        return [p["urgent"] for p in self.pointers("main-acme") if p["from"] == "orche"]

    def test_runner_delivery_spawns_orche_run_detached(self):
        unit = self.relay()
        [(argv, kw)] = self.spawned
        # R-B: the hook's own interpreter runs bin/orche.py; the sh launcher is never asked to find Python.
        self.assertEqual(argv, [sys.executable, str(support.ROOT / "bin" / "orche.py"), "run", unit])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertLessEqual(int(kw["env"]["ORCHE_RUN_SINCE"]), time.time_ns())   # dates the request
        self.assertTrue(kw["env"]["PATH"].startswith(str(support.ROOT / "bin") + os.pathsep))
        self.assertEqual(kw["stdout"].name, str(self.home / "logs" / "runs" / f"{unit}.log"))
        self.assertEqual(self.sends(), [])
        self.assertIn(f"unit={unit} rev=1 to=div-acme-build by=main-acme delivery=runner-started", self.events())
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 1 (runner-started)"])
        self.assertEqual(self.urgent(), [False])
        self.assertEqual(self.pointers("div-acme-build"), [])     # ruling 6: a runner unit gets no target pointer
        self.assertEqual(self.deliverers, [])
        self.assertLess(self.elapsed, 1.0)

    def test_spawn_failure_files_an_urgent_pointer(self):
        unit = self.relay(popen=mock.Mock(side_effect=OSError("no exec")))
        self.assertEqual((self.sends(), self.deliverers, self.pointers("div-acme-build")), ([], [], []))
        self.assertIn(f"unit={unit} rev=1 to=div-acme-build by=main-acme delivery=runner-failed\n", self.events())
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 1 (runner-failed)"
                                           f" — the runner did not start: run `orche deliver {unit}`"])
        self.assertEqual(self.urgent(), [True])

    def test_pane_delivery_spawns_the_deliverer(self):
        self.config({"claude_bin": str(self.claude), "delivery": "pane"})
        unit = self.relay()
        self.assertEqual((self.spawned, self.sends()), ([], []))      # the hook itself types nothing
        [(argv, kw)] = self.deliverers
        self.assertEqual(argv, DELIVER + ["--", unit])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdout"].name, str(self.home / "logs" / "deliver.log"))
        self.assertIn(f"unit={unit} rev=1 to=div-acme-build by=main-acme delivery=deliverer-started", self.events())
        [ptr] = self.pointers("div-acme-build")
        self.assertEqual((ptr["unit"], ptr["status"], ptr["via"]), (unit, "info", "dispatch"))
        self.assertEqual((self.feedback(), self.urgent()),
                         ([f"relayed {unit} → div-acme-build rev 1 (deliverer-started)"], [False]))

    def test_deliverer_spawn_failure_files_an_urgent_pointer(self):
        self.config({"claude_bin": str(self.claude), "delivery": "pane"})
        unit = self.relay(deliverer=mock.Mock(side_effect=OSError("no exec")))
        self.assertIn(f"unit={unit} rev=1 to=div-acme-build by=main-acme delivery=deliverer-failed", self.events())
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 1 (deliverer-failed)"
                                           f" — run `orche deliver {unit}`"])
        self.assertEqual(self.urgent(), [True])
        self.assertEqual(len(self.pointers("div-acme-build")), 1)   # the pointer waits for `orche deliver`

    def test_main_target_uses_the_pane_every_tier_the_runner(self):
        self.relay(to="main-acme")
        self.assertEqual(self.spawned, [])
        self.assertEqual(len(self.deliverers), 1)
        unit = self.relay()                       # spawned once
        self.relay(body=f"AMEND {unit} rev 2\nSUPERSEDES rev 1: smaller\nGOAL      Smaller")
        self.assertEqual([a for a, _ in self.spawned], [SPAWN + [unit]] * 2)
        self.assertIn(f"kind=amend unit={unit} rev=2 to=div-acme-build by=main-acme delivery=runner-started",
                      self.events())
        self.relay(tier="hard")                   # hard goes to the runner
        self.assertEqual(len(self.spawned), 3)
        self.config({"claude_bin": str(self.claude), "runner": {"hard_in_pane": True}})   # a removed key: ignored
        hard = self.relay(tier="hard")
        self.relay(body=f"AMEND {hard} rev 2\nGOAL      Smaller")
        self.assertEqual(len(self.spawned), 5)
        self.assertEqual(len(self.deliverers), 1)

    def test_stop_of_an_idle_runner_unit_files_no_target_pointer_and_never_says_wake(self):
        unit = self.brief()
        self.relay(body=f"STOP {unit} enough")
        self.assertEqual((self.spawned, self.sends(), self.deliverers), ([], [], []))
        self.assertIn(f"kind=stop unit={unit} rev=1 to=div-acme-build by=main-acme "
                      "delivery=runner-stop-requested (no live run)", self.events())
        [fb] = self.feedback()
        self.assertEqual(fb, f"relayed {unit} → div-acme-build rev 1 (runner-stop-requested (no live run)) — "
                             "already finished (or still queued: a queued run will not start)")
        self.assertEqual(self.pointers("div-acme-build"), [])
        self.assertTrue(runs.stop_requested("acme", unit))    # marker written: a queued run gives up

    def test_stop_runner_failure_feedback_is_stop_specific(self):
        """Round-2 M-8: a STOP whose runner delivery raised never advises a re-run; it is urgent."""
        unit = self.brief()
        with mock.patch.object(runs, "stop", side_effect=OSError("boom")):
            self.relay(body=f"STOP {unit}")
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 1 (runner-failed) — the STOP did "
                                           "not reach the runner: check `orche status` and send the STOP again"])
        self.assertEqual(self.urgent(), [True])

    def test_stop_with_a_recycled_pid_drops_the_lock_and_signals_nothing(self):
        """Round 2 (deferred M-3): the lock's pid is alive but is not `orche run <unit>`: a lost run
        (resilience R-A): `failed` "runner died" is filed, the locks go, the pid is never signalled."""
        unit = self.brief()
        other = self.sleeper()
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{unit}.lock").write_text(json.dumps({"pid": other.pid, "unit": unit, "to": "div-acme-build"}))
        (folder / "div-acme-build.target.lock").write_text(
            json.dumps({"pid": other.pid, "unit": unit, "to": "div-acme-build"}))
        self.relay(body=f"STOP {unit}")
        self.assertIsNone(other.poll())                       # never signalled
        self.assertFalse((folder / f"{unit}.lock").exists())
        self.assertFalse((folder / "div-acme-build.target.lock").exists())
        self.assertIn(f"run unit={unit} to=div-acme-build runner-reconciled pid={other.pid}", self.events())
        self.assertIn("delivery=runner-stop-requested (no live run)", self.events())
        [ptr] = [p for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        self.assertEqual(ptr["status"], "failed")
        self.assertTrue(ptr["summary"].startswith(f"runner died: {unit} (pid {other.pid}, started ?)"), ptr)

    def test_stop_while_waiting_for_a_slot_never_starts_claude(self):
        """Round-2 B: a real run waits (4 busy slots); STOP writes the marker (no lock yet); the run
        gives up on its next poll, files "stopped by Main before start" and never starts claude."""
        self.config({"claude_bin": str(self.claude), "delivery": "runner", "runner": {"queue_wait_s": 60}})
        self.busy_slots()
        unit = self.brief()
        self.reply("STATUS: done")
        # As the Stop hook spawns it (ORCHE_RUN_SINCE): a STOP right after the spawn counts even
        # though the new process may not have reached its wait loop yet.
        proc = self.spawn(unit, env={**os.environ, "ORCHE_RUN_SINCE": str(time.time_ns())})
        self.relay(body=f"STOP {unit}")
        self.assertIn("delivery=runner-stop-requested (no live run)", self.events())
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        self.assertFalse((self.fake / "stdin.txt").exists())
        got = [(p["status"], p["summary"], p.get("via")) for p in self.pointers("main-acme")
               if p["from"] == "div-acme-build"]
        self.assertEqual(got, [("info", "stopped by Main before start", "runner")])
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["is_error"]), ("info", "stopped", False))
        self.assertIn(f"run unit={unit} to=div-acme-build delivery=runner-stopped", self.events())
        self.assertFalse(runs.stop_requested("acme", unit))

    def test_stop_after_the_run_finished_says_already_finished_and_files_nothing_new(self):
        """Round 2: the run is over; STOP files its pointer to the target, says "already finished",
        and the marker never stops a later run of the unit."""
        unit = self.brief()
        self.reply("STATUS: blocked\nfirst")
        proc = self.spawn(unit)
        self.assertEqual(proc.wait(timeout=20), 0)
        self.relay(body=f"STOP {unit}")
        self.assertIn("already finished", self.feedback()[0])
        self.assertEqual([p["status"] for p in self.pointers("main-acme") if p["from"] == "div-acme-build"],
                         ["blocked"])
        self.reply("STATUS: blocked\nsecond")
        self.assertEqual(self.spawn(unit).wait(timeout=20), 0)
        self.assertEqual(sorted(p["summary"] for p in self.pointers("main-acme") if p["from"] == "div-acme-build"),
                         ["first", "second"])

    def test_stop_of_a_running_unit_kills_it_and_files_stopped(self):
        """I-6: STOP reaches the live `claude -p`; the runner files `info` "stopped by Main"."""
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        lock = self.lock(unit)
        self.assertEqual((lock["pid"], lock["child_pid"], lock["pgid"]),
                         (proc.pid, int((self.fake / "pid").read_text()), lock["child_pid"]))
        self.relay(body=f"STOP {unit}")
        self.assertLess(self.elapsed, 1.0)        # the hook never waits for the kill
        self.assertIn(f"kind=stop unit={unit} rev=1 to=div-acme-build by=main-acme delivery=runner-stop-requested\n",
                      self.events() + "\n")
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 1 (runner-stop-requested)"])
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["is_error"]), ("info", "stopped", False))
        [ptr] = [p for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        self.assertEqual((ptr["status"], ptr["summary"]), ("info", "stopped by Main"))
        self.assertFalse(alive((self.fake / "pid").read_text().strip()))
        self.assertIsNone(self.lock(unit))
        self.assertFalse(runs.stop_requested("acme", unit))
        self.assertEqual(self.spawned, [])

    def test_amend_of_a_running_unit_stops_it_and_runs_the_new_rev(self):
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: blocked\nneeds design")
        first = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        old_claude = (self.fake / "pid").read_text().strip()
        self.claude_sleep_pid()                   # rev 1's claude has read its `sleep` file
        (self.fake / "sleep").unlink()            # so only the fresh run answers at once
        procs = []

        def popen(argv, **kw):
            procs.append(REAL_POPEN(argv, **kw))
            self.addCleanup(self.reap, procs[-1])
            return procs[-1]
        self.relay(body=f"AMEND {unit} rev 2\nSUPERSEDES rev 1: smaller\nGOAL      Smaller", popen=popen)
        self.assertIn(f"kind=amend unit={unit} rev=2 to=div-acme-build by=main-acme delivery=runner-restarted",
                      self.events())
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 2 (runner-restarted)"])
        self.assertEqual(first.wait(timeout=20), 0)
        self.assertFalse(alive(old_claude))
        log = self.home / "logs" / "runs" / f"{unit}.log"
        self.assertTrue(self.wait_for(lambda: len([p for p in self.pointers("main-acme")
                                                   if p["from"] == "div-acme-build"]) == 2 and self.lock(unit) is None,
                                      timeout=30), log.read_text() if log.exists() else "no log")
        got = sorted((p["status"], p["summary"]) for p in self.pointers("main-acme") if p["from"] == "div-acme-build")
        self.assertEqual(got, [("blocked", "needs design"), ("info", "stopped by Main")])
        self.assertEqual(procs[0].wait(timeout=20), 0)
        self.assertTrue((self.fake / "stdin.txt").read_text().startswith("rev: 2\n"))
        self.assertEqual(self.run_record(unit)["status"], "blocked")

    # -- round 3 -------------------------------------------------------------
    def test_stop_when_ps_fails_keeps_the_lock_and_still_stops_the_run(self):
        """m-1 / round 4: `ps` fails (timeout 124) while a real run is live: the lock counts as live
        (`runner-pid-check-failed`) and nothing is signalled; the run's marker poll stops claude and
        files "stopped by Main"; a second `orche run` of the unit is refused meanwhile."""
        unit = self.brief()
        (self.fake / "ignore_term").write_text("")      # a 5 s kill window to look into
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        calls = []
        real = runs.shell.RUNNER

        def failing_ps(argv, *, timeout, input):
            if argv[0] == "ps":
                calls.append(timeout)
                return runs.shell.Result(124, "", f"timeout after {timeout}s", list(argv))
            return real(argv, timeout=timeout, input=input)
        with mock.patch.object(runs.shell, "RUNNER", failing_ps):
            self.relay(body=f"STOP {unit}")
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], runs.PS_TIMEOUT_S)
        self.assertIn(f"run unit={unit} to=div-acme-build runner-pid-check-failed pid={proc.pid}", self.events())
        self.assertNotIn("runner-stale-lock", self.events())
        self.assertEqual(self.feedback(), [f"relayed {unit} → div-acme-build rev 1 (runner-stop-requested)"])
        self.assertEqual(self.lock(unit)["pid"], proc.pid)                   # never dropped
        self.assertTrue((self.home / "runs" / "acme" / "div-acme-build.target.lock").exists())
        with self.assertRaises(runs.Busy):                                    # no second run starts
            runs.acquire("acme", unit, {"to": "div-acme-build"}, 4, wait_s=0)
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        got = [(p["status"], p["summary"]) for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        self.assertEqual(got, [("info", "stopped by Main")])
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["is_error"]), ("info", "stopped", False))
        self.assertEqual(rec["error"], "stopped by STOP marker")             # the poll, not a SIGTERM
        self.assertIsNone(self.lock(unit))

    def test_a_hanging_ps_never_holds_the_stop_hook_past_its_budget(self):
        """m-1: a real `ps` on PATH that hangs is cut at PS_TIMEOUT_S; with no budget left `ps` is
        not run at all. Either way the lock counts as live."""
        unit = self.brief()
        other = self.stubborn()
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{unit}.lock").write_text(json.dumps({"pid": other.pid, "unit": unit, "to": "div-acme-build"}))
        fakebin = self.root / "fakeps"
        fakebin.mkdir()
        (fakebin / "ps").write_text("#!/bin/sh\nexec sleep 10\n")
        (fakebin / "ps").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}):
            started = time.monotonic()
            self.assertIsNone(runs.is_run_process(other.pid, unit))
            self.assertLess(time.monotonic() - started, runs.PS_TIMEOUT_S + 0.5)
            self.relay(body=f"STOP {unit}")
        self.assertLess(self.elapsed, runs.PS_TIMEOUT_S + 0.5)
        self.assertIn(f"runner-pid-check-failed pid={other.pid}", self.events())
        self.assertTrue((folder / f"{unit}.lock").exists())
        with mock.patch.object(runs.shell, "RUNNER", mock.Mock(side_effect=AssertionError("ps ran"))):
            self.assertIsNone(runs.is_run_process(other.pid, unit, timeout=0))
            self.assertIsNone(runs.is_run_process(other.pid, unit, timeout=-1.0))
            self.assertTrue(runs.stop("acme", unit, timeout=0))             # live: kept, not signalled
        self.assertTrue((folder / f"{unit}.lock").exists())
        self.assertIsNone(other.poll())

    def test_stop_inside_an_amend_kill_window_files_stopped_for_the_old_rev(self):
        """m-3: AMEND stops rev 1 (claude ignores SIGTERM: 5 s to the SIGKILL) and spawns rev 2,
        which waits; a STOP then cancels rev 2 ("stopped by Main before start", no RUN.json) and
        leaves the marker to rev 1, which files "stopped by Main", never `failed`."""
        unit = self.brief()
        (self.fake / "ignore_term").write_text("")
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        first = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        procs = []

        def popen(argv, **kw):
            procs.append(REAL_POPEN(argv, **kw))
            self.addCleanup(self.reap, procs[-1])
            return procs[-1]
        self.relay(body=f"AMEND {unit} rev 2\nGOAL      Smaller", popen=popen)
        self.relay(body=f"STOP {unit}")
        self.assertEqual(procs[0].wait(timeout=20), 0)          # rev 2 gave up while rev 1 was live
        self.assertIsNotNone(self.lock(unit))
        self.assertEqual(first.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        got = sorted((p["status"], p["summary"]) for p in self.pointers("main-acme") if p["from"] == "div-acme-build")
        self.assertEqual(got, [("info", "stopped by Main"), ("info", "stopped by Main before start")])
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["is_error"]), ("info", "stopped", False))
        self.assertFalse((self.fake / "stdin.txt").read_text().startswith("rev: 2\n"))   # rev 2 never ran
        self.assertFalse(runs.stop_requested("acme", unit))      # rev 1 consumed and cleared it
        self.assertIsNone(self.lock(unit))


class RelayRunnerHookTest(RunnerCase):
    """End to end: the Stop hook spawns `bin/orche run`, which runs the fake claude and files the pointer."""

    def test_hook_returns_within_budget_and_the_run_completes(self):
        self.commit_action(verdict="type-check-only")
        self.reply("STATUS: done\nshipped")
        path = self.transcript("Relaying.\n\n```dispatch\n" + BODY.format(to="build", tier="standard") + "\n```")
        started = time.monotonic()
        res = self.run_hook("dispatch_relay.py", self.payload("stop.json", transcript_path=path),
                            ORCHE_SESSION="main-acme")
        self.assertLess(time.monotonic() - started, 3.0 + 1.5)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        unit = self.only_unit()
        record = self.units_dir() / unit / "RUN.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (record.exists() and not list(
                (self.home / "runs" / "acme").glob("*.lock"))):
            time.sleep(0.1)
        self.assertTrue(record.exists(), (self.home / "logs" / "runs" / f"{unit}.log").read_text())
        self.assertEqual(json.loads(record.read_text())["status"], "done")
        statuses = sorted(p["status"] for p in self.pointers("main-acme"))
        self.assertEqual(statuses, ["done", "info"])   # the run's done + the relay feedback


class GateScopeTest(RunnerCase):
    """Round-2 A: the strict `done` rule applies only where `orche report done` is gated."""

    def add_research(self):
        reg = registry.load()
        reg["projects"]["acme"]["divisions"]["research"] = {
            "status": "active", "worktree": f"repo-acme::{self.root / 'acme-research'}", "branch": "div/research",
            "session": "div-acme-research", "model": "claude-opus-5-5",
            "last_activity": "2026-10-15T18:30:00+09:00"}
        registry.save(reg)
        self.git_init(self.root / "acme-research")

    def outcome(self, unit):
        [ptr] = [p for p in self.pointers("main-acme") if p["unit"] == unit and p["from"] != "orche"]
        return ptr["status"], ptr["summary"]

    def test_done_rule_by_target(self):
        self.add_research()
        self.reply("STATUS: done\nfindings in REPORT.md")
        research = self.brief(to="div-acme-research")
        self.assertEqual(self.orche("run", research)[0], 0)
        self.assertEqual(self.outcome(research), ("done", "findings in REPORT.md"))   # no commit needed
        main = self.brief(to="main-acme")
        self.git_init(self.repo, "docs/LEDGER.tsv")
        self.orche("run", main)
        self.assertEqual(self.outcome(main)[0], "done")
        build = self.brief()
        self.orche("run", build)
        self.assertEqual(self.outcome(build), ("info", "unverified: STATUS: done (no commit)\nfindings in REPORT.md"))
        verified = self.brief()
        self.commit_action()
        self.orche("run", verified)
        self.assertEqual(self.outcome(verified)[0], "done")


class RoundTwoRunTest(RunnerCase):
    """Round-2 D: duplicates, one run per target, process-group sweep, env, errors before the run."""

    def test_duplicate_of_a_live_run_answers_main_and_never_hides_the_live_result(self):
        """I-A: a real live run + `orche run` of the same unit: the duplicate files `info` (via
        runner, exit 4, no RUN.json); the live run still files its own `blocked`."""
        unit = self.brief()
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: blocked\nlive result")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        nap = self.claude_sleep_pid()
        code, _, err = self.orche("run", unit)
        self.assertEqual(code, 4, err)
        self.assertIn(f"{unit} is already running (pid {proc.pid}, started ", err)
        os.kill(nap, signal.SIGTERM)          # only now does the live claude answer
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        got = sorted((p["status"], p["summary"].split(" (pid")[0], p["via"]) for p in self.pointers("main-acme"))
        self.assertEqual(got, [("blocked", "live result", "runner"),
                               ("info", f"not started: {unit} is already running", "runner")])
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["pointer_filed"]), ("blocked", True))

    def test_two_units_for_one_target_run_one_after_the_other(self):
        """I-B: unit 2 waits for the target lock while unit 1 (a real run) works; unit 1's commit
        is no longer credited to unit 2."""
        first, second = self.brief(), self.brief()
        self.ledger()
        self.action(f"grep -q 'unit: {first}' \"$(dirname \"$0\")/stdin.txt\" || exit 0\n" + COMMIT
                    + ROW.format(verdict="unit-test-verified"))
        (self.fake / "sleep").write_text("1")
        self.reply("STATUS: done\nok")
        proc = self.spawn(first)
        self.assertTrue(self.claude_sleeping(first), (self.root / f"{first}.out").read_text())
        self.assertTrue((self.home / "runs" / "acme" / "div-acme-build.target.lock").exists())
        self.assertEqual(runs.count("acme"), 1)                 # target locks are no extra runs
        started = time.monotonic()
        with mock.patch.object(runs, "POLL_S", 0.05):
            code, _, err = self.orche("run", second)
        self.assertEqual(code, 0, err)
        self.assertEqual(proc.wait(timeout=20), 0)
        got = {p["unit"]: (p["status"], p["summary"]) for p in self.pointers("main-acme")}
        self.assertEqual(got, {first: ("done", "ok"), second: ("info", "unverified: STATUS: done (no commit)\nok")})
        self.assertGreater(time.monotonic() - started, 0.3)
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])

    def test_target_wait_expires_as_runner_busy(self):
        self.config({"claude_bin": str(self.claude), "runner": {"queue_wait_s": 0.2}})
        other = self.fake_run("acme-other-20261018-9")
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True)
        record = {"pid": other.pid, "unit": "acme-other-20261018-9", "to": "div-acme-build"}
        (folder / "acme-other-20261018-9.lock").write_text(json.dumps(record))
        (folder / "div-acme-build.target.lock").write_text(json.dumps(record))
        unit = self.brief()
        with mock.patch.object(runs, "POLL_S", 0.05):
            code, _, err = self.orche("run", unit)
        self.assertEqual(code, 3)
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["summary"], "runner busy: div-acme-build is running acme-other-20261018-9, "
                                         f"retry with `orche run {unit}`")
        self.assertFalse((self.fake / "stdin.txt").exists())

    def test_children_outliving_claude_are_swept(self):
        """A grandchild that ignores SIGTERM and left claude's stdout is killed after the run."""
        unit = self.brief()
        self.action("(trap '' TERM; exec sleep 30) >/dev/null 2>&1 &\necho $! > \"$(dirname \"$0\")/grandchild\"\n")
        self.reply("STATUS: blocked\nx")
        started = time.monotonic()
        self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertGreaterEqual(time.monotonic() - started, headless.SWEEP_WAIT_S - 0.5)   # TERM ignored: KILL
        self.assertFalse(alive((self.fake / "grandchild").read_text().strip()))

    def test_stop_escalates_to_sigkill_for_a_claude_ignoring_sigterm(self):
        """A real run whose claude (and its sleep) ignore SIGTERM: STOP kills the group after 5 s."""
        unit = self.brief()
        (self.fake / "ignore_term").write_text("")
        (self.fake / "sleep").write_text("30")
        self.reply("STATUS: done")
        proc = self.spawn(unit)
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        started = time.monotonic()
        self.assertTrue(runs.stop("acme", unit))
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        self.assertGreaterEqual(time.monotonic() - started, headless.KILL_WAIT_S - 0.5)
        self.assertFalse(alive((self.fake / "pid").read_text().strip()))
        [ptr] = self.pointers("main-acme")
        self.assertEqual((ptr["status"], ptr["summary"]), ("info", "stopped by Main"))

    def test_bad_config_before_the_run_still_files_failed(self):
        """M-5: build_argv raising (a non-integer turns_ceiling) files `failed` with a RUN.json."""
        self.config({"claude_bin": str(self.claude), "runner": {"turns_ceiling": "lots"}})
        unit = self.brief()
        code, _, err = self.orche("run", unit)
        self.assertEqual(code, 1)
        self.assertIn("ValueError", err)
        [ptr] = self.pointers("main-acme")
        self.assertEqual(ptr["status"], "failed")
        self.assertTrue(ptr["summary"].startswith("run failed: runner error: ValueError"), ptr["summary"])
        self.assertEqual(self.run_record(unit)["model"], "claude-opus-5-5")
        code, _, err = self.orche("--dry-run", "run", unit)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.pointers("main-acme")), 1)       # a dry run files nothing

    def test_doctor_reconciles_a_dead_runs_lock(self):
        """A SIGKILLed `orche run` filed nothing; `orche doctor` reconciles it (resilience R-A):
        `failed` "runner died" is filed, the lock goes, one WARN row says so; a dry run touches nothing."""
        from orche.install import doctor
        dead = subprocess.Popen(["true"])
        dead.wait()
        unit = self.brief()
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True)
        (folder / f"{unit}.lock").write_text(json.dumps({"pid": dead.pid, "unit": unit, "to": "div-acme-build"}))
        self.assertEqual(doctor.runner_lock_checks(registry.load(), dry_run=True), [])
        self.assertTrue((folder / f"{unit}.lock").exists())
        self.assertEqual(doctor.runner_lock_checks(registry.load()),
                         [("WARN", "runner lock", f"acme: runner died: {unit} (pid {dead.pid}, started ?); filed "
                                                  f"`failed`; lock removed; re-run with `orche run {unit}`")])
        self.assertFalse((folder / f"{unit}.lock").exists())
        self.assertEqual([p["status"] for p in self.pointers("main-acme")], ["failed"])
        self.assertEqual(doctor.runner_lock_checks(registry.load()), [])


class RoundThreeTest(RunnerCase):
    """Round-3 m-2, m-4, m-5, m-7: the unit token, lock ownership, stale-lock trace, recycled pids."""

    def lock_record(self, unit, pid, to="div-acme-build", **extra):
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        record = {"pid": pid, "unit": unit, "to": to, **extra}
        (folder / f"{unit}.lock").write_text(json.dumps(record))
        if to:
            (folder / f"{to}.target.lock").write_text(json.dumps(record))
        return folder / f"{unit}.lock"

    def test_unit_id_matches_as_a_whole_token(self):
        """m-2: `…-1` never matches the command line of `orche run …-12`."""
        unit = self.brief()
        twelve_id = unit + "2"
        twelve = self.fake_run(twelve_id)
        self.assertTrue(self.wait_for(lambda: "run acme" in subprocess.run(
            ["ps", "-o", "command=", "-p", str(twelve.pid)], capture_output=True, text=True).stdout))
        self.assertTrue(runs.is_run_process(twelve.pid, twelve_id))
        self.assertIs(runs.is_run_process(twelve.pid, unit), False)
        self.assertIs(runs.is_run_process(twelve.pid, unit.rsplit("-", 1)[0]), False)
        one = self.fake_run(unit)
        self.assertTrue(self.wait_for(lambda: runs.is_run_process(one.pid, unit)))
        self.assertIs(runs.is_run_process(self.sleeper().pid, unit), False)
        rx = runs._run_command(unit)
        self.assertTrue(rx.search(f"python3 /x/orche.py run {unit} --json"))
        self.assertIsNone(rx.search(f"/x/run {unit}"))            # no launcher
        self.assertIsNone(rx.search(f"/x/orche run {unit}x"))
        # A STOP of `…-1` whose lock pid is now `orche run …-12` drops the lock, never signals.
        self.lock_record(unit, twelve.pid)
        self.assertFalse(runs.stop("acme", unit))
        self.assertIsNone(twelve.poll())
        self.assertFalse((self.home / "runs" / "acme" / f"{unit}.lock").exists())

    def test_ps_with_no_such_process_is_not_a_failure(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.assertIs(runs.is_run_process(dead.pid, "acme-coupon-field-20261018-1"), False)

    def test_release_removes_only_this_runs_locks(self):
        """m-4: a lock (unit or target) whose pid is another process stays; `pid=` names the owner."""
        other = self.sleeper()
        unit = self.brief()
        path = self.lock_record(unit, other.pid)
        target = self.home / "runs" / "acme" / "div-acme-build.target.lock"
        runs.release(path)
        self.assertTrue(path.exists() and target.exists())
        runs.release(path, pid=other.pid)
        self.assertFalse(path.exists() or target.exists())
        mine = runs.acquire("acme", unit, {"to": "div-acme-build"}, 4, wait_s=0)
        self.assertTrue(target.exists())
        runs.release(mine)
        self.assertFalse(mine.exists() or target.exists())

    def test_an_older_runs_release_never_deletes_a_newer_runs_lock(self):
        """m-4: run A's lock was taken over by run B (after a stale drop); A's `finally` leaves B's."""
        unit = self.brief()
        other = self.fake_run(unit)
        path = self.lock_record(unit, other.pid)          # B holds the unit and the target
        runs.release(path)                                # A (this process) releases
        self.assertEqual(json.loads(path.read_text())["pid"], other.pid)
        with self.assertRaises(runs.Duplicate):
            runs.acquire("acme", unit, {"to": "div-acme-build"}, 4, wait_s=0)

    def test_a_run_that_clears_a_stale_lock_logs_it(self):
        """m-5 / R-A: the project's next run settles a dead `orche run`'s lock before taking a slot:
        an unknown unit files nothing and logs `run unit=… to=… runner-stale-lock pid=…` (with
        claude's child pid when known); doctor has nothing left to say."""
        from orche.install import doctor
        dead = subprocess.Popen(["true"])
        dead.wait()
        stale = "acme-old-20261018-5"
        self.lock_record(stale, dead.pid, to="div-acme-other", child_pid=dead.pid)
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        self.assertEqual(self.orche("run", unit)[0], 0)
        self.assertIn(f"run unit={stale} to=div-acme-other runner-stale-lock pid={dead.pid} child_pid={dead.pid}",
                      self.events())
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])
        self.assertEqual(doctor.runner_lock_checks(registry.load()), [])

    def test_a_recycled_pid_never_holds_the_target(self):
        """m-7 / R-A: a lock whose pid now belongs to another program is a lost run: the next run's
        start, `orche doctor` or `STOP <unit>` settles it, signalling nothing."""
        from orche.install import doctor
        self.config({"claude_bin": str(self.claude), "runner": {"queue_wait_s": 0.2}})
        other = self.sleeper()
        held = "acme-other-20261018-9"
        self.lock_record(held, other.pid)
        unit = self.brief()
        self.reply("STATUS: blocked\nx")
        with mock.patch.object(runs, "POLL_S", 0.05):
            self.assertEqual(self.orche("run", unit)[0], 0)      # the queue never stalls
        self.assertIn(f"run unit={held} to=div-acme-build runner-stale-lock pid={other.pid}", self.events())
        self.lock_record(held, other.pid)
        self.assertEqual(doctor.runner_lock_checks(registry.load()), [(
            "WARN", "runner lock", f"acme: {held}: runner pid {other.pid} is gone; no registered project has the "
                                   f"unit, nothing filed; lock removed; re-run with `orche run {held}`")])
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])
        self.lock_record(held, other.pid)
        self.assertFalse(runs.stop("acme", held))
        self.assertEqual(list((self.home / "runs" / "acme").glob("*.lock")), [])
        self.assertIsNone(other.poll())                          # never signalled


class RoundFourTest(RunnerCase):
    """Round 4: the STOP-marker poll while claude runs, no signal to an unverified pid, flags
    before the unit id, `release` -> bool, `ps -ww`."""

    lock_record = RoundThreeTest.lock_record

    def failing_ps(self, calls):
        real = runs.shell.RUNNER

        def runner(argv, *, timeout, input):
            if argv[0] == "ps":
                calls.append(list(argv))
                return runs.shell.Result(124, "", f"timeout after {timeout}s", list(argv))
            return real(argv, timeout=timeout, input=input)
        return mock.patch.object(runs.shell, "RUNNER", runner)

    def test_a_stop_marker_written_while_claude_runs_stops_it_within_one_poll(self):
        """Item 1: claude sleeps 20 s; a marker written 1 s in (no `runs.stop`, so no signal to
        `orche run`) ends the run within ~one poll and files "stopped by Main"."""
        unit = self.brief()
        (self.fake / "sleep").write_text("20")
        self.reply("STATUS: done")
        proc = self.spawn(unit, env={**os.environ, "ORCHE_RUN_SINCE": str(time.time_ns())})
        self.assertTrue(self.claude_sleeping(unit), (self.root / f"{unit}.out").read_text())
        time.sleep(1)
        started = time.monotonic()
        (self.home / "runs" / "acme" / f"{unit}.stop").write_text("now\n")
        self.assertEqual(proc.wait(timeout=10), 0, (self.root / f"{unit}.out").read_text())
        self.assertLess(time.monotonic() - started, 5.0)
        rec = self.run_record(unit)
        self.assertEqual((rec["status"], rec["stop_reason"], rec["is_error"], rec["error"]),
                         ("info", "stopped", False, "stopped by STOP marker"))   # not "terminated by SIGTERM"
        got = [(p["status"], p["summary"], p.get("via")) for p in self.pointers("main-acme")
               if p["from"] == "div-acme-build"]
        self.assertEqual(got, [("info", "stopped by Main", "runner")])
        self.assertFalse(alive((self.fake / "pid").read_text().strip()))
        self.assertIsNone(self.lock(unit))
        self.assertFalse(runs.stop_requested("acme", unit))          # consumed and cleared

    def test_a_marker_older_than_the_run_never_stops_it(self):
        """Item 1, ORCHE_RUN_SINCE rule: a marker from before the run was requested is ignored by
        the poll; the run files its result and leaves that marker alone."""
        unit = self.brief()
        folder = self.home / "runs" / "acme"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{unit}.stop").write_text("old\n")
        (self.fake / "sleep").write_text(str(headless.POLL_S + 1.5))   # spans at least one poll
        self.reply("STATUS: blocked\nneeds design")
        proc = self.spawn(unit, env={**os.environ, "ORCHE_RUN_SINCE": str(time.time_ns())})
        self.assertEqual(proc.wait(timeout=20), 0, (self.root / f"{unit}.out").read_text())
        got = [(p["status"], p["summary"]) for p in self.pointers("main-acme") if p["from"] == "div-acme-build"]
        self.assertEqual(got, [("blocked", "needs design")])
        self.assertEqual(self.run_record(unit)["error"], None)
        self.assertTrue((folder / f"{unit}.stop").exists())

    def test_stop_signals_only_a_verified_pid(self):
        """Item 2: `ps` cannot tell -> lock kept, marker written, `runner-pid-check-failed`, no
        signal; a pid `ps` shows is `orche run <unit>` still gets SIGTERM (the fast path)."""
        unit = self.brief()
        victim = self.sleeper()                 # dies on SIGTERM
        self.lock_record(unit, victim.pid)
        calls = []
        with self.failing_ps(calls):
            self.assertTrue(runs.stop("acme", unit))
        self.assertEqual(len(calls), 1)
        time.sleep(0.3)
        self.assertIsNone(victim.poll())                                   # never signalled
        self.assertEqual(self.lock(unit)["pid"], victim.pid)
        self.assertTrue(runs.stop_requested("acme", unit))
        self.assertIn(f"run unit={unit} to=div-acme-build runner-pid-check-failed pid={victim.pid}", self.events())
        verified = self.fake_run(unit)
        self.assertTrue(self.wait_for(lambda: runs.is_run_process(verified.pid, unit)))
        self.lock_record(unit, verified.pid)
        self.assertTrue(runs.stop("acme", unit))
        self.assertTrue(self.wait_for(lambda: verified.poll() is not None, timeout=5))
        self.assertEqual(verified.returncode, -signal.SIGTERM)

    def test_unit_after_a_global_flag_or_double_dash_still_matches(self):
        """Item 3 (m3-1): `orche run --json <unit>` is a live run: matched, so neither doctor nor
        STOP drops its lock; `ps` is asked for the full command line (`-ww`, m3-4)."""
        from orche.install import doctor
        unit = self.brief()
        rx = runs._run_command(unit)
        for line in (f"/x/orche run --json {unit}", f"/x/orche run --dry-run {unit}", f"/x/orche run -- {unit}",
                     f"/x/orche run --json --dry-run -- {unit}", f"python3 /x/orche.py run --json {unit}",
                     f"python3 /x/orche.py --json run {unit}", f"/x/orche run {unit} --json"):
            self.assertTrue(rx.search(line), line)
        for line in (f"/x/orche run --json {unit}2", f"/x/orche run --other {unit}", f"/x/orche run--json {unit}",
                     f"/x/run --json {unit}"):
            self.assertIsNone(rx.search(line), line)
        live = self.fake_run("--json", unit)
        self.assertTrue(self.wait_for(lambda: runs.is_run_process(live.pid, unit)))
        self.lock_record(unit, live.pid)
        self.assertEqual(doctor.runner_lock_checks(registry.load()), [])
        self.assertEqual(self.lock(unit)["pid"], live.pid)
        seen = []
        real = runs.shell.RUNNER

        def spy(argv, *, timeout, input):
            seen.append(list(argv))
            return real(argv, timeout=timeout, input=input)
        with mock.patch.object(runs.shell, "RUNNER", spy):
            self.assertTrue(runs.is_run_process(live.pid, unit))
        self.assertEqual(seen, [["ps", "-ww", "-o", "command=", "-p", str(live.pid)]])

    def test_release_says_whether_it_removed_the_lock_and_only_a_removal_is_logged(self):
        """Item 4 (m3-3): `release` -> bool; a run that released its own lock between `running()`
        and the `ps` answer gives no `runner-stale-lock` line and no doctor row."""
        from orche.install import doctor
        unit = self.brief()
        other = self.sleeper()
        path = self.lock_record(unit, other.pid)
        self.assertIs(runs.release(path), False)                 # not this process's lock
        self.assertIs(runs.release(path, pid=other.pid), True)
        self.assertIs(runs.release(path, pid=other.pid), False)  # already gone

        def released_meanwhile(pid, unit_, timeout=runs.PS_TIMEOUT_S):
            (self.home / "runs" / "acme" / f"{unit_}.lock").unlink(missing_ok=True)   # the run's `finally`
            return False
        self.lock_record(unit, other.pid)
        with mock.patch.object(runs, "is_run_process", released_meanwhile):
            self.assertFalse(runs.stop("acme", unit))
        self.lock_record(unit, other.pid)
        with mock.patch.object(runs, "is_run_process", released_meanwhile):
            self.assertEqual(doctor.runner_lock_checks(registry.load()), [])
        self.assertNotIn("runner-stale-lock", self.events())
        self.assertNotIn("runner-reconciled", self.events())     # R-A: a settled lock is never filed twice
        self.assertEqual(self.pointers("main-acme"), [])
        self.assertIsNone(other.poll())


class KeychainHermeticTest(RunnerCase):
    """T1: a child `orche run` reads the empty stub keychain of support.no_claude_dir, never the
    real one, so its `claude -p` carries an OAuth token only when the test passes one."""

    def test_child_run_hits_the_stub_and_carries_no_token(self):
        self.assertEqual(shutil.which("security"), str(Path(support.no_claude_dir()) / "security"))
        unit = self.brief()
        self.reply("STATUS: blocked\nfirst")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        self.assertEqual(self.spawn(unit, env=env).wait(timeout=20), 0)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN=", (self.fake / "env.txt").read_text())
        if sys.platform == "darwin":       # elsewhere the store is a file under the temp ORCHE_HOME
            calls = (self.home / support.SECURITY_CALLS).read_text().split()
            self.assertEqual(calls[:5], ["find-generic-password", "-s", "orche", "-a", "CLAUDE_CODE_OAUTH_TOKEN"])

    def test_child_run_carries_a_token_only_when_the_test_sets_it(self):
        unit = self.brief()
        self.reply("STATUS: blocked\nfirst")
        token = "tok-test-" + "Z9" * 8
        env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token}
        self.assertEqual(self.spawn(unit, env=env).wait(timeout=20), 0)
        self.assertIn(f"CLAUDE_CODE_OAUTH_TOKEN={token}", (self.fake / "env.txt").read_text())


if __name__ == "__main__":
    import unittest
    unittest.main()
