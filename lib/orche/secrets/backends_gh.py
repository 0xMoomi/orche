"""GitHub Actions secrets (write-only). `gh secret set` reads the value from stdin."""
from . import store


def secret_name(name, env) -> str:
    return name if env == "prod" else f"{name}_{env.upper()}"


def put(ex, cfg, name, env, value) -> None:
    gname = secret_name(name, env)
    result = ex.run(["gh", "secret", "set", gname, "--repo", cfg["gh_repo"]], stdin=value)
    if not result.ok:
        raise store.failure(result, f"storing {gname} in GitHub {cfg['gh_repo']}", value)
