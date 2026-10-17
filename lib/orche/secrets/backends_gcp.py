"""GCP Secret Manager backend (primary). Values go to gcloud on stdin (`--data-file=-`).

Naming: secret id `<NAME>` inside `gcp_project[env]`; when only one project id is configured,
every env lives in that project as `<NAME>__<env>`.
"""
import json
import shlex

from . import SecretError, store

ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"


def locate(cfg, name, env):
    """(gcp project id, secret id) for `name` in `env`."""
    configured = cfg.get("gcp_project") or {}
    ids = {v for v in configured.values() if v}
    if len(ids) > 1:
        if configured.get(env):
            return configured[env], name
        raise SecretError(f"secrets.gcp_project has no id for env {env!r} in project {cfg['project']!r}")
    if len(ids) == 1:
        return next(iter(ids)), f"{name}__{env}"
    raise SecretError(f"secrets.gcp_project is not configured for project {cfg['project']!r}")


def put(ex, cfg, name, env, value, rotate=False) -> None:
    project, sid = locate(cfg, name, env)
    exists = ex.run(["gcloud", "secrets", "describe", sid, "--project", project, "--format", "value(name)"])
    if exists.ok:
        argv = ["gcloud", "secrets", "versions", "add", sid, "--project", project, "--data-file=-"]
    else:
        argv = ["gcloud", "secrets", "create", sid, "--project", project, "--replication-policy", "automatic",
                "--data-file=-"]
    result = ex.run(argv, stdin=value)
    if not result.ok:
        raise store.failure(result, f"storing {name} ({env}) in GCP", value)
    if rotate:
        disable_old_versions(ex, project, sid)


def _version_number(entry):
    try:
        return int(str(entry.get("name", "")).rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None


def disable_old_versions(ex, project, sid) -> list:
    """Disable every enabled version except the newest. Returns the disabled version numbers."""
    listed = ex.run(["gcloud", "secrets", "versions", "list", sid, "--project", project, "--format", "json"])
    if ex.dry_run:
        ex.planned.append(shlex.join(["gcloud", "secrets", "versions", "disable", "<each older enabled version>",
                                      "--secret", sid, "--project", project]))
        return []
    if not listed.ok:
        raise store.failure(listed, f"listing versions of {sid}")
    try:
        entries = json.loads(listed.out or "[]")
    except ValueError:
        raise SecretError(f"listing versions of {sid}: unreadable gcloud output") from None
    numbered = [(n, e) for e in entries if isinstance(e, dict) and (n := _version_number(e)) is not None]
    if not numbered:
        return []
    newest = max(n for n, _ in numbered)
    disabled = []
    for n, entry in sorted(numbered):
        if n == newest or entry.get("state", "ENABLED") != "ENABLED":
            continue
        result = ex.run(["gcloud", "secrets", "versions", "disable", str(n), "--secret", sid, "--project", project])
        if not result.ok:
            raise store.failure(result, f"disabling version {n} of {sid}")
        disabled.append(n)
    return disabled


def get(ex, cfg, name, env) -> str:
    project, sid = locate(cfg, name, env)
    result = ex.run(["gcloud", "secrets", "versions", "access", "latest", "--secret", sid, "--project", project])
    if not result.ok:
        raise SecretError(f"{name} ({env}): not readable from GCP project {project} (exit {result.code})", code=2)
    return result.out


def iam(ex, project, email, add=True) -> None:
    verb = "add-iam-policy-binding" if add else "remove-iam-policy-binding"
    result = ex.run(["gcloud", "projects", verb, project, "--member", f"user:{email}", "--role", ACCESSOR_ROLE])
    if not result.ok:
        raise store.failure(result, f"gcloud {verb}")
