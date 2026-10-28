"""`orche triage enable|disable|status` and `orche triage eval <file>` (lib/orche/triage.py).

status: whether triage is on, the backend it resolves to, classifier latency p50/p95, the last eval
(ORCHE_HOME/triage/eval.json: when, which labelled file and whether it still exists, the scores; a
warning when triage is enabled on Jev with no Jev eval) and the last 20 decisions from
ORCHE_HOME/triage/samples.jsonl.
eval: labelled prompts (JSONL, one `{"prompt": "...", "label": "direct"|"main"}` per line) run through
stages 1-4 (no transcript, no relay) -> precision/recall of `direct`, in the shape of
`orche jev eval report` (`{"models": {<backend>: {"direct": {...}}}}`), and the summary (no prompts)
is recorded in eval.json per backend. `--backend heuristics` scores the no-model stages alone (every
candidate counts as direct); `--dry-run` only counts classifier calls and records nothing.
"""
import copy
import json
import sys
import time
from pathlib import Path

from . import cli, config, registry, triage, util

EXIT_OK, EXIT_USER = 0, 1
EVAL_BACKENDS = ("auto", "jev", "claude-haiku", "heuristics")


# ---- enable / disable ------------------------------------------------------

def _set_enabled(args, value):
    path = config.path()
    try:
        raw = util.read_json(path, default={}) or {}
    except util.JsonError as exc:
        print(f"orche: cannot read config: {exc}", file=sys.stderr)
        return EXIT_USER
    if not isinstance(raw.get("triage"), dict):
        raw["triage"] = {}
    raw["triage"]["enabled"] = value
    if not args.dry_run:
        config.save(raw)
    lines = [f"{'would set' if args.dry_run else 'set'} triage.enabled={'true' if value else 'false'}"]
    if value and config.load().get("delivery", "runner") != "runner":
        lines.append("note: delivery is not `runner`; triage routes only through the runner, so it stays inert")
    if value:
        cfg = config.load()
        resolved = triage.backend(cfg)[0]
        warning = triage.eval_warning({"enabled": True, "resolved_backend": resolved,
                                       "eval": triage.eval_status(resolved)})
        if warning:
            lines.append(warning)
    cli.out(args, {"enabled": value, "dry_run": bool(args.dry_run)}, lines)
    return EXIT_OK


# ---- status ----------------------------------------------------------------

def eval_lines(d) -> list:
    """`eval: …` lines for `orche triage status` (and the warning `orche status` repeats)."""
    e = d["eval"]
    if e["last_run"] is None:
        lines = ["eval: never run (no labelled eval set on file; `orche triage eval <file>`)"]
    else:
        r = e["last_run"]
        fmt = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "-"  # noqa: E731
        lines = [f"eval: last run {r.get('at', '?')} ({e['last_backend']}, n {r.get('n', '?')}, precision "
                 f"{fmt(r.get('precision'))} recall {fmt(r.get('recall'))}) on {e['set_file'] or '?'} "
                 f"({'present' if e['set_exists'] else 'missing'})"]
        if not e["resolved_evaluated"]:
            lines.append(f"eval: none on file for the {d['resolved_backend']} backend")
    warning = triage.eval_warning(d)
    return lines + ([warning] if warning else [])


def _status_lines(d) -> list:
    lines = [f"triage: {'enabled' if d['enabled'] else 'disabled'}",
             f"backend: {d['backend']} -> {d['resolved_backend']} (min_confidence {d['min_confidence']}, "
             f"max_latency_s {d['max_latency_s']}, keep_prompts {'true' if d['keep_prompts'] else 'false'})",
             f"latency: {d['classifier_calls']} classifier calls, p50 "
             f"{'-' if d['p50_ms'] is None else str(d['p50_ms']) + ' ms'}, p95 "
             f"{'-' if d['p95_ms'] is None else str(d['p95_ms']) + ' ms'}",
             *eval_lines(d),
             f"last {len(d['last'])} of {d['samples']} decisions:" if d["last"] else "no decisions yet"]
    for r in d["last"]:
        conf = "-" if r["confidence"] is None else f"{float(r['confidence']):.2f}"
        lines.append(f"  {r['at']}  {r['decision']:<6} stage {r['stage']}  conf {conf}  {r['backend']}  "
                     f"{r['ms']} ms  {r['unit'] or r['reason'] or '-'}")
    return lines


def cmd_status(args):
    data = triage.status_data()
    cli.out(args, data, _status_lines(data))
    return EXIT_OK


# ---- eval ------------------------------------------------------------------

def read_labelled(path) -> list:
    """[(prompt, is_direct)] from a JSONL file; ValueError naming the first bad line."""
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"line {n}: not JSON ({exc})") from None
        if not isinstance(row, dict) or not isinstance(row.get("prompt"), str) or row.get("label") not in triage.ROUTES:
            raise ValueError(f'line {n}: want {{"prompt": str, "label": "direct"|"main"}}')
        out.append((row["prompt"], row["label"] == "direct"))
    return out


def _heuristic_classifier(prompt, divisions, cfg):
    """Stage 3 alone: every candidate is direct (for scoring the no-model stages)."""
    division = "build" if "build" in divisions else (divisions[0] if divisions else "build")
    return ({"route": "direct", "division": division, "tier": "standard", "confidence": 1.0, "goal": "",
             "accept": "", "verify": "", "scope": []}, "heuristics", 0.0, None)


def evaluate(rows, cfg, divisions, classifier=None) -> list:
    out = []
    for prompt, is_direct in rows:
        started = time.monotonic()
        d = triage.decide(prompt, divisions, cfg, None, classifier)
        out.append({"label": is_direct, "direct": d.decision == "direct", "decision": d.decision,
                    "stage": d.stage, "backend": d.backend, "ms": int((time.monotonic() - started) * 1000),
                    "model_ms": int(d.model_s * 1000), "called": d.backend != "none"})
    return out


def report(results, model) -> dict:
    tp = sum(1 for r in results if r["direct"] and r["label"])
    fp = sum(1 for r in results if r["direct"] and not r["label"])
    fn = sum(1 for r in results if not r["direct"] and r["label"])
    tn = len(results) - tp - fp - fn
    calls = [r["model_ms"] for r in results if r["called"]]
    return {"models": {model: {"direct": {
        "n": len(results), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "classifier_calls": len(calls), "p50_ms": triage.percentile(calls, 0.5)}}}}


def _report_lines(rep) -> list:
    lines = []
    for model, placements in rep["models"].items():
        lines.append(f"model {model}")
        for name, r in placements.items():
            fmt = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
            lines.append(f"{name}: n {r['n']} precision {fmt(r['precision'])} recall {fmt(r['recall'])} "
                         f"tp {r['tp']} fp {r['fp']} fn {r['fn']} tn {r['tn']} "
                         f"classifier calls {r['classifier_calls']} p50 {r['p50_ms']} ms")
    return lines


def cmd_eval(args):
    try:
        rows = read_labelled(args.file)
    except (OSError, ValueError) as exc:
        print(f"orche: cannot read {args.file}: {exc}", file=sys.stderr)
        return EXIT_USER
    if not rows:
        print(f"orche: no labelled prompts in {args.file}", file=sys.stderr)
        return EXIT_USER
    cfg = copy.deepcopy(config.load())
    divisions = list(registry.DIVISIONS)
    if args.project:
        reg = registry.load()
        if args.project not in reg["projects"]:
            print(f"orche: unknown project {args.project!r}", file=sys.stderr)
            return EXIT_USER
        divisions = triage.project_divisions(reg, args.project)
    classifier = None
    if args.backend == "heuristics":
        classifier, model = _heuristic_classifier, "heuristics"
    else:
        if args.backend:
            cfg.setdefault("triage", {})["backend"] = args.backend
        model, client = triage.backend(cfg)
        if model == "jev" and client is None:
            print("orche: the jev backend is unconfigured (jev.base_url or the key env var is missing)",
                  file=sys.stderr)
            return EXIT_USER
    if args.dry_run:
        calls = []

        def count(prompt, _divisions, _cfg):
            calls.append(prompt)
            return None, model, 0.0, None
        for prompt, _label in rows:
            triage.decide(prompt, divisions, cfg, None, count)
        cli.out(args, {"dry_run": True, "prompts": len(rows), "classifier_calls": len(calls), "backend": model},
              [f"would make {len(calls)} {model} classifier calls for {len(rows)} labelled prompts"])
        return EXIT_OK
    rep = report(evaluate(rows, cfg, divisions, classifier), model)
    try:
        triage.save_eval(args.file, model, rep)
    except OSError as exc:
        print(f"orche: cannot record the eval in {triage.eval_path()}: {exc}", file=sys.stderr)
    cli.out(args, rep, _report_lines(rep))
    return EXIT_OK


def register(subparsers):
    p = subparsers.add_parser("triage", help="triage of Main prompts: enable/disable, status, offline eval")
    sub = p.add_subparsers(dest="triage_command", metavar="<triage-command>")
    sub.add_parser("enable", help="set triage.enabled=true in config").set_defaults(
        func=lambda a: _set_enabled(a, True))
    sub.add_parser("disable", help="set triage.enabled=false in config").set_defaults(
        func=lambda a: _set_enabled(a, False))
    sub.add_parser("status", help="backend, classifier latency p50/p95, the last 20 decisions and the "
                                  "eval on file").set_defaults(
        func=cmd_status)
    ev = sub.add_parser("eval", help="precision/recall of `direct` on labelled prompts (JSONL)")
    ev.add_argument("file", help='JSONL: {"prompt": "...", "label": "direct"|"main"} per line')
    ev.add_argument("--backend", choices=EVAL_BACKENDS, default=None,
                    help="override triage.backend for this eval; heuristics = the no-model stages only")
    ev.add_argument("--project", default=None, metavar="KEY", help="use this project's divisions (default all)")
    ev.set_defaults(func=cmd_eval)
