#!/usr/bin/env bash
# Installed at /usr/bin/gh by the setup script; the real binary is moved
# to /usr/bin/gh-real. Commit at scripts/gh-shim.sh in logbook.
#
# Why a shim rather than exporting GH_TOKEN once: installation tokens die
# after an hour, the environment's variables are copied in once at
# startup, and a shift can easily run longer than that. Minting per call
# (cached, so it's cheap) means the push at the end of a long build still
# works.

set -euo pipefail

if [[ -z "${APP_ID:-}" ]]; then
  # Not a configured shift session — behave like normal gh.
  exec /usr/bin/gh-real "$@"
fi

exec env GH_TOKEN="$(/usr/local/bin/mint-token.sh)" /usr/bin/gh-real "$@"
