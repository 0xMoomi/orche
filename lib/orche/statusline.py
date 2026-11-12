"""Per-project visibility counts and the compact status-line segment (registry + file counts only).

No orca calls, no transcript read, no subprocess here: `orche statusline` must stay under 50 ms. The
status line itself only prints the cached ORCHE_HOME/status/<proj>.line (templates/statusline.sh):
its first line is the segment, with its ANSI colours (the one place they are added is segment(); the
plain readers, `orche statusline --json` and the tests, strip them with plain()); a second line
`spent <YYYY-MM-DD> <usd|n/a>` keeps the day's spend (project.spent_today) for the refreshes that do not
read transcripts, and is dropped the next day.

The words are the owner's language (templates/statusline/labels.<lang>.json, read once per process):
the one word cached in ORCHE_HOME/status/lang, "en" when absent. The heartbeat pass, `orche init` and
`orche update` write it (learn_language: staff.language over the owner's newest prompts to every Main).
"""
import datetime as _dt
import functools
import json
import re

from . import paths, registry, roster, runs, units, util

ACTIVE, ASLEEP, ONBOARDING = "●", "○", "◐"
SHORT = {"rnd": "R&D", "research": "RS", "build": "BUILD", "gtm": "GTM", "ops": "OPS"}
NEW_WINDOW = _dt.timedelta(hours=24)
WAITING, RUNNING = "⚠", "▶"
RESET, YELLOW, GREEN_BOLD, DIM, RED = "\033[0m", "\033[33m", "\033[1;32m", "\033[2m", "\033[31m"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
LABELS = paths.PLUGIN_ROOT / "templates" / "statusline"   # labels.<lang>.json


def _json_files(pattern):
    for path in sorted(paths.orche_home().glob(pattern)):
        try:
            data = util.read_json(path)
        except (util.JsonError, OSError):
            continue
        if isinstance(data, dict):
            yield path, data


def counts(reg: dict, key: str, now=None, r=None) -> dict:
    """{awake, active_features, workers, busy, new, keys, unverified, running} for project `key` (running =
    live `orche run` lock files; workers = {division: active workers}, active_features their sum; busy =
    {division: live runs of its workers}, from the locks' `to`). `r` is the caller's Roster of `reg`, built
    here when absent."""
    now = now or units.now()
    proj = reg["projects"][key]
    workers = {name: n for name, div in (proj.get("divisions") or {}).items()
               if (n := sum(1 for f in (div or {}).get("features") or [] if f.get("status") == "active"))}
    new = 0
    for ev in reg.get("events", []):
        at = units.parse_iso(ev.get("at"))
        if ev.get("type") == "create" and ev.get("project") == key and at and now - at <= NEW_WINDOW:
            new += 1
    keys = sum(1 for _, req in _json_files("secrets/requests/*.json")
               if req.get("status") == "pending" and req.get("project", key) == key)
    r = r if r is not None else roster.Roster(reg)

    def owner(session):
        member = r.member(session)
        return member.project if member else None

    unverified = 0
    for path, ptr in _json_files("inbox/*/*.json"):
        target = owner(path.parent.name) or owner(ptr.get("to")) or owner(ptr.get("from"))
        if target == key and ptr.get("status") == "done" and ptr.get("ledger_row") is False:
            unverified += 1
    live, busy = runs.live(key), {}
    for lock in live:
        member = r.member(lock.get("to"))
        if member is not None and member.project == key and member.kind == "feat":
            busy[member.division] = busy.get(member.division, 0) + 1
    return {
        "awake": len(registry.awake_sessions({"projects": {key: proj}})),
        "active_features": sum(workers.values()),
        "workers": workers,
        "busy": busy,
        "new": new,
        "keys": keys,
        "unverified": unverified,
        "running": len(live),
    }


def custom_cells(proj: dict) -> dict:
    """{custom division: status-line name}, unique within the project and never a standard cell.

    From the label's letters, upper case (the name's when the label has none): the first 3, else 4,
    else 5 letters, whichever is still free, else the 3-letter form plus the first free digit from 2
    (`Design` → DES, then `Designers` → DESI; a second `QA` → QA2). Cells are assigned in the order
    the divisions were added, so an earlier division keeps its cell."""
    taken, cells = set(SHORT.values()), {}
    for name, spec in (proj.get("custom_divisions") or {}).items():
        label = (spec or {}).get("label") if isinstance(spec, dict) else None
        letters = "".join(c for c in (label or "") if c.isalpha()).upper() \
            or "".join(c for c in name if c.isalpha()).upper()
        cell = next((letters[:n] for n in (3, 4, 5) if letters[:n] not in taken), None)
        digit = 2
        while cell is None:
            cell = f"{letters[:3]}{digit}" if f"{letters[:3]}{digit}" not in taken else None
            digit += 1
        taken.add(cell)
        cells[name] = cell
    return cells


def division_cells(proj: dict, workers: dict) -> list:
    """`orche status`'s cells: one per division of the project (`registry.divisions_of`), with its active
    worker count from `workers` ({division: n}, `counts`); a custom division whose record is missing
    renders as asleep."""
    cells = []
    for name in registry.divisions_of(proj):
        div = proj.get("divisions", {}).get(name, {})
        glyph = {"active": ACTIVE, "onboarding": ONBOARDING}.get(div.get("status"), ASLEEP)
        cell = name + glyph
        if workers.get(name):
            cell += str(workers[name])
        cells.append(cell)
    return cells


def waiting(reg: dict, key: str, now=None) -> tuple:
    """(what waits on the owner: the open ask plus the units blocked on the owner, why a pause holds: the
    spend cap reached (its USD), "owner" for an owner pause, else None)."""
    from . import project                # light at import: the status line loads only what it uses
    from .flow import guard, owner
    n = len(project.blocked(reg, key, now)) + (1 if owner.status(key, now) else 0)
    s, p = guard.spend_status(key, now), guard.pause_status(key)
    return n, (s["cap"] if s else "owner" if p else None)


def _lang_path():
    return paths.orche_home() / "status" / "lang"


def cached_language() -> str:
    """The owner's language learn_language cached (status/lang); "en" when absent or without labels."""
    try:
        word = _lang_path().read_text(encoding="utf-8").strip()
    except OSError:
        return "en"
    return word if word.isalpha() and (LABELS / f"labels.{word}.json").is_file() else "en"


def learn_language(reg: dict, keys=None) -> None:
    """Cache the owner's language (staff.language over the newest exchanges of the Mains of `keys`,
    default every project) in status/lang. It reads transcripts (or the macOS locale): the heartbeat
    pass, `orche init` and `orche update` call it, never the status line. Best effort, never raises."""
    from . import staff                  # not at import: the status line stays light
    try:
        keys = sorted(reg.get("projects") or {}) if keys is None else keys
        rows = [x for key in keys for x in staff.recent(reg, key, limit=staff.PER_PROJECT)]
        util.atomic_write(_lang_path(), staff.language(rows) + "\n")
    except Exception:  # noqa: BLE001 - the status line never fails its caller
        pass


@functools.lru_cache(maxsize=None)
def labels(lang: str) -> dict:
    """The status-line words in `lang` (templates/statusline/labels.<lang>.json), read once."""
    with open(LABELS / f"labels.{lang}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _paint(colour, text) -> str:
    return f"{colour}{text}{RESET}"


def plain(line: str) -> str:
    """`line` without its ANSI colours."""
    return _ANSI.sub("", line)


def segment(reg: dict, key: str, now=None, r=None, spent=None) -> str:
    """The status line in the owner's language (cached_language), zero cells omitted:
    `⚠ waiting 1 · ▶ running 3 (oldest 22m/30m) · ⚠ stalled 1 · BUILD 3/8 · RS ● · R&D ○ GTM ○ DES ○ · 🔑1 ·
    $10.75 today`: what waits on the owner (yellow), the live runs with the oldest one's elapsed time against
    its TIMEBOX (green bold), the headless runs without a stream event for progress.STALLED_S (yellow;
    runs/progress.py), one cell per division with active workers (`<cell>
    running/assigned`: its workers' live runs / its active workers), then the awake divisions without
    workers (`●`, `◐` onboarding), the asleep ones dimmed, pending secret requests and today's spend
    (`spent`: USD, "n/a" when unreadable, None when not read today). While a spend cap or a pause holds,
    the whole line is red: `PAUSED <why> · say "resume" in Main`."""
    words = labels(cached_language())
    n, held = waiting(reg, key, now)
    if held is not None:
        why = words["owner"] if held == "owner" else words["cap"].format(cap=f"{held:g}")
        return _paint(RED, words["paused"].format(why=why))
    c, proj = counts(reg, key, now, r), reg["projects"][key]
    names = {**custom_cells(proj), **SHORT}
    work, awake, asleep = [], [], []
    for name in registry.divisions_of(proj):
        cell, status = names.get(name, name), (proj.get("divisions", {}).get(name) or {}).get("status")
        if c["workers"].get(name):
            work.append(f"{cell} {c['busy'].get(name, 0)}/{c['workers'][name]}")
        elif status in ("active", "onboarding"):
            awake.append(f"{cell} {ACTIVE if status == 'active' else ONBOARDING}")
        else:
            asleep.append(f"{cell} {ASLEEP}")
    parts = ([_paint(YELLOW, f"{WAITING} {words['waiting'].format(n=n)}")] if n else []) \
        + _running(words, key, c["running"], now) + work + ([" ".join(awake)] if awake else []) + ([_paint(DIM, " ".join(asleep))] if asleep else []) \
        + ([f"🔑{c['keys']}"] if c["keys"] else [])
    if spent == "n/a":
        parts.append(words["spend_na"])
    elif spent:
        parts.append(words["today"].format(usd=f"{spent:.2f}"))
    return " · ".join(parts)


def _running(words, key, n, now) -> list:
    """The live runs' cells: `▶ running 3 (oldest 22m/30m)` and, when any is stalled, `⚠ stalled 1`."""
    if not n:
        return []
    from .runs import progress
    views = progress.views(key, now)
    oldest = next((v for v in views if v["elapsed_s"] is not None and v["timebox_s"]), None)
    text = (words["running_oldest"].format(n=n, age=progress.minutes(oldest["elapsed_s"]),
                                           box=progress.minutes(oldest["timebox_s"]))
            if oldest is not None else words["running"].format(n=n))
    stalled = sum(1 for v in views if v["stalled"])
    return ([_paint(GREEN_BOLD, f"{RUNNING} {text}")]
            + ([_paint(YELLOW, f"{WAITING} {words['stalled'].format(n=stalled)}")] if stalled else []))


def _path(key):
    return paths.orche_home() / "status" / f"{key}.line"


def _today(now=None) -> str:
    return (now or units.now()).date().isoformat()


def kept_spend(key, now=None):
    """Today's spend the cached line keeps (float, or "n/a"), else None (none, or another day's)."""
    try:
        lines = _path(key).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    word, day, value = (lines[1].split() + ["", "", ""])[:3] if len(lines) > 1 else ("", "", "")
    if word != "spent" or day != _today(now):
        return None
    try:
        return "n/a" if value == "n/a" else float(value)
    except ValueError:
        return None


def _write(reg, key, now=None, r=None, spent=False) -> str:
    """Render and cache the segment; `spent` (a number or None for unreadable) replaces the kept spend."""
    spent = kept_spend(key, now) if spent is False else ("n/a" if spent is None else spent)
    line = segment(reg, key, now, r, spent)
    tail = f"spent {_today(now)} {spent if spent == 'n/a' else f'{spent:.6f}'}\n" if spent is not None else ""
    try:
        util.atomic_write(_path(key), line + "\n" + tail)
    except OSError:
        pass
    return line


def refresh(reg: dict, key: str, spent=False) -> None:
    """Rewrite ORCHE_HOME/status/<proj>.line after a change (best effort, never raises); `spent`, when
    given, is today's spend (project.spent_today: a number, None when unreadable).

    The one helper every state change uses: init, sleep/wake (also feat and `hr audit --apply`),
    feat new/retire, stage set, a report to Main, the owner's ask, flow pause/resume, `orche status`
    and the heartbeat (which reads the transcripts: the spend is at most one pass old).
    """
    try:
        _write(reg, key, spent=spent)
    except Exception:  # noqa: BLE001 - the status line never fails its caller
        pass


def touch(key: str) -> None:
    """refresh() for a caller without the registry at hand (best effort, never raises)."""
    try:
        reg = registry.load()
        if key in reg.get("projects", {}):
            _write(reg, key)
    except Exception:  # noqa: BLE001 - the status line never fails its caller
        pass


def run(cwd, as_json=False) -> int:
    """Resolve the project from `cwd`, write status/<proj>.line, print it. Silent (exit 0) when unresolved."""
    try:
        reg = registry.load()
    except registry.RegistryError:
        return 0
    r = roster.Roster(reg)
    key = r.place(cwd).project
    if key is None:
        return 0
    line = _write(reg, key, r=r)
    print(json.dumps({"project": key, "line": plain(line)}, ensure_ascii=False) if as_json else line)
    return 0
