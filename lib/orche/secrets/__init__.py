"""Keymaster: the model-free secrets broker behind `orche secret` and `orche dev`.

Agents only ever see secret NAMES. Values flow human -> dialog/getpass -> backend
(GCP Secret Manager | Vercel env | GitHub Actions | macOS keychain cache) -> child
process environment. A value must never reach stdout, stderr, logs, exceptions,
dry-run output, audit entries or argv; it travels to tools on stdin only.
"""

ENVS = ("dev", "preview", "prod")
VALUE_PLACEHOLDER = "<value>"


class SecretError(RuntimeError):
    """A user-facing keymaster failure. Messages carry names, never values."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def parse_envs(text) -> list:
    """`dev,prod` -> ["dev", "prod"] (ordered, unique, validated)."""
    out = []
    for part in str(text or "").split(","):
        env = part.strip().lower()
        if not env:
            continue
        if env not in ENVS:
            raise SecretError(f"unknown env {env!r} (allowed: {'|'.join(ENVS)})")
        if env not in out:
            out.append(env)
    if not out:
        raise SecretError("no env given")
    return out
