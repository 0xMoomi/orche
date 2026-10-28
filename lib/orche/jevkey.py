"""Where the Jev API key lives when it is not in the environment.

Resolution order (`resolve`): env var (`jev.api_key_env`, default JEV_API_KEY) -> stored key -> none.
Storage (`set_key` / `remove` / `stored`):
- macOS: the login keychain, generic password service `orche`, account `JEV_API_KEY`. Writes go
  through `security -i` with the command on stdin (as `secrets/backends.py` does), so the
  key never appears in argv; `status` uses `find-generic-password` WITHOUT `-w`, so the value is
  not even read to report where it is.
- elsewhere: `ORCHE_HOME/secrets/jev.key`, mode 0600.

The same store keeps the Claude OAuth token from `claude setup-token` (`orche auth`): account
`CLAUDE_CODE_OAUTH_TOKEN`, file `ORCHE_HOME/secrets/oauth.token`; pass `account=OAUTH_ACCOUNT`.

Every message that can carry text from `security` passes `scrub` (the key replaced, then
`mask.redact`). Nothing here prints; callers decide what to show.
"""
import os
import sys

from . import paths, shell, util
from . import secrets
from .secrets import store
from .secrets.backends import keychain_quote

SERVICE = "orche"
ACCOUNT = "JEV_API_KEY"
OAUTH_ACCOUNT = "CLAUDE_CODE_OAUTH_TOKEN"
FILES = {ACCOUNT: "jev.key", OAUTH_ACCOUNT: "oauth.token"}
NOT_FOUND = 44  # `security` exit status for "item could not be found"
# Seams for tests: the `security` executable (resolved on PATH) and the platform.
SECURITY = "security"
PLATFORM = sys.platform


class KeyStoreError(RuntimeError):
    """A storage failure; the message is already scrubbed."""


def backend() -> str:
    return "keychain" if PLATFORM == "darwin" else "file"


def key_file(account=ACCOUNT):
    return store.secrets_dir() / FILES[account]


def _stored_file(account):
    """The key file's path without creating the secrets dir (reads run in every headless env())."""
    return paths.orche_home() / "secrets" / FILES[account]


def scrub(text, key=None) -> str:
    return secrets.scrub(text, key)


SECURITY_TIMEOUT = 15   # seconds; a test patches it


def _security(args, stdin=None):
    return shell.run([SECURITY, *args], input=stdin, timeout=SECURITY_TIMEOUT)


def _failure(what, result, key=None) -> KeyStoreError:
    detail = scrub((result.err or result.out or "").strip(), key).splitlines()
    tail = f": {detail[-1][:200]}" if detail else ""
    return KeyStoreError(f"keychain {what} failed (exit {result.code}){tail}")


def check(key) -> str:
    """The key stripped of surrounding whitespace; KeyStoreError when empty, multi-line, or holding
    whitespace, `"` or `\\` (real keys are token-safe; quoted forms would escape the literal scrub)."""
    key = str(key or "").strip()
    if not key:
        raise KeyStoreError("empty key")
    if "\n" in key or "\r" in key:
        raise KeyStoreError("the key must be a single line")
    if any(ch.isspace() or ch in '"\\' for ch in key):
        raise KeyStoreError("the key may not contain spaces, quotes or backslashes")
    return key


def set_key(key, account=ACCOUNT) -> str:
    """Store the key; returns the backend name."""
    key = check(key)
    if backend() == "keychain":
        cmd = (f"add-generic-password -U -s {keychain_quote(SERVICE)} -a {keychain_quote(account)} "
               f"-w {keychain_quote(key)}\n")
        try:
            result = _security(["-i"], stdin=cmd)
        except OSError as exc:
            raise KeyStoreError(f"keychain unavailable: {scrub(exc.strerror or exc, key)}") from None
        if not result.ok or result.err.strip():
            raise _failure("write", result, key)
        return "keychain"
    try:
        util.atomic_write(key_file(account), key + "\n", mode=0o600)
    except OSError as exc:
        raise KeyStoreError(f"cannot write {key_file(account).name}: {exc.strerror or exc}") from None
    return "file"


def read(account=ACCOUNT):
    """(value or None, error or None). The error is None when nothing is stored (`security` exit 44,
    no file); otherwise it names the failure ("exit N", "keychain unavailable", ...) and never
    carries the value or `security`'s output."""
    if backend() == "keychain":
        try:
            result = _security(["find-generic-password", "-s", SERVICE, "-a", account, "-w"])
        except OSError as exc:
            return None, f"keychain unavailable: {scrub(exc.strerror or exc)}"
        if result.code == NOT_FOUND:
            return None, None
        if not result.ok:
            return None, f"exit {result.code}"
        return result.out.strip() or None, None
    try:
        key = _stored_file(account).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot read {FILES[account]}: {exc.strerror or exc}"
    return key or None, None


def get(account=ACCOUNT):
    """The stored key, or None when nothing is stored or the store cannot be read."""
    return read(account)[0]


def stored(account=ACCOUNT):
    """(present, error): whether a key is stored, without reading the value from the keychain."""
    if backend() == "keychain":
        try:
            result = _security(["find-generic-password", "-s", SERVICE, "-a", account])
        except OSError as exc:
            return False, f"keychain unavailable: {scrub(exc.strerror or exc)}"
        if result.ok:
            return True, None
        if result.code == NOT_FOUND:
            return False, None
        return False, str(_failure("lookup", result))
    path = _stored_file(account)
    return path.is_file() and path.stat().st_size > 0, None


def remove(account=ACCOUNT) -> bool:
    """Delete the stored key; True when one was there."""
    if backend() == "keychain":
        try:
            result = _security(["delete-generic-password", "-s", SERVICE, "-a", account])
        except OSError as exc:
            raise KeyStoreError(f"keychain unavailable: {scrub(exc.strerror or exc)}") from None
        if result.ok:
            return True
        if result.code == NOT_FOUND:
            return False
        raise _failure("delete", result)
    try:
        key_file(account).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KeyStoreError(f"cannot remove {key_file(account).name}: {exc.strerror or exc}") from None


def resolve(env_name=ACCOUNT):
    """(key, source): source is "env", "keychain" or "file"; (None, None) when there is no key."""
    key = os.environ.get(env_name or ACCOUNT)
    if key:
        return key, "env"
    key = get()
    return (key, backend()) if key else (None, None)


def source(env_name=ACCOUNT):
    """(source or None, error or None) without reading a stored value: env first, then the store."""
    if os.environ.get(env_name or ACCOUNT):
        return "env", None
    present, err = stored()
    return (backend() if present else None), err
