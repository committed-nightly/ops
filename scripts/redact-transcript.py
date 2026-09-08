#!/usr/bin/env python3
"""
Strip credentials out of a shift's Claude Code execution log so the log can be
kept as a build artifact.

Every shift ends by claiming what it did. Until now there has been no way to
check that claim: `show_full_output` is off, so the agent's turns never reach
the job log, and `CLAUDE_CODE_REMOTE_SESSION_ID` — which both shift routines
told themselves to read — is not a thing that exists inside a GitHub Actions
runner (logbook#6). The one real record is the file the action writes at
`$RUNNER_TEMP/claude-execution-output.json`, and it is deleted with the runner.

Keeping it means uploading it, and uploading it means facing what is in it. A
shift's environment holds an installation token, a Slack bot token and an OAuth
token, and the transcript is a verbatim record of every command the shift ran
and everything those commands printed back. GitHub masks secrets in *job logs*;
it does not mask artifacts. `ops` is public. So the file cannot go up as it is.

The design has one rule behind it: **nothing is uploaded unless it was
successfully redacted.** Not "redacted where possible" — a partial redaction is
the failure mode that matters, because it looks exactly like a clean one. So
this writes to a temporary file and only moves it into place after a self-check
that re-scans the finished text for every secret it knows about. If anything at
all goes wrong the output does not exist, the step goes red, and there is
nothing for the upload to find.

Two things it does *not* do, stated here rather than discovered later:

  * It matches raw substrings. A token that reaches the transcript base64'd,
    hex-encoded, split across two strings, or JSON-escaped character by
    character will not be caught. In practice tokens arrive verbatim in command
    output, which is what this catches.
  * It cannot redact a secret it was never told about and whose shape it does
    not recognise. Add new ones to DEFAULT_SECRET_ENV or pass REDACT_ENV_VARS.

Redactions name what they replaced — `[redacted: SLACK_BOT_TOKEN]`, not a wall
of asterisks. A transcript that says which credential leaked and where is worth
considerably more than one that only says something did.

Usage:
    redact-transcript.py INPUT OUTPUT

Env:
    REDACT_ENV_VARS   optional, comma-separated extra env var names to redact

Exit codes:
    0   OUTPUT written and self-checked
    1   INPUT exists but could not be redacted safely; OUTPUT was not written
    2   usage error
    3   INPUT does not exist; nothing to do, OUTPUT was not written

Stdlib only. A script that needs `pip install` to run has one more way to not
run, and this one runs in the step after a shift has already gone wrong.
"""

import json
import os
import re
import sys

# The credentials that reach a shift's own environment. RICHMOND_PRIVATE_KEY
# and friends never do — they are consumed by the token-minting step — but the
# PEM pattern below covers them anyway in case one is ever echoed.
DEFAULT_SECRET_ENV = [
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "SLACK_BOT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_MESSAGING_TOKEN",
]

# Below this length a value is more likely to be a flag, a channel id or the
# word "true" than a credential, and redacting it would carpet-bomb the
# transcript. Skipping is the lesser evil, but it is announced on stderr and in
# the summary — a redactor that quietly declines to redact something is exactly
# the failure this script exists to avoid.
MIN_SECRET_LENGTH = 8

# Shape-based rules, applied after the known values. These catch the case that
# actually worries me: a credential that belongs to something nobody listed —
# another org's token pasted into a search result, a PAT in a fixture, the
# token GitHub embeds in `remote.origin.url` (logbook#10).
PATTERNS = [
    (
        # Deliberately wider than the `gh[pousr]_[A-Za-z0-9]{36}` shape the
        # published secret-scanning patterns use. The installation token this
        # org's shifts actually run on is 383 characters of
        # `[A-Za-z0-9_.-]` — checked against a live one, not read off a docs
        # page — and the canonical pattern matches none of it. A pattern that
        # matches the first 20 characters and stops is worse than no pattern,
        # because it leaves a redacted-looking line with a working credential
        # in the tail of it.
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_.\-]{16,}"),
    ),
    (
        "github-pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    ),
    (
        "slack-token",
        re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{10,}"),
    ),
    (
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ),
    (
        "aws-access-key-id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
]

# `https://x-access-token:ghs_...@github.com/...`. Handled separately from the
# patterns above because the scheme and host are worth keeping — knowing a push
# went to github.com is the useful half of the line. It runs last on purpose:
# by then a recognised token has already become `[redacted: ...]`, which
# contains a space and so no longer looks like a URL userinfo field. What
# reaches this rule is credentials of a shape nobody listed.
URL_CREDENTIALS = re.compile(
    r"""(?P<scheme>[a-z][a-z0-9+.\-]*://)[^\s/@"']+:[^\s/@"']+@"""
)


def fail(message, code=1):
    print(f"redact-transcript: {message}", file=sys.stderr)
    sys.exit(code)


def secret_names():
    """DEFAULT_SECRET_ENV plus anything named in REDACT_ENV_VARS, deduped."""
    names = list(DEFAULT_SECRET_ENV)
    for extra in os.environ.get("REDACT_ENV_VARS", "").split(","):
        extra = extra.strip()
        if extra and extra not in names:
            names.append(extra)
    return names


def collect_secrets(names):
    """Return [(name, value)] for env vars long enough to be worth replacing.

    Sorted longest first so that a secret which happens to be a substring of
    another — a token and the same token inside a URL, say — doesn't get its
    prefix replaced out from under the longer match, leaving a fragment.
    """
    found, skipped = [], []
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            continue
        if len(value) < MIN_SECRET_LENGTH:
            skipped.append(name)
            continue
        found.append((name, value))
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return found, skipped


def redact(text, secrets):
    """Replace known values, then known shapes. Returns (text, {rule: count})."""
    counts = {}

    for name, value in secrets:
        hits = text.count(value)
        if hits:
            text = text.replace(value, f"[redacted: {name}]")
            counts[name] = counts.get(name, 0) + hits

    for label, pattern in PATTERNS:
        text, hits = pattern.subn(f"[redacted: {label}]", text)
        if hits:
            counts[label] = counts.get(label, 0) + hits

    text, hits = URL_CREDENTIALS.subn(
        r"\g<scheme>[redacted: url-credentials]@", text
    )
    if hits:
        counts["url-credentials"] = counts.get("url-credentials", 0) + hits

    return text, counts


def summarise(counts, skipped, size):
    """The line a human reads. Zero redactions is a result, not a silence."""
    lines = [f"redact-transcript: {size:,} bytes"]
    if counts:
        total = sum(counts.values())
        lines.append(f"{total} redaction{'s' if total != 1 else ''}:")
        for rule in sorted(counts, key=lambda r: (-counts[r], r)):
            lines.append(f"  {rule:<28} {counts[rule]}")
    else:
        lines.append("no credentials found in the transcript")
    for name in skipped:
        lines.append(
            f"  WARNING: {name} is set but shorter than {MIN_SECRET_LENGTH} "
            f"characters, so it was NOT redacted"
        )
    return "\n".join(lines)


def main(argv):
    if len(argv) != 3:
        fail(f"usage: {os.path.basename(argv[0])} INPUT OUTPUT", code=2)

    source, target = argv[1], argv[2]

    if not os.path.exists(source):
        # The action writes the execution file when the agent finishes or
        # errors, but not when the job is killed outright — a timeout, or the
        # runner going away. Nothing to redact is a normal Tuesday, not a
        # fault, so this is its own exit code and the caller can shrug at it.
        fail(f"{source} does not exist; no transcript to redact", code=3)

    try:
        with open(source, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as error:
        fail(f"cannot read {source}: {error}")

    secrets, skipped = collect_secrets(secret_names())
    redacted, counts = redact(text, secrets)

    # Self-check. Both of these should be impossible: the replacements contain
    # no JSON metacharacters, and every known value was just replaced. They are
    # here because "impossible" and "unverified" read the same in an incident.
    try:
        json.loads(redacted)
    except ValueError as error:
        fail(f"redacted transcript is not valid JSON ({error}); refusing to write it")

    for name, value in secrets:
        if value in redacted:
            fail(f"{name} survived redaction; refusing to write {target}")

    # Write beside the target and move, so a crash midway leaves no
    # half-redacted file for an `if: always()` upload step to find.
    scratch = target + ".partial"
    try:
        with open(scratch, "w", encoding="utf-8") as handle:
            handle.write(redacted)
        os.replace(scratch, target)
    except OSError as error:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        fail(f"cannot write {target}: {error}")

    summary = summarise(counts, skipped, len(redacted.encode("utf-8")))
    print(summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write(f"```\n{summary}\n```\n")
        except OSError as error:
            # The transcript is already written and clean. Losing the pretty
            # summary is not worth failing the step over.
            print(
                f"redact-transcript: could not write the step summary: {error}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
