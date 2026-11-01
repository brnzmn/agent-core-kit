#!/usr/bin/env bash
# Mark a new worktree as one tool's lane. The guard in both tools refuses commits there from the other tool.
#   bash <kit folder>/bin/lane.sh <worktree> <claude|codex> [session]
# Writes <worktree>/.agent-lane (tool, session, branch, started) and keeps it out of git via info/exclude.
# Only for worktrees a lane creates; existing worktrees get no lane file.
set -euo pipefail
WT="${1:?worktree path}"; TOOL="${2:?claude or codex}"; SESSION="${3:-${CLAUDE_SESSION_ID:-${CODEX_THREAD_ID:-unknown}}}"
case "$TOOL" in claude|codex) ;; *) echo "lane: tool must be claude or codex" >&2; exit 2 ;; esac
[ -e "$WT/.git" ] || { echo "lane: $WT is not a worktree root" >&2; exit 2; }
[ -e "$WT/.agent-lane" ] && { echo "lane: $WT already has a lane file:"; cat "$WT/.agent-lane"; exit 1; }
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
printf 'tool: %s\nsession: %s\nbranch: %s\nstarted: %s\n' "$TOOL" "$SESSION" "$BRANCH" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "$WT/.agent-lane"
EXCLUDE="$(git -C "$WT" rev-parse --git-common-dir)/info/exclude"
case "$EXCLUDE" in /*) ;; *) EXCLUDE="$WT/$EXCLUDE" ;; esac
mkdir -p "$(dirname "$EXCLUDE")"
grep -qx '.agent-lane' "$EXCLUDE" 2>/dev/null || echo '.agent-lane' >> "$EXCLUDE"
echo "lane: $WT is a $TOOL lane on $BRANCH"
