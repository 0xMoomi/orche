"""Keymaster dialog loop: asks the human for each pending secret and stores it.

Runs as its own process (spawned detached by `orche secret request`, with ORCHE_KEYMASTER=1)
or in-process from `orche secret fill` / `rotate` / `revoke`. GUI calls go through a UI object
(`UI_FACTORY`) so tests can drive the loop without a display.
"""
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: python3 -I .../dialog.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orche import log, registry, shell  # noqa: E402
from orche.secrets import VALUE_PLACEHOLDER, SecretError, scrub, store  # noqa: E402

PASTE, ENTER, CANCEL = "Paste from clipboard", "Enter", "Cancel"
MAX_ATTEMPTS = 3
# Fixed words only: the request's text is `item 1 of argv` (dialog_argv), never AppleScript.
DIALOG = ("on run argv",
          f'display dialog (item 1 of argv) default answer "" with hidden answer '
          f'buttons {{"{PASTE}","{ENTER}","{CANCEL}"}} default button "{PASTE}"',
          "end run")


def dialog_argv(text) -> list:
    """The osascript argv: the fixed DIALOG lines, then `text` as the one argument (control characters
    but newlines dropped; a leading `-` gets a space before it, never an osascript option)."""
    plain = "".join(ch if ch == "\n" or ch >= " " else " " for ch in str(text))
    return ["osascript", *(x for line in DIALOG for x in ("-e", line)), " " + plain if plain.startswith("-") else plain]


def _resume(project, name, rid, filled_at) -> None:
    """Tell flow.owner the secret is in: the owner's ask is answered and Main hears, once, which unit
    to resume (owner.secret_filled never raises)."""
    from orche.flow import owner
    owner.secret_filled(project, name, rid, filled_at=filled_at)


class OsaUI:
    """Real UI: osascript dialog with hidden answer, pbpaste/pbcopy, notifications."""

    def __init__(self, ex=None):
        self.ex = ex or store.Exec()

    def ask(self, req, error=None):
        """(button, typed_text). Cancel (or a closed dialog) returns (CANCEL, "")."""
        envs = ",".join(req.get("envs") or [])
        text = f"{req.get('name')} ({req.get('why') or 'no reason given'}) · {envs}\n" \
               f"Get it at: {req.get('how_url') or '-'}\n(The clipboard will be cleared.)"
        if error:
            text = f"Rejected: {error}\n\n{text}"
        result = shell.run(dialog_argv(text), timeout=3600)
        if not result.ok:
            return CANCEL, ""
        return parse_answer(result.out)

    def paste(self) -> str:
        return shell.run(["pbpaste"], timeout=5).out

    def clear_clipboard(self) -> None:
        shell.run(["pbcopy"], input="", timeout=5)

    def notify(self, text) -> None:
        store.notify(self.ex, text)


UI_FACTORY = OsaUI


def parse_answer(out: str):
    """`button returned:X, text returned:Y` -> (X, Y)."""
    body = out[:-1] if out.endswith("\n") else out
    prefix, sep = "button returned:", ", text returned:"
    if not body.startswith(prefix):
        return CANCEL, ""
    rest = body[len(prefix):]
    cut = rest.find(sep)
    if cut < 0:
        return rest, ""
    return rest[:cut], rest[cut + len(sep):]


def _plan(ex, reg, rid, req) -> str:
    cfg = store.project_cfg(reg, req["project"])
    for env in req.get("envs") or []:
        store.put(ex, cfg, req["name"], env, VALUE_PLACEHOLDER, rotate=req.get("mode") == "rotate")
    return "planned"


def handle(rid, req, ui, ex, reg) -> str:
    """Ask, validate, store one request. Returns filled | skipped | invalid | failed."""
    if ex.dry_run:
        return _plan(ex, reg, rid, req)
    name, envs = req.get("name"), list(req.get("envs") or [])
    error, value = None, None
    try:
        for _ in range(MAX_ATTEMPTS):
            button, typed = ui.ask(req, error)
            value = (ui.paste() or "") if button == PASTE else (typed or "")
            # Clear after every round (paste, typed, rejected or cancelled): a value copied from the
            # issuer page must not stay where any process can `pbpaste` it.
            ui.clear_clipboard()
            if button not in (PASTE, ENTER):
                return "skipped"
            value = value.strip()
            ok, error = store.validate_format(name, value)
            if ok:
                break
        else:
            ui.notify(f"{name}: not stored ({error})")
            return "invalid"
        try:
            cfg = store.project_cfg(reg, req.get("project"))
            for env in envs:
                store.put(ex, cfg, name, env, value, rotate=req.get("mode") == "rotate",
                          actor="human", request_id=rid)
        except SecretError as exc:
            msg = scrub(str(exc), value)
            log.log("keymaster", f"fill {rid} failed: {msg}")
            ui.notify(f"{name}: storing failed")
            return "failed"
    finally:
        value = None
    filled = store.mark_filled(rid)
    store.audit("filled", req.get("project"), name, ",".join(envs), "-", "human", rid)
    log.log("keymaster", f"filled {rid} envs={','.join(envs)}")
    ui.notify(f"{name} stored ({','.join(envs)})")
    _resume(req.get("project"), name, rid, filled.get("filled_at"))
    return "filled"


def run_loop(ui=None, ex=None, project=None, only=None) -> dict:
    """Handle pending requests (oldest first, new arrivals included) until none is left untried.

    Returns {outcome: [request ids]}.
    """
    ex = ex or store.Exec()
    ui = ui or UI_FACTORY(ex)
    reg = registry.load()
    seen, summary = set(), {}
    while True:
        pending = [(rid, req) for rid, req in store.list_requests(status="pending", project=project)
                   if rid not in seen and (only is None or rid in only)]
        if not pending:
            return summary
        rid, req = pending[0]
        seen.add(rid)
        try:
            outcome = handle(rid, req, ui, ex, reg)
        except SecretError as exc:
            log.log("keymaster", f"{rid}: {exc}")
            outcome = "failed"
        summary.setdefault(outcome, []).append(rid)


def main() -> int:
    os.environ["ORCHE_KEYMASTER"] = "1"
    pid_path = store.secrets_dir() / "dialog.pid"
    pid_path.write_text(str(os.getpid()))
    try:
        summary = run_loop()
        log.log("keymaster", "dialog done " + " ".join(f"{k}={len(v)}" for k, v in sorted(summary.items())))
    except Exception as exc:  # noqa: BLE001 - detached process: log and exit
        log.log("keymaster", f"dialog error: {type(exc).__name__}")
        return 1
    finally:
        try:
            pid_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
