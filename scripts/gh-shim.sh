#!/usr/bin/env bash
# Installed at /usr/bin/gh by the setup script; the real binary is moved
# to /usr/bin/gh-real. Commit at scripts/gh-shim.sh in logbook.
#
# Why a shim rather than exporting GH_TOKEN once: installation tokens die
# after an hour, the environment's variables are copied in once at
# startup, and a shift can easily run longer than that. Minting per call
# (cached, so it's cheap) means the push at the end of a long build still
# works.

set -uo pipefail

REAL_GH=/usr/local/bin/gh-real

[ -x "$REAL_GH" ] || {
  echo "gh shim: $REAL_GH missing — the setup script did not complete" >&2
  exit 127
}

# Not a configured shift session: behave like normal gh.
if [[ -z "${APP_ID:-}" ]]; then
  exec "$REAL_GH" "$@"
fi

# Mint an installation token so commits are attributed to the app rather
# than to the account behind the session's built-in GitHub auth.
#
# Minting can fail for reasons that are not ours to route around — an
# egress policy that rejects the installation-token endpoint, for one.
# When it does, fall back to running gh unmodified: the built-in GitHub
# proxy authenticates it, so the shift can still do its work. It loses
# the bot identity, which the shift's own prompt tells it to report.
if TOKEN="$(/usr/local/bin/mint-token.sh 2>/tmp/.mint-error)"; then
  exec env GH_TOKEN="$TOKEN" "$REAL_GH" "$@"
else
  if [[ ! -f /tmp/.mint-warned ]]; then
    echo "gh shim: token minting unavailable — $(tr -d '\n' < /tmp/.mint-error)" >&2
    echo "gh shim: falling back to the session's own GitHub auth; commits will NOT be attributed to ${SHIFT_NAME:-the bot}" >&2
    touch /tmp/.mint-warned
  fi
  exec "$REAL_GH" "$@"
fi
