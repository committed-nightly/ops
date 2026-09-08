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

Pull requests are disabled here; changes land by direct push, reviewed by
hand rather than by branch protection.

MIT licensed, see `LICENSE`.
