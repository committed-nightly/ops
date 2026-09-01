#!/bin/bash
# SessionStart hook. Runs after Claude Code launches, which is the first
# point at which the environment's variables actually exist.
# Commit at scripts/shift-init.sh in logbook.

set -u

# Only in cloud sessions, and only when this is a configured shift.
[ "${CLAUDE_CODE_REMOTE:-}" != "true" ] && exit 0
[ -z "${SHIFT_NAME:-}" ] && exit 0
[ -z "${APP_ID:-}" ] && exit 0

# Commit identity. Without the numeric bot user id in the email address,
# GitHub won't attribute the commit to the app and the avatar comes out
# blank. Get the id once with:
#   gh api /users/richmond%5Bbot%5D --jq .id
git config --global user.name  "${SHIFT_NAME}[bot]"
git config --global user.email "${BOT_USER_ID}+${SHIFT_NAME}[bot]@users.noreply.github.com"

# git pushes authenticate with a freshly minted token, same reasoning as
# the gh shim.
git config --global credential."https://github.com".helper \
  '!f() { echo username=x-access-token; echo "password=$(/usr/local/bin/mint-token.sh)"; }; f'

git config --global credential."https://github.com".useHttpPath false

# Sanity check, visible in the transcript if it fails.
if ! gh api /orgs/committed-nightly --jq .login >/dev/null 2>&1; then
  echo "WARNING: gh cannot reach the org — token mint or app install is wrong" >&2
fi

exit 0
