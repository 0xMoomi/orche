"""Vercel env backend (MVPs). Value on stdin; sensitive values cannot be read back."""
from . import store

TARGETS = {"dev": "development", "preview": "preview", "prod": "production"}


def put(ex, cfg, name, env, value, rotate=False) -> None:
    target = TARGETS[env]
    tail = ["--cwd", cfg["root"]] if cfg.get("root") else []
    if rotate:
        ex.run(["vercel", "env", "rm", name, target, "--yes", *tail])
    # Vercel only accepts --sensitive for preview/production targets.
    flags = ["--sensitive"] if target != "development" else []
    result = ex.run(["vercel", "env", "add", name, target, *flags, *tail], stdin=value)
    if not result.ok:
        raise store.failure(result, f"storing {name} ({env}) in Vercel", value)
