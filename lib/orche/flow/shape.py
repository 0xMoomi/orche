"""Post-time shaping, called by inbox.post before the write.

Repeat: when the previous pointer from the same sender for the same unit in the same recipient's
inbox (pending or drained) has the same `brief_rev` and `status`, the new one gets `quiet: true`
and `repeat: <n>` (n counts the run of equal reports, 2 for the first repeat): injected at the next
prompt, never wakes, never escalated, urgent or not. Text is not compared. Only reports filed under a
brief are shaped: not orche's own, a dispatch, a STOP, a runner's filing, an external request or a
brief-less (`adhoc-`/`xreq-`) unit. Only pointers since the project's window W (guard.window: the owner's
last prompt or local midnight) count as the previous one, so a re-run the owner asked for wakes again.

Lineage: a done/stopped report to the project's Main of a lineage with two or more members is
quiet too (flow.lineage.quiets); failed, blocked and question still wake, and a division recipient always
wakes. A `lineage_closed` pointer is never shaped.
"""
from .. import inbox, registry, roster
from . import guard, lineage

REPEATS = 3          # `orche status` names a unit whose newest report repeated this many times


def _report(ptr):
    """A unit session's report filed under a brief (not orche's own feedback, a dispatch, a STOP, a runner's
    filing, an external request or a brief-less unit)."""
    return not (ptr.get("from") in (None, "orche") or ptr.get("via") in ("dispatch", "runner")
                or ptr.get("status") in ("stop", "external-request")
                or str(ptr.get("unit") or "").startswith(("adhoc-", "xreq-")))


def _order(path, ptr):
    """Newest-last order: the pointer's `at` (seconds), then its file's mtime (names do not sort)."""
    try:
        ns = path.stat().st_mtime_ns
    except OSError:
        ns = 0
    return (inbox.pointer_time(path, ptr), ns)


def shape(ptr, sc=None):
    """Mark `ptr` (in place) as a repeat when it is one, or quiet when `sc` (its lineage scope) has it so.
    Returns `ptr`."""
    if ptr.get("lineage_closed"):
        return ptr
    if lineage.quiets(sc, ptr):
        ptr["quiet"] = True
    if not _report(ptr):
        return ptr
    prev = _previous(ptr["to"], ptr.get("unit"), ptr["from"], _since(sc, ptr))
    if prev is not None and prev.get("brief_rev") == ptr.get("brief_rev") and prev.get("status") == ptr.get("status"):
        ptr["quiet"] = True
        ptr["repeat"] = int(prev.get("repeat") or 1) + 1
    return ptr


def _since(sc, ptr):
    """W of the recipient's project (the sender's when the recipient has none); None when neither is known."""
    if sc is not None:
        return guard.window(sc.project)
    r = roster.Roster(registry.load())
    m = r.member(ptr.get("to")) or r.member(ptr.get("from"))
    return guard.window(m.project) if m is not None else None


def _previous(to, unit, frm, since=None):
    best = None
    for path, ptr in inbox.about(to, unit, frm):
        if not _report(ptr):
            continue
        k = _order(path, ptr)
        if k[0] is not None and (since is None or k[0] >= since) and (best is None or k > best[0]):
            best = (k, ptr)
    return best[1] if best else None


def repeats(session, since):
    """`session`'s units whose newest report (orche's feedback and dispatches aside) since `since` repeated REPEATS or more times:
    [{unit, n, status, rev}] for `orche status`."""
    try:
        found = inbox.about_all(session)
    except ValueError:
        return []
    newest = {}
    for path, ptr in found:
        k = _order(path, ptr)
        unit = ptr.get("unit")
        if k[0] is None or k[0] < since or not unit or not _report(ptr):
            continue
        if unit not in newest or k > newest[unit][0]:
            newest[unit] = (k, ptr)
    return [{"unit": u, "n": int(p.get("repeat") or 0), "status": p.get("status"), "rev": p.get("brief_rev")}
            for u, (_, p) in sorted(newest.items()) if int(p.get("repeat") or 0) >= REPEATS]
