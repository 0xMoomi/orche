"""The only place ORCHE shells out. Tests replace `RUNNER` with a fake."""
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Result:
    code: int
    out: str
    err: str
    argv: list

    @property
    def ok(self) -> bool:
        return self.code == 0


class ShellError(RuntimeError):
    def __init__(self, result: Result):
        super().__init__(f"command failed ({result.code}): {' '.join(result.argv)}: {result.err.strip()}")
        self.result = result


def _subprocess_runner(argv, *, timeout, input):
    # errors="replace": a non-UTF-8 byte in any output (a `ps` table listing a legacy file name or
    # environment value) becomes U+FFFD instead of raising UnicodeDecodeError.
    try:
        cp = subprocess.run(argv, input=input, capture_output=True, text=True, errors="replace",
                            timeout=timeout)
        return Result(cp.returncode, cp.stdout, cp.stderr, list(argv))
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timeout after {timeout}s", list(argv))
    except (FileNotFoundError, PermissionError) as exc:
        return Result(127, "", str(exc), list(argv))


RUNNER = _subprocess_runner


def run(argv, *, timeout=20, dry_run=False, input=None, check=False) -> Result:
    argv = [str(a) for a in argv]
    if dry_run:
        return Result(0, "", "", argv)
    result = RUNNER(argv, timeout=timeout, input=input)
    if check and result.code != 0:
        raise ShellError(result)
    return result


def which(name: str):
    return shutil.which(name)
