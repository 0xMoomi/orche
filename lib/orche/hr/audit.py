"""`orche hr audit`: idle units, awake count, scope overlaps, weekly metrics, repeated corrections.

Writes `ORCHE_HOME/hr-report-<YYYY-MM-DD>.md` (same-day runs overwrite). `--apply` only sleeps:
idle active units without the HANDOFF freshness check, then, while awake > max_awake_sessions,
the oldest-idle extras with it. Never wakes, never retires.

Idle = days since max(registry last_activity, mtime of ORCHE_HOME/activity/<session>); the touch
file is updated by the Stop/UserPromptSubmit hooks, relay delivery, `orche report` and wake.

Repeated corrections (R-STANDING-1 escalation): a STANDING.md line added twice or more in the
last 7 days (git history plus uncommitted additions; duplicate lines when not a git checkout),
and DECISIONS.tsv rows of the last 7 days whose decision/why contains `rev `.

Mistake classes (hr/mistakes.py): per project, what head reviews, the merge queue and reverts sent back
twice in the last `days` days, with the proposed enforcement level; `--apply` also writes each project's
Enforcement table in STANDING.md (the rules map of SPEC §5.10). `orche hr audit <project>` is that part
alone, for one project (`run_mistakes`), and sleeps nothing.
"""
import datetime as _dt
import re
from collections import Counter
from pathlib import Path

from .. import janitor, log, metrics, paths, registry, roster, shell, units, util
from . import book, limits, transitions

WINDOW = _dt.timedelta(days=7)
AUDIT_DAYS = 30     # the mistake classes' window (hr/mistakes.py)
_NUM = re.compile(r"^\s*\d+\.\s*")


def _norm(line) -> str:
    return _NUM.sub("", line).strip()


def _git(root, *argv):
    res = shell.git(root, *argv, timeout=30)
    return res.out if res.ok else None


def _added(diff_text) -> list:
    return [ln[1:] for ln in (diff_text or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def standing_additions(root, rel) -> list:
    """Order lines (normalised) added to STANDING.md in the last 7 days."""
    path = Path(root) / rel
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        added = lines
    else:
        added = _added(_git(root, "log", "-p", "--since=7.days", "--format=", "--", rel))
        tracked = (_git(root, "ls-files", "--", rel) or "").strip()
        if tracked:
            added += _added(_git(root, "diff", "--no-color", "HEAD", "--", rel))
        elif path.is_file():
            added += path.read_text(encoding="utf-8").splitlines()
    return [n for n in (_norm(ln) for ln in added if _NUM.match(ln)) if n]


def _decision_files(proj) -> list:
    files = [Path(proj["path"]) / ".orche" / "DECISIONS.tsv"]
    for dname, div in proj.get("divisions", {}).items():
        files.append(Path(units.worktree_path(div.get("worktree", ""))) / ".orche" / "div" / dname / "DECISIONS.tsv")
        for feat in div.get("features", []):
            files.append(Path(units.worktree_path(feat.get("worktree", ""))) / ".orche" / "feat" / feat["name"]
                         / "DECISIONS.tsv")
    return files


def rev_rows(proj, now) -> list:
    out = []
    for f in _decision_files(proj):
        try:
            rows = f.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cells = row.split("\t")
            at = units.parse_iso(cells[0].replace("Z", "+00:00")) if cells else None
            if len(cells) < 4 or at is None or now - at > WINDOW:
                continue
            if "rev " in cells[2] or "rev " in cells[3]:
                out.append({"file": str(f), "text": cells[2], "why": cells[3]})
    return out


def corrections(reg, now) -> list:
    found = []
    for key, proj in sorted(reg.get("projects", {}).items()):
        counts = Counter(standing_additions(proj["path"], proj["standing"]))
        found += [{"project": key, "source": "STANDING.md", "text": text, "count": n}
                  for text, n in sorted(counts.items()) if n >= 2]
        found += [{"project": key, "source": "DECISIONS.tsv", "text": r["text"], "why": r["why"], "count": 1}
                  for r in rev_rows(proj, now)]
    return found


def overlaps(reg) -> list:
    found = []
    for key, proj in sorted(reg.get("projects", {}).items()):
        feats = [{**f, "name": f"{name}/{f['name']}"} for name, div in (proj.get("divisions") or {}).items()
                 for f in (div or {}).get("features") or [] if f.get("status") in ("active", "paused")]
        for i, feat in enumerate(feats):
            for other, theirs, mine in limits.overlaps(feat["scope"], feats[i + 1:]):
                found.append({"project": key, "a": feat["name"], "b": other, "glob_a": mine, "glob_b": theirs})
    return found


def metrics_line(now) -> str:
    try:
        root = janitor.configured_dir(janitor.settings()["claude_projects"])
        m = metrics.compute(root, days=7, now=now)
    except Exception as exc:  # noqa: BLE001 - the audit must not fail on metrics
        log.log("hr", f"metrics unavailable: {exc}")
        return "metrics (7d): unavailable"
    return (f"metrics (7d): {m['responses']} responses, ${m['cost_usd']:.2f} list price, "
            f"main wake-ups {m['wake']['user']}, routine {m['routine']['share']:.0%}")


def _active(reg, now) -> list:
    """{session, at, idle_days} of every active division and feature, in `orche depts` order: project
    keys sorted, divisions in divisions_of order, each followed by its features. `at` =
    units.last_active (max of registry last_activity and the activity file)."""
    rows = []
    for key in sorted(reg["projects"]):
        proj = reg["projects"][key]
        divisions = proj.get("divisions", {})
        for name in registry.divisions_of(proj):
            if name not in divisions:
                continue
            for entry in [divisions[name], *divisions[name].get("features", [])]:
                if entry.get("status", "dormant") == "active":
                    at = units.last_active(entry)
                    rows.append({"session": entry.get("session", ""), "at": at,
                                 "idle_days": None if at is None else max(0, (now - at).days)})
    return rows


def proposals(reg, now) -> list:
    """Sleep proposals: idle active units first, then the oldest-idle extras over the awake limit."""
    idle_limit = reg["limits"]["idle_days_to_sleep"]
    rows = _active(reg, now)
    idle = [r for r in rows if r["idle_days"] is not None and r["idle_days"] > idle_limit]
    out = [{"action": "sleep", "unit": r["session"], "idle_days": r["idle_days"], "handoff": False,
            "why": f"idle {r['idle_days']}d > {idle_limit}d"} for r in idle]
    count, limit = limits.awake(reg)
    awake = count - len(out)
    if awake > limit:
        oldest = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
        rest = sorted((r for r in rows if r not in idle),
                      key=lambda r: oldest if r["at"] is None else r["at"].replace(microsecond=0))
        for r in rest[:awake - limit]:
            out.append({"action": "sleep", "unit": r["session"], "idle_days": r["idle_days"], "handoff": True,
                        "why": f"awake {awake} > limit {limit} (oldest idle)"})
    return out


def apply(reg, props, *, dry_run) -> list:
    results = []
    limit = reg["limits"]["idle_days_to_sleep"]
    for p in props:
        unit = roster.Roster(reg).member(p["unit"])
        handoff = p["handoff"]
        if not handoff:  # re-check at apply time: skip the HANDOFF check only for a truly idle unit
            at = units.last_active(unit.entry)
            handoff = at is None or (units.now() - at).days <= limit
        try:
            _, line = transitions.sleep(reg, unit, dry_run=dry_run, require_handoff=handoff)
            results.append({"unit": p["unit"], "result": line})
        except book.Refused as exc:
            results.append({"unit": p["unit"], "error": exc.msg})
    return results


def report_path(now) -> Path:
    return paths.orche_home() / f"hr-report-{now.date().isoformat()}.md"


def render(data) -> str:
    lines = [f"# HR report: {data['date']}", "", f"Awake: {data['awake']}/{data['limit']}", "",
             data["metrics"], "", "## Proposals", ""]
    lines += [f"- sleep {p['unit']}: {p['why']}" for p in data["proposals"]] or ["- none"]
    lines += ["", "## Scope overlaps", ""]
    lines += [f"- {o['project']}: {o['a']} ({o['glob_a']}) overlaps {o['b']} ({o['glob_b']})"
              for o in data["overlaps"]] or ["- none"]
    lines += ["", "## Candidates for structural enforcement", ""]
    lines += [f"- {c['project']} {c['source']}: {c['text']}" + (f" (x{c['count']})" if c["count"] > 1 else "")
              for c in data["corrections"]] or ["- none"]
    lines += ["", f"## Mistake classes (last {data['days']} days)", ""]
    lines += [f"- {key} {c['key']} x{c['count']} ({', '.join(c['units'])}): {c['level']}"
              for key, found in sorted(data["mistakes"].items()) for c in found] or ["- none"]
    if data.get("applied"):
        lines += ["", "## Applied", ""]
        lines += [f"- {a['unit']}: {a.get('result') or 'ERROR: ' + a['error']}" for a in data["applied"]]
    return "\n".join(lines) + "\n"


def run_mistakes(reg, key, *, now=None, days=AUDIT_DAYS, do_apply=False, dry_run=False) -> dict:
    """{project, days, classes[, table]}: `key`'s mistake classes; `do_apply` writes its Enforcement table.
    mistakes.py is imported here: it reads the ledger and the queue, which orche.hr itself never loads."""
    from . import mistakes
    proj = reg["projects"][key]
    found = mistakes.classes(key, proj, now or units.now(), days)
    data = {"project": key, "days": days, "classes": found}
    if do_apply and found:
        data["table"] = mistakes.apply(key, proj, found, dry_run=dry_run)
    return data


def run(reg, *, now=None, do_apply=False, dry_run=False, days=AUDIT_DAYS) -> dict:
    now = now or units.now()
    props = proposals(reg, now)
    count, limit = limits.awake(reg)
    found = {key: run_mistakes(reg, key, now=now, days=days, do_apply=do_apply, dry_run=dry_run)
             for key in sorted(reg.get("projects", {}))}
    data = {"date": now.date().isoformat(), "awake": count,
            "limit": limit, "metrics": metrics_line(now), "proposals": props,
            "overlaps": overlaps(reg), "corrections": corrections(reg, now), "days": days,
            "mistakes": {key: m["classes"] for key, m in found.items() if m["classes"]}, "applied": []}
    if do_apply and props:
        data["applied"] = apply(reg, props, dry_run=dry_run)
    path = report_path(now)
    if not dry_run:  # --dry-run writes nothing, the report included
        util.atomic_write(path, render(data))
    data["report"] = str(path)
    return data
