#!/usr/bin/env bash
# Adapted from Cursor's pstack show-me-your-work log.sh (MIT)
#
# Append one row to a DECISIONS.tsv: ts, phase, decision, why, evidence, result.
# Usage: log.sh [-f FILE] <phase> <decision> <why> <evidence> <result>
# FILE defaults to $ORCHE_DECISIONS_FILE, else .orche/DECISIONS.tsv under the cwd.
# Escaping matches `orche decide`: tabs/CR/LF become spaces; a cell starting with
# = + - @ gets a leading ' (spreadsheet formula guard). Header on first use; ts is UTC ISO-8601.
set -euo pipefail

file="${ORCHE_DECISIONS_FILE:-.orche/DECISIONS.tsv}"
if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--file" ]; then
  [ $# -ge 2 ] || { echo "log.sh: $1 needs a path" >&2; exit 1; }
  file="$2"
  shift 2
fi
if [ "${1:-}" = "--" ]; then
  shift
fi
if [ $# -ne 5 ]; then
  echo "usage: log.sh [-f FILE] <phase> <decision> <why> <evidence> <result>" >&2
  exit 1
fi

esc() {
  local v="$1"
  v="${v//$'\t'/ }"
  v="${v//$'\n'/ }"
  v="${v//$'\r'/ }"
  case "$v" in
    [=+@-]*) v="'$v" ;;
  esac
  printf '%s' "$v"
}

mkdir -p "$(dirname "$file")"
if [ ! -s "$file" ]; then
  printf 'ts\tphase\tdecision\twhy\tevidence\tresult\n' > "$file"
elif [ -n "$(tail -c 1 "$file")" ]; then
  printf '\n' >> "$file"
fi
row="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
for cell in "$@"; do
  row="$row"$'\t'"$(esc "$cell")"
done
printf '%s\n' "$row" >> "$file"
printf '%s\n' "$row"
