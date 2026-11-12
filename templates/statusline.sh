#!/bin/bash
# Orche's status line. `orche install` copies this file to ORCHE_HOME/statusline.sh and points
# settings.json `statusLine.command` at the copy; `orche uninstall` puts your own command back.
#
# Line 1: when ORCHE_HOME/statusline.inner holds a command (mode `keep`: your own status line), that
# command runs with the same stdin and its output is printed as it is; else (mode `orche`) Orche's line:
#   ⎇ branch │ model · effort │ Session: 12% ▓▓░░░ ░░░░░ ░░░░░ ░░░░░ → Reset: 09:31 PM │ Weekly: 78% ▓▓▓▓▓ ▓▓▓▓▓ ▓▓▓▓┃ ▓░░░░ → Reset: Sun 04:00 PM
# Each gauge has 20 cells (5% each) in four groups of five, so every gap is a quarter mark.
# Then the Orche project line: walk up from the session directory to the first `.orche/project` marker
# and print the cached first line of ORCHE_HOME/status/<project>.line.
# Tools: bash 3.2 builtins, git, date; no JSON tool. Never fails loudly: any error prints what it has and exits 0.
# The inner command runs with ORCHE_STATUSLINE=1; this script started with it set prints nothing (no loop, and
# the outer run prints the project line once).
[ -n "${ORCHE_STATUSLINE:-}" ] && exit 0
{
  input=""
  IFS= read -r -d '' input   # all of stdin, a builtin (no cat)
  __self="${BASH_SOURCE[0]:-$0}"
  case "$__self" in
    */*) __here="${__self%/*}" ;;
    *) __here="." ;;
  esac
  __s='[[:space:]]*'

  dir=""
  __re="\"current_dir\"${__s}:${__s}\"([^\"]*)\""
  [[ $input =~ $__re ]] && dir="${BASH_REMATCH[1]}"
  if [ -z "$dir" ]; then
    __re="\"cwd\"${__s}:${__s}\"([^\"]*)\""
    [[ $input =~ $__re ]] && dir="${BASH_REMATCH[1]}"
  fi

  inner=""
  [ -f "$__here/statusline.inner" ] && IFS= read -r -d '' inner < "$__here/statusline.inner"
  inner="${inner%$'\n'}"   # the whole stored command (it may span lines), its trailing newline stripped
  case "$inner" in
    *orche/statusline.sh*) inner="" ;;   # Orche's own script: never run it from itself
  esac
  case "$__self" in
    */*) case "$inner" in *"$__self"*) inner="" ;; esac ;;
  esac

  if [ -n "$inner" ]; then
    __out=$(printf '%s' "$input" | ORCHE_STATUSLINE=1 /bin/sh -c "$inner")
    [ -n "$__out" ] && printf '%s\n' "$__out"
  else
    RESET=$'\033[0m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[0;33m'
    GRAY=$'\033[0;90m'
    sep="${GRAY} │ ${RESET}"
    line=""
    model=""
    effort=""
    __re="\"model\"${__s}:${__s}\\{[^}]*\"display_name\"${__s}:${__s}\"([^\"]*)\""
    [[ $input =~ $__re ]] && model="${BASH_REMATCH[1]}"
    __re="\"effort\"${__s}:${__s}\\{[^}]*\"level\"${__s}:${__s}\"([^\"]*)\""
    [[ $input =~ $__re ]] && effort="${BASH_REMATCH[1]}"

    branch=$(git -C "${dir:-.}" branch --show-current 2>/dev/null)
    [ -n "$branch" ] && line="${GREEN}⎇ ${branch}${RESET}"
    [ -n "$model" ] && line="${line:+${line}${sep}}${YELLOW}${model}${effort:+ · ${effort}}${RESET}"

    now=""
    levels=(22 28 34 100 142 178 172 166 160 124)
    # __window <label> <json key> <window seconds> <date format>: one usage part, or nothing.
    __window() {
      local obj="" pct="" reset="" re
      re="\"$2\"${__s}:${__s}\\{([^}]*)\\}"
      [[ $input =~ $re ]] || return 0
      obj="${BASH_REMATCH[1]}"
      re="\"used_percentage\"${__s}:${__s}([0-9]+)(\\.([0-9]))?"
      [[ $obj =~ $re ]] || return 0
      pct="${BASH_REMATCH[1]}"
      [ "${BASH_REMATCH[3]:-0}" -ge 5 ] && pct=$(( pct + 1 ))
      re="\"resets_at\"${__s}:${__s}([0-9]+)"
      [[ $obj =~ $re ]] && reset="${BASH_REMATCH[1]}"
      local win=$3 idx filled colour marker=-1 pace="" reset_text="" bar="" i=0 remaining elapsed projected when
      idx=$(( (pct - 1) / 10 ))
      [ "$idx" -lt 0 ] && idx=0
      [ "$idx" -gt 9 ] && idx=9
      colour=$'\033[38;5;'"${levels[$idx]}m"
      filled=$(( (pct * 20 + 50) / 100 ))
      [ "$filled" -gt 20 ] && filled=20
      if [ -n "$reset" ]; then
        [ -n "$now" ] || now=$(date +%s)
        remaining=$(( reset - now ))
        if [ "$remaining" -gt 0 ] && [ "$remaining" -lt "$win" ]; then
          # Pace marker: a bar at how far the window has gone, coloured by where it would end at this pace.
          elapsed=$(( win - remaining ))
          marker=$(( (elapsed * 20 + win / 2) / win ))
          [ "$marker" -gt 19 ] && marker=19
          pace="$colour"
          if [ "$elapsed" -ge $(( win / 200 )) ]; then
            projected=$(( pct * win / elapsed ))
            if   [ "$projected" -lt 50 ];  then pace=$'\033[38;5;34m'
            elif [ "$projected" -lt 75 ];  then pace=$'\033[38;5;37m'
            elif [ "$projected" -lt 90 ];  then pace=$'\033[38;5;178m'
            elif [ "$projected" -lt 100 ]; then pace=$'\033[38;5;208m'
            elif [ "$projected" -lt 120 ]; then pace=$'\033[38;5;160m'
            else                                pace=$'\033[38;5;135m'
            fi
          fi
        fi
        if [ "$remaining" -gt 0 ]; then   # a reset in the past: no time to show
          when=$(date -r "$reset" "+$4" 2>/dev/null || date -d "@$reset" "+$4" 2>/dev/null)
          [ -n "$when" ] && reset_text=" → Reset: ${when}"
        fi
      fi
      while [ "$i" -lt 20 ]; do
        [ "$i" -gt 0 ] && [ $(( i % 5 )) -eq 0 ] && bar="${bar} "   # a gap at each quarter
        if [ "$i" -eq "$marker" ]; then
          bar="${bar}${pace}┃${RESET}${colour}"
        elif [ "$i" -lt "$filled" ]; then
          bar="${bar}▓"
        else
          bar="${bar}░"
        fi
        i=$(( i + 1 ))
      done
      line="${line:+${line}${sep}}${colour}$1: ${pct}% ${bar}${reset_text}${RESET}"
    }
    __window Session five_hour 18000 "%I:%M %p"
    __window Weekly seven_day 604800 "%a %I:%M %p"
    [ -n "$line" ] && printf '%s\n' "$line"
  fi

  # The Orche project line: builtins only. A CEO Staff pane (ORCHE_SESSION staff-<key>) has no
  # `.orche/project` in its worktree: its line is `STAFF · ` and the project's cached line (or its key).
  __orche_dir="${dir:-$PWD}"
  __orche_staff="${ORCHE_SESSION:-}"
  case "$__orche_staff" in
    staff-?*)
      __orche_key="${__orche_staff#staff-}"
      case "$__orche_key" in
        *[!a-z0-9-]*) ;;
        *)
          __orche_text=""
          __orche_line="${ORCHE_HOME:-$__here}/status/$__orche_key.line"
          [ -f "$__orche_line" ] && IFS= read -r __orche_text < "$__orche_line"
          printf 'STAFF · %s\n' "${__orche_text:-$__orche_key}"
          __orche_dir=""
          ;;
      esac
      ;;
  esac
  while [ -n "$__orche_dir" ]; do
    if [ -f "$__orche_dir/.orche/project" ]; then
      __orche_key=""
      IFS= read -r __orche_key < "$__orche_dir/.orche/project"
      case "$__orche_key" in
        ''|*[!a-z0-9-]*) ;;
        *)
          __orche_line="${ORCHE_HOME:-$__here}/status/$__orche_key.line"
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
} 2>/dev/null
exit 0
