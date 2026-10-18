# Sourced (`. lib/find_python.sh`) by hooks/run.sh and bin/orche: find the first Python 3.11+.
#
# A login shell's bare `python3` can be macOS's /usr/bin/python3 (3.9). `orche_find_python` tries
# the candidates in order and sets ORCHE_PY to the first that passes the version probe (return 0);
# with none it returns 1. Plain POSIX sh with builtins only, so it works whatever PATH the caller has.

ORCHE_PY_CANDIDATES="python3.14 python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3"

orche_find_python() {
  for ORCHE_PY in $ORCHE_PY_CANDIDATES; do
    if command -v "$ORCHE_PY" >/dev/null 2>&1 &&
      "$ORCHE_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' </dev/null >/dev/null 2>&1; then
      return 0
    fi
  done
  ORCHE_PY=
  return 1
}
