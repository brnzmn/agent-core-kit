#!/usr/bin/env bash
# One place for the tokens agents use, named agent/<provider>/<account>: the macOS login keychain, or on Linux
# the desktop secret store through secret-tool (package libsecret-tools). Values never pass through argv.
#
#   bin/secret.sh get <provider>/<account>        print the value (use inside $(...), never echo it into chat)
#   bin/secret.sh set <provider>/<account>        read the value from stdin, e.g. `gh auth token | secret.sh set github/<user>`
#   bin/secret.sh has <provider>/<account>        exit 0 if stored
#   bin/secret.sh list                            names only
#
# The provider is the service (github, vercel, cloudflare...); the account is that service's user, team or org name.
set -euo pipefail

die() { echo "secret.sh: $*" >&2; exit 2; }
valid() { [[ "$1" =~ ^[a-z0-9-]+/[A-Za-z0-9._@-]+$ ]] || die "name must be <provider>/<account>, got '$1'"; }

usage() {
  echo "usage: secret.sh get|set|has <provider>/<account>"
  echo "       secret.sh list"
  echo "Stores agent/<provider>/<account> in the macOS keychain, or on Linux in the desktop secret store (secret-tool)."
}

# macos, linux, or a one-line hint when neither store is available
backend() {
  if [[ "$OSTYPE" == darwin* ]] && command -v security >/dev/null 2>&1; then
    echo macos
  elif command -v secret-tool >/dev/null 2>&1; then
    echo linux
  else
    die "no secret store found: macOS needs the security tool, Linux needs secret-tool (apt install libsecret-tools)"
  fi
}

cmd="${1:-}"
case "$cmd" in
  get)
    valid "${2:-}"
    kind="$(backend)"
    if [ "$kind" = macos ]; then
      security find-generic-password -s "agent/$2" -w 2>/dev/null || die "agent/$2 is not in the keychain; store it with: <source> | secret.sh set $2"
    else
      value="$(secret-tool lookup service "agent/$2" 2>/dev/null)" && [ -n "$value" ] || die "agent/$2 is not in the secret store; store it with: <source> | secret.sh set $2"
      printf '%s\n' "$value"
    fi
    ;;
  has)
    valid "${2:-}"
    kind="$(backend)"
    if [ "$kind" = macos ]; then
      security find-generic-password -s "agent/$2" >/dev/null 2>&1 || exit 1
    else
      secret-tool lookup service "agent/$2" >/dev/null 2>&1 || exit 1
    fi
    ;;
  set)
    valid "${2:-}"
    kind="$(backend)"
    [ -t 0 ] && echo "secret.sh: paste the value, then press Ctrl-D" >&2
    value="$(cat)"
    value="${value%$'\n'}"
    [ -n "$value" ] || die "empty value; nothing stored"
    account="${2#*/}"
    if [ "$kind" = macos ]; then
      [[ "$value" != *[\"\\$'\n']* ]] || die "value contains a quote, backslash or newline; store it with Keychain Access instead"
      printf 'add-generic-password -U -s "agent/%s" -a "%s" -l "agent/%s" -w "%s"\n' "$2" "$account" "$2" "$value" \
        | security -i >/dev/null
      security find-generic-password -s "agent/$2" >/dev/null 2>&1 || die "keychain write failed for agent/$2"
    else
      printf '%s' "$value" | secret-tool store --label="agent/$2" app agent-core service "agent/$2" account "$account" \
        || die "secret-tool could not store agent/$2 (is a keyring unlocked?)"
      secret-tool lookup service "agent/$2" >/dev/null 2>&1 || die "secret store write failed for agent/$2"
    fi
    echo "stored agent/$2"
    ;;
  list)
    kind="$(backend)"
    if [ "$kind" = macos ]; then
      security dump-keychain 2>/dev/null | sed -n 's/^ *"svce"<blob>="agent\/\(.*\)"$/\1/p' | sort -u || true
    else
      { secret-tool search --all app agent-core 2>/dev/null || true; } \
        | sed -n 's/^attribute\.service = agent\/\(.*\)$/\1/p' | sort -u
    fi
    ;;
  *)
    usage
    [ -z "$cmd" ] || exit 2
    ;;
esac
