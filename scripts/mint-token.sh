#!/usr/bin/env bash
# Mint a GitHub App installation token inside a cloud session.
# Prints the token to stdout. Commit at scripts/mint-token.sh in logbook.
#
# Reads these from the cloud environment's Environment variables panel:
#   SHIFT_NAME=richmond
#   APP_ID=123456
#   INSTALLATION_ID=78901234
#   APP_PRIVATE_KEY_B64=<base64 of the .pem, single line>
#
# The PEM is base64'd because the panel takes .env format and a
# multi-line PEM won't survive it. Generate with:
#   base64 -w0 richmond.pem

set -euo pipefail

: "${APP_ID:?APP_ID not set — check the environment variables panel}"
: "${INSTALLATION_ID:?INSTALLATION_ID not set}"
: "${APP_PRIVATE_KEY_B64:?APP_PRIVATE_KEY_B64 not set}"

CACHE="/tmp/.${SHIFT_NAME:-shift}.token"
KEY="/tmp/.${SHIFT_NAME:-shift}.pem"

# Reuse a cached token while it has real life left.
if [[ -f "$CACHE" ]]; then
  cached_exp=$(head -1 "$CACHE")
  if [[ "$cached_exp" =~ ^[0-9]+$ ]] && (( cached_exp - 300 > $(date +%s) )); then
    tail -1 "$CACHE"
    exit 0
  fi
fi

umask 077
base64 -d <<<"$APP_PRIVATE_KEY_B64" > "$KEY"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

now=$(date +%s)
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$APP_ID" | b64url)
signature=$(printf '%s.%s' "$header" "$payload" \
  | openssl dgst -sha256 -sign "$KEY" -binary | b64url)
jwt="$header.$payload.$signature"

response=$(curl -sS -X POST \
  -H "Authorization: Bearer $jwt" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/app/installations/${INSTALLATION_ID}/access_tokens")

token=$(jq -er '.token' <<<"$response") || {
  echo "token mint failed: $response" >&2
  exit 1
}

exp_epoch=$(date -u -d "$(jq -r '.expires_at' <<<"$response")" +%s)
printf '%s\n%s\n' "$exp_epoch" "$token" > "$CACHE"
echo "$token"
