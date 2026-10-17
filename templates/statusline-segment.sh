# >>> orche >>>
# ORCHE project segment (added by `orche install`, removed by `orche uninstall`).
# Walks up from the session directory to the first `.orche/project` marker and prints the cached
# `${ORCHE_HOME:-~/.claude/orche}/status/<project>.line`. Builtins only on the fast path; silent on errors.
{
  __orche_dir="${current_dir_path:-}"
  if [ -z "$__orche_dir" ]; then
    [ -n "${input+set}" ] || input=$(cat)
    __orche_dir=$(printf '%s' "$input" | grep -o '"current_dir" *: *"[^"]*"' | head -n 1 | sed 's/.*: *"//;s/"$//')
  fi
  [ -n "$__orche_dir" ] || __orche_dir="$PWD"
  while [ -n "$__orche_dir" ]; do
    if [ -f "$__orche_dir/.orche/project" ]; then
      __orche_key=""
      IFS= read -r __orche_key < "$__orche_dir/.orche/project"
      case "$__orche_key" in
        ''|*[!a-z0-9-]*) ;;
        *)
          __orche_line="${ORCHE_HOME:-$HOME/.claude/orche}/status/$__orche_key.line"
          if [ -f "$__orche_line" ]; then
            __orche_text=""
            IFS= read -r __orche_text < "$__orche_line"
            [ -n "$__orche_text" ] && printf '%s\n' "$__orche_text"
          fi
          ;;
      esac
      break
    fi
    [ "$__orche_dir" = "/" ] && break
    __orche_up="${__orche_dir%/*}"
    [ "$__orche_up" = "$__orche_dir" ] && break
    __orche_dir="${__orche_up:-/}"
  done
} 2>/dev/null || true
# <<< orche <<<
