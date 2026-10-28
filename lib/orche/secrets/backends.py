"""The secret backends. Every value goes to its tool on stdin, never in argv.

GCP Secret Manager (primary; `--data-file=-`): secret id `<NAME>` inside `gcp_project[env]`; when only one
project id is configured, every env lives in that project as `<NAME>__<env>`.
Vercel env (MVPs): sensitive values cannot be read back.
GitHub Actions secrets (write-only): `gh secret set` reads the value from stdin.
macOS keychain as a local cache: service `orche/<proj>/<env>/<NAME>`, account `orche`. Writing with the
value as a command-line argument would put it in argv, so writes go through `security -i` with the
command on stdin. Values with line breaks cannot be expressed there and are not cached (the caller logs
`cache skipped`).
"""
import json
import shlex

from . import VALUE_PLACEHOLDER, SecretError, failure

# ---------------------------------------------------------------- GCP Secret Manager

ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"


def gcp_locate(cfg, name, env):
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


def gcp_put(ex, cfg, name, env, value, rotate=False) -> None:
    project, sid = gcp_locate(cfg, name, env)
    exists = ex.run(["gcloud", "secrets", "describe", sid, "--project", project, "--format", "value(name)"])
    if exists.ok:
        argv = ["gcloud", "secrets", "versions", "add", sid, "--project", project, "--data-file=-"]
    else:
        argv = ["gcloud", "secrets", "create", sid, "--project", project, "--replication-policy", "automatic",
                "--data-file=-"]
    result = ex.run(argv, stdin=value)
    if not result.ok:
        raise failure(result, f"storing {name} ({env}) in GCP", value)
    if rotate:
        gcp_disable_old_versions(ex, project, sid)


def _version_number(entry):
    try:
        return int(str(entry.get("name", "")).rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None


def gcp_disable_old_versions(ex, project, sid) -> list:
    """Disable every enabled version except the newest. Returns the disabled version numbers."""
    listed = ex.run(["gcloud", "secrets", "versions", "list", sid, "--project", project, "--format", "json"])
    if ex.dry_run:
        ex.planned.append(shlex.join(["gcloud", "secrets", "versions", "disable", "<each older enabled version>",
                                      "--secret", sid, "--project", project]))
        return []
    if not listed.ok:
        raise failure(listed, f"listing versions of {sid}")
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
            raise failure(result, f"disabling version {n} of {sid}")
        disabled.append(n)
    return disabled


def gcp_get(ex, cfg, name, env) -> str:
    project, sid = gcp_locate(cfg, name, env)
    result = ex.run(["gcloud", "secrets", "versions", "access", "latest", "--secret", sid, "--project", project])
    if not result.ok:
        raise SecretError(f"{name} ({env}): not readable from GCP project {project} (exit {result.code})", code=2)
    return result.out


def gcp_iam(ex, project, email, add=True) -> None:
    verb = "add-iam-policy-binding" if add else "remove-iam-policy-binding"
    result = ex.run(["gcloud", "projects", verb, project, "--member", f"user:{email}", "--role", ACCESSOR_ROLE])
    if not result.ok:
        raise failure(result, f"gcloud {verb}")


# ---------------------------------------------------------------- Vercel env

VERCEL_TARGETS = {"dev": "development", "preview": "preview", "prod": "production"}


def vercel_put(ex, cfg, name, env, value, rotate=False) -> None:
    target = VERCEL_TARGETS[env]
    tail = ["--cwd", cfg["root"]] if cfg.get("root") else []
    if rotate:
        ex.run(["vercel", "env", "rm", name, target, "--yes", *tail])
    # Vercel only accepts --sensitive for preview/production targets.
    flags = ["--sensitive"] if target != "development" else []
    result = ex.run(["vercel", "env", "add", name, target, *flags, *tail], stdin=value)
    if not result.ok:
        raise failure(result, f"storing {name} ({env}) in Vercel", value)


# ---------------------------------------------------------------- GitHub Actions secrets

def gh_secret_name(name, env) -> str:
    return name if env == "prod" else f"{name}_{env.upper()}"


def gh_put(ex, cfg, name, env, value) -> None:
    gname = gh_secret_name(name, env)
    result = ex.run(["gh", "secret", "set", gname, "--repo", cfg["gh_repo"]], stdin=value)
    if not result.ok:
        raise failure(result, f"storing {gname} in GitHub {cfg['gh_repo']}", value)


# ---------------------------------------------------------------- macOS keychain cache

def keychain_service(project, env, name) -> str:
    return f"orche/{project}/{env}/{name}"


def keychain_quote(text) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def keychain_put(ex, project, name, env, value) -> bool:
    if "\n" in value or "\r" in value:
        return False
    head = f"add-generic-password -U -a orche -s {keychain_quote(keychain_service(project, env, name))} -w "
    result = ex.run(["security", "-i"], stdin=f"{head}{keychain_quote(value)}\n",
                    shown_stdin=repr(head + VALUE_PLACEHOLDER))
    return result.ok and not result.err.strip()


def keychain_get(ex, project, name, env):
    result = ex.run(["security", "find-generic-password", "-a", "orche", "-s", keychain_service(project, env, name),
                     "-w"])
    if not result.ok:
        return None
    return result.out[:-1] if result.out.endswith("\n") else result.out
