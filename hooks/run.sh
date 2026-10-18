#!/bin/sh
# Run an ORCHE hook under the first Python 3.11+ available: `run.sh <hook>` execs hooks/<hook>.py.
#
# The interpreter search lives in lib/find_python.sh (shared with bin/orche). With no suitable
# interpreter this fails open: one timestamped line goes to
# ${ORCHE_HOME:-$HOME/.claude/orche}/logs/hooks.log and the hook exits 0.
# Plain POSIX sh with builtins and absolute /bin tools only, so it works whatever PATH the hook runs with.

hook=$1
case $0 in
  */*) dir=${0%/*} ;;
  *) dir=. ;;
esac

case $hook in
  '' | *[!a-z_]*)
    reason="bad hook name '$hook'"
    ;;
  *)
    if [ -r "$dir/../lib/find_python.sh" ] && . "$dir/../lib/find_python.sh" && orche_find_python; then
      exec "$ORCHE_PY" "$dir/$hook.py"
    fi
    reason="no Python 3.11+ found for hook $hook (tried ${ORCHE_PY_CANDIDATES:-$dir/../lib/find_python.sh missing}); brew install python"
    ;;
esac

# Same layout as the Python log lines: ISO timestamp, log name, message.
ts=$(/bin/date +%Y-%m-%dT%H:%M:%S%z 2>/dev/null) && ts="${ts%??}:${ts#"${ts%??}"}"
logs=${ORCHE_HOME:-$HOME/.claude/orche}/logs
{ /bin/mkdir -p "$logs" && printf '%s\thooks\trun.sh: %s\n' "$ts" "$reason" >>"$logs/hooks.log"; } 2>/dev/null
exit 0
