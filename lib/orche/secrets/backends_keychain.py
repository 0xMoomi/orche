"""macOS keychain as a local cache: service `orche/<proj>/<env>/<NAME>`, account `orche`.

`security add-generic-password -w <value>` would put the value in argv, so writes go through
`security -i` with the command on stdin. Values with line breaks cannot be expressed there and
are not cached (the caller logs `cache skipped`).
"""
from . import VALUE_PLACEHOLDER


def service(project, env, name) -> str:
    return f"orche/{project}/{env}/{name}"


def _quote(text) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def put(ex, project, name, env, value) -> bool:
    if "\n" in value or "\r" in value:
        return False
    head = f"add-generic-password -U -a orche -s {_quote(service(project, env, name))} -w "
    result = ex.run(["security", "-i"], stdin=f"{head}{_quote(value)}\n",
                    shown_stdin=repr(head + VALUE_PLACEHOLDER))
    return result.ok and not result.err.strip()


def get(ex, project, name, env):
    result = ex.run(["security", "find-generic-password", "-a", "orche", "-s", service(project, env, name), "-w"])
    if not result.ok:
        return None
    return result.out[:-1] if result.out.endswith("\n") else result.out
