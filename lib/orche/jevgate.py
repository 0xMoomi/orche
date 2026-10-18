"""The env-read gate's Jev-key rules (`hooks/env_read_gate.py` calls `reason(tool, tool_input)`).

The Jev key is entered by the user (dialog or hidden prompt) and read only inside the orche
process: an agent may not read the keychain value, the key file, or orche's key API, nor feed a
key through orche's stdin flags. A guard against accidental reads, not a sandbox.

Bash, in this order:
1. Prefilter: text with none of PREFILTER's words and not the configured ORCHE_HOME path is
   allowed without a scan.
2. Raw text: the ORCHE_HOME-anchored key path (`~/.claude/orche/secrets/jev.key`, `$HOME/…`,
   `${HOME}/…`, `$ORCHE_HOME/secrets/jev.key`, any path ending `/orche/secrets/jev.key`, the
   configured ORCHE_HOME's), normalised (`//`, `/./`, `x/../`, backslashes), anywhere: quoted,
   in a heredoc, in a commit message.
3. Structure (`orche.cmdscan`): only command words count. Wrappers are skipped (`timeout 5`,
   `sudo -u x`, `/usr/bin/time`, …). Refused:
   - `security find-generic-password|find-internet-password` with `-w`/`-g`, `dump-keychain -d`,
     `security -i`;
   - a word, redirect target or (after `cd`/`pushd` into the secrets dir) bare name naming
     `secrets/jev.key` (normalised) or a glob in a secrets dir matching it; the PATTERN argument of
     `rg|grep|egrep|fgrep|ag|ack|git grep` is a pattern, not a path, and is exempt;
   - a read (`cat head tail less more xxd od strings base64 cp mv tee dd open source .`, or an
     input redirect `< path`) of anything under `…/orche/secrets/`; `ls`, `stat`, `test`, `file`
     and `rm` of other files there stay allowed;
   - a search tool whose PATH argument is ORCHE_HOME, its secrets dir or the key file;
   - orche's stdin key flags (`--stdin`, `--key-stdin`, `--jev-key-stdin`, argparse prefixes);
   - inline interpreter code (python, perl, ruby, node, php, osascript, awk: `-c`/`-e`/`-E`/`-r`/
     `-l`/`--eval`/`-p`, awk's program, a heredoc or here-string, or text piped in) matching the
     TEXT rules in `code_denied` (Python `#` comment lines stripped first);
   - shells (`sh -c`, `bash -c`, a heredoc or text piped into a shell, `eval`, `watch`, `script
     -c`, `flock -c`, `env -S`) have their string re-scanned as a command.
4. Fail closed: nesting past the scanner's or this module's depth, a RecursionError or any other
   exception while scanning text that passed the prefilter refuses with TOO_COMPLEX.

Known gaps (best effort): a script file that reads the key (`python3 x.py`); variable
indirection (`s=security; $s …`); brace expansion (`{-w,}`, `{jev,x}.key`); a glob in a directory
component (`secre*/jev.key`); `tar`/`cp -r`/`find -exec` on the directory; flags supplied by
`xargs` from stdin; Read through a symlink; Grep over an ancestor of ORCHE_HOME without a glob.

Stdlib only and free of new syntax (the hook also runs on old interpreters).
"""
import fnmatch
import os
import re

from . import cmdscan

REASON = ("[orche] the Jev key is entered by the user with 'orche jev key set' (dialog or hidden "
          "prompt) and is never read or passed by agents. 'orche jev key status' says where it is.")
TOO_COMPLEX = "[orche] command too complex to check for a key read; simplify it (no nested eval/sh -c)"
# `orche` is in the list because argparse accepts flag prefixes (`--jev-key`, `--st`); `$'` because
# ANSI-C quoting can spell any of the others.
PREFILTER = ("security", "jev.", "secrets/", "secrets\\", "jevkey", "JEV_API_KEY", "--stdin", "-stdin",
             ".key(", "orche", "$'")
KEY_NAME = "jev.key"
KEY_FILE = re.compile(r"(?:^|/)secrets/jev\.key$")
GLOB_CHARS = re.compile(r"[*?\[]")
DEPTH = 4

# `security` subcommands that print a secret only with these flags (`-w` value, `-g` value on stderr,
# `-d` decrypted dump); without them only attributes are printed.
SECURITY_SECRET_FLAGS = {"find-generic-password": "wg", "find-internet-password": "wg", "dump-keychain": "d"}
SECURITY_ARG_OPTS = "acCdDGjlprPst"  # getopt letters of those subcommands that take a value
SECURITY_GLOBAL_ARG_OPTS = "p"
STDIN_FLAGS = (("--jev-key-stdin", ()), ("--key-stdin", ("jev", "setup")), ("--stdin", ("jev", "key", "set")))
SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
PYTHON = re.compile(r"^python[0-9.]*$")
INTERPRETERS = re.compile(r"^(?:python[0-9.]*|perl[0-9.]*|ruby[0-9.]*|node|nodejs|php[0-9.]*|osascript|"
                          r"[gmn]?awk)$")
CODE_OPTS = ("-c", "-e", "-E", "-r", "-l", "-p", "--eval", "--print")
READERS = ("cat", "head", "tail", "less", "more", "xxd", "od", "strings", "base64", "cp", "mv", "tee", "dd",
           "open", "source", ".", "bat", "hexdump", "nl")
SEARCHERS = {"rg": "efgtTmABCjMrE", "grep": "efmABCdD", "egrep": "efmABCdD", "fgrep": "efmABCdD",
             "ag": "GgmABC", "ack": "mABC", "git-grep": "efmABC"}
SEARCH_LONG_VALUES = ("--glob", "--iglob", "--type", "--type-not", "--regexp", "--file", "--max-count",
                      "--context", "--after-context", "--before-context", "--replace", "--max-depth",
                      "--encoding", "--ignore-file")

# Raw-text rules for inline interpreter code (and the anchored key path for any Bash text).
FIND_PASSWORD = re.compile(r"find-(?:generic|internet)-password")
FLAG_WG = re.compile(r"(?:^|[\s'\",\[(])-[A-Za-z]*[wg](?![A-Za-z0-9_-])")
DUMP = re.compile(r"dump-keychain")
FLAG_D = re.compile(r"(?:^|[\s'\",\[(])-[A-Za-z]*d(?![A-Za-z0-9_-])")
SECURITY_I = re.compile(r"\bsecurity\b['\"]?[\s,]*['\"]?-i\b")
KEY_API = re.compile(r"\bjevkey\b|\.key\s*\(\s*\)")
ENV_KEY = re.compile(r"(?:environ|getenv|keychain|\bENV\b|\benv\b)[\s\S]{0,40}JEV_API_KEY|"
                     r"JEV_API_KEY[\s\S]{0,40}(?:environ|getenv|keychain)")
SECRETS_GLOB = re.compile(r"orche/secrets/([^\s'\"/;|&)<>]*)")
REL_KEY = re.compile(r"secrets/jev\.key")


class TooComplex(Exception):
    pass


# ---- paths ------------------------------------------------------------------------------------

def orche_home() -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(os.environ.get("ORCHE_HOME") or "~/.claude/orche")))


def _norm_text(text):
    """Variants of `text` with paths normalised: `//` and `/./` collapsed, `x/../` resolved,
    backslashes removed (shell escapes) or read as separators (Windows-style)."""
    out = []
    for t in (text.replace("\\", ""), text.replace("\\", "/")):
        prev = None
        while prev != t:
            prev = t
            t = re.sub(r"/+", "/", t)
            t = t.replace("/./", "/")
            t = re.sub(r"(?<![^/\s'\"=])(?!\.\.?/)[^/\s'\"]+/\.\./", "", t)
        out.append(t)
    return out


def anchored_key_path(text) -> bool:
    """The ORCHE_HOME-anchored key path anywhere in `text` (see the module docstring)."""
    own = orche_home() + "/secrets/jev.key"
    for t in _norm_text(text):
        if "/orche/secrets/jev.key" in t or own in t:
            return True
        if re.search(r"ORCHE_HOME\}?['\"]?/secrets/jev\.key", t):
            return True
    return False


def _expand(word):
    home = os.path.expanduser("~")
    w = word.replace("${ORCHE_HOME}", orche_home()).replace("$ORCHE_HOME", orche_home())
    w = w.replace("${HOME}", home).replace("$HOME", home)
    if w == "~" or w.startswith("~/"):
        w = home + w[1:]
    return w


def _resolve(word, cwd):
    """`word` as a normalised path, joined to the known `cwd` when relative."""
    w = _expand(word)
    if cwd and not os.path.isabs(w):
        w = os.path.join(cwd, w)
    return os.path.normpath(w) if w else w


def _secrets_dir(path) -> bool:
    return path.rstrip("/").endswith("/orche/secrets") or path == os.path.join(orche_home(), "secrets")


def _is_orche_home(path) -> bool:
    p = path.rstrip("/")
    return p == orche_home() or p.endswith("/.claude/orche")


def key_path(word, in_secrets=False, cwd="") -> bool:
    """A word naming the stored key file: `…/secrets/jev.key` (normalised), a glob in a secrets dir
    that matches it, or (inside the secrets dir) the bare name. A bare `jev.key` elsewhere is a word."""
    if not word or any(ch.isspace() for ch in word):
        return False
    path = os.path.normpath(_expand(word))
    if KEY_FILE.search(path):
        return True
    head, base = os.path.split(path)
    if head:
        in_dir = os.path.basename(head) == "secrets"
    else:
        in_dir = in_secrets
    if not in_dir:
        return False
    return base == KEY_NAME or bool(GLOB_CHARS.search(base) and fnmatch.fnmatchcase(KEY_NAME, base))


def under_secrets(word, cwd="") -> bool:
    """A word naming anything inside ORCHE_HOME's secrets dir (`…/orche/secrets/…`)."""
    if not word or any(ch.isspace() for ch in word):
        return False
    w = word[3:] if word.startswith("if=") else word
    path = _resolve(w, cwd)
    if not path:
        return False
    head = os.path.dirname(path)
    return _secrets_dir(head) or "/orche/secrets/" in path + "/" and not _secrets_dir(path)


# ---- inline interpreter code ------------------------------------------------------------------

def code_denied(code, python=False) -> bool:
    """TEXT rules for interpreter code: the key path, `security` value lookups, orche's key API,
    the key's env var next to an env or keychain lookup."""
    if python:
        code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    for t in _norm_text(code):
        if REL_KEY.search(t) or ("secrets" in t and KEY_NAME in t):
            return True
        for m in SECRETS_GLOB.finditer(t):
            if GLOB_CHARS.search(m.group(1)) and fnmatch.fnmatchcase(KEY_NAME, m.group(1)):
                return True
    if FIND_PASSWORD.search(code) and FLAG_WG.search(code):
        return True
    if DUMP.search(code) and FLAG_D.search(code):
        return True
    return bool(SECURITY_I.search(code) or KEY_API.search(code) or ENV_KEY.search(code))


def _interpreter_code(name, args, cmd):
    code = [args[i + 1] for i, a in enumerate(args[:-1]) if a in CODE_OPTS]
    code += [a.split("=", 1)[1] for a in args if a.startswith("--eval=")]
    if name.endswith("awk"):
        rest = [a for a in args[1:] if not a.startswith("-")]
        code += rest[:1]
    code += cmd.stdin
    if cmd.pipe_in is not None:
        code.append(cmd.pipe_in.text())
    return code


# ---- commands ---------------------------------------------------------------------------------

def _security_reads(args) -> bool:
    """`security …` args that print a secret (see SECURITY_SECRET_FLAGS), or interactive mode."""
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        opts = args[i][1:]
        if "i" in opts:  # `security -i` reads commands from stdin: not reviewable here
            return True
        i += 2 if opts[-1:] in SECURITY_GLOBAL_ARG_OPTS else 1
    if i >= len(args):
        return False
    flags = SECURITY_SECRET_FLAGS.get(args[i])
    if not flags:
        return False
    rest = args[i + 1:]
    j = 0
    while j < len(rest):
        arg = rest[j]
        j += 1
        if arg == "--":
            break
        if not arg.startswith("-") or arg == "-":
            continue
        for k, ch in enumerate(arg[1:]):
            if ch in flags:
                return True
            if ch in SECURITY_ARG_OPTS:
                if k == len(arg) - 2:
                    j += 1  # the value is the next word
                break
    return False


def _orche_args(args):
    """The orche CLI arguments of argv (`orche …`, `…/orche.py …`, `python3 …/orche …`), or None."""
    if not args:
        return None
    name = os.path.basename(args[0])
    if name in ("orche", "orche.py"):
        return args[1:]
    if PYTHON.match(name):
        rest = args[1:]
        for i, arg in enumerate(rest):
            if arg == "-m":
                return rest[i + 2:] if rest[i + 1:i + 2] == ["orche"] else None
            if arg.startswith("-"):
                continue
            return rest[i + 1:] if os.path.basename(arg) in ("orche", "orche.py") else None
    return None


def _feeds_key(args) -> bool:
    words = [a for a in args if not a.startswith("-")]
    opts = [a.split("=", 1)[0] for a in args if a.startswith("--") and len(a.split("=", 1)[0]) > 2]
    for flag, needs in STDIN_FLAGS:
        if all(w in words for w in needs) and any(flag.startswith(o) for o in opts):
            return True
    return False


def _search_parts(tool, args):
    """(pattern words, path args, glob values) of a search tool's argv tail: the `-e`/`--regexp`
    values, else the first positional, are patterns."""
    values = SEARCHERS[tool]
    explicit, positional, globs, patterns = False, [], [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional += list(range(i + 1, len(args)))
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            value = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if name in ("--regexp", "--file"):
                explicit = True
                if name == "--regexp":
                    patterns.append(value)
            if name in ("--glob", "--iglob", "--include"):
                globs.append(value)
            i += 2 if ("=" not in a and name in SEARCH_LONG_VALUES) else 1
            continue
        if a.startswith("-") and a != "-":
            i += 1
            for k, ch in enumerate(a[1:]):
                if ch in values:
                    rest = a[k + 2:]
                    value = rest if rest else (args[i] if i < len(args) else "")
                    if not rest:
                        i += 1
                    if ch in "ef":
                        explicit = True
                    if ch == "e":
                        patterns.append(value)
                    if ch == "g" and tool == "rg":
                        globs.append(value)
                    break
            continue
        positional.append(i)
        i += 1
    first = None if explicit or not positional else positional[0]
    if first is not None:
        patterns.append(args[first])
    paths = [args[j] for j in positional if j != first]
    return patterns, paths, globs


def _search_denied(tool, args, cwd):
    _patterns, paths, globs = _search_parts(tool, args)
    for p in paths:
        path = _resolve(p, cwd)
        if _is_orche_home(path) or _secrets_dir(path) or under_secrets(p, cwd) or KEY_FILE.search(path):
            return True
        if any(fnmatch.fnmatchcase(KEY_NAME, os.path.basename(g)) for g in globs) and \
                (orche_home() + "/").startswith(path.rstrip("/") + "/"):
            return True
    return False


def _commands_denied(text, depth):
    if depth > DEPTH:
        raise TooComplex()
    cwd = ""
    for cmd in cmdscan.scan(text):
        args = cmdscan.argv(cmd)
        name = os.path.basename(args[0]) if args else ""
        if name in ("cd", "pushd"):
            target = next((a for a in args[1:] if not a.startswith("-")), "~")
            cwd = _resolve(target, cwd)
        in_secrets = bool(cwd) and os.path.basename(cwd.rstrip("/")) == "secrets"
        search = "git-grep" if name == "git" and args[1:2] == ["grep"] else name
        tail = args[2:] if search == "git-grep" else args[1:]
        words = list(cmd.words)
        if search in SEARCHERS:
            if _search_denied(search, tail, cwd):
                return True
            for pattern in _search_parts(search, tail)[0]:  # a search pattern, not a path
                if pattern in words:
                    words.remove(pattern)
        if any(key_path(w, in_secrets) for w in words + cmd.redirects):
            return True
        strict_cwd = cwd if cwd and _secrets_dir(cwd) else ""
        if any(under_secrets(w, strict_cwd) for w in cmd.inputs):
            return True
        if name in READERS and any(under_secrets(w, strict_cwd) for w in args[1:]):
            return True
        if name == "security" and _security_reads(args[1:]):
            return True
        orche_args = _orche_args(args)
        if orche_args is not None and _feeds_key(orche_args):
            return True
        if INTERPRETERS.match(name) and any(code_denied(c, python=bool(PYTHON.match(name)))
                                            for c in _interpreter_code(name, args, cmd)):
            return True
        inner = []
        if name in SHELLS:
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
    if not any(word in command for word in PREFILTER) and orche_home() not in command:
        return None
    if anchored_key_path(command):
        return REASON
    try:
        return REASON if _commands_denied(command, 0) else None
    except Exception:  # noqa: BLE001 - TooComplex, cmdscan.TooDeep, RecursionError, anything: fail closed
        return TOO_COMPLEX


def _grep_denied(tool_input) -> bool:
    """Grep over the key file, ORCHE_HOME (no glob, or a glob matching jev.key), its secrets dir,
    or an ancestor of ORCHE_HOME with a glob matching jev.key."""
    raw = str(tool_input.get("path") or "")
    if not raw:
        return False
    path = os.path.normpath(os.path.abspath(_expand(raw)))
    if KEY_FILE.search(path) or _secrets_dir(path) or under_secrets(path):
        return True
    glob = os.path.basename(str(tool_input.get("glob") or ""))
    matches = bool(glob) and fnmatch.fnmatchcase(KEY_NAME, glob)
    if _is_orche_home(path):
        return not glob or matches
    return matches and (orche_home() + "/").startswith(path.rstrip("/") + "/")


def reason(tool, tool_input):
    """REASON / TOO_COMPLEX when the call would read the stored Jev key or hand one to orche; None."""
    if tool in ("Read", "Edit", "Write"):
        raw = str(tool_input.get("file_path") or "")
        path = os.path.normpath(_expand(raw)) if raw else ""
        return REASON if path and (KEY_FILE.search(path) or under_secrets(path)) else None
    if tool == "Grep":
        return REASON if _grep_denied(tool_input) else None
    if tool == "Bash":
        return bash_reason(str(tool_input.get("command") or ""))
    return None
