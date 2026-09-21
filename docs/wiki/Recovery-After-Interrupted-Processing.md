# Recovery After Interrupted Processing

Batch processing is designed to be interrupted. **Nothing already read is
lost**, whether the run was cancelled, the window closed, the application
crashed or the machine lost power.

## Why it works

Each sheet's result is committed to the project database as that sheet
finishes, not accumulated in memory and written at the end. So at any instant
the database holds every sheet completed so far, and the rest are still
marked as outstanding.

## What to do

Reopen the project and go to the **Scan** stage.

| Button | Use it when |
|---|---|
| **Resume Batch** | Continue the run. Sheets already completed are **not** reprocessed |
| **Retry Failed** | Re-attempt only the sheets that failed |

Both act on the *stored* batch, so they work after a restart and are enabled
only when there is genuinely something left to do.

When a project with an interrupted batch is opened, OMRFlow recovers the
outstanding work and reports how many sheets it picked up.

## Closing while a batch runs

You are asked to confirm, and told that sheets already read are saved and the
batch can be resumed. On confirmation the batch is stopped **and waited for**
before the project is released — the worker processes own the database
writes, and closing underneath them would abort the very writes that make the
run resumable.

## Checking afterwards

**Application menu → Tools → Project Health / Recovery…** reports structural
integrity, foreign keys, stale jobs left in a running state, and missing or
changed source scans. It reports and never silently repairs.

## Worker processes

Recognition runs in a pool of worker processes. If the coordinator dies, the
workers die with it — they are bound to its lifetime, so a crashed run cannot
leave processes consuming CPU in the background. Workers never have database
write access, so an abrupt end cannot corrupt the project.

## What is not covered

Recovery is designed against **process interruption** — cancellation,
crashes, a lost coordinator. It is not a defence against storage failure or
filesystem corruption; nothing in OMRFlow is. That is what
[backups](Backup-and-Data-Retention) are for.

## Related

- [Processing](Processing)
- [Backup & Data Retention](Backup-and-Data-Retention)
- [Troubleshooting](Troubleshooting)
