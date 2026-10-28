"""Developer records, `age` share bundles, revocation, and transcript redaction.

`shares.json` holds pubkeys, IAM bindings and which NAMES went to whom; never values.
"""
import datetime as _dt
import io
import json
import os
from pathlib import Path

from .. import log, mask, paths, util
from . import SecretError, failure, store


def load() -> dict:
    try:
        data = util.read_json(store.shares_path(), default=None)
    except util.JsonError as exc:
        raise SecretError(f"{exc.path}: invalid JSON ({exc.reason})") from None
    data = data if isinstance(data, dict) else {}
    data.setdefault("devs", {})
    data.setdefault("shares", [])
    return data


def save(data) -> None:
    util.atomic_write(store.shares_path(), json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=0o600)


def find_dev(data, who):
    """(email, record) by email or by the part before `@` (case-insensitive), else (None, None)."""
    needle = str(who or "").strip().lower()
    for email, rec in data["devs"].items():
        if email.lower() == needle or email.lower().split("@", 1)[0] == needle:
            return email, rec
    return None, None


# ---------------------------------------------------------------- dev records


def record_dev(email, project, env, gcp_project, pubkey, dry_run=False) -> dict:
    data = load()
    rec = data["devs"].get(email, {})
    rec.update({"project": project, "env": env, "gcp_project": gcp_project, "added": util.now_iso()})
    if pubkey:
        rec["age_pubkey"] = pubkey
    rec.pop("removed", None)
    data["devs"][email] = rec
    if not dry_run:
        save(data)
    return rec


def mark_dev_removed(email, dry_run=False) -> None:
    data = load()
    if email in data["devs"]:
        data["devs"][email]["removed"] = util.now_iso()
        if not dry_run:
            save(data)


# ---------------------------------------------------------------- share / revoke


def share(ex, cfg, who, env, names, out_path) -> dict:
    """Encrypt the env's values for `who` with age (dotenv text on stdin) and record the names."""
    data = load()
    email, rec = find_dev(data, who)
    if not email:
        raise SecretError(f"unknown developer {who!r}; add one with `orche dev add <email> --age-pubkey`", code=2)
    pubkey = rec.get("age_pubkey")
    if not pubkey:
        raise SecretError(f"{email} has no age public key; run `orche dev add {email} --age-pubkey <key>`")
    if not names:
        raise SecretError(f"no secrets known for {cfg['project']} ({env})", code=2)
    if store.is_env_example(out_path):
        raise SecretError("refusing to write .env.example")
    values = {name: store.get(ex, cfg, name, env) for name in names}
    result = ex.run(["age", "-r", pubkey, "-o", str(out_path)], stdin=store.dotenv_text(values),
                    shown_stdin=f"<dotenv: {','.join(names)}>")
    values = None
    if not result.ok:
        raise failure(result, "age")
    entry = {"to": email, "project": cfg["project"], "env": env, "secrets": list(names),
             "at": util.now_iso(), "file": str(out_path)}
    if not ex.dry_run:
        data["shares"].append(entry)
        save(data)
        for name in names:
            store.audit("share", cfg["project"], name, env, "age", None, email)
    return entry


def received(who) -> tuple:
    """(email, [(project, name, [envs], revoke_requested)]) for every name shared with `who` whose
    revocation has not completed. `revoke_requested` is the earliest pending revoke time, or None."""
    data = load()
    email, _ = find_dev(data, who)
    if not email:
        email = str(who)
    out = {}
    for entry in data["shares"]:
        if entry.get("to") != email or entry.get("revoked"):
            continue
        done = entry.get("revoked_names") or {}
        for name in entry.get("secrets") or []:
            if name in done:
                continue
            item = out.setdefault((entry.get("project"), name), {"envs": [], "since": None})
            if entry.get("env") not in item["envs"]:
                item["envs"].append(entry.get("env"))
            since = entry.get("revoke_requested")
            if since and (item["since"] is None or since < item["since"]):
                item["since"] = since
    return email, [(p, n, v["envs"], v["since"]) for (p, n), v in sorted(out.items())]


def record_revoke(email, project, rotated, pending) -> None:
    """Mark `rotated` names revoked per share; shares still holding `pending` names get
    `revoke_requested` (first request time kept) and stay active. A share whose names are all
    rotated gets `revoked`."""
    data = load()
    now = util.now_iso()
    for entry in data["shares"]:
        if entry.get("to") != email or entry.get("project") != project or entry.get("revoked"):
            continue
        names = entry.get("secrets") or []
        done = entry.setdefault("revoked_names", {})
        for name in names:
            if name in rotated and name not in done:
                done[name] = now
        if all(name in done for name in names):
            entry["revoked"] = now
            entry.pop("revoke_requested", None)
        elif any(name in pending for name in names):
            entry.setdefault("revoke_requested", now)
    save(data)


# ---------------------------------------------------------------- transcript redaction


def redact_counts(text):
    """(redacted_text, {pattern: count}) for one decoded string, using the shared mask patterns."""
    counts = {}
    for name, rx in mask.PATTERNS:
        if name == "mnemonic":
            text, n = mask.redact_mnemonics(rx, text)
        else:
            text, n = rx.subn(mask.REDACTED, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts


def _redact_obj(obj, totals):
    """Redact every string value (keys untouched) of a decoded JSON value; sums counts into `totals`."""
    if isinstance(obj, str):
        new, counts = redact_counts(obj)
        for name, n in counts.items():
            totals[name] = totals.get(name, 0) + n
        return new
    if isinstance(obj, list):
        return [_redact_obj(v, totals) for v in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v, totals) for k, v in obj.items()}
    return obj


def _dump_record(obj) -> str:
    """Compact JSON; falls back to ASCII escapes when the record holds a lone surrogate."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    return text


def redact_jsonl(text):
    """(new_text, {pattern: count}, unparsed_lines). Each record is decoded, redacted string by string
    and re-encoded compactly; records without a match, and lines that do not parse, stay byte-identical."""
    out, totals, unparsed = [], {}, 0
    # Split on "\n" only: str.splitlines also breaks on U+2028/U+2029/NEL/\x0b..., which JSON
    # writers leave unescaped inside strings.
    for line in io.StringIO(text, newline="").readlines():
        body = line[:-1] if line.endswith("\n") else line
        if body.endswith("\r"):
            body = body[:-1]
        if not body.strip():
            out.append(line)
            continue
        try:
            obj = json.loads(body)
        except ValueError:
            unparsed += 1
            out.append(line)
            continue
        counts = {}
        new = _redact_obj(obj, counts)
        if counts:
            for name, n in counts.items():
                totals[name] = totals.get(name, 0) + n
            line = _dump_record(new) + line[len(body):]
        out.append(line)
    return "".join(out), totals, unparsed


def redact_transcripts(ex, root) -> dict:
    """Back up `root` with tar (archive chmod 0600, kept until the user deletes it), then redact
    string values in every `*.jsonl` record with [REDACTED].

    Dry-run only counts. Returns {root, backup, files, changed, unparsed, counts}.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        raise SecretError(f"{root}: not a directory", code=2)
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    archive = paths.orche_home() / "archive"
    backup = archive / f"transcripts-backup-{stamp}.tar.gz"
    if not ex.dry_run:
        archive.mkdir(parents=True, exist_ok=True)
    result = ex.run(["tar", "czf", str(backup), str(root)], timeout=600)
    if not result.ok:
        raise failure(result, "transcript backup")
    if not ex.dry_run:
        try:
            os.chmod(backup, 0o600)
        except OSError:
            raise SecretError(f"transcript backup {backup} was not created; nothing redacted") from None
    files, changed, unparsed, errors, totals = 0, 0, 0, 0, {}
    for path in sorted(root.rglob("*.jsonl")):
        try:
            with open(path, encoding="utf-8", newline="") as fh:  # keep \r\n endings as written
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            errors += 1  # unreadable: counted, never silently skipped (exit 1)
            log.log("keymaster", f"redact-transcripts: {path.name}: unreadable ({type(exc).__name__})")
            continue
        files += 1
        try:
            new, counts, bad = redact_jsonl(text)
            if counts and not ex.dry_run:
                util.atomic_write(path, new)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            errors += 1
            log.log("keymaster", f"redact-transcripts: {path.name}: {type(exc).__name__}")
            continue
        unparsed += bad
        for name, n in counts.items():
            totals[name] = totals.get(name, 0) + n
        if counts:
            changed += 1
    return {"root": str(root), "backup": str(backup), "files": files, "changed": changed,
            "unparsed": unparsed, "errors": errors, "counts": totals}
