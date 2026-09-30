# Implementation prompt pack — `0.1.1-alpha.0`

> **Written, not executed.** These are instructions for later Claude Code
> sessions, one per phase, to be run in order on the merged, current `main`
> after `MERGE_NOTES.md` has been followed.

| # | File | Phase |
|---|---|---|
| 01 | [01-architecture-persistence.md](01-architecture-persistence.md) | 0.1.1-A — Architecture & persistence (includes the version bump) |
| 02 | [02-multisource-live-intake.md](02-multisource-live-intake.md) | 0.1.1-B — Intake engine |
| 03 | [03-incremental-review-rescan.md](03-incremental-review-rescan.md) | 0.1.1-C — Incremental processing, review & rescan |
| 04 | [04-operational-gui.md](04-operational-gui.md) | 0.1.1-D — Operational GUI |
| 05 | [05-session-results.md](05-session-results.md) | 0.1.1-E — Session-scoped reconciliation, scoring & reporting |
| 06 | [06-qualification-release.md](06-qualification-release.md) | 0.1.1-F — Operational qualification & Alpha release |

How to use one: start a fresh session in the repository on a new branch named
in the prompt, paste the whole file as the first message, and let the session
finish with its handoff. Do not run two prompts at once; each depends on the
previous phase's handoff.

Every prompt is self-contained on purpose — each repeats the preservation
list, the verification commands and the status-reporting rules, so none of
them can be run without them.
