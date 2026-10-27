"""orche auth: store, remove or locate the Claude OAuth token headless runs use.

`claude setup-token` prints a one-year token (Pro/Max plan). `orche auth set` reads it from a
hidden prompt, or from stdin when stdin is not a terminal, and keeps it in the Jev key store
(macOS keychain, else ORCHE_HOME/secrets/oauth.token, mode 0600). Only headless `claude -p` runs
get it (see `oauth`); panes keep the interactive login. The token is never read from the command
line and never printed, logged or shown in a dry run.
"""
import argparse
import sys

from . import cli, jev_setup, jevkey, oauth

EXIT_OK, EXIT_USER = 0, 1
ARGV_REFUSED = ("orche: the token is never taken from the command line (it would land in shell "
                "history and process lists); run `orche auth set` and paste it at the hidden prompt, "
                "or pipe it on stdin")


def _read_token():
    """The token from the hidden prompt (stdin is a terminal) or one stdin line; "" for none."""
    if not jev_setup.terminal():
        return jev_setup.stdin_line()
    try:
        return jev_setup.read_secret("Claude OAuth token (from `claude setup-token`): ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\n")
        return ""


def cmd_set(args):
    if args.token:  # refused before anything else; the value is not echoed
        print(ARGV_REFUSED, file=sys.stderr)
        return EXIT_USER
    backend = jevkey.backend()
    if args.dry_run:
        cli.out(args, {"dry_run": True, "backend": backend},
              [f"would store the Claude OAuth token in the {backend}"])
        return EXIT_OK
    token = _read_token()
    if not token:
        print("orche: no token stored", file=sys.stderr)
        return EXIT_USER
    try:
        backend = jevkey.set_key(token, account=oauth.ENV)
    except jevkey.KeyStoreError as exc:
        print(f"orche: {jevkey.scrub(str(exc), token)}", file=sys.stderr)
        return EXIT_USER
    cli.out(args, {"stored": True, "backend": backend},
          [f"Claude OAuth token stored in the {backend}; headless runs use it (panes keep the login)"])
    return EXIT_OK


def cmd_rm(args):
    backend = jevkey.backend()
    if args.dry_run:
        cli.out(args, {"dry_run": True, "backend": backend},
              [f"would remove the Claude OAuth token from the {backend}"])
        return EXIT_OK
    try:
        removed = jevkey.remove(account=oauth.ENV)
    except jevkey.KeyStoreError as exc:
        print(f"orche: {exc}", file=sys.stderr)
        return EXIT_USER
    cli.out(args, {"removed": removed, "backend": backend},
          [f"Claude OAuth token removed from the {backend}" if removed
           else f"no Claude OAuth token in the {backend}"])
    return EXIT_OK


def cmd_status(args):
    src, err = oauth.source()
    data = {"source": src, "backend": jevkey.backend(), "env": oauth.ENV, "error": err}
    if err:
        cli.out(args, data, [f"oauth token: unknown ({err})"])
        return EXIT_USER
    cli.out(args, data, [f"oauth token: {src} — headless runs use it (panes keep the login)" if src
                       else f"oauth token: {oauth.UNSET_LINE}"])
    return EXIT_OK


def register(subparsers):
    p = subparsers.add_parser("auth", help="the Claude OAuth token headless runs use (never prints it)")
    sub = p.add_subparsers(dest="auth_command", metavar="<auth-command>")
    st = sub.add_parser("set", help="store the token from `claude setup-token` (hidden prompt, or stdin "
                                    "when not a terminal)")
    st.add_argument("token", nargs="*", help=argparse.SUPPRESS)
    st.set_defaults(func=cmd_set)
    sub.add_parser("rm", help="remove the stored token").set_defaults(func=cmd_rm)
    sub.add_parser("status", help="where the token comes from: env, keychain, file or none").set_defaults(
        func=cmd_status)
    p.set_defaults(func=lambda a: (p.print_help(sys.stderr), EXIT_USER)[1])
