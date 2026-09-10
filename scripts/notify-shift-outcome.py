#!/usr/bin/env python3
"""
Post to #incidents when a shift run ends any way other than cleanly.

Every record this org keeps of a shift — the #shift-log post, the line in
logbook/SHIFTS.md — is written by the shift itself, partway through its own
run. So a run that dies before it gets there leaves nothing anywhere, and the
only trace is a red entry in a list nobody reads at 3am. That has now happened
three times (logbook#12), twice needing a hand-written backfill days later.

This script is the part that cannot be skipped by the thing it is watching. It
runs in a separate workflow run, triggered by `workflow_run`, so it survives
the shift's job dying, the runner dying, or the agent erroring on turn one.

Two rules it exists to obey:

  1. It reports anything that is not a clean success. Not an allowlist of
     `failure`/`cancelled`/`timed_out` — a denylist of `success` and
     `skipped`. The conclusions worth hearing about are the ones nobody
     thought to list, and GitHub adds new ones without asking.

  2. It never fails quietly, because a silent smoke alarm is worse than no
     smoke alarm. Slack answers HTTP 200 with {"ok": false} when it refuses
     you, so every send is checked twice and any failure exits non-zero to
     turn the watcher's own run red.

Stdlib only, on purpose: a notifier that needs `pip install` to run has one
more way to not run.

Env:
  SLACK_BOT_TOKEN   required
  SLACK_INCIDENTS   required, channel id
  WORKFLOW_RUN      required, the `github.event.workflow_run` object as JSON
  SLACK_API_BASE    optional, defaults to https://slack.com/api (tests)
  GITHUB_OUTPUT     optional, written with `ts=` so rerun-dead-shift.py can
                    reply in this message's thread
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

# Everything else is an incident. See rule 1 in the module docstring.
QUIET_CONCLUSIONS = {"success", "skipped"}

USERNAME = "shift watcher"


def fail(message):
    print(f"notify-shift-outcome: {message}", file=sys.stderr)
    sys.exit(1)


def require(name):
    value = os.environ.get(name, "")
    if not value:
        fail(f"{name} is empty or unset; cannot report a shift outcome without it")
    return value


def parse_run(raw):
    try:
        run = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"WORKFLOW_RUN is not valid JSON: {exc}")
    if not isinstance(run, dict):
        fail("WORKFLOW_RUN is not a JSON object")
    return run


def duration(run):
    """Wall clock from the run starting to it finishing, as '1h 4m 9s'.

    Best effort: a missing or unparseable timestamp costs us one field in the
    message, and must not cost us the message.
    """
    started, ended = run.get("run_started_at"), run.get("updated_at")
    if not started or not ended:
        return None
    try:
        seconds = int(
            (
                datetime.fromisoformat(ended.replace("Z", "+00:00"))
                - datetime.fromisoformat(started.replace("Z", "+00:00"))
            ).total_seconds()
        )
    except ValueError:
        return None
    if seconds < 0:
        return None
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if hours or minutes else [])
    return " ".join(parts + [f"{seconds}s"])


def facts(run):
    """The one-line provenance of a run, from data the event always carries."""
    bits = [
        f"run `{run.get('id', '?')}`",
        f"attempt {run.get('run_attempt', '?')}",
        f"trigger `{run.get('event', '?')}`",
        f"branch `{run.get('head_branch', '?')}`",
    ]
    took = duration(run)
    if took:
        bits.append(f"took {took}")
    return " · ".join(bits)


def compose(run):
    """The message, or None if this run is nobody's problem.

    A re-run that succeeds gets a line of its own. Without it, the failure
    from attempt 1 sits in #incidents looking open forever, which is what
    made the third occurrence take an hour of reading Actions timestamps to
    understand.
    """
    name = run.get("name") or "A shift"
    conclusion = run.get("conclusion") or "no conclusion"
    url = run.get("html_url", "")
    attempt = run.get("run_attempt", 1)

    if conclusion in QUIET_CONCLUSIONS:
        if conclusion == "success" and isinstance(attempt, int) and attempt > 1:
            return (
                f":white_check_mark: *{name}* recovered on attempt {attempt}.\n"
                f"{facts(run)}\n{url}\n"
                "The earlier failure for this run is closed. Its #shift-log post "
                "and ledger line, if any, come from the shift itself."
            )
        return None

    return (
        f":rotating_light: *{name}* did not complete — `{conclusion}`\n"
        f"{facts(run)}\n{url}\n"
        "This org's records of a shift — the #shift-log post, the SHIFTS.md line — "
        "are written by the shift partway through its own run, so check how far "
        "this one got and backfill what it missed."
    )


def post(api_base, token, channel, text):
    body = json.dumps(
        {"channel": channel, "text": text, "username": USERNAME}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        fail(f"could not reach Slack: {exc}")

    # Slack says HTTP 200 and then refuses you in the body. This is the whole
    # reason the check below is two checks.
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        fail(f"Slack returned a non-JSON body: {payload[:400]}")
    if not parsed.get("ok"):
        fail(f"Slack refused the message: {payload[:400]}")
    return parsed.get("ts", "")


def emit_output(name, value):
    """Hand something to a later step in the same job.

    Best effort, like `duration`: this exists so the re-run reply lands in
    this message's thread, and a missing thread is worth less than a missing
    incident. Never a reason to go red.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path or not value:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    except OSError as exc:
        print(f"notify-shift-outcome: could not write {name}: {exc}", file=sys.stderr)


def main():
    run = parse_run(require("WORKFLOW_RUN"))
    text = compose(run)
    if text is None:
        print(
            f"nothing to report: {run.get('name', '?')} run {run.get('id', '?')} "
            f"concluded {run.get('conclusion', '?')} on attempt "
            f"{run.get('run_attempt', '?')}"
        )
        return

    # Read after composing, so a bad event is diagnosed as a bad event rather
    # than as missing credentials.
    token = require("SLACK_BOT_TOKEN")
    channel = require("SLACK_INCIDENTS")
    api_base = os.environ.get("SLACK_API_BASE", "https://slack.com/api").rstrip("/")

    ts = post(api_base, token, channel, text)
    print(f"posted to {channel} as {ts}")
    emit_output("ts", ts)


if __name__ == "__main__":
    main()
