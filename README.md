# ops

Shift definitions and automation for Committed Nightly, an unsupervised build
shop run in two shifts: Richmond builds overnight, Jen reviews in the
morning. Nothing reaches `main` anywhere else without both.

This repo is the scheduling and skill definitions that run those shifts —
`.github/workflows/` triggers them, `.claude/skills/` is what each one
follows. It is not where the work product lives.

- **[logbook](https://github.com/committed-nightly/logbook)** — the shift
  ledger (`SHIFTS.md`) and Jen's backlog for Richmond, as issues.
- **[wiki](https://github.com/committed-nightly/ops/wiki)** — who does what,
  in more detail.
- Everything else in the org is something Richmond shipped and Jen approved.

## The shift watcher

Each shift writes its own record — the `#shift-log` post, the line in
`SHIFTS.md` — partway through its own run. So a run that dies before it gets
that far leaves no trace anywhere except a red row in the Actions list, which
is not somewhere anyone looks. That has happened three times, twice needing a
hand-written backfill days later.

`.github/workflows/shift-watcher.yaml` closes that. It runs on `workflow_run`
after each shift finishes, in a run of its own, so the shift's job dying
cannot take the reporting down with it, and it posts to `#incidents` for any
conclusion that isn't a clean success. A re-run that succeeds gets a line too,
so a failure sitting in the channel doesn't look open forever.

The logic is `scripts/notify-shift-outcome.py` rather than shell in the YAML,
so it can be tested without waiting for a shift to fail:

```
python3 scripts/test_notify_shift_outcome.py
```

Two things to know before editing it: `workflow_run` only fires from the copy
of the file on `main`, and `workflows:` matches a workflow's `name:`, not its
filename — rename a shift and the watcher stops watching it without saying so.

### The second attempt

Reporting a dead shift still leaves it dead until somebody presses the button.
Run 33819369404 failed at 23:59 and was re-run by hand at 06:48 the next
morning, where it succeeded — a night lost to a button nobody was awake to
press. So the watcher now asks for one more attempt itself, and replies in the
incident's thread with what it did or why it didn't:

```
:arrows_counterclockwise: Re-running Richmond — attempt 2 requested.
```

It is deliberately hard to talk into this. Only a plain `failure`, and only on
attempt 1 — `cancelled` means a person stopped it, `timed_out` had its whole
120 minutes and would get 120 more, and a conclusion nobody enumerated is a
person's problem before it is a machine's. Everything it declines gets the
reason and the `gh run rerun` line to do it by hand.

One automatic re-run per run, ever. The guard is GitHub's own `run_attempt`
counter rather than any state this repo keeps, so it holds whoever started the
second attempt.

There is no check for "did the shift already get some work done", because
there is no signal for one. Duration doesn't separate the cases — run
33819369404's agent step failed after 7m33s and Jen's shift on 09-04 succeeded
in 5m50s — and the `#shift-log` post doesn't either, since only two of the
fourteen notes there quote their own run id, Jen posts hers as thread replies
that `conversations.history` can't see, and Moss doesn't post there at all.

What makes a second attempt safe is the shift, not the watcher: every shift
begins by reading `SHIFTS.md`, `#orders` and the open PRs, so a shift that got
far enough to leave records has those records read back to it at clock-in. If
clock-in ever stops reading the ledger first, this stops being safe.

To turn it off without touching the workflow, set the repository variable
`SHIFT_AUTO_RERUN` to `off`. It still posts the thread reply saying it is off,
because a feature that is silently disabled reads exactly like one that is
silently broken.

```
python3 scripts/test_rerun_dead_shift.py
```

The one thing those tests can't reach is whether GitHub itself allows the
re-run: it needs `actions: write`, which the job grants its own `GITHUB_TOKEN`
in `permissions:`. Richmond's and Jen's app installations do not have that
permission — asking with either of their tokens comes back `403 Resource not
accessible by integration` with `X-Accepted-Github-Permissions: actions=write`
— so the workflow token is the only one here that can do it.

## Shift transcripts

Each shift ends by claiming what it did. The transcript is the only way to
check that claim against what it actually ran, and until now there wasn't one:
`show_full_output` is off, so the agent's turns never reach the job log, and
the action's own copy under `RUNNER_TEMP` is deleted with the runner.

Both shift routines used to end by printing
`https://claude.ai/code/$CLAUDE_CODE_REMOTE_SESSION_ID`. That variable is not
set in a GitHub Actions runner and won't be — it belongs to cloud sessions,
and a shift runs the CLI on a runner (`CLAUDE_CODE_ENTRYPOINT` says
`claude-code-github-action`). Ten shifts printed nothing rather than a link
that goes nowhere, which was the right call and also meant nobody chased it.
That's logbook#6.

Now each of the three shift workflows keeps its own session as a
`transcript-<shift>-<run>-<attempt>` artifact on the run page, for 90 days.
The link a shift quotes is `$SHIFT_TRANSCRIPT_URL`, which is that page. It is
named per *attempt* rather than per run, because a re-run is a different
session and would otherwise overwrite the one someone came looking for.

Nothing is uploaded that hasn't been redacted first. This repo is public,
artifacts are not masked the way job logs are, and a shift's environment holds
a live installation token and a Slack bot token — so the transcript is a
verbatim record of every command a shift ran and everything those commands
printed back at it.

`scripts/redact-transcript.py` replaces known credential values by name, then
anything matching a credential *shape*, and refuses to write its output at all
if it can't finish or if a known secret survives its own re-scan. A step that
half-redacts is worse than one that doesn't run, because it looks identical to
one that worked.

```
python3 scripts/test_redact_transcript.py
```

One thing worth knowing if you extend the patterns: the installation token
these shifts run on is ~383 characters of `[A-Za-z0-9_.-]`, not the
40-character `ghs_[A-Za-z0-9]+` that the published secret-scanning patterns
match. Copying those patterns gets you a rule that redacts a prefix and leaves
a working credential in the tail.

## Who a push lands as

Every shift checks its own commits before pushing them:

```
git log -1 --format='%an <%ae>'
```

That check cannot fail here, and it cannot catch what goes wrong here either.
A commit's author is baked into the object. The identity a *push* lands as
comes from the credential git sends with the request, and nothing local tells
you which credential that will be.

`actions/checkout` used to leave its own token behind in a config file pulled
into the workspace checkout by `includeIf.gitdir`:

```
http.https://github.com/.extraheader=AUTHORIZATION: basic <base64 of
                                      x-access-token:GITHUB_TOKEN>
```

An extra header is attached to the request, so it beats a credential helper
and it beats credentials embedded in the remote URL. Every push from that
directory went up as `github-actions[bot]` — with the commits still authored
correctly — and the only reason nobody shipped a night's work under the wrong
name is that `github-actions[bot]` has no write access here, so it 403s. Add
`contents: write` to a shift's `permissions:` for some unrelated reason and
the same push starts succeeding, quietly. Six shifts worked around it by hand
before it was fixed; that's logbook#18.

Every checkout in this repo now sets `persist-credentials: false`, which
leaves exactly one credential on the box — the one `gh auth setup-git`
installs, which is the shift's own and works from any clone rather than only
from the workspace one.

The YAML being right is not the check, though. A second checkout step added
later, for a good reason, brings the header back. So each shift preflight
resolves the credential that actually wins and compares it to `$GH_TOKEN`:

```
python3 scripts/check-push-identity.py .
```

It works through git's order — an `Authorization` extra header that applies to
github.com, then credentials in the remote URL, then the helpers — and exits 1
if the winner isn't ours, 2 if it can't tell. It reports credentials as
truncated digests and never prints one; a test asserts that for every case,
since this runs in a job whose transcript is kept as a public artifact.

It takes a directory, so it works on a scratch clone too, not just the
workspace:

```
python3 "$GITHUB_WORKSPACE/scripts/check-push-identity.py" ./logbook
```

```
python3 scripts/test_check_push_identity.py
```

One thing those tests restate rather than prove: that a header really does
beat everything else. That was established against a live remote — from a
checkout carrying the persisted header, a private repo the shift can read and
`github-actions[bot]` cannot comes back `Repository not found`, with the
shift's own token sitting in the remote URL the whole time, and lists its refs
the moment the header is reset.

Pull requests are disabled here; changes land by direct push, reviewed by
hand rather than by branch protection.

MIT licensed, see `LICENSE`.
