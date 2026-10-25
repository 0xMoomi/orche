"""`orche resume [<project>]`: the resume card (lib/orche/resume.py) — the project's open lineages, what
each waits on, the owner's open questions and the merge queue, so Main picks up after a `/clear` or a
restart without the owner repeating the request. The project defaults to the calling session's."""
import os

from . import cli, registry, resume, roster


def register(subparsers):
    p = subparsers.add_parser("resume", help="the resume card: open lineages, what waits on whom, the queue")
    p.add_argument("project", nargs="?", help="project key or alias (default: this session's project)")
    p.set_defaults(func=cmd_resume)


def cmd_resume(args):
    reg = registry.load()
    want = args.project
    if not want:
        try:
            who = roster.Roster(reg).identify(os.getcwd())
            want = (who.member.project if who.member is not None else None) or who.place.project
        except Exception:  # noqa: BLE001 - no session project: ask for one
            want = None
        if not want:
            return cli.fail(cli.EXIT_USER, "which project? orche resume <project>")
    found = registry.project(reg, want)
    if found is None:
        return cli.fail(cli.EXIT_NOT_FOUND, f"unknown project: {want}")
    d = resume.data(reg, found[0])
    cli.out(args, {**d, "text": resume.render(d)}, resume.render(d))
    return cli.EXIT_OK
