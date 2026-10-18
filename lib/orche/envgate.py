"""The env-read gate's secret-file and environment rules (`hooks/env_read_gate.py` calls
`reason(tool, tool_input)` after the Jev rules in `jevgate`).

Secrets are read by `orche secret run`, not by agents. Like `jevgate`, these rules look at what a
call DOES, never at text that merely mentions secrets: a Markdown file about `.env`, a commit
message "fix .env loading" or `rg SECRET lib/` all pass.

Secret paths (`secret_path`), case-insensitive on the file name:
- `.env` and `.env.*`, except the names-only files `.env.example`, `.env.sample`, `.env.template`;
- `*.pem`, `*.key`, `*.p12`;
- a file inside a `secrets/` or `.secrets/` directory, unless it is source code or prose
  (CODE_SUFFIXES: `lib/orche/secrets/store.py` is code, `secrets/prod.json` is a secret);
- `/proc/<pid>/environ`;
- a glob that targets them: it matches a secret name but not an ordinary one (`.env*`, `*.pem`,
  `secrets/*`, `/proc/*/environ` are refused; `*` and `*.ts` are not).

Read, Edit, Write: by `file_path` only, never by content. Grep: its `path` is a secret path, its
`glob` targets secret names, or its `path` is a secrets directory searched without a glob or type
limiting it to code. Glob lists names only and is not gated.

Bash: only command words count (`orche.cmdscan`; wrappers like `timeout 5`, `sudo -u x` are
skipped). Refused:
- a reader (READERS: `cat`, `head`, `less`, `sed`, `source`, `.`, `dd if=…`, `diff`, …) with a
  secret path argument, or any command whose input redirect (`< path`) is one;
- a search tool (`rg grep egrep fgrep ag ack`, `git grep`) whose PATH argument is a secret path,
  whose `-g/--glob/--include` targets secret names, or whose path is a secrets directory without
  a code-only glob; its PATTERN argument is a pattern, not a path;
- environment dumps: `printenv` (with or without a name), `env` that runs no command (`env`,
  `env | sort`, `env -0`, `env -u X`, `env > f`), `export`/`declare`/`typeset` with options only
  (`export -p`), `set` alone, `launchctl getenv|export`, `gcloud secrets versions access`;
- `echo`/`printf`/`print` with an argument expanding a secret-shaped variable: `$NAME` or
  `${NAME…}` where NAME is upper case (`[A-Z0-9_]`) and contains KEY, TOKEN, SECRET, PASSWORD,
  PASSWD, CREDENTIAL, PRIVATE, MNEMONIC or SEED (`$OPENAI_API_KEY`, `${GITHUB_TOKEN}`; not `$key`,
  `$HOME`, `$secret_sauce`). Passing such a variable to another program (`curl -H "…$TOKEN"`) is
  allowed: the value goes to the program, not to the transcript;
- inline interpreter code (python, perl, ruby, node, php, osascript, awk; `-c`/`-e`/…, heredoc,
  here-string, piped text) matching the TEXT rules in `code_denied` (Python comments stripped
  first): an environment lookup next to a secret-shaped name (`os.environ['OPENAI_API_KEY']`,
  `process.env.API_KEY`), a whole-environment read (`print(os.environ)`, `console.log(process.env)`,
  `p ENV`, `%ENV`, `$_ENV`, `getenv()`, awk's `ENVIRON`), a string literal naming a secret path
  (`open('.env')`), or a multi-word string literal that is itself a refused command
  (`os.system('cat .env')`, `execSync('printenv')`);
- shells (`sh -c`, `bash -c`, a heredoc or text piped into a shell, `eval`, `watch`, `script -c`,
  `flock -c`, `env -S`) have their string re-scanned as a command.
Fail closed: nesting past the depth limit or any exception while scanning text that passed the
prefilter refuses with TOO_COMPLEX.

Known gaps (best effort, not a sandbox): a script file that reads a secret (`python3 x.py`);
programs that load `.env` themselves (`node --env-file=.env`, `dotenv`); `git show :.env`;
variable indirection; `cat *` in a directory holding `key.pem`; `subprocess.run(['env'])` in
inline code (single-word literals other than secret paths and `printenv` are not re-scanned).

Stdlib only and free of new syntax (the hook also runs on old interpreters).
"""
import fnmatch
import os
import re

from . import cmdscan, jevgate

REASON = "[orche] secrets are read by 'orche secret run', not by agents. Use 'orche secret ls' for names."
TOO_COMPLEX = "[orche] command too complex to check for a secret read; simplify it (no nested eval/sh -c)"
DEPTH = 4

ENV_FILE = re.compile(r"^\.env(?:\..+)?$")
NAMES_ONLY = (".env.example", ".env.sample", ".env.template")
KEY_SUFFIXES = (".pem", ".key", ".p12")
SECRETS_DIRS = ("secrets", ".secrets")
CODE_SUFFIXES = (".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java",
                 ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".cs", ".php", ".sh", ".md", ".rst")
CODE_TYPES = ("py", "python", "js", "ts", "go", "rust", "ruby", "java", "kotlin", "swift", "c", "cpp", "cs",
              "php", "sh", "md", "markdown", "rst")  # ripgrep `--type` names (the Grep tool's `type`)
PROC_ENVIRON = re.compile(r"^/proc/[^/]+/environ$")
GLOB_CHARS = re.compile(r"[*?\[]")
# A glob "targets" secrets when it matches one of SECRET_SAMPLES and none of PLAIN_SAMPLES.
SECRET_SAMPLES = (".env", ".env.local", ".env.production", "id.pem", "id.key", "id.p12")
PLAIN_SAMPLES = ("x", "x.ts", "x.md", "x.json")

READERS = ("cat", "head", "tail", "less", "more", "bat", "nl", "xxd", "od", "hexdump", "strings", "base64",
           "dd", "source", ".", "sed", "awk", "gawk", "mawk", "nawk", "diff", "sort", "uniq", "cut", "tac",
           "rev", "jq", "yq", "column", "fold", "paste", "vim", "vi", "view", "nano", "emacs")
SEARCH_GLOB_OPTS = ("-g", "--glob", "--iglob", "--include")
ENV_LISTERS = ("export", "declare", "typeset")

SECRET_WORD = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|MNEMONIC|SEED)"
SECRET_NAME = r"[A-Z0-9_]*" + SECRET_WORD + r"[A-Z0-9_]*"
SECRET_EXPANSION = re.compile(r"\$\{?[A-Z0-9_]*" + SECRET_WORD + r"[A-Z0-9_]*(?![A-Za-z0-9_])")
# Text with none of these (lower-cased) and no secret-shaped upper-case word is allowed unscanned.
# Glob characters because `.e*` spells `.env`; `$'` and backslashes because quoting can spell anything.
PREFILTER = ("env", "set", "export", "declare", ".pem", ".key", ".p12", "secret", "launchctl", "gcloud",
             "$'", "\\", "*", "?", "[")
PREFILTER_NAME = re.compile(SECRET_WORD)

# ---- inline interpreter code (TEXT rules) ------------------------------------------------------
ENV_LOOKUP_SECRET = re.compile(r"(?:environ|getenv|Getenv|process\.env|\bENV\b|%ENV|\$_ENV|\$_SERVER|ENVIRON)"
                               r"[\s\S]{0,40}?(?<![A-Za-z0-9_])" + SECRET_NAME)
# Whole-environment reads; (pattern, prefix that makes the match a hand-over, not a read).
ENV_DUMPS = {
    "python": (re.compile(r"\benviron\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b|\.copy\b|\.keys\b|"
                          r"\.update\b|\.__contains__\b))"), re.compile(r"(?:\benv\s*=|\bimport)\s*(?:os\.)?$")),
    "node": (re.compile(r"\bprocess\.env\b(?!\s*(?:\.|\[|\?\.))"), re.compile(r"\benv\s*[:=]\s*$")),
    "ruby": (re.compile(r"\bENV\b(?!\s*(?:\[|\.fetch\b|\.key\?|\.has_key\?|\.include\?))"), None),
    "perl": (re.compile(r"%ENV\b"), None),
    "php": (re.compile(r"\bgetenv\s*\(\s*\)|\$_ENV\b(?!\s*\[)|\$_SERVER\b(?!\s*\[)|\bphpinfo\b"), None),
    "awk": (re.compile(r"\bENVIRON\b(?!\s*\[)"), None),
}
STRING_LITERAL = re.compile(r"'([^'\n]{1,300})'|\"([^\"\n]{1,300})\"|`([^`\n]{1,300})`")


class TooComplex(Exception):
    pass


# ---- paths --------------------------------------------------------------------------------------

def _parts(word):
    """(directory parts, lower-case base name) of a path word, or None for a non-path."""
    if not word or any(ch.isspace() for ch in word):
        return None
    path = os.path.normpath(word.replace("\\", "/")).replace("\\", "/")
    parts = path.split("/")
    return [p.lower() for p in parts[:-1]], parts[-1].lower(), path


def _secret_name(base) -> bool:
    if ENV_FILE.match(base):
        return base not in NAMES_ONLY
    return base.endswith(KEY_SUFFIXES)


def _targets(pattern, secret, plain) -> bool:
    """A glob that matches a secret sample and no plain one."""
    return any(fnmatch.fnmatchcase(s, pattern) for s in secret) and \
        not any(fnmatch.fnmatchcase(s, pattern) for s in plain)


def secret_path(word) -> bool:
    """A word naming a secret file, or a glob targeting secret files (see the module docstring)."""
    parsed = _parts(word)
    if parsed is None:
        return False
    dirs, base, path = parsed
    if PROC_ENVIRON.match(path) or (path.startswith("/proc/") and GLOB_CHARS.search(path)
                                     and fnmatch.fnmatchcase("/proc/1/environ", path)):
        return True
    in_secrets = any(d in SECRETS_DIRS for d in dirs)
    if GLOB_CHARS.search(base):
        if _targets(base, SECRET_SAMPLES, PLAIN_SAMPLES):
            return True
        return in_secrets and _targets(base, ("x", "x.json", "x.txt", "x.yaml"), ("x.py", "x.ts", "x.md"))
    if _secret_name(base):
        return True
    return in_secrets and base not in ("", ".", "..") and not base.endswith(CODE_SUFFIXES)


def secrets_dir(word) -> bool:
    """A word naming a `secrets/` (or `.secrets/`) directory itself."""
    parsed = _parts(word.rstrip("/") if word else word)
    return parsed is not None and parsed[1] in SECRETS_DIRS


def _code_only(globs) -> bool:
    """Globs that limit a search to source code (`*.py`), so a secrets directory's data is skipped."""
    globs = [os.path.basename(g).lower() for g in globs if g]
    return bool(globs) and all(g.endswith(CODE_SUFFIXES) and not GLOB_CHARS.search(g.rsplit(".", 1)[-1])
                               for g in globs)


def glob_targets(glob) -> bool:
    """A search glob (`.env*`, `**/.env.local`, `*.pem`) that targets secret names."""
    base = os.path.basename(str(glob or "")).lower()
    return bool(base) and (_targets(base, SECRET_SAMPLES, PLAIN_SAMPLES) or (
        not GLOB_CHARS.search(base) and _secret_name(base)))


# ---- inline interpreter code --------------------------------------------------------------------

def _family(name):
    if jevgate.PYTHON.match(name):
        return "python"
    for family in ("node", "ruby", "perl", "php", "awk"):
        if family in name:
            return family
    return ""


def code_denied(code, family, depth=0) -> bool:
    """TEXT rules for interpreter code (see the module docstring)."""
    if family == "python":
        code = jevgate.strip_python_comments(code)
    if ENV_LOOKUP_SECRET.search(code):
        return True
    dump = ENV_DUMPS.get(family)
    if dump:
        pattern, handover = dump
        for m in pattern.finditer(code):
            if handover is None or not handover.search(code[max(0, m.start() - 40):m.start()]):
                return True
    for m in STRING_LITERAL.finditer(code):
        literal = next(g for g in m.groups() if g is not None)
        if secret_path(literal) or literal.strip() == "printenv":
            return True
        if " " in literal.strip() and _commands_denied(literal, depth + 1):
            return True
    return False


# ---- commands -----------------------------------------------------------------------------------

def _env_dump(cmd, args, name) -> bool:
    if not args:  # wrappers only: `env`, `env -0`, `timeout 5 env -u X` print the environment
        return any(os.path.basename(w) == "env" for w in cmd.words)
    rest = args[1:]
    if name == "printenv":
        return True
    if name in ENV_LISTERS:
        return all(a.startswith("-") for a in rest)
    if name == "set":
        return not rest
    if name == "launchctl":
        return rest[:1] in (["getenv"], ["export"])
    if name == "gcloud":
        return "secrets versions access" in " ".join(a for a in rest if not a.startswith("-"))
    return False


def _search_denied(search, tail) -> bool:
    _patterns, paths, globs = jevgate._search_parts(search, tail)
    for i, a in enumerate(tail):  # `-g` values `_search_parts` keeps only for rg
        if a in SEARCH_GLOB_OPTS and i + 1 < len(tail):
            globs.append(tail[i + 1])
        elif a.startswith(("--glob=", "--iglob=", "--include=")):
            globs.append(a.split("=", 1)[1])
    if any(glob_targets(g) for g in globs):
        return True
    return any(secret_path(p) or (secrets_dir(p) and not _code_only(globs)) for p in paths)


def _commands_denied(text, depth):
    if depth > DEPTH:
        raise TooComplex()
    for cmd in cmdscan.scan(text):
        if any(secret_path(w) for w in cmd.inputs):
            return True
        args = cmdscan.argv(cmd)
        name = os.path.basename(args[0]).lower() if args else ""
        if _env_dump(cmd, args, name):
            return True
        search = "git-grep" if name == "git" and args[1:2] == ["grep"] else name
        if search in jevgate.SEARCHERS:
            if _search_denied(search, args[2:] if search == "git-grep" else args[1:]):
                return True
        elif name in READERS and any(secret_path(a[3:] if a.startswith("if=") else a) for a in args[1:]):
            return True
        if name in ("echo", "printf", "print") and any(SECRET_EXPANSION.search(a) for a in args[1:]):
            return True
        if jevgate.INTERPRETERS.match(name) and any(code_denied(c, _family(name), depth)
                                                    for c in jevgate._interpreter_code(name, args, cmd)):
            return True
        inner = []
        if name in jevgate.SHELLS:
            inner = [args[i + 1] for i, a in enumerate(args[:-1])
                     if a.startswith("-") and not a.startswith("--") and "c" in a]
            inner += cmd.stdin
            if cmd.pipe_in is not None and not inner:
                inner.append(cmd.pipe_in.text())
        elif name == "eval":
            inner = [" ".join(args[1:])]
        for c in inner:
            if _commands_denied(c, depth + 1):
                return True
    return False


def bash_reason(command):
    lowered = command.lower()
    if not any(word in lowered for word in PREFILTER) and not PREFILTER_NAME.search(command):
        return None
    try:
        return REASON if _commands_denied(command, 0) else None
    except Exception:  # noqa: BLE001 - TooComplex, cmdscan.TooDeep, RecursionError, anything: fail closed
        return TOO_COMPLEX


def _grep_denied(tool_input) -> bool:
    path = str(tool_input.get("path") or "")
    glob = str(tool_input.get("glob") or "")
    if secret_path(path) or glob_targets(glob):
        return True
    if path and secrets_dir(path):
        return not (_code_only([glob]) or str(tool_input.get("type") or "") in CODE_TYPES)
    return False


def reason(tool, tool_input):
    """REASON / TOO_COMPLEX when the call would read a secret file or the environment; None."""
    if tool in ("Read", "Edit", "Write"):
        return REASON if secret_path(str(tool_input.get("file_path") or "")) else None
    if tool == "Grep":
        return REASON if _grep_denied(tool_input) else None
    if tool == "Bash":
        return bash_reason(str(tool_input.get("command") or ""))
    return None
