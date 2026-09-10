#!/usr/bin/env python3
"""
Give a shift that died one automatic second attempt, and say so in #incidents.

A shift that fails at 03:00 currently sits there until a person notices and
presses the button. That has happened twice: run 33769793117 was re-run by
hand 18 minutes later (and failed again), and run 33819369404 failed at 23:59
and was re-run at 06:48 the next morning, where it succeeded. The second one
is the whole argument for this script — a night was lost to a button nobody
was awake to press.

It runs after notify-shift-outcome.py, in the same watcher run, and replies in
that message's thread. It never posts top-level noise of its own: if the
watcher had nothing to say about a run, neither does this.

## What it re-runs, and why so little

Only `failure`, only on attempt 1. Everything else gets a line in the thread
saying why not, and the command to do it by hand.

  * `cancelled` — somebody stopped this on purpose. Undoing that is rude.
  * `timed_out` — it had all 120 minutes. A re-run gets the same 120 minutes
    and, in the usual case, the same ending; it is also the most expensive
    re-run available.
  * `startup_failure` and anything else — the run never really began, or GitHub
    invented a conclusion after this was written. Either way a person should
    look before a machine repeats it.
  * attempt > 1 — one automatic re-run per run, ever. GitHub's own attempt
    counter is the loop guard, which means it holds even if the second attempt
    is started by a human, or by a future version of this script.

## The thing this deliberately does not try to do

There is no check for "did the shift already do some work". I went looking for
one and there isn't a signal the watcher can see:

  * Duration doesn't separate them. Run 33819369404's agent step failed after
    7m33s; Jen's shift on 09-04 *succeeded* in 5m50s. Any threshold that
    catches the first one is under the second.
  * The #shift-log post doesn't either. Only two of the fourteen notes in the
    channel quote their own run id (the convention landed on 2026-09-08), Jen
    posts hers as thread replies where conversations.history can't see them,
    and Moss doesn't post there at all.

What actually makes a second attempt safe is the shift itself: every shift
starts by reading SHIFTS.md, #orders and the open PRs. If attempt 1 got far
enough to leave records, attempt 2 reads them at clock-in and carries on from
there rather than starting again. That is a property of the skill, not of this
script, which is why it is written down here — if clock-in ever stops reading
the ledger first, this script stops being safe.

All three shift failures so far left no record at all, so this has never yet
been tested against a shift that got halfway. The known hole is a branch
pushed with no PR and no ledger line: attempt 2 would not see it.

## Switching it off

Set the repository variable `SHIFT_AUTO_RERUN` to `off` (or `0`, `false`,
`no`). Unset means on — the point of the thing is to work at 3am without
anyone having configured it. When it is off it still posts the thread reply
saying so, because a feature that is silently disabled and a feature that is
silently broken read identically.

Env:
  WORKFLOW_RUN      required, the `github.event.workflow_run` object as JSON
  SLACK_BOT_TOKEN   required unless the run is quiet
  SLACK_INCIDENTS   required unless the run is quiet, channel id
  GITHUB_TOKEN      required only when actually re-running; needs actions:write
  GITHUB_REPOSITORY required only when actually re-running, as owner/repo
  INCIDENT_TS       optional, ts of the watcher's message, to reply in thread
  SHIFT_AUTO_RERUN  optional, `off` to disable
  SLACK_API_BASE    optional, defaults to https://slack.com/api (tests)
  GITHUB_API_BASE   optional, defaults to https://api.github.com (tests)
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Kept identical to notify-shift-outcome.py on purpose: this script must be
# silent for exactly the runs the watcher was silent about, or it replies in a
# thread that does not exist.
QUIET_CONCLUSIONS = {"success", "skipped"}

# The only conclusion worth repeating without asking a person first.
RERUNNABLE_CONCLUSION = "failure"

OFF_VALUES = {"off", "0", "false", "no"}

USERNAME = "shift watcher"

DECLINED_REASONS = {
    "cancelled": "somebody stopped this on purpose, and undoing that by hand is not this script's job",
    "timed_out": "it had its whole time budget; a re-run gets the same budget and usually the same ending",
    "startup_failure": "the run never really started, so the workflow file or the runner is the problem and a re-run hits it again",
}


def fail(message):
    print(f"rerun-dead-shift: {message}", file=sys.stderr)
    sys.exit(1)


def require(name):
    value = os.environ.get(name, "")
    if not value:
        fail(f"{name} is empty or unset; cannot decide on a re-run without it")
    return value


def parse_run(raw):
    try:
        run = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"WORKFLOW_RUN is not valid JSON: {exc}")
    if not isinstance(run, dict):
        fail("WORKFLOW_RUN is not a JSON object")
    return run


def decide(run, switch):
    """(should_rerun, reason). Only reached for runs the watcher reported."""
    if switch.strip().lower() in OFF_VALUES:
        return False, "automatic re-runs are switched off (`SHIFT_AUTO_RERUN`)"

    attempt = run.get("run_attempt", 1)
    if not isinstance(attempt, int) or attempt > 1:
        return False, (
            f"this is attempt {attempt} — there is one automatic re-run per run, "
            "so the next move is a person's"
        )

    conclusion = run.get("conclusion")
    if conclusion != RERUNNABLE_CONCLUSION:
        shown = f"`{conclusion}`" if conclusion else "no conclusion"
        why = DECLINED_REASONS.get(
            conclusion, "only a plain `failure` is repeated without asking"
        )
        return False, f"the conclusion is {shown} — {why}"

    return True, ""


def rerun(api_base, token, repository, run_id):
    """Ask GitHub for another attempt. Returns None, or a description of why not."""
    request = urllib.request.Request(
        f"{api_base}/repos/{repository}/actions/runs/{run_id}/rerun",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if 200 <= response.status < 300:
                return None
            return f"GitHub answered HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        # 403 here almost always means the job's `permissions:` block is
        # missing `actions: write`, which is the one thing about this script
        # that cannot be caught by its tests.
        return f"GitHub refused with HTTP {exc.code}: {body}"
    except (urllib.error.URLError, OSError) as exc:
        return f"could not reach GitHub: {exc}"


def by_hand(run, repository):
    return (
        f"By hand: `gh run rerun {run.get('id', '?')} "
        f"--repo {repository or 'committed-nightly/ops'}`"
    )


def rerun_message(run):
    name = run.get("name") or "The shift"
    return (
        f":arrows_counterclockwise: Re-running *{name}* — attempt 2 requested.\n"
        f"{run.get('html_url', '')}\n"
        "This is the one automatic re-run. If attempt 2 ends badly too, it will "
        "be reported here and left alone.\n"
        "A second attempt starts the way every shift does, by reading "
        "`SHIFTS.md` and the open PRs, so anything attempt 1 managed to finish "
        "is picked up rather than repeated."
    )


def refused_message(run, repository, error):
    return (
        f":warning: Wanted to re-run *{run.get('name') or 'the shift'}* and could not — "
        f"{error}\n" + by_hand(run, repository)
    )


def declined_message(run, repository, reason):
    return (
        f":no_entry_sign: Not re-running automatically — {reason}.\n"
        + by_hand(run, repository)
    )


def post(api_base, token, channel, text, thread_ts):
    payload = {"channel": channel, "text": text, "username": USERNAME}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    request = urllib.request.Request(
        f"{api_base}/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        fail(f"could not reach Slack: {exc}")

    # Slack says HTTP 200 and then refuses you in the body.
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        fail(f"Slack returned a non-JSON body: {body[:400]}")
    if not parsed.get("ok"):
        fail(f"Slack refused the message: {body[:400]}")
    return parsed.get("ts", "")


def main():
    run = parse_run(require("WORKFLOW_RUN"))

    if run.get("conclusion") in QUIET_CONCLUSIONS:
        print(
            f"nothing to decide: {run.get('name', '?')} run {run.get('id', '?')} "
            f"concluded {run.get('conclusion', '?')}"
        )
        return

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    should, reason = decide(run, os.environ.get("SHIFT_AUTO_RERUN", ""))

    error = None
    if should:
        # Ask GitHub before announcing it, so the thread never claims a re-run
        # that was refused.
        error = rerun(
            os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
            require("GITHUB_TOKEN"),
            require("GITHUB_REPOSITORY"),
            run.get("id", ""),
        )
        text = rerun_message(run) if error is None else refused_message(run, repository, error)
    else:
        text = declined_message(run, repository, reason)

    ts = post(
        os.environ.get("SLACK_API_BASE", "https://slack.com/api").rstrip("/"),
        require("SLACK_BOT_TOKEN"),
        require("SLACK_INCIDENTS"),
        text,
        os.environ.get("INCIDENT_TS", ""),
    )
    print(f"posted to {os.environ.get('SLACK_INCIDENTS')} as {ts}")

    if error:
        fail(error)


if __name__ == "__main__":
    main()
