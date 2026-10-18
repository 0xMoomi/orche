"""`orche apply-all`: run `init` (in-process, with its defaults) for every Orca repo not yet registered.

Refuses when Orca does not answer. The awake limit is honoured across the whole run: when a
repo's `--wake` divisions would exceed it, that repo is initialised all-dormant instead. A failing
repo is reported and the run continues; the exit code is 1 when any repo failed. The ORCHE
plugin repo itself is skipped. The plan and the summary print how many panes the run opens
(main + liaison per repo, plus the `--wake` divisions).
"""
import argparse
import io
import json
import os
import shlex
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from .. import cmd_init, orca, registry, util
from ..hr import EXIT_BLOCKED, EXIT_OK, EXIT_USER, PLUGIN_ROOT, fail, out


def _csv(text) -> list:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def registered_as(reg, repo):
    real = os.path.realpath(repo["path"]) if repo.get("path") else None
    for key, proj in reg["projects"].items():
        if proj.get("repo_id") == repo.get("id") or (
                real and proj.get("path") and os.path.realpath(proj["path"]) == real):
            return key
    return None


def init_namespace(argv):
    """`init` arguments parsed by init's own parser, so its defaults stay the single source."""
    parser = argparse.ArgumentParser(prog="orche", add_help=False)
    cmd_init.register(parser.add_subparsers(dest="command"))
    ns = parser.parse_args(argv)
    ns.json, ns.dry_run = True, False
    return ns


def _run_init(argv):
    """(exit code, init's JSON result or None, last error line)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        ns = init_namespace(argv)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cmd_init.cmd_init(ns)
    except SystemExit as exc:
        return EXIT_USER, None, f"bad init arguments ({exc})"
    except Exception as exc:  # noqa: BLE001 - one failing repo must not stop the run
        return EXIT_USER, None, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(stdout.getvalue()) if code == EXIT_OK else None
    except ValueError:
        data = None
    errors = [ln for ln in stderr.getvalue().splitlines() if ln.startswith("orche:")]
    return code, data, (errors[-1].removeprefix("orche: ") if errors else "")


def run(args) -> int:
    wake = _csv(args.wake)
    bad = [d for d in wake if d not in registry.DIVISIONS]
    if bad:
        return fail(EXIT_USER, f"unknown division(s): {','.join(bad)} (allowed: {','.join(registry.DIVISIONS)})")
    if "ops" in wake and args.stage == "building":
        return fail(EXIT_BLOCKED, "ops is gated by stage: it stays dormant while stage is building")
    try:
        orca.status(timeout=10)
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"Orca is not running or not reachable ({exc}); start Orca and retry")
    try:
        result = orca.repo_list()
    except orca.OrcaError as exc:
        return fail(EXIT_USER, f"orca repo list failed: {exc}")
    repos = [r for r in (result.get("repos", []) if isinstance(result, dict) else result or [])
             if isinstance(r, dict) and r.get("id")]
    reg = registry.load()
    limit = reg["limits"]["max_awake_sessions"]
    budget = limit - len(registry.awake_sessions(reg))
    rows, failed = [], 0
    plugin_root = os.path.realpath(PLUGIN_ROOT)
    for repo in repos:
        name = repo.get("displayName") or repo["id"]
        key = util.slugify(repo.get("displayName") or Path(repo.get("path") or repo["id"]).name)
        if repo.get("path") and os.path.realpath(repo["path"]) == plugin_root:
            rows.append({"repo": name, "key": key, "result": "skipped (ORCHE plugin repo)"})
            continue
        if not args.dry_run:
            reg = registry.load()  # an earlier init in this run may have registered the same path
        owner = registered_as(reg, repo)
        if owner:
            rows.append({"repo": name, "key": owner, "result": "already registered"})
            continue
        if not args.dry_run:
            budget = limit - len(registry.awake_sessions(registry.load()))
        woken = wake if len(wake) <= budget else []
        note = "" if woken == wake else f"--wake {','.join(wake)} dropped (awake limit {limit})"
        argv = ["init", repo["id"], "--stage", args.stage, "--divisions", ",".join(registry.DIVISIONS)] \
            + (["--wake", ",".join(woken)] if woken else [])   # the standard set, never the setup screen
        row = {"repo": name, "key": key, "argv": ["orche", *argv], "note": note, "pane_count": 2 + len(woken)}
        if args.dry_run:
            budget -= len(woken)
            rows.append({**row, "result": "planned"})
            continue
        code, data, err = _run_init(argv)
        if code != EXIT_OK or data is None:
            failed += 1
            rows.append({**row, "result": f"failed (exit {code}): {err or 'no output'}"})
            continue
        rows.append({**row, "key": data.get("project", key), "result": "ok",
                     "created": [d for d in data.get("divisions", {})],
                     "skipped": data.get("skipped", []), "panes": data.get("panes", []),
                     "pane_count": len(data.get("panes", [])) + len(woken)})
    lines = []
    skipped_results = ("already registered", "skipped (ORCHE plugin repo)")
    for row in rows:
        if row["result"] == "already registered":
            lines.append(f"skip {row['repo']}: already registered as {row['key']}")
        elif row["result"] == "skipped (ORCHE plugin repo)":
            lines.append(f"skip {row['repo']}: skipped (ORCHE plugin repo)")
        elif args.dry_run:
            lines.append(f"plan {row['repo']} ({row['key']}): {shlex.join(row['argv'])}"
                         + (f" [{row['note']}]" if row["note"] else ""))
    if not args.dry_run:
        lines.append("repo\tkey\tcreated\tskipped\tpanes\tresult")
        for row in rows:
            if row["result"] in skipped_results:
                continue
            lines.append("\t".join([row["repo"], row["key"], ",".join(row.get("created", [])) or "-",
                                    ",".join(row.get("skipped", [])) or "-", ",".join(row.get("panes", [])) or "-",
                                    row["result"] + (f" [{row['note']}]" if row["note"] else "")]))
    planned = sum(1 for r in rows if r["result"] not in skipped_results)
    panes = sum(r.get("pane_count", 0) for r in rows if r["result"] in ("planned", "ok"))
    lines.append(f"{len(repos)} repo(s), {planned} {'planned' if args.dry_run else 'processed'}, {failed} failed")
    lines.append(f"panes: {panes} {'to open' if args.dry_run else 'opened'} (main + liaison per repo, plus --wake)")
    out(args, {"dry_run": args.dry_run, "repos": rows, "failed": failed, "panes": panes}, lines)
    return EXIT_USER if failed else EXIT_OK
