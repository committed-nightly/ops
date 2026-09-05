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

Pull requests are disabled here; changes land by direct push, reviewed by
hand rather than by branch protection.

MIT licensed, see `LICENSE`.
