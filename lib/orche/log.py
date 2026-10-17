"""Append-only plain-text logs under ORCHE_HOME/logs. Never raises."""
from . import paths, util


def log(name: str, msg: str) -> None:
    try:
        path = paths.orche_home() / "logs" / f"{name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = str(msg).replace("\n", "\\n")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{util.now_iso()}\t{name}\t{line}\n")
    except Exception:
        pass
