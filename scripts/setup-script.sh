#!/bin/bash
# Paste this into the "Setup script" field of the Richmond and Jen
# cloud environments (identical for both).
#
# Runs as root, before Claude Code launches, and CANNOT see the
# environment variables you configured — they're only injected once the
# session is running. So this script does no minting. All it does is
# install the gh shim, which reads those variables later, at call time.
#
# Must exit zero or the session won't start.

set -u

REPO_RAW="https://raw.githubusercontent.com/committed-nightly/logbook/main/scripts"

# Move the real gh aside and drop the shim in its place. The shim mints a
# fresh installation token per invocation, which is what keeps a shift
# alive past the one-hour token expiry.
if [ -x /usr/bin/gh ] && [ ! -x /usr/bin/gh-real ]; then
  mv /usr/bin/gh /usr/bin/gh-real
fi

curl -fsSL "$REPO_RAW/mint-token.sh" -o /usr/local/bin/mint-token.sh || true
curl -fsSL "$REPO_RAW/gh-shim.sh"    -o /usr/bin/gh || true

chmod +x /usr/local/bin/mint-token.sh /usr/bin/gh 2>/dev/null || true

# If either download failed, restore the real gh rather than leaving a
# broken one on PATH.
if [ ! -s /usr/bin/gh ] && [ -x /usr/bin/gh-real ]; then
  cp /usr/bin/gh-real /usr/bin/gh
fi

exit 0
