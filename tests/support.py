"""Shared test helpers: put lib/ on sys.path and isolate ORCHE_HOME per test."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

# Process-wide default for every test module (TempHomeCase re-applies it per test): the Jev key
# store never touches the real keychain and no real dialog opens.
from orche import headless, jev_setup as _jev_setup, jevkey as _jevkey, reconcile as _reconcile  # noqa: E402

_jevkey.PLATFORM = "linux"
_jevkey.SECURITY = "/nonexistent/orche-test/security"
_jev_setup.OSASCRIPT = "/nonexistent/orche-test/osascript"
# No in-process pass arms the watchdog (SIGALRM, its exit thread) unless the test asks: run_pass(watchdog_s=…).
_reconcile.WATCHDOG_S = 0


_NO_CLAUDE_DIR = None
NO_CLAUDE = "#!/bin/sh\necho 'tests never run the real claude (support.NO_CLAUDE)' >&2\nexit 1\n"
NO_ORCA = "#!/bin/sh\necho 'tests never run the real orca (support.NO_ORCA)' >&2\nexit 1\n"
NO_CODEX = "#!/bin/sh\necho 'tests never run the real codex (support.NO_CODEX)' >&2\nexit 1\n"
# No test reaches GitHub: the default `gh` records its argv in $ORCHE_HOME/gh-calls.log and fails.
NO_GH = ("#!/bin/sh\n"
         "[ -n \"$ORCHE_HOME\" ] && printf '%s\\n' \"$*\" >> \"$ORCHE_HOME/gh-calls.log\" 2>/dev/null\n"
         "echo 'tests never run the real gh (support.NO_GH)' >&2\nexit 1\n")
GH_CALLS = "gh-calls.log"
NO_WAKE = {"inbox": {"wake": False, "quiet_s": 0}, "review": {"build": False}, "runner": {"min_free_mb": 0}}
# every test home's config: no test types into a pane, no nudger waits out a quiet window (tests that
# exercise it set inbox.quiet_s and a fake clock), no publish starts a detached Build review (tests that
# want one set review.build) and no run waits on the machine's real free memory (memory tests set
# runner.min_free_mb or pass min_free_mb, and mock procs.free_mb)
NO_REVIEW = {"review": {"cross_vendor": "off"}}      # no_wake's configs: no pre-review (test_prereview turns it on)
# An empty keychain for child processes (`bin/orche.py run|intake|card` read the stored OAuth token
# through `security` on macOS); each call is recorded, argv only, in $ORCHE_HOME/security-calls.log.
NO_SECURITY = ("#!/bin/sh\n"
               "[ -n \"$ORCHE_HOME\" ] && printf '%s\\n' \"$*\" >> \"$ORCHE_HOME/security-calls.log\" 2>/dev/null\n"
               "echo 'security: The specified item could not be found in the keychain.' >&2\n"
               "exit 44\n")
SECURITY_CALLS = "security-calls.log"
# No real notification and no real launchd query: `osascript` succeeds and `launchctl` fails (nothing
# loaded), each recording its argv (one line per call) in $ORCHE_HOME/<name>-calls.log.
RECORDING = ("#!/bin/sh\n"
             "[ -n \"$ORCHE_HOME\" ] && printf '%s\\n' \"$*\" >> \"$ORCHE_HOME/{name}-calls.log\" 2>/dev/null\n"
             "exit {code}\n")
OSASCRIPT_CALLS = "osascript-calls.log"
LAUNCHCTL_CALLS = "launchctl-calls.log"
OPEN_CALLS = "open-calls.log"


def no_claude_dir() -> str:
    """A directory whose `claude`, `codex`, `orca`, `security` and `launchctl` always fail and whose `osascript`
    and `open` only record their call (no test shows a notification, a Finder window or System
    Settings). It goes first on PATH for the
    whole test process (below), so code that falls back to the default `claude_bin` (e.g. the intake
    at the end of `orche init`) or `orca_bin` (a nudger) can never start the developer's real Claude
    Code or reach the live Orca, and no child process reads the real keychain (it sees an empty one),
    whatever the test's base class; tests that want a worker set config `claude_bin`, tests that
    want Orca use ORCHE_FAKE_ORCA=1, keychain tests put their own fake `security` before it."""
    global _NO_CLAUDE_DIR
    if _NO_CLAUDE_DIR is None:
        import atexit
        import shutil
        _NO_CLAUDE_DIR = tempfile.mkdtemp(prefix="orche-no-claude-")
        atexit.register(shutil.rmtree, _NO_CLAUDE_DIR, True)
        for name, text in (("claude", NO_CLAUDE), ("codex", NO_CODEX), ("orca", NO_ORCA), ("security", NO_SECURITY),
                           ("gh", NO_GH),
                           ("osascript", RECORDING.format(name="osascript", code=0)),
                           ("open", RECORDING.format(name="open", code=0)),
                           ("launchctl", RECORDING.format(name="launchctl", code=113))):
            stub = Path(_NO_CLAUDE_DIR) / name
            stub.write_text(text)
            stub.chmod(0o755)
    return _NO_CLAUDE_DIR


def no_wake(cfg: dict) -> dict:
    """`cfg` with `inbox.wake` false unless it sets `inbox.wake` itself, the pre-review off unless it
    sets `review.cross_vendor` itself (test config writers; tests/test_prereview.py turns it on) and no
    memory guard unless it sets `runner.min_free_mb` itself."""
    inbox = cfg.get("inbox") if isinstance(cfg.get("inbox"), dict) else {}
    review = cfg.get("review") if isinstance(cfg.get("review"), dict) else {}
    runner = cfg.get("runner") if isinstance(cfg.get("runner"), dict) else {}
    return {**cfg, "inbox": {**NO_WAKE["inbox"], **inbox},
            "review": {**NO_WAKE["review"], **NO_REVIEW["review"], **review},
            "runner": {**NO_WAKE["runner"], **runner}}


SCRUBBED_ENV = ("ORCHE_", "ORCA_", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
os.environ["PATH"] = no_claude_dir() + os.pathsep + os.environ.get("PATH", "")


class TempHomeCase(unittest.TestCase):
    """Runs each test with ORCHE_HOME pointing at a fresh temporary directory (and PATH restored
    afterwards, the failing `claude` and `orca` of no_claude_dir still first). The home's config
    turns the wake off; a test that turns it on sets `self.wakes = True` (and waits for its
    nudger's events line), else tearDown fails it when `logs/nudge.log` exists."""

    def setUp(self):
        super().setUp()
        self._saved_path = os.environ.get("PATH", "")
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "config.json").write_text(json.dumps(NO_WAKE))
        # A fresh hook-kick marker: no hook a test runs starts a detached `orche reconcile` pass
        # (reconcile.kick waits every_s after it); kick tests remove it and stub the spawn.
        (self.home / "reconcile").mkdir()
        (self.home / "reconcile" / ".kicked").touch()
        # An ORCHE pane exports ORCHE_SESSION / ORCA_TERMINAL_HANDLE (and maybe more): every test
        # starts without any ORCHE_* / ORCA_* variable (nor a Claude OAuth token, which headless
        # runs and doctor would pick up), and the caller's values come back after.
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith(SCRUBBED_ENV)}
        for key in self._saved_env:
            del os.environ[key]
        os.environ["ORCHE_HOME"] = str(self.home)
        # Claude Code's transcripts (wake resolves a session id there) live in the temp home too.
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home / "claude")
        # The Jev key store must never reach the developer's real keychain nor pop a real dialog:
        # every test starts on the file backend (under this temp ORCHE_HOME) with no `security`
        # or `osascript`; keychain/dialog tests put fakes on PATH and switch these seams back.
        from orche import jev_setup, jevkey
        self._jev_seams = (jevkey.PLATFORM, jevkey.SECURITY, jev_setup.OSASCRIPT)
        jevkey.PLATFORM = "linux"
        jevkey.SECURITY = str(self.home / "no-such-security")
        jev_setup.OSASCRIPT = str(self.home / "no-such-osascript")

    def tearDown(self):
        from orche import jev_setup, jevkey
        woke = (self.home / "logs" / "nudge.log").exists() and not getattr(self, "wakes", False)
        kicked = (self.home / "logs" / "reconcile.log").exists()
        reviewed = (self.home / "logs" / "review").exists()
        jevkey.PLATFORM, jevkey.SECURITY, jev_setup.OSASCRIPT = self._jev_seams
        for key in [k for k in os.environ if k.startswith(SCRUBBED_ENV)]:
            del os.environ[key]
        os.environ.update(self._saved_env)
        os.environ["PATH"] = self._saved_path
        self._tmp.cleanup()
        super().tearDown()
        if kicked:
            self.fail("a reconcile pass was started (logs/reconcile.log): keep reconcile/.kicked fresh "
                      "or stub procs.spawn_detached")
        if reviewed:
            self.fail("a Build review was started (logs/review): keep the test home's review.build false "
                      "(support.no_wake) or stub procs.spawn_detached")
        if woke:
            self.fail("a nudger was started (logs/nudge.log): keep the test home's inbox.wake false "
                      "(support.no_wake) or set self.wakes = True and wait for its events line")


ABSOLUTE_PYTHONS = "/opt/homebrew/bin/python3 /usr/local/bin/python3"


def plugin_copy(dest: Path, *rels: str) -> Path:
    """Copy plugin files (e.g. `hooks/run.sh`, `lib/find_python.sh`) under `dest`, keeping modes.

    The interpreter finder's absolute candidates are pointed at /nowhere, so this machine's own
    Homebrew python cannot satisfy a "no Python 3.11+ found" test.
    """
    for rel in rels:
        src, out = ROOT / rel, dest / rel
        text = src.read_text()
        if rel == "lib/find_python.sh":
            assert ABSOLUTE_PYTHONS in text, "finder candidates changed; update support.plugin_copy"
            text = text.replace(ABSOLUTE_PYTHONS, "/nowhere/python3 /nowhere/local/python3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        out.chmod(src.stat().st_mode & 0o777)
    return dest


# A fake `claude` for runner/doctor tests, selected through config `claude_bin` (never the real one).
FAKE_CLAUDE = """#!/bin/sh
d=$(dirname "$0")
case "$1" in
  --help) cat "$d/help.txt"; exit 0 ;;
  --version) if [ -f "$d/version" ]; then cat "$d/version"; else echo "9.9.9 (Claude Code)"; fi; exit 0 ;;
esac
if [ -f "$d/ignore_term" ]; then trap '' TERM; fi
cat > "$d/stdin.txt"
printf '%s\\n' "$@" > "$d/argv.txt"
printf '%s\\n' "$ORCHE_SESSION" > "$d/session.txt"
pwd -P > "$d/cwd.txt"
printf '%s\\n' "$PATH" > "$d/path.txt"
if [ -f "$d/report_to" ]; then echo "worker wrote this" > "$(cat "$d/report_to")"; fi
env > "$d/env.txt"
if [ -f "$d/action.sh" ]; then sh "$d/action.sh"; fi
if [ -f "$d/sleep" ]; then echo $$ > "$d/pid"; sleep "$(cat "$d/sleep")"; fi
if [ -f "$d/reply.json" ]; then cat "$d/reply.json"; fi
exit "$(cat "$d/exit" 2>/dev/null || echo 0)"
"""
HELP_FULL = "Usage: claude [options]\n" + "".join(f"  {flag} <x>\n" for flag in headless.FLAGS)


def write_fake_claude(folder: Path, help_text=HELP_FULL) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    (folder / "help.txt").write_text(help_text)
    return script


# A fake `codex` for tests/test_codex.py. Assumptions about the real codex it encodes (unverified
# against a working codex): `--version` prints one line; `exec ... -C <dir> ... -o <file> -` reads the
# prompt on stdin, prints JSONL events on stdout and writes the final message to the -o file.
FAKE_CODEX = r"""#!{python}
import json, os, signal, subprocess, sys, time
SPEC = json.loads({spec!r})
HERE = {here!r}
args = sys.argv[1:]
if args[:1] == ["--version"]:
    with open(os.path.join(HERE, "version_env.json"), "w") as fh:
        json.dump(sorted(os.environ), fh)
    if SPEC.get("version_fail"):
        print("codex: broken", file=sys.stderr); sys.exit(2)
    print("codex-cli 0.153.0"); sys.exit(0)
if args[:1] != ["exec"]:
    print("usage: codex exec", file=sys.stderr); sys.exit(2)
cwd = args[args.index("-C") + 1]
out = args[args.index("-o") + 1]
stdin = sys.stdin.read()
os.makedirs(os.path.join(HERE, "calls"), exist_ok=True)
with open(os.path.join(HERE, "calls", f"{{os.getpid()}}.json"), "w") as fh:
    json.dump({{"argv": sys.argv, "cwd": os.getcwd(), "stdin_len": len(stdin), "stdin": stdin,
               "env": {{k: k in os.environ for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ORCHE_SESSION",
                                                    "ORCHE_RUNNER", "ORCHE_RUN_ID", "CODEX_HOME")}},
               "claude_names": sorted(k for k in os.environ if "CLAUDE" in k),
               "anthropic_names": sorted(k for k in os.environ if "ANTHROPIC" in k),
               "run_id": os.environ.get("ORCHE_RUN_ID")}}, fh)
for name, text in SPEC.get("files", {{}}).items():
    with open(os.path.join(cwd, name), "w") as fh:
        fh.write(text)
if SPEC.get("commit"):
    subprocess.run(["git", "-C", cwd, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                    SPEC["commit"]], check=True, capture_output=True)
if SPEC.get("action"):     # a shell script run in the worktree (a ledger row for the new HEAD, …)
    subprocess.run(["/bin/sh", "-c", SPEC["action"]], cwd=cwd, check=True, capture_output=True)
QUIET = {{"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}}
pids = {{"self": os.getpid()}}
if SPEC.get("child"):
    pids["child"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **QUIET).pid
if SPEC.get("escaped"):
    pids["escaped"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                       start_new_session=True, **QUIET).pid
with open(os.path.join(HERE, "pids.json"), "w") as fh:
    json.dump(pids, fh)
if SPEC.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
for event in SPEC.get("events", []):
    print(event if isinstance(event, str) else json.dumps(event), flush=True)
if SPEC.get("final") is not None:
    with open(out, "w") as fh:
        fh.write(SPEC["final"])
time.sleep(SPEC.get("sleep", 0))
sys.exit(SPEC.get("code", 0))
"""


def fake_codex(folder: Path, spec: dict) -> Path:
    """An executable fake `codex` in `folder` acting out `spec` (see FAKE_CODEX). Each exec call
    leaves folder/calls/<pid>.json (argv, cwd, stdin and its length, which env names were set, every
    name containing CLAUDE) and folder/pids.json (its pid and its child/escaped pids). `files`,
    `commit` and `action` (a shell script run in the worktree) act before the events are printed."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "codex"
    script.write_text(FAKE_CODEX.format(python=sys.executable, spec=json.dumps(spec), here=str(folder)))
    script.chmod(0o755)
    return script


# The fake `gh` of every test that reaches GitHub (publish, the queue, the head review). It answers from
# folder/spec.json, read on every call (a test may rewrite it): {"prs": [{"number", "url", "headRefName",
# "baseRefName", "headRefOid", "isDraft", "state", "reviewDecision", ...}], "fail": "<stderr>" (every call) or
# {"pr list": "<stderr>", ...} (one subcommand), "origin": a bare repository, "merge_fail", "merge_other"}.
# A PR without headRefOid has its branch's head on the caller's origin; states compare in any case.
# `pr list --head B [--state S]`, `pr view N` (the --json fields), `pr create` (a pushed head only: a ready
# OPEN PR, its URL printed), `pr ready N`, `auth status`, `repo view`; `pr merge N --merge|--squash
# --match-head-commit H` marks the PR MERGED and, with spec["origin"], acts as GitHub's merge button there:
# it refuses a head other than the PR's (GitHub's text) or with spec["merge_fail"], else writes a commit with
# parents (base, head) — (base) for --squash — and the 3-way merge's tree (base's own tree with
# spec["merge_other"]: "something else landed"), moves the base branch and appends {"argv", "cwd", "base",
# "merge", "clone_head"} to folder/merges.jsonl. Anything else exits 1. Each call appends {"argv", "cwd"} to
# folder/gh-calls.jsonl.
FAKE_GH = r"""#!{python}
import json, os, subprocess, sys
HERE = {here!r}
SPEC_PATH = os.path.join(HERE, "spec.json")
args = sys.argv[1:]
with open(os.path.join(HERE, "gh-calls.jsonl"), "a") as fh:
    fh.write(json.dumps({{"argv": args, "cwd": os.getcwd()}}) + "\n")
with open(SPEC_PATH) as fh:
    SPEC = json.load(fh)
key = " ".join(args[:2])
fail = SPEC.get("fail")
fail = fail.get(key) if isinstance(fail, dict) else fail
if fail:
    print(fail, file=sys.stderr); sys.exit(1)
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def save():
    with open(SPEC_PATH, "w") as fh:
        json.dump(SPEC, fh)
def remote_head(branch):
    r = subprocess.run(["git", "ls-remote", "origin", "refs/heads/" + branch], capture_output=True, text=True)
    return r.stdout.split()[0] if r.stdout.split() else ""
def find(n):
    return next((p for p in SPEC.setdefault("prs", []) if str(p.get("number")) == str(n)), None)
def shown(p):
    p = dict({{"state": "OPEN", "isDraft": False}}, **p)
    if not p.get("headRefOid"):
        p["headRefOid"] = remote_head(p.get("headRefName", ""))
    fields = [f for f in (opt("--json") or "").split(",") if f]
    return {{k: p.get(k) for k in fields}} if fields else p
def missing(n):
    print(f"GraphQL: Could not resolve to a PullRequest with the number of {{n}}.", file=sys.stderr); sys.exit(1)
if key == "auth status":
    print("Logged in to github.com account tester"); sys.exit(0)
if key == "repo view":
    print(json.dumps({{"nameWithOwner": "acme/acme", "defaultBranchRef": {{"name": "main"}}}})); sys.exit(0)
if key == "pr list":
    head, state = opt("--head"), (opt("--state") or "open").upper()
    hits = [p for p in SPEC.get("prs", []) if (head is None or p.get("headRefName") == head)
            and (state == "ALL" or str(p.get("state") or "OPEN").upper() == state)]
    print(json.dumps([shown(p) for p in hits[:int(opt("--limit", 30))]])); sys.exit(0)
if key == "pr view":
    p = find(args[2]) if len(args) > 2 else None
    if p is None:
        missing(args[2] if len(args) > 2 else "")
    print(json.dumps(shown(p))); sys.exit(0)
if key == "pr ready":
    p = find(args[2]) if len(args) > 2 else None
    if p is None:
        missing(args[2] if len(args) > 2 else "")
    p["isDraft"] = False
    save(); sys.exit(0)
if key == "pr create":
    head, base = opt("--head"), opt("--base")
    if not remote_head(head):
        print("pull request create failed: head branch not pushed", file=sys.stderr); sys.exit(1)
    n = max([p["number"] for p in SPEC.setdefault("prs", [])] + [0]) + 1
    body = open(opt("--body-file")).read() if opt("--body-file") else ""
    url = "https://github.com/acme/acme/pull/%d" % n
    SPEC["prs"].append({{"number": n, "url": url, "headRefName": head, "baseRefName": base, "isDraft": False,
                        "state": "OPEN", "title": opt("--title"), "body": body}})
    save(); print(url); sys.exit(0)
if key == "pr merge":
    pr = find(args[2]) if len(args) > 2 else None
    if pr is None:
        missing(args[2] if len(args) > 2 else "")
    if SPEC.get("origin"):
        if SPEC.get("merge_fail"):
            print(SPEC["merge_fail"], file=sys.stderr); sys.exit(1)
        if opt("--match-head-commit") != pr.get("headRefOid"):
            print("GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)", file=sys.stderr); sys.exit(1)
        def g(*a):
            return subprocess.run(["git", "-c", "user.name=gh", "-c", "user.email=gh@example.invalid", "-C", SPEC["origin"], *a],
                                  check=True, capture_output=True, text=True).stdout.strip()
        ref = "refs/heads/" + pr.get("baseRefName", "main")
        base, head = g("rev-parse", ref), pr["headRefOid"]
        tree = g("rev-parse", base + "^{{tree}}") if SPEC.get("merge_other") else g("merge-tree", "--write-tree", base, head).splitlines()[0]
        parents = ["-p", base] if "--squash" in args else ["-p", base, "-p", head]
        merge = g("commit-tree", tree, *parents, "-m", f"Merge pull request #{{pr['number']}}")
        g("update-ref", ref, merge, base)
        clone = subprocess.run(["git", "-C", os.getcwd(), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        with open(os.path.join(HERE, "merges.jsonl"), "a") as fh:
            fh.write(json.dumps({{"argv": args, "cwd": os.getcwd(), "base": base, "merge": merge, "clone_head": clone}}) + "\n")
    pr["state"] = "MERGED"
    save()
    print(f"Merged pull request #{{pr['number']}}"); sys.exit(0)
print(f"fake gh: unsupported {{args}}", file=sys.stderr); sys.exit(1)
"""


def fake_gh(folder: Path, spec: dict | None = None) -> Path:
    """An executable fake `gh` in `folder` answering from `spec` (see FAKE_GH; default no PRs), put on PATH
    right before no_claude_dir (it holds only `gh`, so it shadows just the failing NO_GH stub; TempHomeCase
    restores PATH). Rewrite folder/spec.json to change its answers; folder/gh-calls.jsonl records each call."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "spec.json").write_text(json.dumps(spec if spec is not None else {"prs": []}))
    script = folder / "gh"
    script.write_text(FAKE_GH.format(python=sys.executable, here=str(folder)))
    script.chmod(0o755)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p != str(folder)]
    at = parts.index(no_claude_dir()) if no_claude_dir() in parts else 0
    os.environ["PATH"] = os.pathsep.join(parts[:at] + [str(folder)] + parts[at:])
    return script


def gh_calls(folder: Path) -> list:
    path = folder / "gh-calls.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def gh_state(folder: Path) -> dict:
    return json.loads((folder / "spec.json").read_text())
