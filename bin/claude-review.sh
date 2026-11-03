#!/usr/bin/env bash
# Read-only Claude Opus review of the current branch, for Codex (skill claude-review) and the merge gate.
#   bash bin/claude-review.sh [base]     run from the repo's root; base defaults to origin/HEAD
# Never loads the user's settings: --setting-sources "" drops the user, project and local files, so their allow
# rules can't widen it, and the only tools are file reads and git diff/log/show/status. A hostile diff therefore
# cannot run code through those rules. agent-core's own deny rules go in as an inline JSON string via --settings,
# built below: never a file path, never the user's settings. Do not add --mcp-config, and do not point --settings at
# a file.
# Prints the review; the last line is VERDICT: PASS or VERDICT: BLOCK.
# Exit 0 pass, 1 block, 3 local/no-verdict error, 4 failed provider execution (no verdict).
set -uo pipefail
BASE="${1:-origin/HEAD}"
# the agent-core folder, from this script's own location (bin/ sits one level below it)
CORE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
git rev-parse --verify -q "$BASE" >/dev/null || { echo "claude-review: base $BASE not found"; exit 3; }
command -v claude >/dev/null || { echo "claude-review: claude CLI not found"; exit 4; }
[ -f "$CORE/claude/agents/reviewer.md" ] || { echo "claude-review: $CORE/claude/agents/reviewer.md not found"; exit 3; }
ROLE=$(awk 'n >= 2 { print } /^---$/ { n++ }' "$CORE/claude/agents/reviewer.md")
OUT="$(mktemp "${TMPDIR:-/tmp}/claude-review-$(git rev-parse --short HEAD).XXXXXX")" || { echo "claude-review: cannot create a temp file"; exit 3; }
# agent-core's deny rules (they beat the allow list below). Credential files stay unreadable even if a hostile diff talks
# the reviewer into reading them; git diff/log/show may not write a file (--output) or read one outside the repo
# (git diff --no-index).
DENY=(
  "Read(~/.ssh/**)" "Read(~/.aws/**)" "Read(~/.config/gh/**)" "Read(~/.claude/.credentials.json)"
  "Read(~/.codex/auth.json)" "Read(~/.netrc)" "Read(**/.env)" "Read(**/.env.*)" "Read(**/*.pem)" "Read(**/*.key)"
  "Read(**/id_rsa)" "Read(**/id_ed25519)"
  "Bash(git diff --output*)" "Bash(git diff *--output*)" "Bash(git diff -o*)" "Bash(git diff * -o*)"
  "Bash(git diff --no-index*)" "Bash(git diff *--no-index*)"
  "Bash(git log --output*)" "Bash(git log *--output*)" "Bash(git show --output*)" "Bash(git show *--output*)"
)
SETTINGS='{"permissions":{"deny":['
SEP=""
for RULE in "${DENY[@]}"; do SETTINGS="$SETTINGS$SEP\"$RULE\""; SEP=","; done
SETTINGS="$SETTINGS]}}"
ASK="Review the change on this branch: git diff $BASE...HEAD. Read the surrounding code you need. You are the Claude verdict; another tool gives its own, so don't run other reviews. End with exactly one line: VERDICT: PASS if nothing is blocking, otherwise VERDICT: BLOCK."
# the prompt goes right after -p: the tool-list options take every argument that follows them
env -u CLAUDECODE claude -p "$ASK" --model opus --effort high --permission-mode dontAsk --setting-sources "" \
  --settings "$SETTINGS" --strict-mcp-config --append-system-prompt "$ROLE" \
  --allowedTools "Read" "Grep" "Glob" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git status:*)" \
  --disallowedTools "Edit" "Write" "NotebookEdit" < /dev/null > "$OUT" 2>&1
CLAUDE_STATUS=$?
cat "$OUT"
echo "(saved to $OUT)"
[ "$CLAUDE_STATUS" -eq 0 ] || exit 4
case "$(grep -E '^VERDICT: (PASS|BLOCK)[[:space:]]*$' "$OUT" | tail -1 | tr -d '[:space:]')" in
  VERDICT:PASS) exit 0 ;;
  VERDICT:BLOCK) exit 1 ;;
  *) exit 3 ;;
esac
