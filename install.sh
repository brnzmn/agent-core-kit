#!/usr/bin/env bash
# agent-core-kit installer for Claude Code and Codex: one rulebook, subagents, hooks and skills for both.
#
#   bash install.sh                    first install; when the kit is already installed, the same as --sync
#   bash install.sh --sync             rebuild and copy rules, subagents, skills and hooks; refresh git pre-commit hooks
#   bash install.sh --check            show drift between this kit (plus your local/ folder) and what is installed
#   bash install.sh --restore <name>   put a backup from local/backups/ back
#   bash install.sh --uninstall        take the kit out of both tools and put back what the install changed (your own
#                                      CLAUDE.md, settings values, ...); the kit's files move into a backup, nothing is deleted
#   bash install.sh --clean-slate      first install that also moves your existing ~/.claude agents, skills, commands,
#                                      rules and hooks into the backup (default: they stay and the kit installs beside them)
#
# Environment: AGENT_CORE_LOCAL moves the local/ folder (install.sh records it in ~/.claude/hooks/agent-core/.local-dir, so
# later runs and the hooks find it without the variable); AGENT_CORE_CODEX_RUNNING=0 skips the "is Codex running" check.
set -euo pipefail

CORE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
CODEX_DIR="$HOME/.codex"
# The personal folder: AGENT_CORE_LOCAL, else the one recorded at install (.local-dir), else <kit>/local.
RECORDED_LOCAL=""
if [ -f "$CLAUDE_DIR/hooks/agent-core/.local-dir" ]; then
  RECORDED_LOCAL="$(cat "$CLAUDE_DIR/hooks/agent-core/.local-dir" 2>/dev/null || true)"
fi
LOCAL="${AGENT_CORE_LOCAL:-}"
if [ -z "$LOCAL" ] && [ -n "$RECORDED_LOCAL" ]; then
  LOCAL="$RECORDED_LOCAL"
  # <old kit>/local that is gone because the kit folder moved: the personal folder moved with it
  OLD_CORE="$(cat "$CLAUDE_DIR/hooks/agent-core/.core-dir" 2>/dev/null || true)"
  if [ -n "$OLD_CORE" ] && [ "$OLD_CORE" != "$CORE" ] && [ "$LOCAL" = "$OLD_CORE/local" ] && [ ! -d "$LOCAL" ]; then
    LOCAL="$CORE/local"
  fi
fi
[ -n "$LOCAL" ] || LOCAL="$CORE/local"
case "$LOCAL" in "~" | "~/"*) LOCAL="$HOME${LOCAL#\~}" ;; esac
case "$LOCAL" in /*) ;; *) LOCAL="$PWD/$LOCAL" ;; esac
export AGENT_CORE_DIR="$CORE"
export AGENT_CORE_LOCAL="$LOCAL"
PY=""
BK=""

tilde() {
  local rest
  case "$1" in
    "$HOME") echo "~" ;;
    "$HOME"/*) rest="${1#$HOME}"; echo "~$rest" ;;
    *) echo "$1" ;;
  esac
}
KIT_T="$(tilde "$CORE")"

die() { echo "agent-core: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

preflight() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    MINGW* | MSYS* | CYGWIN*) die "Windows is not supported. Use WSL, or macOS or Linux." ;;
  esac
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || die "python3 not found. On macOS run: xcode-select --install, then rerun this script."
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
    die "python3 3.9 or newer is needed (found $("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')). On macOS run: xcode-select --install"
  command -v git >/dev/null 2>&1 || die "git not found. On macOS run: xcode-select --install, then rerun this script."
  if [ "${1:-}" != "quiet" ]; then
    command -v claude >/dev/null 2>&1 ||
      warn "claude CLI not found; the Codex plugin for Claude Code is skipped. Install Claude Code, then rerun: bash $KIT_T/install.sh --sync"
    if ! command -v codex >/dev/null 2>&1 && [ ! -d "$CODEX_DIR" ]; then
      warn "codex CLI not found and $(tilde "$CODEX_DIR") does not exist; Codex files are skipped. Install and start Codex once, then rerun: bash $KIT_T/install.sh --sync"
    fi
  fi
}

build() { "$PY" "$CORE/bin/build.py" "$@" || die "bin/build.py $* failed (see above). Backups, if any: $(tilde "$LOCAL/backups")"; }

is_installed() { [ -f "$CLAUDE_DIR/hooks/agent-core/guard.py" ] && [ -f "$CLAUDE_DIR/hooks/agent-core/.core-dir" ]; }

git_identity() {
  # local/ commits need a name and email; borrow neutral ones only when git has none configured.
  if [ -z "$(git -C "$LOCAL" config user.name 2>/dev/null || true)" ]; then
    export GIT_AUTHOR_NAME="agent-core" GIT_COMMITTER_NAME="agent-core"
  fi
  if [ -z "$(git -C "$LOCAL" config user.email 2>/dev/null || true)" ]; then
    export GIT_AUTHOR_EMAIL="agent-core@localhost" GIT_COMMITTER_EMAIL="agent-core@localhost"
  fi
}

ensure_local() {
  local d
  mkdir -p "$LOCAL/learning"
  for d in inbox review proposals; do
    mkdir -p "$LOCAL/learning/$d"
  done
  if [ ! -f "$LOCAL/AGENTS.md" ]; then
    cp "$CORE/templates/core/AGENTS.md" "$LOCAL/AGENTS.md"
    chmod u+rw "$LOCAL/AGENTS.md"
    echo "       created $(tilde "$LOCAL")/AGENTS.md (your rulebook; /setup rewrites it)"
  fi
  if [ ! -f "$LOCAL/config.json" ]; then
    cp "$CORE/config.example.json" "$LOCAL/config.json"
    chmod u+rw "$LOCAL/config.json"
    echo "       created $(tilde "$LOCAL")/config.json"
  fi
  if [ ! -f "$LOCAL/.gitignore" ]; then
    printf '%s\n' "# backups can hold copies of settings.json, which may contain tokens" "backups/" "install-state.json" \
      "learning/inbox/" "learning/review/" "learning/.state.json" "learning/.notice_state.json" "learning/hook-errors.log" ".DS_Store" "__pycache__/" \
      >"$LOCAL/.gitignore"
  elif ! grep -qx 'install-state.json' "$LOCAL/.gitignore"; then
    # a local/ made by an earlier kit: the install record now lives here and stays out of its commits
    [ -z "$(tail -c1 "$LOCAL/.gitignore")" ] || printf '\n' >>"$LOCAL/.gitignore"
    printf '%s\n' "install-state.json" >>"$LOCAL/.gitignore"
  fi
  if [ ! -d "$LOCAL/.git" ]; then
    for d in inbox review proposals; do
      [ -e "$LOCAL/learning/$d/.gitkeep" ] || : >"$LOCAL/learning/$d/.gitkeep"
    done
    git_identity
    git -C "$LOCAL" init -q
    git -C "$LOCAL" symbolic-ref HEAD refs/heads/main >/dev/null 2>&1 || true
    git -C "$LOCAL" add -- AGENTS.md config.json .gitignore learning
    git -C "$LOCAL" -c commit.gpgsign=false commit -q -m "agent-core-kit: initial local files" >/dev/null ||
      warn "could not commit in $(tilde "$LOCAL"); run git there yourself"
    echo "       $(tilde "$LOCAL") is now a git repo with those files committed"
  fi
}

snapshot() {
  # first install: copy every file the kit may change into the backup before anything is touched (a symlink is saved
  # by content and noted as a link)
  build --snapshot
}

copy_managed() {
  build --install-claude
  build --install-skills
  build --install-codex
  build --install-plugin
}

git_hooks() {
  "$PY" "$CORE/bin/git_hooks.py" "$1" -q || true
}

new_backup_dir() {
  BK="$LOCAL/backups/$(date +%Y%m%d-%H%M%S)"
  export AGENT_CORE_BACKUP="$BK"
}

first_install() {
  local clean="${1:-}"
  new_backup_dir
  "$PY" "$CORE/bin/build.py" --validate || die "nothing was changed"
  echo "1/7  Setting up your local folder ($(tilde "$LOCAL"))"
  ensure_local
  echo "2/7  Backing up the files the kit will change"
  snapshot
  if [ "$clean" = "clean" ]; then
    echo "3/7  Moving your existing ~/.claude agents, skills, commands, rules and hooks into the backup (--clean-slate)"
    build --clean-slate
  else
    echo "3/7  Leaving your existing agents, skills, commands, rules and hooks where they are"
  fi
  echo "4/7  Building"
  build
  echo "5/7  Installing instructions, subagents, hooks and skills"
  copy_managed
  echo "6/7  Merging settings and installing the git pre-commit hook"
  if [ "$clean" = "clean" ]; then
    build --merge-settings clean
  else
    build --merge-settings fresh
  fi
  git_hooks install
  echo "7/7  Checking"
  "$PY" "$CORE/bin/build.py" --check || true
  cat <<MSG

Installed.

Next:
  1. Start a new Claude Code session so the hooks and subagents load.
  2. Run /setup. It asks a few questions and writes your rules and config into $(tilde "$LOCAL").
  3. Codex: run codex, type /hooks, and trust the new hooks once.

Update later:   cd $KIT_T && git pull && bash install.sh --sync
Check drift:    bash $KIT_T/install.sh --check
MSG
  if [ -d "$BK" ]; then
    echo "Backup of what was replaced: $(tilde "$BK")"
    echo "Put it back:    bash $KIT_T/install.sh --restore $(basename "$BK")"
    if [ -f "$BK/claude/CLAUDE.md" ] || [ -f "$BK/codex/AGENTS.md" ]; then
      echo "Your old CLAUDE.md or AGENTS.md is no longer loaded; copy anything you want to keep into"
      echo "$(tilde "$LOCAL")/AGENTS.md (or claude.md / codex.md there), or run /setup."
    fi
  fi
  echo "Remove the kit again: bash $KIT_T/install.sh --uninstall (puts your own files back; the kit's files move into a backup)"
}

sync_install() {
  local old=""
  new_backup_dir
  "$PY" "$CORE/bin/build.py" --validate || die "nothing was changed"
  if [ -f "$CLAUDE_DIR/hooks/agent-core/.core-dir" ]; then
    old="$(cat "$CLAUDE_DIR/hooks/agent-core/.core-dir" 2>/dev/null || true)"
    if [ -n "$old" ] && [ "$old" != "$CORE" ]; then
      echo "note: the installed hooks pointed at $old; they now point at $CORE"
    fi
  fi
  if [ -n "$RECORDED_LOCAL" ] && [ "$RECORDED_LOCAL" != "$LOCAL" ]; then
    echo "note: the installed hooks pointed at the personal folder $RECORDED_LOCAL; they now point at $LOCAL"
  fi
  ensure_local
  build
  copy_managed
  build --merge-settings keep
  git_hooks install
  "$PY" "$CORE/bin/build.py" --check || true
  if [ -d "$BK" ]; then
    echo "Backup of what was replaced: $(tilde "$BK") (restore: bash $KIT_T/install.sh --restore $(basename "$BK"))"
  fi
  echo "Synced. Changes to hooks, subagents and instructions apply from the next session."
}

check_install() {
  local rc=0
  is_installed || die "the kit is not installed here yet. Run: bash $KIT_T/install.sh"
  "$PY" "$CORE/bin/build.py" --check || rc=$?
  "$PY" "$CORE/bin/git_hooks.py" check -q || rc=$?
  return "$rc"
}

restore() {
  local name="${1:-}"
  if [ -z "$name" ]; then
    die "usage: install.sh --restore <name>. Available: $(ls "$LOCAL/backups" 2>/dev/null | tr '\n' ' ')"
  fi
  case "$name" in */* | .*) die "give the backup's name (a folder in $(tilde "$LOCAL/backups")), not a path" ;; esac
  "$PY" "$CORE/bin/build.py" --restore "$name"
}

uninstall_all() {
  git_hooks uninstall
  build --uninstall
}

case "${1:-}" in
  "")
    preflight
    if is_installed; then
      echo "agent-core-kit is already installed; running --sync (use --restore to go back)."
      sync_install
    else
      first_install
    fi
    ;;
  --sync)
    preflight
    if is_installed; then sync_install; else first_install; fi
    ;;
  --clean-slate)
    preflight
    if is_installed; then
      die "agent-core-kit is already installed, and --clean-slate is for a first install only. To move more of your own files aside, run bash $KIT_T/install.sh --uninstall first, then --clean-slate again."
    fi
    first_install clean
    ;;
  --check)
    preflight quiet
    check_install
    ;;
  --restore)
    preflight quiet
    restore "${2:-}"
    ;;
  --uninstall)
    preflight quiet
    uninstall_all
    ;;
  -h | --help)
    usage
    ;;
  *)
    die "unknown option $1 (use --sync, --check, --restore <name>, --uninstall or --clean-slate)"
    ;;
esac
