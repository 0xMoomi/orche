#!/usr/bin/env bash
# ORCHE one-command installer (macOS and Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash
#   bash install.sh [options]          # from a clone: that clone is used as is
#
# Steps, in order: OS gate, requirements (git, Python 3.11+, claude, orca), clone or update
# ORCHE_DIR, PATH line in the login profile, `orche install`, the Claude Code plugin (or the
# settings.json hooks), optionally `orche apply-all`, then `orche doctor` and a short summary.
# Safe to re-run: a second run updates the clone and changes nothing else that is already in place.
# No sudo, no eval. Exit codes: 0 done, 1 user/environment problem, 2 unexpected failure.
#
# Test-only environment, honoured only when set: ORCHE_INSTALL_UNAME stands in for `uname -s`;
# ORCHE_INSTALL_TTY stands in for /dev/tty (where the Jev answers are read).
# Bash 3.2 compatible (macOS /bin/bash): no associative arrays, no empty-array expansions.

set -euo pipefail

ORCHE_REPO_URL="https://github.com/0xMoomi/orche"
# Same candidates, same order as lib/find_python.sh (the clone does not exist yet when we search).
ORCHE_PY_CANDIDATES="python3.14 python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3"
PATH_MARKER="# orche"
WINDOWS_MSG="Windows is not supported: Orca is a macOS app and the ORCHE CLI relies on POSIX process control (ps, process groups, signals) and the macOS keychain. WSL2 is unverified."

usage() {
  cat <<'EOF'
ORCHE installer: clone (or update) ORCHE, put `orche` on PATH, run `orche install`,
install the Claude Code plugin, and check the result with `orche doctor`.

Usage:
  curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/0xMoomi/orche/main/install.sh | bash -s -- [options]
  bash install.sh [options]

Options:
  --dir PATH       where ORCHE lives (default: $ORCHE_DIR, else this clone when run from one,
                   else ~/orche)
  --ref REF        branch or tag to install (default: main)
  --mode MODE      plugin (default): hooks via the Claude Code plugin;
                   settings: hooks registered in ~/.claude/settings.json instead
  --all-projects   afterwards run `orche apply-all` (init every Orca repo not registered yet,
                   without the paid intake: run `orche intake <proj>` per project afterwards);
                   without it, apply ORCHE per project with `orche init <project>`
  --no-jev         do not ask for the optional Jev key (add it later: orche jev setup)
  --yes            ask nothing (implies --no-jev)
  --no-profile     do not touch your login profile (print the PATH line instead)
  --dry-run        print every step, write nothing
  -h, --help       this help

Re-run it any time to update.
EOF
}

say() { printf '%s\n' "$*"; }
step() { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
unexpected() {
  printf 'error: unexpected failure (exit %s, line %s); re-run with \140bash -x install.sh\140 to see where\n' "$1" "$2" >&2
  exit 2
}

# A URL without any user:password@ part (never print credentials).
masked() { printf '%s' "$1" | sed -E 's#://[^/@]*@#://#'; }

# Comparable form of a git remote: no scheme, no credentials, no trailing / or .git, lower case.
norm_remote() {
  local r=$1
  case $r in
    /*) if [ -d "$r" ]; then r=$(cd "$r" && pwd -P); fi ;;
  esac
  r=$(printf '%s' "$r" | sed -E 's#^[a-zA-Z+]+://##; s#^[^/@]*@##; s#^(github\.com):#\1/#; s#/+$##; s#\.git$##')
  printf '%s' "$r" | tr '[:upper:]' '[:lower:]'
}

# Print a command; run it unless --dry-run.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '   would run:'; printf ' %q' "$@"; printf '\n'
    return 0
  fi
  "$@"
}

find_python() {
  local c
  for c in $ORCHE_PY_CANDIDATES; do
    if command -v "$c" >/dev/null 2>&1 &&
      "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' </dev/null >/dev/null 2>&1; then
      PY=$c
      return 0
    fi
  done
  return 1
}

# --- steps --------------------------------------------------------------------------------------

check_os() {
  local os
  os=${ORCHE_INSTALL_UNAME:-$(uname -s)}
  if [ "${OS:-}" = "Windows_NT" ]; then die "$WINDOWS_MSG"; fi
  case $os in
    Darwin) OS_NAME=macos ;;
    Linux) OS_NAME=linux
      warn "Linux: the CLI, runner and hooks work here, but Orca (a macOS app) is still needed for panes and \`orche init\`" ;;
    MINGW* | MSYS* | CYGWIN* | Windows*) die "$WINDOWS_MSG" ;;
    *) die "unsupported OS '$os' (ORCHE supports macOS; Linux without Orca panes)" ;;
  esac
  step "OS: $os"
}

check_requirements() {
  if ! command -v git >/dev/null 2>&1; then
    if [ "$OS_NAME" = macos ]; then
      die "git not found: install it with \`xcode-select --install\` (or \`brew install git\`), then re-run"
    fi
    die "git not found: install it with \`sudo apt install git\` (Debian/Ubuntu) or \`sudo dnf install git\` (Fedora), then re-run"
  fi
  if ! find_python; then
    local have=""
    if command -v python3 >/dev/null 2>&1; then
      have=" (python3 is $(python3 --version 2>&1 </dev/null | head -n 1 || true))"
    fi
    if [ "$OS_NAME" = macos ]; then
      die "ORCHE needs Python 3.11+, none found$have. Install it with \`brew install python\`, then re-run"
    fi
    die "ORCHE needs Python 3.11+, none found$have. Install it with \`sudo apt install python3.12\` (Debian/Ubuntu) or \`sudo dnf install python3.12\` (Fedora), then re-run"
  fi
  step "git: $(command -v git); python: $(command -v "$PY") ($("$PY" -c 'import platform; print(platform.python_version())' </dev/null))"

  PLUGIN_NOTE=""
  if command -v claude >/dev/null 2>&1; then
    local help
    help=$(claude plugin --help </dev/null 2>&1 || true)
    if printf '%s\n' "$help" | grep -Eq '^[[:space:]]+install([|[:space:]]|$)' &&
      printf '%s\n' "$help" | grep -Eq '^[[:space:]]+marketplace([[:space:]]|$)'; then
      CLAUDE_PLUGIN=1
      step "claude: $(command -v claude) (plugin install supported)"
    else
      CLAUDE_PLUGIN=0
      PLUGIN_NOTE="this claude has no \`claude plugin install\`: plugin step skipped, hooks registered in settings.json (--mode settings)"
    fi
  else
    CLAUDE_PLUGIN=0
    PLUGIN_NOTE="claude not found on PATH: plugin step skipped, hooks registered in settings.json (--mode settings); install Claude Code, then re-run"
  fi
  if [ "$MODE" = plugin ] && [ "$CLAUDE_PLUGIN" = 0 ]; then
    MODE=settings
    warn "$PLUGIN_NOTE"
  fi
  if command -v orca >/dev/null 2>&1; then
    step "orca: $(command -v orca)"
  else
    warn "orca not found on PATH: install Orca (and its \`orca\` CLI) before \`orche init\`"
  fi
}

clone_or_update() {
  local shown_source
  shown_source=$(masked "$SOURCE")
  if [ -n "$SELF_CLONE" ] && [ "$(cd "$ORCHE_DIR" 2>/dev/null && pwd -P)" = "$SELF_CLONE" ]; then
    step "using this clone as is: $ORCHE_DIR (update it with git pull)"
    return 0
  fi
  # A missing or empty folder is a clone target (git clones into an empty folder).
  if [ ! -e "$ORCHE_DIR" ] || { [ -d "$ORCHE_DIR" ] && [ -z "$(ls -A "$ORCHE_DIR")" ]; }; then
    step "clone $shown_source ($REF) into $ORCHE_DIR"
    run env GIT_TERMINAL_PROMPT=0 git clone --quiet --branch "$REF" "$SOURCE" "$ORCHE_DIR" </dev/null ||
      die "git clone of $shown_source ($REF) failed (see above): check the network and --ref"
    return 0
  fi
  if [ ! -d "$ORCHE_DIR" ]; then
    die "$ORCHE_DIR exists and is not a folder: move it away or pass --dir <another folder>"
  fi
  local top
  top=$(git -C "$ORCHE_DIR" rev-parse --show-toplevel 2>/dev/null || true)
  if [ -z "$top" ] || [ "$(cd "$top" && pwd -P)" != "$(cd "$ORCHE_DIR" && pwd -P)" ]; then
    die "$ORCHE_DIR exists but is not a git clone: move it away or pass --dir <another folder>"
  fi
  local remote
  remote=$(git -C "$ORCHE_DIR" remote get-url origin 2>/dev/null || true)
  if [ "$(norm_remote "$remote")" != "$(norm_remote "$SOURCE")" ]; then
    die "$ORCHE_DIR is a clone of $(masked "${remote:-(no origin)}"), not $shown_source: move it away or pass --dir <another folder>"
  fi
  if [ -n "$(git -C "$ORCHE_DIR" status --porcelain)" ]; then
    die "$ORCHE_DIR has uncommitted changes: commit or stash them (git -C \"$ORCHE_DIR\" stash), then re-run"
  fi
  step "update $ORCHE_DIR ($REF)"
  run env GIT_TERMINAL_PROMPT=0 git -C "$ORCHE_DIR" fetch --quiet --tags --prune origin </dev/null ||
    die "git fetch in $ORCHE_DIR failed (see above): check the network"
  run git -C "$ORCHE_DIR" checkout --quiet "$REF" </dev/null ||
    die "git checkout $REF in $ORCHE_DIR failed (see above): check --ref"
  if [ "$DRY_RUN" = 1 ]; then
    say "   would run: git -C $ORCHE_DIR pull --ff-only (when $REF is a branch)"
  elif git -C "$ORCHE_DIR" symbolic-ref -q HEAD >/dev/null; then
    GIT_TERMINAL_PROMPT=0 git -C "$ORCHE_DIR" pull --quiet --ff-only </dev/null ||
      die "cannot fast-forward $ORCHE_DIR to origin/$REF (local commits?): reset or move the clone, then re-run"
  fi
}

profile_path() {
  case $(basename "${SHELL:-}") in
    zsh) printf '%s' "${ZDOTDIR:-$HOME}/.zprofile" ;;
    bash) if [ -f "$HOME/.bash_profile" ]; then printf '%s' "$HOME/.bash_profile"; else printf '%s' "$HOME/.profile"; fi ;;
    *) printf '' ;;
  esac
}

setup_path() {
  local shown=$ORCHE_DIR line
  case $ORCHE_DIR in
    "$HOME"/*) shown="\$HOME${ORCHE_DIR#"$HOME"}" ;;
  esac
  line="export PATH=\"$shown/bin:\$PATH\"  $PATH_MARKER"
  PROFILE=$(profile_path)
  if [ "$NO_PROFILE" = 1 ]; then
    PROFILE=""
    step "PATH: --no-profile; add this line to your login profile yourself:"
    say "   $line"
  elif [ -z "$PROFILE" ]; then
    step "PATH: unknown shell '${SHELL:-}'; add this line to your shell's login profile:"
    say "   $line"
  elif [ -f "$PROFILE" ] && { grep -qF "$shown/bin" "$PROFILE" || grep -qF "$ORCHE_DIR/bin" "$PROFILE"; }; then
    step "PATH: already in $PROFILE"
  elif [ -f "$PROFILE" ] && grep -q "$PATH_MARKER\$" "$PROFILE"; then
    warn "$PROFILE already has an ORCHE PATH line for another folder; change it to: $line"
  elif [ "$DRY_RUN" = 1 ]; then
    step "PATH: would append to $PROFILE: $line"
  else
    printf '\n%s\n' "$line" >>"$PROFILE"
    step "PATH: added to $PROFILE: $line"
  fi
  export PATH="$ORCHE_DIR/bin:$PATH"
}

# `orche install`. The Jev question needs a terminal: under `curl | bash` stdin is the script, so
# (like rustup) the answers are read from /dev/tty when it can be opened and stdout is a terminal.
# Without one, --no-jev and one line saying so. In plugin mode this script runs the two plugin
# steps itself, so `orche install` is told not to print them (--no-plugin-hint).
orche_install() {
  local orche="$ORCHE_DIR/bin/orche" tty=${ORCHE_INSTALL_TTY:-/dev/tty} hint="" ask=0
  if [ "$MODE" = plugin ]; then hint="--no-plugin-hint"; fi
  if [ "$YES" = 0 ] && [ "$NO_JEV" = 0 ] && [ -t 1 ] && (: <"$tty") 2>/dev/null; then ask=1; fi
  if [ "$DRY_RUN" = 1 ]; then
    if [ -x "$orche" ]; then
      step "orche --dry-run install --mode $MODE (preview; nothing written)"
      PYTHONDONTWRITEBYTECODE=1 "$orche" --dry-run install --mode "$MODE" --no-jev ${hint:+"$hint"} </dev/null || true
    else
      step "would run: orche install --mode $MODE$([ "$ask" = 1 ] || printf ' --no-jev')${hint:+ $hint}"
    fi
    return 0
  fi
  if [ "$ask" = 1 ]; then
    step "orche install --mode $MODE${hint:+ $hint} (Jev question on the terminal)"
    "$orche" install --mode "$MODE" ${hint:+"$hint"} <"$tty" ||
      die "orche install reported errors (above): fix them and re-run"
    return 0
  fi
  if [ "$YES" = 0 ] && [ "$NO_JEV" = 0 ]; then
    say "Jev: skipped (no terminal); add it later with \`orche jev setup\`"
  fi
  step "orche install --mode $MODE --no-jev${hint:+ $hint}"
  "$orche" install --mode "$MODE" --no-jev ${hint:+"$hint"} </dev/null ||
    die "orche install reported errors (above): fix them and re-run"
}

# json_find KIND JSON: does `claude plugin marketplace list --json` (KIND=marketplace) or
# `claude plugin list --json` (KIND=plugin) list orche? Prints "found <source>" when it does
# (a marketplace's path, repo or URL; empty for a plugin), nothing otherwise. Never fails: it runs
# inside $(...), where a failing command would trip the ERR trap.
json_find() {
  "$PY" -c '
import json, sys
kind, text = sys.argv[1], sys.argv[2]
try:
    rows = json.loads(text)
except ValueError:
    rows = []
for row in rows if isinstance(rows, list) else []:
    if not isinstance(row, dict):
        continue
    if kind == "marketplace" and row.get("name") == "orche":
        print("found", row.get("path") or row.get("repo") or row.get("url") or "")
        break
    if kind == "plugin" and row.get("id") == "orche@orche":
        print("found")
        break
' "$1" "$2" </dev/null || true
}

plugin_step() {
  if [ "$MODE" != plugin ]; then
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    step "Claude Code plugin"
    run claude plugin marketplace add "$ORCHE_DIR"
    run claude plugin install orche@orche
    say "   (or, when already there: claude plugin marketplace update orche; claude plugin update orche@orche)"
    return 0
  fi
  step "Claude Code plugin (orche@orche)"
  local listed found where
  listed=$(claude plugin marketplace list --json </dev/null 2>/dev/null || true)
  found=$(json_find marketplace "$listed")
  if [ -n "$found" ]; then
    where=${found#found}
    where=${where# }
    if [ -n "$where" ] && [ "$(norm_remote "$where")" != "$(norm_remote "$ORCHE_DIR")" ]; then
      warn "a marketplace named orche already points at $(masked "$where"); kept as is (to use this clone: claude plugin marketplace remove orche, then re-run)"
    fi
    claude plugin marketplace update orche </dev/null || warn "claude plugin marketplace update orche failed; continuing"
  elif ! claude plugin marketplace add "$ORCHE_DIR" </dev/null; then
    plugin_failed "claude plugin marketplace add"
    return 0
  fi
  listed=$(claude plugin list --json </dev/null 2>/dev/null || true)
  if [ -n "$(json_find plugin "$listed")" ]; then
    if claude plugin update orche@orche </dev/null; then
      PLUGIN_UPDATED=1
    else
      warn "claude plugin update orche@orche failed; the installed plugin stays as it was"
    fi
  elif ! claude plugin install orche@orche </dev/null; then
    plugin_failed "claude plugin install orche@orche"
    return 0
  fi
}

# The plugin could not be installed: register the hooks in settings.json so they still run.
plugin_failed() {
  warn "$1 failed (above): registering the hooks in settings.json instead (orche install --mode settings)"
  MODE=settings
  PLUGIN_NOTE="plugin step failed; hooks registered in settings.json. Re-run this installer to retry the plugin"
  "$ORCHE_DIR/bin/orche" install --mode settings --no-jev </dev/null || die "orche install --mode settings reported errors (above)"
}

apply_all() {
  if [ "$ALL_PROJECTS" = 0 ]; then
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    step "would run: orche apply-all (init every Orca repo not registered yet; no intake)"
    return 0
  fi
  step "orche apply-all"
  "$ORCHE_DIR/bin/orche" apply-all </dev/null ||
    warn "orche apply-all did not finish (above); ORCHE is installed: run \`orche apply-all\` again once Orca is running"
}

doctor() {
  if [ "$DRY_RUN" = 1 ]; then
    step "would run: orche doctor"
    return 0
  fi
  step "orche doctor (WARN and FAIL lines only; run \`orche doctor\` for all)"
  local out rc=0
  out=$("$ORCHE_DIR/bin/orche" doctor </dev/null 2>&1) || rc=$?
  printf '%s\n' "$out" | grep -E '^(WARN|FAIL)' | sed 's/^/   /' || true
  local fails warns
  fails=$(printf '%s\n' "$out" | grep -c '^FAIL' || true)
  warns=$(printf '%s\n' "$out" | grep -c '^WARN' || true)
  say "   doctor: $warns WARN, $fails FAIL"
  if [ "$rc" != 0 ]; then
    DOCTOR_FAILED=1
  fi
}

summary() {
  say ""
  if [ "$DRY_RUN" = 1 ]; then
    say "dry run: nothing was written. Run again without --dry-run to install."
    return 0
  fi
  local version
  version=$("$ORCHE_DIR/bin/orche" version </dev/null)
  say "$version installed in $ORCHE_DIR ($MODE mode${PLUGIN_UPDATED:+; plugin updated, restart Claude Code to load it})"
  if [ -n "$PLUGIN_NOTE" ]; then say "note: $PLUGIN_NOTE"; fi
  if [ -n "$PROFILE" ]; then
    say "Open a new terminal (or run: source $PROFILE) so \`orche\` is on your PATH."
  else
    say "Add the PATH line above to your login profile, then open a new terminal."
  fi
  say "Next: orche init <project>   (or /orche-apply <project> inside Claude Code)"
  if [ "${DOCTOR_FAILED:-0}" = 1 ]; then
    die "orche doctor reported a FAIL line (above): fix it, then run \`orche doctor\` again"
  fi
}

parse_args() {
  DIR_ARG=""; REF=main; SOURCE=$ORCHE_REPO_URL; MODE=plugin
  ALL_PROJECTS=0; DRY_RUN=0; YES=0; NO_JEV=0; NO_PROFILE=0
  while [ $# -gt 0 ]; do
    case $1 in
      --dir) [ $# -ge 2 ] || die "--dir needs a path"; DIR_ARG=$2; shift ;;
      --dir=*) DIR_ARG=${1#*=} ;;
      --ref) [ $# -ge 2 ] || die "--ref needs a branch or tag"; REF=$2; shift ;;
      --ref=*) REF=${1#*=} ;;
      --source) [ $# -ge 2 ] || die "--source needs a URL or path"; SOURCE=$2; shift ;;  # hidden (tests)
      --source=*) SOURCE=${1#*=} ;;
      --mode) [ $# -ge 2 ] || die "--mode needs plugin or settings"; MODE=$2; shift ;;
      --mode=*) MODE=${1#*=} ;;
      --all-projects) ALL_PROJECTS=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --yes | -y) YES=1 ;;
      --no-jev) NO_JEV=1 ;;
      --no-profile) NO_PROFILE=1 ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown option '$1' (see --help)" ;;
    esac
    shift
  done
  case $MODE in plugin | settings) ;; *) die "--mode must be plugin or settings, not '$MODE'" ;; esac
  [ -n "$REF" ] || die "--ref needs a branch or tag"
}

resolve_dir() {
  SELF_CLONE=""
  local src=${BASH_SOURCE[0]:-} d
  if [ -n "$src" ] && [ -f "$src" ]; then   # run from a file (bash install.sh), not from a pipe
    d=$(cd "$(dirname "$src")" && pwd -P)
    if [ -x "$d/bin/orche" ] && [ -f "$d/.claude-plugin/plugin.json" ]; then SELF_CLONE=$d; fi
  fi
  if [ -n "$DIR_ARG" ]; then ORCHE_DIR=$DIR_ARG
  elif [ -n "${ORCHE_DIR:-}" ]; then :
  elif [ -n "$SELF_CLONE" ]; then ORCHE_DIR=$SELF_CLONE
  else ORCHE_DIR=$HOME/orche
  fi
  # A literal ~ (from a quoted --dir "~/x" or ORCHE_DIR) is expanded here on purpose.
  # shellcheck disable=SC2088
  case $ORCHE_DIR in
    "~") ORCHE_DIR=$HOME ;;
    "~/"*) ORCHE_DIR=$HOME/${ORCHE_DIR#"~/"} ;;
    /*) ;;
    *) ORCHE_DIR=$PWD/$ORCHE_DIR ;;
  esac
  ORCHE_DIR=${ORCHE_DIR%/}
  case $ORCHE_DIR in
    *'"'* | *'$'* | *'`'* | *\\*) die "--dir must not contain \", \$, \` or \\: $ORCHE_DIR" ;;
  esac
  if [ -d "$ORCHE_DIR" ]; then ORCHE_DIR=$(cd "$ORCHE_DIR" && pwd); fi
}

main() {
  trap 'unexpected "$?" "$LINENO"' ERR
  set -E
  parse_args "$@"
  : "${HOME:?HOME is not set}"
  if [ "$DRY_RUN" = 1 ]; then say "dry run: printing the steps, writing nothing"; fi
  check_os
  check_requirements
  resolve_dir
  clone_or_update
  setup_path
  orche_install
  plugin_step
  apply_all
  doctor
  summary
}

main "$@"
