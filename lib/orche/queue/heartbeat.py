"""The heartbeat's queue step (lib/orche/reconcile.py `_pass`, phase `queue`): start the merge queue's
integrator where an entry waits. Its rows are owner.heartbeat's shape (`note`)."""
from .. import config, log, queue
from . import integrator


def note(action, cls, project, target, line, failed=None) -> dict:
    """A row of a pass step that acted itself (queue, usage, review), as owner.heartbeat's."""
    return {"action": action, "class": cls, "project": project, "target": target, "rev": None, "attempt": 1,
            "cap": 1, "line": line, "failed": failed}


def heartbeat(reg, keys, dry_run) -> list:
    """Class `queue` (§5.10, R19), per project whose merge queue has entries and no integrator running:
    under `land: auto` entries still waiting for the owner's word become plain waiting (queue.release);
    a `testing` entry whose integrator is gone (its pid dead, or queue.lost_after_s passed) goes back to
    the front (integrator.recover; only its gate's own processes are stopped); then work the integrator
    may take (queue.startable: an approved waiting entry or a merge whose proof is pending; neither the
    queue nor the project paused by `orche queue pause` or `orche flow pause`) -> queue.start(p). The
    queue's own files and the process table only (no git, no gh)."""
    cfg = config.load()
    rows = []
    for key in sorted(reg.get("projects", {})):
        if keys is not None and key not in keys:
            continue
        try:
            s = queue.summary(key)
            if not s["entries"] or queue.running(key):
                continue
            stale = [e for e in s["entries"] if e.get("state") == "testing" and not queue.landed(e)]
            if dry_run:
                lost = stale
            else:
                queue.release(key, cfg)
                lost = integrator.recover(key) if stale else []
                s = queue.summary(key)
            can = queue.startable(key)
        except Exception as exc:  # noqa: BLE001 - one unreadable queue never stops the pass
            log.log("hooks", f"reconcile: queue {key}: {type(exc).__name__}: {exc}")
            continue
        for e in lost:
            rows.append(note("queue-lost", "queue", key, key,
                             f"{'would check the testing' if dry_run else 'recovered the lost'} PR #{e.get('pr')} "
                             f"of {key}{'' if dry_run else ' (back at the front)'}"))
        if not can:
            continue
        waiting = s["waiting"] - s["await"]
        failed = None
        if not dry_run:
            try:
                queue.start(key)
            except Exception as exc:  # noqa: BLE001 - one queue never stops the pass
                failed = type(exc).__name__
                log.log("hooks", f"reconcile: queue start {key}: {failed}: {exc}")
        rows.append(note("queue-start", "queue", key, key, f"{'would start' if dry_run else 'start'} the merge "
                                                           f"queue of {key} ({waiting} waiting)", failed))
    return rows
