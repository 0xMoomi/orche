"""`orche dispatch [--file PATH]`: send brief / AMEND / STOP blocks and read the outcome in the same turn.

The blocks come from stdin (or --file), without fences, separated by a line that is exactly `---`.
The sender is the identified session (ORCHE_SESSION, else the unit worktree owning the cwd); only a
project's Main or a division may send (dispatch.check_sender). Every valid block is delivered; each
block gets one line:

    block 1: delivered <unit> → <to> (rev N, <delivery state>)
    block 2: refused — <reason>
    block 3: skipped — identical to <unit> → <to>, delivered in the last 10 min; not sent again

Exit 0 when nothing was refused, 2 when any block (or the sender) was refused, 1 on a usage error.
A delivered block is remembered per session for dispatch.DELIVERED_TTL (the delivered map,
<ORCHE_HOME>/sessions/<session>.dispatched.json): re-sending it unchanged is skipped, so after a
partial refusal the whole batch can be re-run safely. --dry-run parses and routes without writing.
"""
import os
import sys
import time
from pathlib import Path

from . import brief, cli, dispatch, log, paths, registry, roster, units, util

SEPARATOR = "---"
DEADLINE_S = 10.0       # the CLI's delivery budget (a hook's is orca.hook_deadline's 3 s)
EXIT_REFUSED = 2


def register(subparsers):
    p = subparsers.add_parser("dispatch", help="send brief/AMEND/STOP blocks (stdin; `---` between blocks)")
    p.add_argument("--file", help="read the blocks from this file instead of stdin")
    p.set_defaults(func=cmd_dispatch)


def split_blocks(text) -> list:
    """Block texts in order: split at lines that are exactly `---`; blank-only pieces are dropped."""
    blocks, cur = [], []
    for line in (text or "").splitlines():
        if line.rstrip() == SEPARATOR:
            blocks.append(cur)
            cur = []
        else:
            cur.append(line)
    blocks.append(cur)
    return [body for body in ("\n".join(b).strip("\n") for b in blocks) if body.strip()]


def _map_path(session):
    return paths.orche_home() / "sessions" / f"{util.slugify(session)}.dispatched.json"


def _load_map(session) -> dict:
    """{body_hash: {"routed": "<unit> → <to>", "at": epoch}} still inside DELIVERED_TTL."""
    try:
        raw = util.read_json(_map_path(session), default={}) or {}
    except Exception:  # noqa: BLE001 - a corrupt map only loses the skip
        raw = {}
    now = time.time()
    return {h: e for h, e in raw.items()
            if isinstance(e, dict) and isinstance(e.get("at"), (int, float))
            and 0 <= now - e["at"] <= dispatch.DELIVERED_TTL and isinstance(e.get("routed"), str)}


def _save_map(session, entries) -> None:
    path = _map_path(session)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        util.write_json(path, entries)
    except OSError as exc:
        log.log("hooks", f"dispatch: cannot write the delivered map {path}: {exc}")


def _read_input(args):
    if args.file:
        try:
            return Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read {args.file}: {exc.strerror or exc}") from None
    return sys.stdin.read()


def _dry_run(r, bodies, frm) -> int:
    refused = 0
    for n, plan in enumerate(dispatch.plan_blocks(r, bodies, frm), 1):
        if isinstance(plan, str):
            refused += 1
            print(f"block {n}: refused — {plan.removeprefix(brief.REFUSE)}")
        else:
            what = plan.unit or ("external-request" if plan.external else "new brief")
            print(f"block {n}: would deliver {plan.block.kind} {what} → {plan.to}")
    return EXIT_REFUSED if refused else cli.EXIT_OK


def cmd_dispatch(args):
    try:
        bodies = split_blocks(_read_input(args))
    except ValueError as exc:
        return cli.fail(cli.EXIT_USER, str(exc))
    if not bodies:
        return cli.fail(cli.EXIT_USER, "no block on stdin (blocks without fences, `---` on its own line between them)")
    r = roster.Roster(registry.load())
    try:
        frm = dispatch.check_sender(r, r.identify(os.getcwd()))
    except brief.Refusal as exc:
        print(f"refused — {str(exc).removeprefix(brief.REFUSE)}")
        return EXIT_REFUSED
    if args.dry_run:
        return _dry_run(r, bodies, frm)
    units.touch_activity(frm.session)
    entries = _load_map(frm.session)
    skip = {h: e["routed"] for h, e in entries.items()}
    o = dispatch.relay_outcome(r, bodies, frm, time.monotonic() + DEADLINE_S, skip=skip)
    if o.delivered:
        now = time.time()
        _save_map(frm.session, {**entries, **{h: {"routed": routed, "at": now} for h, routed in o.delivered.items()}})
    for line in o.lines:
        print(line)
    if not o.refused:
        return cli.EXIT_OK
    if len(o.refused) < len(bodies):
        print(f"{len(o.refused)} of {len(bodies)} refused: fix the refused block(s) and run `orche dispatch` "
              "again; the delivered ones went out (re-sending them unchanged is skipped).")
    else:
        print("nothing was delivered: fix the block(s) and run `orche dispatch` again.")
    return EXIT_REFUSED
