"""Jev onboarding, shared by `orche install` and `orche jev setup|probe|key`.

Flow (`setup`): key from the macOS dialog or the hidden terminal prompt (Enter, Cancel, EOF or
Ctrl-C skips; a pipe is never read without an explicit stdin flag) -> key type (gateway default, or
direct) -> ONE probe with the key in memory (`jev.probe`: one Choice question) -> on success only:
key stored (`jevkey`), then `jev.base_url` / `jev.model` / `jev.enabled` true written. Any failure
leaves the stored key and config.json as they were. ORCHE works without Jev: triage falls back to
the subscription's Haiku.

The last probe is kept in ORCHE_HOME/jev-probe.json (`{ok, ms, kind, error, at}`, error masked) so
`orche doctor` reports it without a network call (WARN at worst). No key value is ever printed or
stored outside `jevkey`.
"""
import datetime as _dt
import getpass
import os
import shutil
import sys
import tempfile

from . import config, jev, jevkey, paths, shell, util

KEY_PROMPT = "Jev API key (optional, Enter to skip): "
KIND_PROMPT = "Key type: 1) Vercel AI Gateway (default)  2) TypeSafe direct: "
SKIP_LINE = ("Jev skipped — ORCHE works without it (triage uses the subscription's Haiku); "
             "add later with `orche jev setup`")
OFF_LINE = "Jev stays off — ORCHE works without it (triage uses the subscription's Haiku)"
NOT_CONFIGURED = "not configured (Haiku fallback)"
KIND_ANSWERS = {"": "gateway", "1": "gateway", "gateway": "gateway", "2": "direct", "direct": "direct"}


NO_TERMINAL = "no terminal: use the dialog, or --stdin from a trusted script"


def _isatty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (ValueError, OSError):  # a closed stream
        return False


def interactive() -> bool:
    """Seam for tests: may we prompt? (stdin and stdout are terminals)."""
    return _isatty(sys.stdin) and _isatty(sys.stdout)


def terminal() -> bool:
    """Seam for tests: may the hidden key prompt be used? (stdin is a terminal). A key is never
    read from a pipe unless the caller passed an explicit stdin flag (`stdin_line`)."""
    return _isatty(sys.stdin)


class NoTerminal(Exception):
    """No dialog and no terminal: the key cannot be asked for (a pipe is never read implicitly)."""


# macOS: the key is typed into a native hidden-answer dialog and read from osascript's stdout inside
# this process, so it never passes through a terminal a Claude session might be reading.
OSASCRIPT = "osascript"  # seam for tests (resolved on PATH)
DIALOG_TIMEOUT_S = 120
DIALOG_TEXT = "Jev API key (stored in your keychain; ORCHE never prints it)"
DIALOG_SCRIPT = (f'display dialog "{DIALOG_TEXT}" default answer "" with hidden answer with title "ORCHE" '
                 'buttons {"Cancel","Save"} default button "Save"')
CANCELED = "-128"  # osascript: "User canceled. (-128)"


class NoDialog(Exception):
    """No GUI session for the dialog: the caller falls back to the terminal prompt."""


def dialog_available() -> bool:
    return (jevkey.PLATFORM == "darwin" and shutil.which(OSASCRIPT) is not None
            and not os.environ.get("SSH_CONNECTION"))


def dialog_key() -> str:
    """The key typed into the dialog, "" on Cancel/empty; NoDialog when there is no GUI session."""
    try:
        res = shell.run([OSASCRIPT, "-e", DIALOG_SCRIPT], timeout=DIALOG_TIMEOUT_S)
    except OSError:
        raise NoDialog() from None
    if not res.ok:
        if CANCELED in (res.err or ""):
            return ""
        raise NoDialog()  # "not allowed", no window server, timeout: the terminal prompt takes over
    for line in (res.out or "").splitlines():
        _head, sep, text = line.partition("text returned:")
        if sep:
            return text.strip()
    return ""


def read_key(prompt, dialog=True) -> str:
    """The key from the macOS dialog (default) or, without a GUI session / with `dialog=False`,
    the hidden terminal prompt. "" means no key (Cancel, Enter, EOF or Ctrl-C); NoTerminal when
    neither the dialog nor a terminal is there. Never reads a pipe: scripts use the stdin flags."""
    try:
        if dialog and dialog_available():
            try:
                return dialog_key()
            except NoDialog:
                pass
        return read_secret(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        return ""


def read_secret(prompt) -> str:
    """Hidden input on a terminal; NoTerminal otherwise (EOF / Ctrl-C propagate to `read_key`)."""
    if not terminal():
        raise NoTerminal(NO_TERMINAL)
    return getpass.getpass(prompt)


def read_line(prompt):
    """One answer line from the terminal; None on EOF or Ctrl-C."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        line = ""
    if not line.endswith("\n"):
        sys.stdout.write("\n")
    return line.strip() if line else None


def stdin_line() -> str:
    """The key from stdin for the explicit `--stdin` / `--key-stdin` / `--jev-key-stdin` flags;
    "" on EOF, a closed stdin or Ctrl-C."""
    try:
        return (sys.stdin.readline() if sys.stdin is not None else "").strip()
    except (ValueError, OSError, KeyboardInterrupt):
        return ""


def ask_kind():
    """"gateway" or "direct"; None on EOF or Ctrl-C (the caller skips Jev)."""
    for _ in range(3):
        answer = read_line(KIND_PROMPT)
        if answer is None:
            return None
        kind = KIND_ANSWERS.get(answer.lower())
        if kind:
            return kind
    return "gateway"


# ---- config and the probe record --------------------------------------------

def _raw_config() -> dict:
    raw = util.read_json(config.path(), default={}) or {}
    if not isinstance(raw.get("jev"), dict):
        raw["jev"] = {}
    return raw


def update_config(**fields) -> None:
    """Set `jev.<field>` values in config.json, keeping everything else."""
    raw = _raw_config()
    raw["jev"].update(fields)
    config.save(raw)


def record_path():
    return paths.orche_home() / "jev-probe.json"


def save_record(result) -> None:
    util.write_json(record_path(), {**result, "at": _dt.datetime.now(_dt.timezone.utc).isoformat()})


def load_record():
    try:
        rec = util.read_json(record_path(), default=None)
    except (util.JsonError, OSError):
        return None
    return rec if isinstance(rec, dict) else None


def forget_record() -> None:
    try:
        record_path().unlink()
    except FileNotFoundError:
        pass


def kind_of(base_url) -> str:
    return "gateway" if jev.WIRE["gateway_marker"] in (base_url or "") else "direct"


# ---- steps -------------------------------------------------------------------

def on_line(result) -> str:
    return f"Jev on ({result['kind']}, {result['ms'] / 1000:.1f} s)"


def unreadable_line(reason) -> str:
    return (f"config.json unreadable ({reason}): Jev skipped, nothing changed; fix the file and run "
            "`orche jev setup`")


def config_problem():
    """None when config.json can be read and rewritten; else the one-line reason (bad JSON,
    not UTF-8, unreadable). Checked before any question, probe or store."""
    try:
        _raw_config()
    except util.JsonError as exc:
        return exc.reason
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return None


def _snapshot():
    """config.json's bytes (None when absent), restored byte-for-byte by `_restore` on a failure."""
    try:
        return config.path().read_bytes()
    except FileNotFoundError:
        return None


def _restore(snapshot) -> None:
    """Put config.json's bytes back (mode kept), atomically; remove it when there was none."""
    path = config.path()
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(snapshot)
            fh.flush()
            os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _off(kind, msg, head, ms=None, retry=False, extra=()) -> dict:
    tail = OFF_LINE + ("; nothing was changed, retry with `orche jev setup`" if retry else "")
    return {"status": "off", "kind": kind, "ms": ms, "error": msg, "key": None,
            "lines": [f"{head}: {msg}", *extra, tail]}


def _roll_back_key(previous, backend) -> str:
    """Put the previously stored key back (or remove the new one); the line says the key's state."""
    try:
        if previous:
            jevkey.set_key(previous)
            return f"Jev key: the previous key is stored again in the {backend}"
        jevkey.remove()
        return f"Jev key: the new key was removed again; no key is stored in the {backend}"
    except jevkey.KeyStoreError as exc:
        return (f"Jev key: the NEW key is still stored in the {backend} (rollback failed: "
                f"{jevkey.scrub(jevkey.scrub(str(exc)), previous)}); run `orche jev key rm` or "
                "`orche jev key set`")


def configure(key, kind="gateway", opener=None) -> dict:
    """Probe `kind` once with the key (in memory), then store the key, then point config at `kind`
    with Jev enabled. Nothing changes unless the probe answered AND the key was stored. An
    unreadable config.json stops before anything (`unreadable_line`). A failed probe or store
    changes nothing. A config write failing after the store puts config.json's bytes back and then
    the previously stored key (or removes the new one); if that rollback fails, the message says
    which key is stored.

    Returns {"status": "on"|"off", "kind", "ms", "error", "key": backend, "lines": [...]}; never
    raises for a bad key, an unreadable config, a failed probe or a failed store (masked)."""
    kind = kind if kind in jev.KINDS else "gateway"
    base_url, model = jev.KINDS[kind]
    problem = config_problem()
    if problem:
        return {"status": "off", "kind": kind, "ms": None, "error": problem, "key": None,
                "lines": [unreadable_line(problem)]}
    try:
        key = jevkey.check(key)
        cfg = {**config.load()["jev"], "base_url": base_url, "model": model}
    except jevkey.KeyStoreError as exc:
        return _off(kind, jevkey.scrub(str(exc), key), "Jev key not stored")
    c = jev.client_from(cfg, opener=opener)
    c._key, c.key_source = key, "given"
    result = jev.probe(c)
    if not result["ok"]:
        return _off(kind, result["error"], "Jev probe failed", ms=result["ms"], retry=True)
    snapshot = _snapshot()
    previous = jevkey.get()  # this (the user's) process only; kept in memory for a rollback
    try:
        backend = jevkey.set_key(key)
    except jevkey.KeyStoreError as exc:
        return _off(kind, jevkey.scrub(jevkey.scrub(str(exc), key), previous), "Jev key not stored")
    try:
        update_config(base_url=base_url, model=model, enabled=True)
    except (OSError, util.JsonError) as exc:
        try:
            _restore(snapshot)
            restored = "config.json: put back as it was"
        except OSError as rexc:
            restored = f"config.json: could not be put back ({rexc.strerror or type(rexc).__name__})"
        msg = jevkey.scrub(f"cannot update config: {exc}", key)
        return _off(kind, msg, "Jev not set up", extra=(restored, _roll_back_key(previous, backend)))
    save_record(result)
    lines = [stored_line(backend), on_line(result)]
    env_name = cfg.get("api_key_env") or jev.DEFAULT_KEY_ENV
    if jevkey.source(env_name)[0] == "env":
        lines.append(f"note: {env_name} is set in this environment and is used before the stored key")
    return {"status": "on", "kind": kind, "ms": result["ms"], "error": "", "key": backend, "lines": lines}


def skipped() -> dict:
    return {"status": "skipped", "kind": None, "ms": None, "error": "", "key": None, "lines": [SKIP_LINE]}


def stored_line(backend) -> str:
    if backend == "keychain":
        return f"Jev key stored in the keychain (service {jevkey.SERVICE}, account {jevkey.ACCOUNT})"
    return f"Jev key stored in {jevkey.key_file()} (mode 0600)"


def prompt_flow(opener=None, dialog=True, kind=None) -> dict:
    """The interactive flow: key (dialog on macOS, else hidden prompt), key type (unless `kind` is
    given), configure. Enter, Cancel, EOF, Ctrl-C or no terminal at either question = skipped."""
    try:
        key = read_key(KEY_PROMPT, dialog=dialog)
    except NoTerminal:
        return skipped()
    if not key:
        return skipped()
    kind = kind or ask_kind()
    if kind is None:
        return skipped()
    return configure(key, kind, opener=opener)


def configured():
    """(kind, key source) when config has a base_url and a key resolves, else None."""
    cfg = config.load()["jev"]
    if not cfg.get("base_url"):
        return None
    src, _err = jevkey.source(cfg.get("api_key_env") or jev.DEFAULT_KEY_ENV)
    return (kind_of(cfg["base_url"]), src) if src else None


def probe_configured(opener=None) -> dict:
    """`orche jev probe`: one probe with the configured base_url and key; the result is recorded."""
    cfg = config.load()["jev"]
    c = jev.client_from(cfg, opener=opener)
    if not c.base_url or not c.key():
        return {"ok": False, "ms": None, "kind": c.kind, "error": NOT_CONFIGURED, "configured": False}
    result = jev.probe(c)
    save_record(result)
    return {**result, "configured": True, "enabled": bool(cfg.get("enabled")), "key": c.key_source}


def doctor_row():
    """(level, "jev", detail) for `orche doctor`; reads the last probe, makes no network call.
    Never FAIL: Jev is optional and fails open (triage falls back to Haiku). A failed record of
    another kind than the configured one is stale and ignored; `orche jev disable` forgets it."""
    cfg = config.load()["jev"]
    src, err = jevkey.source(cfg.get("api_key_env") or jev.DEFAULT_KEY_ENV)
    if err:
        return "WARN", "jev", f"{NOT_CONFIGURED}: {err}"
    if not cfg.get("base_url") or not src:
        return "WARN", "jev", NOT_CONFIGURED
    if not jev.secure_url(cfg["base_url"]):
        return "WARN", "jev", f"jev.base_url must be https; {NOT_CONFIGURED}"
    kind = kind_of(cfg["base_url"])
    rec = load_record()
    if rec and not rec.get("ok") and rec.get("kind") == kind:
        reason = jevkey.scrub(str(rec.get("error") or "unknown error"))
        return "WARN", "jev", f"last probe failed {reason}; ORCHE works without Jev; run `orche jev probe`"
    if not cfg.get("enabled"):
        return "WARN", "jev", f"disabled ({kind} configured, key: {src}; turn on: orche jev enable)"
    return "OK", "jev", f"{kind} (key: {src})"
