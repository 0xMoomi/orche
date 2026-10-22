"""The Claude OAuth token headless runs use, so they do not share the pane login.

`claude setup-token` prints a long-lived (one-year) token for a Pro/Max plan; `orche auth set`
stores it in the Jev key store (`jevkey`, account CLAUDE_CODE_OAUTH_TOKEN: the macOS keychain, else
ORCHE_HOME/secrets/oauth.token, mode 0600). `headless.env()` then gives every `claude -p` ORCHE
starts `CLAUDE_CODE_OAUTH_TOKEN`: an inherited value wins (the store is not read), else the stored
token, else nothing changes and the run uses the login like before. Pane commands never carry it.

Nothing here prints or logs the token; callers show only where it is.
"""
import os

from . import jevkey, log

ENV = jevkey.OAUTH_ACCOUNT
SET_LINE = "set — headless runs use it (panes keep the login)"
UNSET_LINE = ("not set — headless runs share the pane login "
              "(run: claude setup-token, then orche auth set)")


def inject(env: dict) -> str:
    """Add the stored token to a headless child's environment unless it already has one.
    Returns "token" when the child authenticates with a token, "login" otherwise. A stored token
    that cannot be read (a keychain error, not "not found") leaves one hooks.log line; the run
    falls back to the login."""
    if env.get(ENV):
        return "token"
    token, err = jevkey.read(account=ENV)
    if err:     # one line, never the value nor `security`'s output
        log.log("hooks", f"oauth: stored token unreadable ({err}); headless run uses the login")
    if not token:
        return "login"
    env[ENV] = token
    return "token"


def source():
    """(source or None, error or None) without reading a stored value: env first, then the store."""
    if os.environ.get(ENV):
        return "env", None
    present, err = jevkey.stored(account=ENV)
    return (jevkey.backend() if present else None), err


def doctor_row():
    """(level, "oauth token", detail) for `orche doctor`. Never FAIL: without a token headless runs
    use the login, as they always did."""
    src, err = source()
    if err:
        return "WARN", "oauth token", f"unknown ({err})"
    return ("OK", "oauth token", SET_LINE) if src else ("WARN", "oauth token", UNSET_LINE)
