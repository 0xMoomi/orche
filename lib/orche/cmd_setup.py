"""`orche setup [--project PATH ...] [--no-auth] [--home PATH]` — one command for a new machine.

Each step is idempotent and printed under its own `== step ==` header:
1. install — `orche install --no-jev` in the mode the last install used (install-state.json), else
   plugin when the ORCHE plugin is enabled, else settings (hooks, status line, agents, the heartbeat
   job), without its own first-pass check: step 4 runs it once the projects exist;
2. auth — `orche auth status` and, when no token is stored, the two commands that store one
   (`claude setup-token`, then `orche auth set`); setup never reads or prints a token. `--no-auth`
   skips the step;
3. init — `orche init PATH --no-intake` per `--project` (a repo Orca or the registry does not know
   yet is added and registered by init itself);
4. heartbeat — the reconcile job's first pass (`privacy.first_pass_check`: the macOS privacy
   guidance when the job cannot read a project), only when the install loaded the job (macOS, real
   home) and not under --dry-run;
5. doctor — `orche doctor`.
Exit 0 when every step succeeded and the doctor has no FAIL, else 1; a heartbeat with no pass is a
failed step, and when macOS privacy is all that is left the last line says so (BLOCKED_LINE).
Ctrl-C during the heartbeat wait: one line, exit 130. Human lines only.
"""
import argparse
import sys
from pathlib import Path

from . import cmd_auth, cmd_init, cmd_install, install, oauth, privacy, units
from .cli import EXIT_OK, EXIT_USER
from .install import launchd


def register(subparsers):
    p = subparsers.add_parser("setup", help="set up this machine in one go: install, auth status, init the "
                                            "--project repos, the heartbeat's first pass, doctor")
    p.add_argument("--project", action="append", default=[], metavar="PATH",
                   help="a repository to init (repeatable; registered when unknown)")
    p.add_argument("--no-auth", action="store_true", help="skip the OAuth token step")
    p.add_argument("--home", help="tests only: home directory to set up (default: $HOME)")
    p.set_defaults(func=cmd_setup)


def _parser():
    parser = argparse.ArgumentParser(prog="orche")
    sub = parser.add_subparsers(dest="command")
    for module in (cmd_install, cmd_init, cmd_auth):
        module.register(sub)
    return parser


def _run(args, argv, **extra) -> int:
    """One ORCHE command in this process, with setup's --dry-run, human output and `extra` attributes."""
    ns = _parser().parse_args(argv)
    ns.json, ns.dry_run = False, args.dry_run
    for key, value in extra.items():
        setattr(ns, key, value)
    code = ns.func(ns)
    sys.stdout.flush()
    return code


BLOCKED_LINE = ("setup: one step left — switch on Full Disk Access in the window opened above, then run: "
                "orche install --grant")


def cmd_setup(args):
    home = ["--home", args.home] if args.home else []
    target = Path(args.home).expanduser() if args.home else Path.home()
    mode = install.installed_mode(target)
    hint = ["--no-plugin-hint"] if mode == "plugin" and install.plugin_enabled(target) else []
    failed = []
    print("== install ==", flush=True)
    if _run(args, ["install", "--mode", mode, "--no-jev", *hint, *home], first_pass=False):
        failed.append("install")
    if not args.no_auth:
        print("== auth ==", flush=True)
        _run(args, ["auth", "status"])   # an unreadable store is its line, not a failed setup
        src, err = oauth.source()
        if not src and not err:
            print("store a token for headless runs (setup never reads it):\n  claude setup-token\n  orche auth set")
    for path in args.project:
        print(f"== init {path} ==", flush=True)
        if _run(args, ["init", "--no-intake", "--", path], first_pass=False, close_window=False):   # step 4 checks once
            failed.append(f"init {path}")
    blocked = False
    if not args.dry_run and launchd.job_loaded(target):
        print("== heartbeat ==", flush=True)
        try:
            first = privacy.first_pass_check(units.now())
        except KeyboardInterrupt:
            return privacy.interrupted()
        print("\n".join(first["lines"]), flush=True)
        if not first["ok"]:
            failed.append("heartbeat")
            blocked = first["hint"] == "privacy"
    print("== doctor ==", flush=True)
    if _run(args, ["doctor", *home]):
        failed.append("doctor")
    if not failed:
        print("setup: done")
    elif blocked and failed == ["heartbeat"]:
        print(BLOCKED_LINE)
    else:
        print(f"setup: needs attention: {', '.join(failed)}")
    return EXIT_OK if not failed else EXIT_USER
