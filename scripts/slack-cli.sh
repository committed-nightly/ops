#!/usr/bin/env bash
# slack — the CLI both shifts call. Commit at scripts/slack-cli.sh in
# logbook; the setup script installs it to /usr/local/bin/slack.
#
#   slack post <channel> [< body]        posts as this shift, prints ts
#   slack reply <channel> <ts> [< body]  threaded reply, prints ts
#   slack read <channel> [n]             oldest first, each line "ts  text"
#   slack permalink <channel> <ts>       prints the message URL
#
# <channel> is a friendly name — shift-log, orders, incidents — resolved
# against the SLACK_* channel ids in the environment. Bodies come from
# stdin so a shift can write a long note to a file and pipe it in without
# fighting shell quoting.
#
# There is no token in this file and none needs to be. The bot token is
# an API credential on the cloud environment: requests leave the VM with
# no Authorization header and the agent proxy attaches the key on the way
# out. A not_authed error means the credential is missing from the
# environment, not that something here is wrong.
#
# Every call checks the `ok` field. Slack answers HTTP 200 with
# {"ok":false,"error":"not_in_channel"} and friends, so without this a
# shift can post nothing for a month and never see a failure.

set -euo pipefail

API="https://slack.com/api"

die() { echo "slack: $*" >&2; exit 1; }

# The display name is per-message, not per-token — one Slack app posts as
# both shifts. Deriving it from SHIFT_NAME is what stops a shift posting
# under the other one's name, or under the bare app name.
case "${SHIFT_NAME:-}" in
  richmond-avenal)      USERNAME="Richmond"; ICON=":crescent_moon:" ;;
  jennifer-barber)      USERNAME="Jen";      ICON=":sunny:" ;;
  *) die "SHIFT_NAME is unset or unrecognised — cannot choose an identity" ;;
esac

resolve() {
  case "$1" in
    shift-log) echo "${SLACK_SHIFT_LOG:?SLACK_SHIFT_LOG not set}" ;;
    orders)    echo "${SLACK_ORDERS:?SLACK_ORDERS not set}" ;;
    incidents) echo "${SLACK_INCIDENTS:?SLACK_INCIDENTS not set}" ;;
    C*)        echo "$1" ;;   # a raw channel id, passed through
    *)         die "unknown channel '$1' (try shift-log, orders, incidents)" ;;
  esac
}

check_ok() {
  local response="$1" what="$2"
  [[ "$(jq -r '.ok' <<<"$response")" == "true" ]] \
    || die "$what failed — $(jq -r '.error // "unknown error"' <<<"$response")"
}

cmd_post() {
  local channel body payload response
  channel=$(resolve "${1:?usage: slack post <channel>}")
  body=$(cat)
  [[ -n "${body//[[:space:]]/}" ]] || die "refusing to post an empty message"

  payload=$(jq -n --arg c "$channel" --arg t "$body" \
                  --arg u "$USERNAME" --arg i "$ICON" \
    '{channel:$c, text:$t, username:$u, icon_emoji:$i, unfurl_links:false}')

  response=$(curl -sS -X POST "$API/chat.postMessage" \
    -H 'Content-Type: application/json; charset=utf-8' -d "$payload")
  check_ok "$response" "post"
  jq -r '.ts' <<<"$response"
}

cmd_reply() {
  local channel parent body payload response
  channel=$(resolve "${1:?usage: slack reply <channel> <ts>}")
  parent="${2:?usage: slack reply <channel> <ts>}"
  body=$(cat)
  [[ -n "${body//[[:space:]]/}" ]] || die "refusing to post an empty reply"

  payload=$(jq -n --arg c "$channel" --arg p "$parent" --arg t "$body" \
                  --arg u "$USERNAME" --arg i "$ICON" \
    '{channel:$c, thread_ts:$p, text:$t, username:$u, icon_emoji:$i, unfurl_links:false}')

  response=$(curl -sS -X POST "$API/chat.postMessage" \
    -H 'Content-Type: application/json; charset=utf-8' -d "$payload")
  check_ok "$response" "reply"
  jq -r '.ts' <<<"$response"
}

cmd_read() {
  local channel limit response
  channel=$(resolve "${1:?usage: slack read <channel> [n]}")
  limit="${2:-20}"

  response=$(curl -sS -G "$API/conversations.history" \
    --data-urlencode "channel=$channel" \
    --data-urlencode "limit=$limit")
  check_ok "$response" "read"

  # Slack returns newest first; reverse so a shift reads it like a
  # conversation. Each line starts with the ts so it can be threaded off.
  jq -r '.messages | reverse | .[]
         | "\(.ts)  \(.username // .user // "?"): \(.text | gsub("\n"; " "))"' \
    <<<"$response"
}

cmd_permalink() {
  local channel ts response
  channel=$(resolve "${1:?usage: slack permalink <channel> <ts>}")
  ts="${2:?usage: slack permalink <channel> <ts>}"

  response=$(curl -sS -G "$API/chat.getPermalink" \
    --data-urlencode "channel=$channel" \
    --data-urlencode "message_ts=$ts")
  check_ok "$response" "permalink"
  jq -r '.permalink' <<<"$response"
}

case "${1:-}" in
  post)      shift; cmd_post "$@" ;;
  reply)     shift; cmd_reply "$@" ;;
  read)      shift; cmd_read "$@" ;;
  permalink) shift; cmd_permalink "$@" ;;
  *) die "usage: slack {post|reply|read|permalink} <channel> [args]" ;;
esac
