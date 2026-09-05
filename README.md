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

Pull requests are disabled here; changes land by direct push, reviewed by
hand rather than by branch protection.

MIT licensed, see `LICENSE`.
