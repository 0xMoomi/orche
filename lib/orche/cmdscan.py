"""Split a shell command line into simple commands, for the env-read gate. Best effort, not a shell.

`scan(text)` returns `Command`s in order. A command's `words` are its words with quotes removed (a
quoted string stays ONE word, so `git commit -m "… security …"` is one argument of `git`, never a
command). Commands start at the beginning of the text and after `;`, `&`, `|`, `&&`, `||`, a
newline (`\r\n` and a lone `\r` count as one, see `newlines`), `(` and `)`; the insides of `$( … )` and `` `…` `` are scanned as commands of their own,
also inside double quotes (the shell runs them there too). `$'…'` (ANSI-C quoting) is decoded.

Not words: redirection operators and their targets (`Command.redirects`; `<` targets also in
`Command.inputs`), heredoc bodies (`<<TAG`, `<<-TAG`, `<<'TAG'`) and here-strings (`<<< word`), both
in `Command.stdin`, and comments (`#` at the start of a word, to the end of the line). A command
after a single `|` has `pipe_in`: the command whose output it reads.

Nesting deeper than MAX_DEPTH raises TooDeep: the caller must treat the text as unreadable.

`argv(command)` drops leading keywords, assignments and wrappers (`sudo`, `timeout 5`, `env -i`,
`/usr/bin/time`, …, matched by basename) and returns the real command; a wrapper that runs a
command STRING (`watch`, `script -c`, `flock -c`, `env -S`) comes back as `["sh", "-c", string]`.

Stdlib only and free of new syntax: the gate that imports this also runs on old interpreters.
"""
import codecs
import os
import re

MAX_DEPTH = 8
SEPARATORS = ";&|()\n"
WORD_END = " \t" + SEPARATORS + "<>"
KEYWORDS = ("!", "{", "}", "if", "then", "elif", "else", "do", "while", "until", "coproc")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# wrapper -> (short options taking a value, long options taking a value, positionals before the
# command, the option whose value is a shell command string or None)
WRAPPERS = {
    "sudo": ("ughpCDrtUT", ("--user", "--group", "--host", "--prompt", "--chdir"), 0, None),
    "doas": ("uC", (), 0, None),
    "env": ("uCSP", ("--unset", "--chdir", "--split-string"), 0, "S"),
    "timeout": ("sk", ("--signal", "--kill-after"), 1, None),
    "gtimeout": ("sk", ("--signal", "--kill-after"), 1, None),
    "stdbuf": ("ioe", ("--input", "--output", "--error"), 0, None),
    "nice": ("n", ("--adjustment",), 0, None),
    "nohup": ("", (), 0, None),
    "time": ("fo", ("--format", "--output"), 0, None),
    "command": ("", (), 0, None),
    "builtin": ("", (), 0, None),
    "exec": ("a", (), 0, None),
    "caffeinate": ("tw", (), 0, None),
    "xargs": ("ILnPEsJRSda", ("--max-args", "--max-procs", "--delimiter", "--arg-file", "--replace"), 0, None),
    "watch": ("nd", ("--interval", "--differences"), 0, "*"),
    "script": ("cFTtE", ("--command", "--timing", "--log-in", "--log-out", "--log-io"), 1, "c"),
    "unbuffer": ("", (), 0, None),
    "chrt": ("T", ("--sched-runtime",), 1, None),
    "ionice": ("cnp", ("--class", "--classdata", "--pid"), 0, None),
    "setsid": ("", (), 0, None),
    "flock": ("wEc", ("--timeout", "--conflict-exit-code", "--command"), 1, "c"),
    "busybox": ("", (), 0, None),
    "arch": ("", (), 0, None),
}


class TooDeep(Exception):
    """Nesting beyond MAX_DEPTH: not scanned, so not known to be harmless."""


class Command:
    def __init__(self):
        self.words = []      # the command and its arguments, quotes removed
        self.redirects = []  # redirection targets (`< file`, `> file`)
        self.inputs = []     # input redirection targets (`< file`)
        self.stdin = []      # heredoc bodies and here-strings fed to the command
        self.pipe_in = None  # the Command piped into this one (`a | b`: b.pipe_in is a)

    def text(self) -> str:
        """Arguments and stdin as text: roughly what `echo …`, `printf …` or `cat <<EOF` hands a pipe."""
        return "\n".join([" ".join(self.words[1:])] + self.stdin)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Command({self.words!r}, redirects={self.redirects!r}, stdin={self.stdin!r})"


def _match_close(text, i, close):
    """Index of the `close` char that ends the construct starting at `i` (quotes and nested
    parentheses skipped), or len(text). Iterative: deep nesting cannot overflow the stack."""
    stack, n = [close], len(text)
    while i < n:
        c, want = text[i], stack[-1]
        if c == "\\":
            i += 2
            continue
        if want == "`":
            if c == "`":
                stack.pop()
                if not stack:
                    return i
        elif want == ")":
            if c == "'":
                j = text.find("'", i + 1)
                i = n if j < 0 else j + 1
                continue
            if c == '"':
                stack.append('"')
            elif c == "(":
                stack.append(")")
            elif c == ")":
                stack.pop()
                if not stack:
                    return i
        elif want == '"':
            if c == '"':
                stack.pop()
                if not stack:
                    return i
            elif c == "$" and text[i + 1:i + 2] == "(":
                stack.append(")")
                i += 2
                continue
            elif c == "`":
                stack.append("`")
        i += 1
    return n


def _ansi_c(s) -> str:
    try:
        return codecs.decode(s.encode("latin-1", "backslashreplace"), "unicode_escape")
    except (UnicodeError, ValueError):
        return s


class _Scanner:
    def __init__(self, text, depth):
        self.text, self.depth = text, depth
        self.commands = []
        self.cur = Command()
        self.word = None          # list of chars of the word being read, None between words
        self.target = None        # "redirect", "input" or "stdin": the next word is an operand
        self.heredocs = []        # (tag, strip_tabs, command) waiting for the next newline
        self.pipe = None          # the command a `|` just ended

    # ---- words and commands ----
    def end_word(self):
        if self.word is None:
            return
        word, self.word = "".join(self.word), None
        if self.target in ("redirect", "input"):
            self.cur.redirects.append(word)
            if self.target == "input":
                self.cur.inputs.append(word)
        elif self.target == "stdin":
            self.cur.stdin.append(word)
        else:
            self.cur.words.append(word)
        self.target = None

    def end_command(self, piped=False):
        self.end_word()
        self.target = None
        done = self.cur
        if done.words or done.redirects or done.stdin:
            self.commands.append(done)
        self.cur = Command()
        self.cur.pipe_in = done if piped else None

    def nested(self, inner):
        if self.depth + 1 > MAX_DEPTH:
            raise TooDeep()
        self.commands.extend(scan(inner, self.depth + 1))

    def add(self, s):
        if self.word is None:
            self.word = []
        self.word.append(s)

    # ---- the pieces ----
    def heredoc_bodies(self, i):
        """Consume the heredoc bodies that follow the newline at `i`; returns the next index."""
        text, n = self.text, len(self.text)
        i += 1
        for tag, strip_tabs, owner in self.heredocs:
            body = []
            while i < n:
                j = text.find("\n", i)
                j = n if j < 0 else j
                line = text[i:j]
                i = j + 1
                if (line.lstrip("\t") if strip_tabs else line) == tag:
                    break
                body.append(line)
            owner.stdin.append("\n".join(body))
        self.heredocs = []
        return i

    def heredoc_tag(self, i):
        """Read the tag after `<<` / `<<-` at `i`; returns the next index."""
        text, n = self.text, len(self.text)
        strip_tabs = text[i:i + 1] == "-"
        i += 1 if strip_tabs else 0
        while i < n and text[i] in " \t":
            i += 1
        tag = []
        while i < n and text[i] not in WORD_END:
            c = text[i]
            if c in "'\"":
                j = text.find(c, i + 1)
                j = n if j < 0 else j
                tag.append(text[i + 1:j])
                i = j + 1
                continue
            if c == "\\":
                i += 1
                continue
            tag.append(c)
            i += 1
        self.heredocs.append(("".join(tag), strip_tabs, self.cur))
        return i

    def redirect(self, i):
        """At `<` or `>` (or `&>`): drop an fd number just read, consume the operator; returns the
        next index."""
        text = self.text
        if self.word is not None and "".join(self.word).isdigit():
            self.word = None  # `2>`: the fd, not an argument
        self.end_word()
        if text.startswith("<<<", i):
            self.target = "stdin"
            return i + 3
        if text.startswith("<<", i):
            return self.heredoc_tag(i + 2)
        op = text[i]
        j = i + 1
        while j < len(text) and text[j] in "<>&|":
            j += 1
        self.target = "input" if op == "<" and text[i:j] in ("<", "<&") else "redirect"
        return j

    def run(self):
        text, n, i = self.text, len(self.text), 0
        while i < n:
            c = text[i]
            if c == "\\":
                if text[i + 1:i + 2] == "\n":
                    i += 2
                    continue
                self.add(text[i + 1:i + 2])
                i += 2
            elif c in " \t\r":
                self.end_word()
                i += 1
            elif c == "\n":
                self.end_command()
                i = self.heredoc_bodies(i) if self.heredocs else i + 1
            elif c == "#" and self.word is None:
                j = text.find("\n", i)
                i = n if j < 0 else j
            elif c == "&" and text[i + 1:i + 2] == ">":
                i = self.redirect(i)
            elif c in "<>":
                i = self.redirect(i)
            elif c == "|":
                if text[i + 1:i + 2] == "|":
                    self.end_command()
                    i += 2
                else:
                    self.end_command(piped=True)
                    i += 2 if text[i + 1:i + 2] == "&" else 1
            elif c in SEPARATORS:
                self.end_command()
                i += 1
            elif c == "'":
                j = text.find("'", i + 1)
                j = n if j < 0 else j
                self.add(text[i + 1:j])
                i = j + 1
            elif c == "$" and text[i + 1:i + 2] == "'":
                j = i + 2
                while j < n and text[j] != "'":
                    j += 2 if text[j] == "\\" else 1
                self.add(_ansi_c(text[i + 2:min(j, n)]))
                i = j + 1
            elif c == '"':
                i = self.double_quoted(i + 1)
            elif c == "$" and text[i + 1:i + 2] == "(":
                j = _match_close(text, i + 2, ")")
                self.nested(text[i + 2:j])
                self.add("")
                i = j + 1
            elif c == "`":
                j = _match_close(text, i + 1, "`")
                self.nested(text[i + 1:j])
                self.add("")
                i = j + 1
            else:
                self.add(c)
                i += 1
        self.end_command()
        return self.commands

    def double_quoted(self, i):
        """Read a double-quoted string's text into the word; returns the index after the quote."""
        text, n = self.text, len(self.text)
        self.add("")
        while i < n:
            c = text[i]
            if c == '"':
                return i + 1
            if c == "\\" and text[i + 1:i + 2] in ('"', "\\", "$", "`", "\n"):
                if text[i + 1] != "\n":
                    self.add(text[i + 1])
                i += 2
                continue
            if c == "$" and text[i + 1:i + 2] == "(":
                j = _match_close(text, i + 2, ")")
                self.nested(text[i + 2:j])
                i = j + 1
                continue
            if c == "`":
                j = _match_close(text, i + 1, "`")
                self.nested(text[i + 1:j])
                i = j + 1
                continue
            self.add(c)
            i += 1
        return n


def scan(text, depth=0):
    """The simple commands of `text` (see the module docstring); TooDeep past MAX_DEPTH."""
    if depth > MAX_DEPTH:
        raise TooDeep()
    return _Scanner(newlines(str(text or "")), depth).run()


def newlines(text):
    """`text` with `\\r\\n` and a lone `\\r` read as `\\n`, before any line or token handling: a bare CR
    must not hide what follows it (Python and a heredoc reader both see a new line there)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _skip_options(words, i, short_values, long_values, string_opt):
    """Index after the options at `i`, and the value of `string_opt` if it was given."""
    command = None
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1, command
        if not w.startswith("-") or w == "-":
            break
        if w.startswith("--"):
            name = w.split("=", 1)[0]
            if "=" not in w and name in long_values:
                if string_opt and name in ("--command", "--split-string"):
                    command = words[i + 1] if i + 1 < len(words) else ""
                i += 2
            else:
                if string_opt and name in ("--command", "--split-string") and "=" in w:
                    command = w.split("=", 1)[1]
                i += 1
            continue
        i += 1
        for k, ch in enumerate(w[1:]):
            if ch in short_values:
                rest = w[k + 2:]
                value = rest if rest else (words[i] if i < len(words) else "")
                if not rest:
                    i += 1
                if string_opt and ch == string_opt:
                    command = value
                break
    return i, command


def argv(command):
    """The command's argv without leading keywords, assignments and wrappers (see the docstring)."""
    words = list(command.words)
    i = 0
    while i < len(words):
        w = words[i]
        name = os.path.basename(w)
        if w in KEYWORDS or ASSIGNMENT.match(w):
            i += 1
            continue
        spec = WRAPPERS.get(name)
        if spec is None:
            break
        short_values, long_values, positionals, string_opt = spec
        i, cmd_string = _skip_options(words, i + 1, short_values, long_values, string_opt)
        if cmd_string is not None:
            return ["sh", "-c", cmd_string]
        if name == "env":
            while i < len(words) and ASSIGNMENT.match(words[i]):
                i += 1
        if string_opt == "*":  # watch: the rest is one shell command string
            return ["sh", "-c", " ".join(words[i:])] if i < len(words) else []
        i += positionals
    return words[i:]
