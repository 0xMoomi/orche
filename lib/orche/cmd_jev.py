"""`orche jev enable|disable` and the offline evaluation harness `orche jev eval extract|run|report`.

SPEC §7.3 rule 5: Jev is validated offline first — masked samples from local transcripts, labels
from a separate Claude session, accuracy and calibration per placement — before hooks rely on it.
Files live in ORCHE_HOME/jev-eval/: samples.jsonl, labels.jsonl, results.jsonl.
"""
import datetime as _dt
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

from . import cli, config, jev, jev_setup, jevkey, mask, metrics, paths, registry, units, util
from .janitor import discover

EXIT_OK, EXIT_USER = 0, 1
PLACEMENTS = ("drain", "routine", "intake")
KIND_PLACEMENTS = {"prompt": ("drain", "routine", "intake"), "report": ("drain",)}
# Placeholder pointer fields: a drain sample is asked exactly as the hook asks about a pointer.
EVAL_POINTER = {"unit": "sample", "from": "worker", "status": "done"}
ACT_TARGET, CONFIRM_TARGET = 0.9, 0.75
_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.DOTALL)
_RESULT = re.compile(r"<result>(.*?)(?:</result>|\Z)", re.DOTALL)
LABEL_HELP = (
    "Labels come from a separate Claude session (Opus 5.5) run over jev-eval/samples.jsonl that writes "
    "jev-eval/labels.jsonl, one JSON object per line: "
    '{"id": <sample id>, "placement": "drain"|"routine"|"intake", "label": <answer>}. '
    "drain: true/false (main must act now); routine: one of "
    + ", ".join(jev.ROUTINE_KINDS)
    + '; intake: {"needs_plan": bool, "irreversible": bool, "urgency": 1-5} (any subset is scored).')


def eval_dir() -> Path:
    return paths.orche_home() / "jev-eval"


def _write_jsonl(path: Path, rows) -> None:
    util.atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), mode=0o600)


def _read_jsonl(path: Path) -> list:
    return [r for r in metrics._lines(path)] if path.exists() else []


# ---- extract ---------------------------------------------------------------

def _sample(kind, text, ts, path):
    masked = mask.redact(jev.strip_code(text))[0]
    return {"id": hashlib.sha1(masked.encode("utf-8")).hexdigest(), "kind": kind, "text": masked,
            "at": ts.isoformat(), "source": path.name}


def main_slugs(reg, project=None):
    """Claude project-dir names of registered projects' MAIN checkouts (unit worktree slugs excluded)."""
    mains, worktrees = set(), set()
    for key, proj in (reg.get("projects") or {}).items():
        if project and key != project:
            continue
        if proj.get("path"):
            mains.add(discover.claude_slug(Path(proj["path"]).expanduser()))
        for div in (proj.get("divisions") or {}).values():
            for entry in [div] + list(div.get("features") or []):
                wt = units.worktree_path(entry.get("worktree") or "")
                if wt:
                    worktrees.add(discover.claude_slug(Path(wt).expanduser()))
    return mains - worktrees


def extract(root=None, days=60, n_prompts=150, n_reports=100, now=None, slugs=None) -> list:
    """Masked samples from main-session transcripts: newest first, de-duplicated by id.

    `slugs`: only transcripts in these project dirs directly under `root` (None = every dir).
    """
    root = Path(root or metrics.default_root()).expanduser()
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=days)
    found = {"prompt": [], "report": []}
    for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else []:
        if metrics.is_subagent(path, root):
            continue
        if slugs is not None and path.relative_to(root).parts[0] not in slugs:
            continue
        for obj in metrics._lines(path):
            if obj.get("type") != "user" or obj.get("isMeta"):
                continue
            ts = metrics._timestamp(obj)
            if ts is None or ts < cutoff or ts > now:
                continue
            text = metrics._user_text(obj.get("message"))
            if not text:
                continue
            if metrics.NOTIFICATION_TAG in text:
                m = _RESULT.search(text)
                body = m.group(1).strip() if m else ""
                if body:
                    found["report"].append(_sample("report", body, ts, path))
                continue
            text = _REMINDER.sub("", text).strip()
            if text:
                found["prompt"].append(_sample("prompt", text, ts, path))
    out = []
    for kind, limit in (("prompt", n_prompts), ("report", n_reports)):
        seen, rows = set(), []
        for row in sorted(found[kind], key=lambda r: r["at"], reverse=True):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
        out.extend(rows[:max(0, limit)])
    return sorted(out, key=lambda r: r["at"], reverse=True)


# ---- run -------------------------------------------------------------------

def request(sample, placement):
    """(state, questions) Jev is asked for a sample; drain goes through the hook's own builder."""
    text = sample.get("text", "")
    if placement == "drain":
        state, questions, _ptrs = jev.drain_request([{**EVAL_POINTER, "summary": text}])
        return state, questions
    return text, jev.routine_questions() if placement == "routine" else jev.intake_questions()


def _answer(placement, a):
    """(answer, confidence) scored against labels."""
    if placement == "drain":
        w = a.get("wake_now.0") or {}
        p = jev.prob(w) if w else None
        return (None if p is None else p >= 0.5), w.get("confidence")
    if placement == "routine":
        r = a.get("routine_kind") or {}
        return r.get("value"), r.get("confidence")
    ans = {}
    for key in ("needs_plan", "irreversible"):
        p = jev.prob(a.get(key))
        if p is not None:
            ans[key] = p >= 0.5
    if "urgency" in a:  # a Score lands between levels; labels are whole levels
        value = a["urgency"].get("value")
        ans["urgency"] = round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    return ans, min(v["confidence"] for v in a.values())


def run(samples, client, placement=None) -> list:
    rows = []
    for s in samples:
        for pl in KIND_PLACEMENTS.get(s.get("kind"), ()):
            if placement and pl != placement:
                continue
            started = time.monotonic()
            a = client.ask(*request(s, pl))
            ms = int((time.monotonic() - started) * 1000)
            answer, conf = _answer(pl, a) if a else (None, None)
            rows.append({"id": s.get("id"), "placement": pl, "model": client.model, "answer": answer,
                         "confidence": conf, "ms": ms})
    return rows


# ---- report ----------------------------------------------------------------

def _correct(answer, label) -> bool:
    if isinstance(label, dict):
        return isinstance(answer, dict) and bool(label) and all(answer.get(k) == v for k, v in label.items())
    return answer == label


def _bin(c) -> int:
    return min(9, max(0, math.floor(float(c) * 10 + 1e-9)))


def _threshold(pairs, target):
    """Lowest 0.1 bin edge whose cumulative accuracy (all rows with confidence >= edge) >= target."""
    for i in range(10):
        edge = round(i / 10, 1)
        sel = [ok for c, ok in pairs if c >= edge - 1e-9]
        if sel and sum(sel) / len(sel) >= target:
            return edge
    return None


def report(results, labels) -> dict:
    """{"models": {model: {placement: stats}}} — thresholds are per model version (SPEC §7.3.2)."""
    lab = {(r.get("id"), r.get("placement")): r.get("label") for r in labels if "label" in r}
    models = sorted({str(r.get("model") or "unknown") for r in results})
    return {"models": {m: _placements([r for r in results if str(r.get("model") or "unknown") == m], lab)
                       for m in models}}


def _placements(results, lab) -> dict:
    out = {}
    for pl in PLACEMENTS:
        rows = [r for r in results if r.get("placement") == pl]
        if not rows:
            continue
        pairs, unanswered, unlabeled = [], 0, 0
        for r in rows:
            key = (r.get("id"), pl)
            if key not in lab:
                unlabeled += 1
            elif r.get("answer") is None or not isinstance(r.get("confidence"), (int, float)):
                unanswered += 1
            else:
                pairs.append((float(r["confidence"]), _correct(r["answer"], lab[key])))
        bins = []
        for i in range(10):
            sel = [ok for c, ok in pairs if _bin(c) == i]
            bins.append({"bin": f"{i / 10:.1f}-{(i + 1) / 10:.1f}", "n": len(sel),
                         "accuracy": round(sum(sel) / len(sel), 4) if sel else None})
        ms = sorted(r["ms"] for r in rows if isinstance(r.get("ms"), int))
        out[pl] = {
            "n": len(pairs), "unanswered": unanswered, "unlabeled": unlabeled,
            "accuracy": round(sum(ok for _c, ok in pairs) / len(pairs), 4) if pairs else None,
            "calibration": bins,
            "thresholds": {"act": _threshold(pairs, ACT_TARGET), "confirm": _threshold(pairs, CONFIRM_TARGET)},
            "p50_ms": ms[len(ms) // 2] if ms else None,
        }
    return out


def _render(rep) -> list:
    lines = []
    for model, placements in rep["models"].items():
        lines.append(f"model {model}")
        lines.extend(_render_model(placements))
    return lines or ["no results"]


def _render_model(placements) -> list:
    lines = []
    for pl, r in placements.items():
        acc = "-" if r["accuracy"] is None else f"{r['accuracy']:.3f}"
        lines.append(f"{pl}: n {r['n']} accuracy {acc} unanswered {r['unanswered']} "
                     f"unlabeled {r['unlabeled']} p50 {r['p50_ms']} ms")
        lines.append(f"{pl}: suggested act {r['thresholds']['act']} confirm {r['thresholds']['confirm']}")
        for b in r["calibration"]:
            if b["n"]:
                lines.append(f"  {b['bin']}\tn {b['n']}\taccuracy {b['accuracy']:.3f}")
    return lines


# ---- commands --------------------------------------------------------------

def cmd_extract(args):
    if args.days <= 0 or args.n_prompts < 0 or args.n_reports < 0:
        print("orche: --days must be positive and counts non-negative", file=sys.stderr)
        return EXIT_USER
    reg = registry.load()
    if args.project and args.project not in reg["projects"]:
        print(f"orche: unknown project {args.project!r}", file=sys.stderr)
        return EXIT_USER
    slugs = main_slugs(reg, args.project) if reg["projects"] else None
    if slugs is None:
        print("orche: warning: registry has no projects — sampling every transcript under the root",
              file=sys.stderr)
    rows = extract(args.root, args.days, args.n_prompts, args.n_reports, slugs=slugs)
    target = eval_dir() / "samples.jsonl"
    counts = {k: sum(1 for r in rows if r["kind"] == k) for k in ("prompt", "report")}
    if not args.dry_run:
        _write_jsonl(target, rows)
    verb = "would write" if args.dry_run else "wrote"
    cli.out(args, {"path": str(target), "dry_run": bool(args.dry_run), **counts},
          [f"{verb} {len(rows)} samples ({counts['prompt']} prompts, {counts['report']} reports) to {target}"])
    return EXIT_OK


def cmd_run(args):
    c = jev.client(require_enabled=False)  # offline eval runs before the hooks are enabled
    if c is None:
        print("orche: Jev is unconfigured — run `orche jev setup` (or set jev.base_url and store the key "
              "with `orche jev key set`; the env var named by jev.api_key_env, default JEV_API_KEY, "
              "also works)", file=sys.stderr)
        return EXIT_USER
    samples = _read_jsonl(eval_dir() / "samples.jsonl")
    if not samples:
        print("orche: no samples — run `orche jev eval extract` first", file=sys.stderr)
        return EXIT_USER
    target = eval_dir() / "results.jsonl"
    if args.dry_run:
        calls = sum(1 for s in samples for pl in KIND_PLACEMENTS.get(s.get("kind"), ())
                    if not args.placement or pl == args.placement)
        cli.out(args, {"dry_run": True, "calls": calls, "path": str(target)},
              [f"would make {calls} Jev calls and write {target}"])
        return EXIT_OK
    rows = run(samples, c, args.placement)
    _write_jsonl(target, rows)
    failed = sum(1 for r in rows if r["answer"] is None)
    cli.out(args, {"path": str(target), "rows": len(rows), "failed": failed},
          [f"wrote {len(rows)} results ({failed} failed) to {target}"])
    return EXIT_OK


def cmd_report(args):
    results = _read_jsonl(eval_dir() / "results.jsonl")
    if not results:
        print("orche: no results — run `orche jev eval run` first", file=sys.stderr)
        return EXIT_USER
    rep = report(results, _read_jsonl(eval_dir() / "labels.jsonl"))
    cli.out(args, rep, _render(rep))
    return EXIT_OK


def _set_enabled(args, value):
    path = config.path()
    try:
        raw = util.read_json(path, default={}) or {}
    except util.JsonError as exc:
        print(f"orche: cannot read config: {exc}", file=sys.stderr)
        return EXIT_USER
    raw.setdefault("jev", {})
    if not isinstance(raw["jev"], dict):
        raw["jev"] = {}
    raw["jev"]["enabled"] = value
    if not args.dry_run:
        config.save(raw)
        if not value:
            jev_setup.forget_record()  # disabled: doctor shows "disabled", not an old failed probe
    word = "true" if value else "false"
    lines = [f"{'would set' if args.dry_run else 'set'} jev.enabled={word}"]
    if value and jev.client() is None and not args.dry_run:
        lines.append("note: Jev is unconfigured (no jev.base_url or no key; run `orche jev setup`); calls stay off")
    cli.out(args, {"enabled": value, "dry_run": bool(args.dry_run)}, lines)
    return EXIT_OK


# ---- setup, probe, key -----------------------------------------------------

def _stdin_key():
    return jev_setup.stdin_line()


def _flow_out(args, res):
    data = {k: v for k, v in res.items() if k != "lines"}
    cli.out(args, data, res["lines"])
    return EXIT_OK if res["status"] in ("on", "skipped") else EXIT_USER


def cmd_setup(args):
    if args.dry_run:
        cli.out(args, {"dry_run": True}, ["would ask for a Jev key, store it, probe once and enable Jev on success"])
        return EXIT_OK
    problem = jev_setup.config_problem()
    if problem:  # before any question or stdin read
        print(jev_setup.unreadable_line(problem), file=sys.stderr)
        return EXIT_USER
    if args.key_stdin:
        key = _stdin_key()
        if not key:
            print("orche: no key on stdin", file=sys.stderr)
            return EXIT_USER
        return _flow_out(args, jev_setup.configure(key, args.kind or "gateway"))
    if not jev_setup.interactive():  # a pipe is never read without --key-stdin: skip, nothing changed
        res = jev_setup.skipped()
        res["lines"] = ["Jev setup skipped: no terminal (use --key-stdin [--kind gateway|direct] from a "
                        "trusted script)", *res["lines"]]
        return _flow_out(args, res)
    return _flow_out(args, jev_setup.prompt_flow(dialog=not args.no_dialog, kind=args.kind))


def cmd_probe(args):
    if args.dry_run:
        cli.out(args, {"dry_run": True}, ["would send one Choice question to the configured Jev endpoint"])
        return EXIT_OK
    res = jev_setup.probe_configured()
    if not res["configured"]:
        cli.out(args, res, [f"jev {jev_setup.NOT_CONFIGURED}: run `orche jev setup`"])
        return EXIT_USER
    if res["ok"]:
        lines = [f"Jev answered ({res['kind']}, {res['ms'] / 1000:.1f} s, key: {res['key']})"]
        if not res["enabled"]:
            lines.append("Jev is off: turn it on with `orche jev enable`")
        cli.out(args, res, lines)
        return EXIT_OK
    cli.out(args, res, [f"Jev probe failed: {res['error']}", jev_setup.OFF_LINE if not res["enabled"] else
                      "Jev stays enabled; its calls fail open (triage falls back to Haiku)"])
    return EXIT_USER


def _key_error(exc):
    print(f"orche: {exc}", file=sys.stderr)
    return EXIT_USER


def cmd_key_set(args):
    if args.dry_run:
        cli.out(args, {"dry_run": True, "backend": jevkey.backend()},
              [f"would store the Jev key in the {jevkey.backend()}"])
        return EXIT_OK
    try:
        key = _stdin_key() if args.stdin else jev_setup.read_key("Jev API key: ", dialog=not args.no_dialog)
    except jev_setup.NoTerminal:
        print(f"orche: {jev_setup.NO_TERMINAL}", file=sys.stderr)
        return EXIT_USER
    if not key:
        print("orche: no key stored", file=sys.stderr)
        return EXIT_USER
    try:
        backend = jevkey.set_key(key)
    except jevkey.KeyStoreError as exc:
        return _key_error(jevkey.scrub(str(exc), key))
    jev_setup.forget_record()
    cli.out(args, {"stored": True, "backend": backend}, [jev_setup.stored_line(backend)])
    return EXIT_OK


def cmd_key_rm(args):
    if args.dry_run:
        cli.out(args, {"dry_run": True, "backend": jevkey.backend()},
              [f"would remove the Jev key from the {jevkey.backend()}"])
        return EXIT_OK
    try:
        removed = jevkey.remove()
    except jevkey.KeyStoreError as exc:
        return _key_error(exc)
    jev_setup.forget_record()
    cli.out(args, {"removed": removed, "backend": jevkey.backend()},
          [f"Jev key removed from the {jevkey.backend()}" if removed else f"no Jev key in the {jevkey.backend()}"])
    return EXIT_OK


def cmd_key_status(args):
    env_name = config.load()["jev"].get("api_key_env") or jev.DEFAULT_KEY_ENV
    src, err = jevkey.source(env_name)
    data = {"source": src, "backend": jevkey.backend(), "env": env_name, "error": err}
    if err:
        cli.out(args, data, [f"Jev key: unknown ({err})"])
        return EXIT_USER
    cli.out(args, data, [f"Jev key: {src}" if src else f"Jev key: none (set one with `orche jev key set`)"])
    return EXIT_OK


def register(subparsers):
    p = subparsers.add_parser("jev", help="Jev typed-decision gate: setup, key, probe, enable/disable, "
                                          "offline evaluation")
    sub = p.add_subparsers(dest="jev_command", metavar="<jev-command>")
    st = sub.add_parser("setup", help="ask for a Jev key, store it, probe once, enable Jev on success")
    st.add_argument("--key-stdin", action="store_true", help="read the key from stdin instead of prompting")
    st.add_argument("--kind", choices=tuple(jev.KINDS), default=None,
                    help="gateway (Vercel AI Gateway, default) or direct (api.typesafe.ai)")
    st.add_argument("--no-dialog", action="store_true", help="hidden terminal prompt instead of the macOS dialog")
    st.set_defaults(func=cmd_setup)
    sub.add_parser("probe", help="one tiny live call to the configured Jev endpoint").set_defaults(func=cmd_probe)
    kp = sub.add_parser("key", help="store, remove or locate the Jev API key (never prints it)")
    ksub = kp.add_subparsers(dest="key_command", metavar="<key-command>")
    ks = ksub.add_parser("set", help="store the key (macOS keychain, else ORCHE_HOME/secrets/jev.key)")
    ks.add_argument("--stdin", action="store_true", help="read the key from stdin (scripts) instead of a prompt")
    ks.add_argument("--no-dialog", action="store_true", help="hidden terminal prompt instead of the macOS dialog")
    ks.set_defaults(func=cmd_key_set)
    ksub.add_parser("rm", help="remove the stored key").set_defaults(func=cmd_key_rm)
    ksub.add_parser("status", help="where the key comes from: env, keychain, file or none").set_defaults(
        func=cmd_key_status)
    sub.add_parser("enable", help="set jev.enabled=true in config").set_defaults(func=lambda a: _set_enabled(a, True))
    sub.add_parser("disable", help="set jev.enabled=false in config").set_defaults(
        func=lambda a: _set_enabled(a, False))
    ev = sub.add_parser("eval", help="offline evaluation: extract, run, report", description=LABEL_HELP)
    esub = ev.add_subparsers(dest="eval_command", metavar="<step>")
    ex = esub.add_parser("extract", help="write masked samples from local transcripts to jev-eval/samples.jsonl")
    ex.add_argument("--days", type=int, default=60, help="window in days (default 60)")
    ex.add_argument("--n-prompts", type=int, default=150, help="max user prompts (default 150)")
    ex.add_argument("--n-reports", type=int, default=100, help="max report texts (default 100)")
    ex.add_argument("--root", default=None, help="transcript root (default ~/.claude/projects)")
    ex.add_argument("--project", default=None, metavar="KEY", help="only this registered project's main session")
    ex.set_defaults(func=cmd_extract)
    rn = esub.add_parser("run", help="call Jev on every sample, write jev-eval/results.jsonl")
    rn.add_argument("--placement", choices=PLACEMENTS, default=None, help="only this placement")
    rn.set_defaults(func=cmd_run)
    esub.add_parser("report", help="accuracy, calibration and suggested thresholds per placement").set_defaults(
        func=cmd_report)
