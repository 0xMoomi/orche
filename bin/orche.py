"""ORCHE command-line entry point, started by the `bin/orche` sh launcher under a Python 3.11+.

Built-in commands: `version`, `projects`. Further commands live in
`lib/orche/cmd_*.py`; each module exposes `register(subparsers)` and is
discovered by name, so adding a module needs no edit here. A command handler
is set with `parser.set_defaults(func=handler)`; `handler(args)` returns the
exit code (0 ok, 1 user error, 2 not found, 3 blocked by policy).

`--json` and `--dry-run` are global: they are read only by the root parser
(accepted anywhere on the command line) and land on `args.json` /
`args.dry_run`. Subcommands must NOT declare them again; a subparser default
would silently overwrite the root value (a dry run could mutate for real), so
`_register` refuses to start when a subcommand redeclares either flag.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

from orche import pyguard  # noqa: E402  (before any other orche module)

pyguard.check_cli()

from orche import __version__, registry  # noqa: E402

EXIT_OK, EXIT_USER, EXIT_NOT_FOUND, EXIT_BLOCKED = 0, 1, 2, 3
GLOBAL_FLAGS = ("--json", "--dry-run")
GLOBAL_DESTS = {"json", "dry_run"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USER, f"{self.prog}: error: {message}\n")


def emit(args, data, human_lines):
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        for line in human_lines:
            print(line)


def cmd_version(args):
    emit(args, {"name": "orche", "version": __version__}, [f"orche {__version__}"])
    return EXIT_OK


def cmd_projects(args):
    reg = registry.load()
    rows = [{"key": key, "stage": proj.get("stage"), "aliases": proj.get("aliases", []),
             "path": proj.get("path")} for key, proj in sorted(reg["projects"].items())]
    lines = [f"{r['key']}\t{r['stage']}\t{','.join(r['aliases'])}" for r in rows] or ["no projects"]
    emit(args, rows, lines)
    return EXIT_OK


def _load_module(path):
    name = f"orche.{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _check_global_flags(subparsers, trail=()):
    """Refuse subcommands (at any depth) that redeclare --json / --dry-run."""
    for name, sub in subparsers.choices.items():
        for action in sub._actions:
            if action.dest in GLOBAL_DESTS:
                cmd = " ".join(trail + (name,))
                raise RuntimeError(f"orche: subcommand '{cmd}' redeclares global flag "
                                   f"--{action.dest.replace('_', '-')}; remove it from its register()")
            if isinstance(action, argparse._SubParsersAction):
                _check_global_flags(action, trail + (name,))


def _register(subparsers, lib_dir=None):
    """Register built-ins, then every `cmd_*.py` module's `register(subparsers)`."""
    subparsers.add_parser("version", help="print the ORCHE version").set_defaults(func=cmd_version)
    subparsers.add_parser("projects", help="list registry projects").set_defaults(func=cmd_projects)
    for path in sorted(Path(lib_dir or LIB / "orche").glob("cmd_*.py")):
        _load_module(path).register(subparsers)
    _check_global_flags(subparsers)


def build_parser(lib_dir=None):
    parser = Parser(prog="orche", description="Orchestration layer for Claude Code + Orca.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--dry-run", action="store_true", help="print actions instead of mutating")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", parser_class=Parser)
    _register(subparsers, lib_dir)
    return parser


def main(argv=None, lib_dir=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Global flags are accepted anywhere before a bare `--`.
    cut = argv.index("--") if "--" in argv else len(argv)
    flags = [a for a in argv[:cut] if a in GLOBAL_FLAGS]
    rest = [a for a in argv[:cut] if a not in GLOBAL_FLAGS] + argv[cut:]
    parser = build_parser(lib_dir)
    args = parser.parse_args(flags + rest)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USER
    try:
        return args.func(args)
    except registry.RegistryError as exc:
        print(f"orche: {exc}", file=sys.stderr)
        return EXIT_USER


if __name__ == "__main__":
    sys.exit(main())
