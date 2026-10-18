"""Project intake: one read-only `claude -p` sweep of a project's main checkout, turned into files.

When ORCHE joins a codebase that already exists, every unit would otherwise rediscover it on its
own (tens of thousands of tokens per worker). Intake does it once, like Claude Code's `/init`
writes CLAUDE.md: a headless Sonnet worker (persona templates/personas/intake.md) reads the repo
with Read, Glob and Grep only (no shell, no write, no web) and answers with one JSON object

    {"card": {"what", "stack", "layout", "build_test_run", "conventions", "risks"},
     "divisions": {"<division>": "<what that division needs to know>", ...}, "notes": [...]}

The facts a shell would give it, ORCHE collects first and puts in the request (repo_facts(), each
part capped, the whole request <= REQUEST_BYTES): the tree to depth 3 without .git, node_modules,
dist, build, .orche, .venv, __pycache__ and caches (<= 300 entries, then "… n more"); `git log
--oneline -30` (run by ORCHE, no diffs); the manifest/config files found; and the variable NAMES of
`.env.example` / `.env.sample` / `.env.template` (never a value). All of it masked.

The worker's answer the CLI (never the worker) masks (mask.redact) and writes, untracked, into
the main checkout:

- `.orche/div/<division>/CONTEXT.md` per division: `# Context for <label> — intake at <sha7>,
  <YYYY-MM-DD>`, then the text capped at `intake.max_chars` (cut at a paragraph boundary,
  "… (truncated)"). The runner appends it to every brief of that division (a feature agent: Build).
- `PROJECT_CARD.md`: a section the liaison has not filled yet (Purpose / Stack / Entry points still
  holding the template's placeholder) gets `what` / `stack` / `layout`; a section with any other
  text is left alone. `build_test_run`, `conventions` and `risks` go into one delimited block
  (`<!-- orche-intake:begin … -->` … `<!-- orche-intake:end -->`, before `## How to ask`) that each
  intake replaces whole; nothing outside the block and the placeholders is touched.
- `.orche/intake.json`: the stamp `{head, date, model, cost_usd, duration_s, divisions, turns}`.

Tools and secrets: `--allowedTools Read,Glob,Grep`; `--disallowedTools` names Bash, Write, Edit,
NotebookEdit, WebFetch, WebSearch, Task and Agent, plus Read deny rules (they cover Read, Grep and
Glob) for `.env*`, `secrets/`, keys and credential files, relative to the working directory and as
`//**/…` absolute twins, and for the credential stores under `~` (`~/.ssh/**`, `~/.aws/**`, …). A
blanket `Read(~/**)` or `Read(//**)` would also deny the repository itself (deny beats allow, and a
rule has no "except"), so confinement to the checkout comes from `--restricted` instead, when the
installed claude lists it (`--help`: removes the code-running tools and WebFetch, ignores
user/project/local settings, confines the file tools to the working directories); `--tools
Read,Glob,Grep` likewise when listed. Then the persona's hard rules (never open them, never quote a
credential, env variable names only) and mask.redact over every string of the result before
anything is written.

The worker runs through runner.run_claude (filtered environment, per-run empty `--mcp-config`,
timeout, process-group kill, sweeps), one intake per project at a time (an flock on
ORCHE_HOME/intake/<proj>.lock). `stale()` decides `--if-stale`: no stamp, HEAD moved at least
`intake.refresh_commits` commits past the stamp's (or the stamp's head is no ancestor), or the
stamp is `intake.refresh_days` old.
"""
import contextlib
import datetime as _dt
import fcntl
import fnmatch
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import dispatch, log, mask, paths, procs, registry, runner, runs, shell, templates, util

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3
PERSONA = paths.PLUGIN_ROOT / "templates" / "personas" / "intake.md"
CARD_TEMPLATE = templates.TEMPLATES_ROOT / "project" / "PROJECT_CARD.md"
# No shell: the facts a shell would give, ORCHE puts in the request (repo_facts).
TOOLS = "Read,Glob,Grep"
# Claude Code permission rules: `**/x` is relative to the working directory (the checkout),
# `//**/x` absolute, `~/x` the home directory. Deny beats any allow a settings file might carry.
_SECRET_GLOBS = (".env*", "*.env", "secrets/**", "*.pem", "*.key", "*.p12", "*.pfx", ".npmrc", ".netrc",
                 "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.keystore", "*.jks", "credentials*.json",
                 "service-account*.json")
HOME_SECRETS = ("~/.ssh/**", "~/.aws/**", "~/.gnupg/**", "~/.kube/**", "~/.docker/config.json",
                "~/.config/gcloud/**", "~/.netrc", "~/.npmrc", "~/.claude.json", "~/.claude/.credentials.json")
SECRET_READS = tuple(f"Read({p}**/{g})" for p in ("", "//") for g in _SECRET_GLOBS) \
    + tuple(f"Read({h})" for h in HOME_SECRETS)
DENIED = ",".join(("Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent")
                  + SECRET_READS)
PROGRESS_S = 30          # `orche init`: one "intake running… N s" line this often (progress())
REQUEST_BYTES = 12 * 1024
TREE_DEPTH, TREE_ENTRIES, TREE_BYTES = 3, 300, 6000
LOG_COMMITS, LOG_BYTES = 30, 3000
MANIFEST_BYTES, ENV_BYTES, DIVISIONS_BYTES = 1200, 800, 3000
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".orche", ".venv", "venv", "__pycache__", ".worktrees"}
MANIFEST_NAMES = {
    "package.json", "pnpm-workspace.yaml", "deno.json", "tsconfig.json", "pyproject.toml", "setup.py",
    "setup.cfg", "Pipfile", "go.mod", "Cargo.toml", "Gemfile", "composer.json", "pom.xml", "build.gradle",
    "build.gradle.kts", "Makefile", "justfile", "Dockerfile", "Procfile", "vercel.json", "netlify.toml",
    "fly.toml", "render.yaml", "wrangler.toml", "firebase.json", "serverless.yml", "app.json",
    ".gitlab-ci.yml", ".travis.yml", "Jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml",
    "CLAUDE.md", "AGENTS.md"}
MANIFEST_GLOBS = ("requirements*.txt", "Dockerfile.*", "docker-compose*.y*ml", "compose.y*ml",
                  ".github/workflows/*", ".circleci/config.yml")
ENV_TEMPLATES = (".env.example", ".env.sample", ".env.template")
_ENV_NAME = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
CARD_FIELDS = ("what", "stack", "layout", "build_test_run", "conventions", "risks")
CARD_CHARS = 600         # per card field: the card stays near the liaison's 2K-token budget
MAX_NOTES = 5
TRUNCATED = "… (truncated)"
BEGIN = ("<!-- orche-intake:begin — written by `orche intake` and replaced whole by the next one; "
         "edit the sections above instead -->")
END = "<!-- orche-intake:end -->"
_BLOCK = re.compile(r"<!-- orche-intake:begin.*?<!-- orche-intake:end -->\n?", re.DOTALL)
# Card section -> card field, filled only while the section still holds the template's placeholder.
PLACEHOLDER_FIELDS = (("Purpose", "what"), ("Stack", "stack"), ("Entry points", "layout"))
BLOCK_FIELDS = (("Build, test, run", "build_test_run"), ("Conventions", "conventions"), ("Risks", "risks"))
NEEDS = {
    "build": "build/test/run commands (exact), module map (directory -> responsibility), coding conventions, "
             "CI, where tests live and how they are named",
    "research": "the product, its users, competitors and market as the docs show them",
    "gtm": "positioning and messaging, CHANGELOG / release notes, the audience, public channels",
    "rnd": "tech debt, TODO/FIXME clusters, spikes, experimental or half-finished areas, recorded decisions",
    "ops": "deploy config, infrastructure, runtime services, environment variable NAMES only (never values), "
           "monitoring, backups, scheduled jobs",
}
_DESCRIPTION = re.compile(r"^description:\s*(.*)$", re.MULTILINE)


class IntakeError(Exception):
    def __init__(self, message, code=EXIT_USER):
        super().__init__(mask.redact(str(message))[0])
        self.code = code


class Interrupted(IntakeError):
    """A SIGTERM stopped the intake (before claude started, or claude's group was killed)."""


def settings(cfg) -> dict:
    """The `intake` config section (config.load() already merged the defaults in)."""
    return dict(cfg.get("intake") or {})


# ---- project ---------------------------------------------------------------

def resolve(reg, name):
    """(key, project dict, main checkout Path); IntakeError(EXIT_NOT_FOUND) when unknown."""
    found = registry.project(reg, name or "")
    if not found:
        raise IntakeError(f"unknown project {name!r} (see: orche projects)", EXIT_NOT_FOUND)
    key, proj = found
    root = proj.get("path") or ""
    if not root or not Path(root).is_dir():
        raise IntakeError(f"main checkout of {key} is missing ({root or 'no path recorded'})", EXIT_NOT_FOUND)
    return key, proj, Path(root)


def _standard_charter(name) -> str:
    try:
        text = (templates.TEMPLATES_ROOT / "personas" / f"div-{name}.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = _DESCRIPTION.search(text)
    return m.group(1).split(": ", 1)[-1].strip() if m else ""


def divisions(proj) -> list:
    """[(name, label, charter, custom?)] in registry order (registry.divisions_of)."""
    custom = proj.get("custom_divisions") or {}
    out = []
    for name in registry.divisions_of(proj):
        spec = custom.get(name)
        if spec:
            out.append((name, spec.get("label") or name, spec.get("charter") or "", True))
        else:
            out.append((name, registry.STANDARD_LABELS.get(name, name), _standard_charter(name), False))
    return out


def stamp_path(root) -> Path:
    return paths.project_local(root) / "intake.json"


def context_path(root, name) -> Path:
    return paths.project_local(root) / "div" / name / "CONTEXT.md"


def read_stamp(root):
    try:
        data = util.read_json(stamp_path(root), default=None)
    except util.JsonError:
        return None
    return data if isinstance(data, dict) else None


# ---- staleness -------------------------------------------------------------

def _commits_since(root, head):
    """Commits from `head` to HEAD, None when `head` is unknown or no ancestor of HEAD."""
    if not head:
        return None
    res = shell.run(["git", "-C", str(root), "merge-base", "--is-ancestor", head, "HEAD"], timeout=30)
    if not res.ok:
        return None
    res = shell.run(["git", "-C", str(root), "rev-list", "--count", f"{head}..HEAD"], timeout=30)
    try:
        return int(res.out.strip()) if res.ok else None
    except ValueError:
        return None


def _age_days(date):
    try:
        then = _dt.datetime.fromisoformat(str(date))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.astimezone()
    return max(0.0, (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds() / 86400)


def stale(root, stamp, cfg) -> dict:
    """{stale, commits, days, head}: stale without a stamp, when HEAD moved `refresh_commits`
    commits past the stamp's head (or that head is no ancestor of HEAD), or after `refresh_days`."""
    s = settings(cfg)
    if not stamp:
        return {"stale": True, "commits": None, "days": None, "head": None, "why": "no intake yet"}
    commits, days = _commits_since(root, stamp.get("head")), _age_days(stamp.get("date"))
    why = None
    if commits is None:
        why = "stamped commit is not an ancestor of HEAD"
    elif commits >= int(s.get("refresh_commits") or 50):
        why = f"{commits} commits since the intake"
    elif days is None or days >= float(s.get("refresh_days") or 14):
        why = "intake date unknown" if days is None else f"intake is {int(days)} days old"
    return {"stale": why is not None, "commits": commits, "days": days, "head": stamp.get("head"), "why": why}


def fresh_line(info) -> str:
    sha = (info.get("head") or "?")[:7]
    return f"intake fresh ({sha}, {info.get('commits')} commits behind, {int(info.get('days') or 0)} days)"


# ---- worker ----------------------------------------------------------------

@dataclass
class Plan:
    key: str
    root: Path
    head: str
    divisions: list
    argv: list
    stdin: str
    timeout: int
    model: str
    run_id: str


def build_argv(cfg, flags, run_id) -> list:
    """claude's argv. `--restricted` (when this claude lists it) replaces `--setting-sources
    project`: it ignores the user/project/local settings files (so no project hook runs during the
    sweep) and confines the file tools to the checkout."""
    s = settings(cfg)
    argv = [cfg.get("claude_bin") or "claude", "-p", "--model", s.get("model") or "claude-sonnet-5-5",
            "--output-format", "json", "--max-turns", str(int(s.get("max_turns") or 30))]
    argv += ["--restricted"] if flags.get("restricted") else ["--setting-sources", "project"]
    if flags.get("tools"):
        argv += ["--tools", TOOLS]
    argv += ["--strict-mcp-config", runner.MCP_FLAG, str(runs.mcp_config_path(run_id)),
             "--allowedTools", TOOLS, "--disallowedTools", DENIED, "--no-session-persistence"]
    if flags.get("system_prompt_file"):
        argv += ["--append-system-prompt-file", str(PERSONA)]
    else:   # a claude without the file flag: the persona goes inline
        argv += ["--append-system-prompt", PERSONA.read_text(encoding="utf-8")]
    return argv


# ---- repository facts (what a shell would have told the worker) -------------

def _skip_dir(name) -> bool:
    return name in SKIP_DIRS or "cache" in name.lower()


def _fit(lines, total, limit) -> list:
    """`lines` cut to at most `limit` bytes, plus "… n more" when anything of `total` is left out."""
    out, size = [], 0
    for line in lines:
        size += len(line.encode("utf-8")) + 1
        if size > limit - 24:
            break
        out.append(line)
    if total > len(out):
        out.append(f"… {total - len(out)} more")
    return out


def _walk(root):
    """(tree lines, total entries, manifest paths, env template paths): the checkout to TREE_DEPTH,
    sorted, directories with a trailing `/`, SKIP_DIRS and caches left out; a nested checkout (its
    own `.git`) is listed but not entered; symlinks are not followed."""
    lines, manifests, envs, total = [], [], [], 0

    def visit(folder, rel, depth):
        nonlocal total
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if entry.name == ".git" or (is_dir and _skip_dir(entry.name)):    # a worktree's .git is a file
                continue
            path = rel + entry.name
            nested = is_dir and os.path.lexists(os.path.join(entry.path, ".git"))
            total += 1
            if len(lines) < TREE_ENTRIES:
                lines.append("  " * (depth - 1) + entry.name + ("/" if is_dir else "")
                             + (" (own git checkout, not listed)" if nested else ""))
            if not is_dir:
                if entry.name in MANIFEST_NAMES or any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(
                        entry.name, g) for g in MANIFEST_GLOBS):
                    manifests.append(path)
                if entry.name in ENV_TEMPLATES:
                    envs.append(path)
            elif depth < TREE_DEPTH and not nested:
                visit(entry.path, path + "/", depth + 1)

    visit(str(root), "", 1)
    return lines, total, manifests, envs


def _env_names(root, rel) -> list:
    """The variable names (left of `=`) of one env template; its values are never read out."""
    try:
        text = (Path(root) / rel).read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return []
    return list(dict.fromkeys(m.group(1) for line in text.splitlines() if (m := _ENV_NAME.match(line))))


def _git_log(root) -> list:
    res = shell.run(["git", "-C", str(root), "-c", "core.pager=cat", "log", "--oneline", f"-{LOG_COMMITS}"],
                    timeout=30)
    return [line[:100] for line in (res.out or "").splitlines()] if res.ok else []


def repo_facts(root, tree_bytes=TREE_BYTES) -> str:
    """The pre-computed request section (masked): tree, history, manifests, env variable names."""
    lines, total, manifests, envs = _walk(root)
    log_lines = _git_log(root)
    parts = ["Repository facts (collected by ORCHE before this run: you have no shell; use these instead "
             "of listing directories or reading git history):", "",
             f"Tree (depth {TREE_DEPTH}; .git, node_modules, dist, build, .orche, .venv, __pycache__ and "
             "caches left out):"]
    parts += _fit(lines, total, tree_bytes) or ["(empty)"]
    parts += ["", f"Recent history (`git log --oneline -{LOG_COMMITS}`, newest first):"]
    parts += _fit(log_lines, len(log_lines), LOG_BYTES) or ["(none)"]
    parts += ["", "Manifest and config files found:"]
    parts += _fit([f"- {p}" for p in manifests], len(manifests), MANIFEST_BYTES) or ["(none)"]
    if envs:
        env_lines = []
        for rel in envs:
            names = _env_names(root, rel)
            env_lines.append(f"- `{rel}` (values omitted): {', '.join(names) or '(no variables)'}")
        parts += ["", "Environment variable names from the env templates:"]
        parts += _fit(env_lines, len(env_lines), ENV_BYTES)
    return mask.redact("\n".join(parts))[0]


def request(key, root, head, divs, max_chars, today) -> str:
    """The user message on the worker's stdin, at most REQUEST_BYTES (the tree gives way first)."""
    lines = ["Project intake request.", "", f"Project: {key}",
             f"Repository (the main checkout; your working directory): {root}", f"HEAD: {head}",
             f"Date: {today}", "", 'Divisions of this project (one "divisions" entry per name):']
    per = max(160, DIVISIONS_BYTES // max(1, len(divs)))     # many long custom charters share the part
    for name, label, charter, custom in divs:
        need = "whatever its charter needs from this codebase" if custom else NEEDS.get(name, "its charter")
        line = f"- {name} ({label}{', custom' if custom else ''}): {charter or '(no charter)'} Needs: {need}."
        lines.append(line if len(line) <= per else line[:per - 1].rstrip() + "…")
    if not divs:
        lines.append("- (none yet: leave \"divisions\" empty)")
    example = ", ".join(f'"{name}": "..."' for name, *_ in divs)
    tail = ["", "Card fields: what = what the project is and who uses it; stack = languages, frameworks, "
                "hosting, data stores; layout = top-level directories and entry points; build_test_run = the "
                "exact build, test and run commands; conventions = code style, naming, patterns, review/CI "
                "rules; risks = fragile areas, missing tests, things a newcomer could break.",
            f"Caps: each \"divisions\" entry at most {max_chars} characters; each \"card\" field at most "
            f"{CARD_CHARS} characters; at most {MAX_NOTES} \"notes\". The CLI cuts anything longer.",
            "", "Answer with ONLY this JSON object (no prose, no code fence):",
            '{"card": {"what": "...", "stack": "...", "layout": "...", "build_test_run": "...", '
            '"conventions": "...", "risks": "..."}, "divisions": {' + example + '}, "notes": ["..."]}', ""]
    head_text, tail_text = "\n".join(lines) + "\n\n", "\n".join(tail)
    budget = REQUEST_BYTES - len((head_text + "\n" + tail_text).encode("utf-8"))
    facts = repo_facts(root)
    over = len(facts.encode("utf-8")) - budget
    if over > 0:
        facts = repo_facts(root, tree_bytes=max(200, TREE_BYTES - over))
    if len(facts.encode("utf-8")) > budget:     # many long custom charters: cut the facts at a line
        kept = _fit(facts.splitlines(), 0, max(0, budget - 40))
        facts = "\n".join(kept + ["… (facts cut to fit the request)"])
    return head_text + facts + "\n" + tail_text


def plan(reg, name, cfg, flags, started_ns="<started_ns>") -> Plan:
    """Everything a run needs, without side effects (what `--dry-run` prints)."""
    key, proj, root = resolve(reg, name)
    head = dispatch.git_head(root, timeout=10)
    if not head:
        raise IntakeError(f"{root} is not a git checkout with a commit")
    s = settings(cfg)
    divs = divisions(proj)
    rid = procs.run_id(f"intake-{key}", started_ns)
    today = _dt.date.today().isoformat()
    return Plan(key, root, head, divs, build_argv(cfg, flags, rid),
                request(key, root, head, divs, int(s.get("max_chars") or 2000), today),
                int(s.get("timeout_s") or 900), s.get("model") or "claude-sonnet-5-5", rid)


@contextlib.contextmanager
def lock(key):
    """One intake per project: a non-blocking flock on ORCHE_HOME/intake/<key>.lock (released when
    this process exits, however it exits). IntakeError(EXIT_BLOCKED) when another holds it."""
    path = paths.orche_home() / "intake" / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            pid = fh.read().strip() or "?"
            raise IntakeError(f"an intake of {key} is already running (pid {pid})", EXIT_BLOCKED) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield path
    finally:
        fh.close()


# ---- result ----------------------------------------------------------------

_MARKERS = re.compile(r"<!--\s*orche-intake:(?:begin|end)\b[^>]*-->|<!--\s*orche-intake:(?:begin|end)")


def _clean(value) -> str:
    """A masked string of the answer, without the card block's own markers (one inside `risks`
    would end the block early on the next intake)."""
    return mask.redact(_MARKERS.sub("", value).strip())[0] if isinstance(value, str) else ""


def parse_result(text) -> dict:
    """The worker's JSON (a code fence or prose around it tolerated), every string masked.
    IntakeError when there is no object of the expected shape."""
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise IntakeError("the worker's answer holds no JSON object")
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError as exc:
        raise IntakeError(f"the worker's answer is not valid JSON ({exc.msg})") from None
    if not isinstance(data, dict) or not isinstance(data.get("card"), dict) \
            or not isinstance(data.get("divisions", {}), dict):
        raise IntakeError('the worker\'s JSON lacks the "card" / "divisions" objects')
    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    return {"card": {f: _clean(data["card"].get(f)) for f in CARD_FIELDS},
            "divisions": {str(k): _clean(v) for k, v in (data.get("divisions") or {}).items()},
            "notes": [n for n in (_clean(x) for x in notes[:MAX_NOTES]) if n]}


def cap(text, limit) -> str:
    """`text` cut to at most `limit` characters at a paragraph (else line, else word) boundary,
    ending in "… (truncated)"."""
    text = text.strip()
    if len(text) <= limit:
        return text
    room = max(0, limit - len(TRUNCATED) - 2)
    cut = text[:room]
    for sep in ("\n\n", "\n", " "):
        at = cut.rfind(sep)
        if at >= room // 3:
            cut = cut[:at]
            break
    return cut.rstrip() + "\n\n" + TRUNCATED


def _placeholders() -> dict:
    """Card heading -> the template's placeholder line under it."""
    text = CARD_TEMPLATE.read_text(encoding="utf-8")
    return {h: m.group(1) for h, _ in PLACEHOLDER_FIELDS
            if (m := re.search(rf"^## {re.escape(h)}[ \t]*\n\n?(\(.*\))[ \t]*$", text, re.MULTILINE))}


def merge_card(text, card, sha7, date) -> str:
    """The card with intake's text merged in (see the module docstring); idempotent per result."""
    for heading, field in PLACEHOLDER_FIELDS:
        placeholder, value = _placeholders().get(heading), card.get(field)
        if placeholder and value:
            text = re.sub(rf"(^## {re.escape(heading)}[ \t]*\n\n?){re.escape(placeholder)}[ \t]*$",
                          lambda m: m.group(1) + value, text, count=1, flags=re.MULTILINE)
    parts = [f"### {label}\n\n{card[field]}\n" for label, field in BLOCK_FIELDS if card.get(field)]
    block = f"{BEGIN}\n## Intake ({sha7}, {date})\n\n" + "\n".join(parts) + f"{END}\n"
    if _BLOCK.search(text):
        return _BLOCK.sub(lambda m: block, text, count=1)
    m = re.search(r"^## How to ask\b", text, re.MULTILINE)
    if m:
        return text[:m.start()] + block + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block


def write_files(plan_, result, cfg, date) -> list:
    """CONTEXT.md per division and the merged PROJECT_CARD.md (run() writes the stamp after);
    the paths written."""
    max_chars = int(settings(cfg).get("max_chars") or 2000)
    sha7 = plan_.head[:7]
    written = []
    for name, label, _charter, _custom in plan_.divisions:
        body = result["divisions"].get(name) or "(the intake found nothing specific for this division)"
        path = context_path(plan_.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write(path, f"# Context for {label} — intake at {sha7}, {date}\n\n{cap(body, max_chars)}\n")
        written.append(str(path))
    card_path = plan_.root / "PROJECT_CARD.md"
    try:
        text = card_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = templates.render(CARD_TEMPLATE.read_text(encoding="utf-8"), PROJECT=plan_.key)
    card = {f: cap(v, CARD_CHARS) for f, v in result["card"].items() if v}
    util.atomic_write(card_path, merge_card(text, card, sha7, date))
    written.append(str(card_path))
    return written


# ---- run -------------------------------------------------------------------

def _failure(code, out, err, error, data) -> str:
    if error:
        return error
    if not data:
        tail = ((err or out or "no output").strip().splitlines() or ["no output"])[-1][:300]
        return f"claude exit {code}: {tail}"
    if data.get("is_error"):
        return f"claude reported an error ({data.get('subtype') or 'is_error'})"
    if code:
        return f"claude exited {code}"
    return ""


def run(reg, name, cfg) -> dict:
    """Run the intake of project `name` now: the stamp plus `project`, `files`, `notes`.
    IntakeError (masked message, `code`) on any failure; nothing is written then."""
    key = resolve(reg, name)[0]
    with lock(key), runner.sigterm_guard() as sig:
        flags = runner.claude_flags(cfg)
        p = plan(reg, key, cfg, flags, started_ns=time.time_ns())
        if sig.signum is not None:          # a SIGTERM during the probe or the plan: never start claude
            raise Interrupted("terminated before start")
        started = time.monotonic()
        code, out, err, error = runner.run_claude(p.argv, p.stdin, p.root, runner.worker_env(p.run_id),
                                                  p.timeout)
        wall = time.monotonic() - started
        if sig.killed:                      # SIGTERM while claude ran: its group was killed
            log.log("events", f"intake project={key} interrupted: {error}")
            raise Interrupted(error or "terminated")
        data = runner.parse_output(out)
        try:
            why = _failure(code, out, err, error, data)
            if why:
                raise IntakeError(why)
            result = parse_result(data.get("result"))
        except IntakeError as exc:
            log.log("events", f"intake project={key} failed: {exc}")
            raise
        date = _dt.date.today().isoformat()
        files = write_files(p, result, cfg, date)
        ms = data.get("duration_ms")
        stamp = {"head": p.head, "date": util.now_iso(), "model": p.model,
                 "cost_usd": data.get("total_cost_usd", data.get("cost_usd")),
                 "duration_s": round(ms / 1000, 1) if isinstance(ms, (int, float)) else round(wall, 1),
                 "divisions": [d[0] for d in p.divisions], "turns": data.get("num_turns")}
        util.write_json(stamp_path(p.root), stamp)
        log.log("events", f"intake project={key} head={p.head[:7]} model={p.model} cost={stamp['cost_usd']} "
                          f"turns={stamp['turns']} divisions={len(p.divisions)}")
    return {"project": key, **stamp, "files": files + [str(stamp_path(p.root))], "notes": result["notes"]}


@contextlib.contextmanager
def progress(model, out=None):
    """While the body runs: `intake running… <s> s (<model>)` on stderr (or `out`) every
    PROGRESS_S, from a daemon thread (`orche init` waits on the sweep in the foreground)."""
    done, started = threading.Event(), time.monotonic()

    def tick():
        while not done.wait(PROGRESS_S):
            print(f"intake running… {int(time.monotonic() - started)} s ({model})", file=out or sys.stderr,
                  flush=True)

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=1)


def summary_line(rec) -> str:
    cost = rec.get("cost_usd")
    money = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$?"
    n = len(rec.get("divisions") or [])
    return (f"intake {rec['project']}: {n} division{'' if n == 1 else 's'}, {money}, "
            f"{int(round(rec.get('duration_s') or 0))} s ({rec.get('model')})")


def missing_divisions(names, stamp) -> list:
    """The division `names` the stamp does not cover (all of them without a stamp)."""
    have = set((stamp or {}).get("divisions") or [])
    return [d for d in names if d not in have]


def refresh_all(reg, cfg) -> list:
    """`orche janitor run`: `--if-stale` for every registered project that has a stamp (a project
    that never had an intake, e.g. `--no-intake`, is left alone). [{project, status, line}]."""
    if not settings(cfg).get("enabled", True):
        return []
    out = []
    for key, proj in sorted((reg.get("projects") or {}).items()):
        root = proj.get("path")
        stamp = read_stamp(root) if root and Path(root).is_dir() else None
        if not stamp:
            continue
        info = stale(root, stamp, cfg)
        if not info["stale"]:
            out.append({"project": key, "status": "fresh", "line": f"{key}: {fresh_line(info)}"})
            continue
        try:
            rec = run(reg, key, cfg)
        except IntakeError as exc:
            out.append({"project": key, "status": "failed", "line": f"intake {key} failed: {exc}"})
            continue
        except Exception as exc:  # noqa: BLE001 - one project's intake never fails the janitor
            out.append({"project": key, "status": "failed",
                        "line": f"intake {key} failed: {mask.redact(f'{type(exc).__name__}: {exc}')[0]}"})
            continue
        out.append({"project": key, "status": "ran", "line": summary_line(rec)})
    return out
