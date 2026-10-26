"""Keymaster state and backend dispatch.

State under ORCHE_HOME/secrets (names, envs and actors only, never values):
- `requests/<ts>-<NAME>.json`: `{name, envs, why, how_url, project, requested_by, at, status}`
  (`status` pending|filled; rotate requests add `"mode": "rotate"`). `orche statusline` counts
  the pending ones, so the shape is shared with Task 2.
- `audit.log`: append-only TSV `ts action project name env backend actor request_id`.
- `shares.json`: `{"devs": {email: {...}}, "shares": [{to, project, env, secrets, at, file}]}`.

Every external command goes through `Exec`, which wraps `shell.run` and, in dry-run, records
the argv instead of running it. Values only ever travel on stdin; recorded lines show `<value>`.
"""
import datetime as _dt
import getpass
import os
import re
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .. import config, log, mask, notify as notice, paths, registry, roster, shell, util
from . import ENVS, VALUE_PLACEHOLDER, SecretError, backends_gcp, backends_gh, backends_keychain, backends_vercel

NAME_RX = re.compile(r"^[A-Z][A-Z0-9_]*$")
BACKENDS = ("gcp", "vercel")
CFG_DEFAULTS = {"backend": "gcp", "gcp_project": {}, "vercel_project": None, "gh_repo": None, "cache_local": False}
AUDIT_COLUMNS = ("ts", "action", "project", "name", "env", "backend", "actor", "request_id")
# Interaction hooks (tests replace them; cmd_* modules are reloaded per CLI call, this one is not).
GETPASS = getpass.getpass
CONFIRM = input
EXEC = os.execvpe
_BREAKS = str.maketrans({"\t": " ", "\n": " ", "\r": " "})


# ---------------------------------------------------------------- locations


def secrets_dir() -> Path:
    path = paths.orche_home() / "secrets"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)  # owner only (applies when created)
    return path


def requests_dir() -> Path:
    path = secrets_dir() / "requests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_path() -> Path:
    return secrets_dir() / "audit.log"


def shares_path() -> Path:
    return secrets_dir() / "shares.json"


def check_name(name: str) -> str:
    if not NAME_RX.match(str(name or "")):
        raise SecretError(f"invalid secret name {name!r} (use UPPER_SNAKE_CASE)")
    return name


@contextmanager
def keymaster_env():
    """Mark this process as the keymaster (exempt from the env-read gate) for the block's duration."""
    old = os.environ.get("ORCHE_KEYMASTER")
    os.environ["ORCHE_KEYMASTER"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ORCHE_KEYMASTER", None)
        else:
            os.environ["ORCHE_KEYMASTER"] = old


# ---------------------------------------------------------------- command execution


class Exec:
    """Runs commands via `shell.run`; with `dry_run` it records them and runs nothing."""

    def __init__(self, dry_run=False):
        self.dry_run = bool(dry_run)
        self.planned = []

    def run(self, argv, *, stdin=None, shown_stdin=None, timeout=60) -> shell.Result:
        argv = [str(a) for a in argv]
        if self.dry_run:
            line = shlex.join(argv)
            if stdin is not None:
                line += " <<< " + (shown_stdin or VALUE_PLACEHOLDER)
            self.planned.append(line)
            return shell.Result(0, "", "", argv)
        return shell.run(argv, input=stdin, timeout=timeout)


def scrub(text, *values) -> str:
    """`text` with every given value replaced by `<value>`, then masked."""
    text = str(text or "")
    for value in values:
        if value:
            text = text.replace(value, VALUE_PLACEHOLDER)
    return mask.redact(text)[0]


def failure(result, what, *values) -> SecretError:
    lines = [ln for ln in scrub(result.err, *values).strip().splitlines() if ln.strip()]
    tail = f": {lines[-1].strip()[:200]}" if lines else ""
    return SecretError(f"{what} failed (exit {result.code}): {shlex.join(result.argv)}{tail}")


# ---------------------------------------------------------------- project config


def resolve_project(reg, name=None, cwd=None) -> str:
    if name:
        hit = registry.project(reg, name)
        if hit is None:
            raise SecretError(f"unknown project {name!r}", code=2)
        return hit[0]
    key = roster.Roster(reg).place(cwd or os.getcwd()).project
    if not key:
        raise SecretError("cannot tell the project from cwd; pass --project")
    return key


def project_cfg(reg, key) -> dict:
    proj = reg.get("projects", {}).get(key)
    if proj is None:
        raise SecretError(f"unknown project {key!r}", code=2)
    cfg = {**CFG_DEFAULTS, **(proj.get("secrets") or {})}
    cfg["project"] = key
    cfg["root"] = proj.get("path") or None
    return cfg


# ---------------------------------------------------------------- format check


_FORMAT_RULES = (
    (lambda n: n == "AWS_ACCESS_KEY_ID", re.compile(r"^AKIA"), "must start with AKIA"),
    (lambda n: n == "GITHUB_TOKEN", re.compile(r"^gh[pousr]_"), "must start with gh[pousr]_"),
    (lambda n: n == "GOOGLE_API_KEY", re.compile(r"^AIza"), "must start with AIza"),
    (lambda n: n.startswith("STRIPE_"), re.compile(r"^(sk_live_|sk_test_|rk_|pk_live_|pk_test_|whsec_)"),
     "must start with sk_live_, sk_test_, rk_, pk_live_, pk_test_ or whsec_"),
    (lambda n: n.endswith("_PRIVATE_KEY"),
     re.compile(r"^(?:(?:0x)?[0-9a-fA-F]{64}|-----BEGIN [A-Z0-9 ]+-----[\s\S]+-----END [A-Z0-9 ]+-----)$"),
     "must be 64 hex characters or a PEM block"),
)


def validate_format(name: str, value: str):
    """(ok, reason). Prefix heuristics per name; the reason never contains the value."""
    if not value:
        return False, "empty value"
    for applies, rx, reason in _FORMAT_RULES:
        if applies(name):
            return (True, "") if rx.search(value) else (False, f"{name} {reason}")
    if any(ch.isspace() for ch in value):
        return False, f"{name} must not contain whitespace"
    if len(value) < 8:
        return False, f"{name} must be at least 8 characters"
    return True, ""


# ---------------------------------------------------------------- requests


def _request_path(rid: str) -> Path:
    return requests_dir() / f"{rid}.json"


def list_requests(status=None, project=None, name=None) -> list:
    """[(id, request)] sorted by id (oldest first). Unreadable files are skipped."""
    out = []
    for path in sorted(requests_dir().glob("*.json")):
        try:
            req = util.read_json(path)
        except util.JsonError as exc:
            log.log("keymaster", f"skip {path.name}: {exc.reason}")
            continue
        if not isinstance(req, dict):
            continue
        if status and req.get("status") != status:
            continue
        if project and req.get("project") != project:
            continue
        if name and req.get("name") != name:
            continue
        out.append((path.stem, req))
    return out


def get_request(rid: str):
    return util.read_json(_request_path(rid))


def new_request(project, name, envs, why, how_url, requested_by, mode=None, dry_run=False):
    """Enqueue (or extend a matching pending request). Returns (id, request, created)."""
    check_name(name)
    for rid, req in list_requests(status="pending", project=project, name=name):
        if req.get("mode") == mode:
            merged = list(req.get("envs") or [])
            merged += [e for e in envs if e not in merged]
            req["envs"] = merged
            if not dry_run:
                util.write_json(_request_path(rid), req)
            return rid, req, False
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    rid, n = f"{stamp}-{name}", 2
    while _request_path(rid).exists():
        rid, n = f"{stamp}-{name}-{n}", n + 1
    req = {"name": name, "envs": list(envs), "why": mask.redact(why or "")[0],
           "how_url": mask.redact(how_url or "")[0], "project": project,
           "requested_by": requested_by, "at": util.now_iso(), "status": "pending"}
    if mode:
        req["mode"] = mode
    if not dry_run:
        util.write_json(_request_path(rid), req)
    return rid, req, True


def mark_filled(rid: str) -> dict:
    req = get_request(rid) or {}
    req["status"] = "filled"
    req["filled_at"] = util.now_iso()
    util.write_json(_request_path(rid), req)
    return req


# ---------------------------------------------------------------- audit


def audit(action, project="", name="", env="", backend="", actor=None, request_id="") -> None:
    row = [util.now_iso(), action, project, name, env, backend, actor or roster.actor(), request_id]
    line = "\t".join(str(v or "-").translate(_BREAKS) for v in row)
    with open(audit_path(), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_audit() -> list:
    try:
        text = audit_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [dict(zip(AUDIT_COLUMNS, ln.split("\t"))) for ln in text.splitlines() if ln.strip()]


def known_names(project, env=None) -> dict:
    """{NAME: [envs]} of secrets stored for `project` (from the audit log)."""
    out = {}
    for row in read_audit():
        if row.get("action") == "store" and row.get("project") == project:
            if env and row.get("env") != env:
                continue
            envs = out.setdefault(row["name"], [])
            if row["env"] not in envs:
                envs.append(row["env"])
    return {k: sorted(v, key=ENVS.index) for k, v in sorted(out.items())}


# ---------------------------------------------------------------- backends


def put(ex, cfg, name, env, value, *, rotate=False, actor=None, request_id="") -> list:
    """Store one value for one env in the primary backend, gh (if configured) and the keychain cache.

    Returns the backends written. Raises SecretError (names only) when a write fails.
    """
    check_name(name)
    project = cfg["project"]
    primary = cfg.get("backend") or "gcp"
    if primary == "gcp":
        backends_gcp.put(ex, cfg, name, env, value, rotate=rotate)
    elif primary == "vercel":
        backends_vercel.put(ex, cfg, name, env, value, rotate=rotate)
    else:
        raise SecretError(f"unknown secrets.backend {primary!r}")
    done = [primary]
    if cfg.get("gh_repo"):
        backends_gh.put(ex, cfg, name, env, value)
        done.append("gh")
    if cfg.get("cache_local"):
        if backends_keychain.put(ex, project, name, env, value):
            done.append("keychain")
        else:
            log.log("keymaster", f"cache skipped {project}/{env}/{name}")
    if not ex.dry_run:
        for backend in done:
            audit("store", project, name, env, backend, actor, request_id)
        if rotate:
            audit("rotate", project, name, env, primary, actor, request_id)
    return done


def get(ex, cfg, name, env) -> str:
    """The current value (keychain cache first when enabled). `<value>` in dry-run."""
    check_name(name)
    if cfg.get("cache_local"):
        value = backends_keychain.get(ex, cfg["project"], name, env)
        if value is not None:
            return VALUE_PLACEHOLDER if ex.dry_run else value
    primary = cfg.get("backend") or "gcp"
    if primary == "gcp":
        value = backends_gcp.get(ex, cfg, name, env)
        return VALUE_PLACEHOLDER if ex.dry_run else value
    raise SecretError(f"{name} ({env}): {primary} values cannot be read back; enable secrets.cache_local",
                      code=2)


# ---------------------------------------------------------------- notifications and dialog process


KEYMASTER = "ORCHE Keymaster"


def notify(ex, text: str, project=None) -> None:
    """A Keymaster notification through notify.send (masked, an argument to a fixed script; config
    `notify`, macOS only; a `project` key makes a click open its Main tab); a dry run only plans its
    osascript line."""
    if not ex.dry_run:
        notice.send(KEYMASTER, text, kind="keymaster", project=project)
    elif notice.enabled():
        ex.run(notice.argv(KEYMASTER, text))


def _pid_path() -> Path:
    return secrets_dir() / "dialog.pid"


def dialog_running() -> bool:
    try:
        pid = int(_pid_path().read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def spawn_dialog():
    """Start the dialog loop as its own detached process (exempt from the env-read gate)."""
    if dialog_running():
        return None
    script = Path(__file__).with_name("dialog.py")
    return subprocess.Popen([sys.executable, "-I", str(script)], start_new_session=True,
                            env={**os.environ, "ORCHE_KEYMASTER": "1"},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- .env.example and dotenv text


def is_env_example(path) -> bool:
    """True when `path` is (or would overwrite) a `.env.example`: case-insensitive name, or the same
    file as the `.env.example` next to it (case-insensitive filesystems, links)."""
    path = Path(path)
    if path.name.lower() == ".env.example":
        return True
    sibling = path.parent / ".env.example"
    try:
        return path.exists() and sibling.exists() and os.path.samefile(path, sibling)
    except OSError:
        return False


def find_env_example(start=None):
    """Nearest `.env.example` walking up from `start`; stops at the first directory holding `.git`."""
    here = Path(os.path.abspath(start or os.getcwd()))
    for directory in (here, *here.parents):
        candidate = directory / ".env.example"
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            return None
    return None


def env_example_names(path) -> list:
    """Keys of a `.env.example` (values in it are ignored)."""
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key = line.split("=", 1)[0].strip()
        if NAME_RX.match(key) and key not in out:
            out.append(key)
    return out


def dotenv_text(values: dict) -> str:
    def quote(v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return "".join(f"{k}={quote(v)}\n" for k, v in values.items())
