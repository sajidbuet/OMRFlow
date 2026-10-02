# Recovery After Interrupted Processing

Batch processing is designed to be interrupted. **Nothing OMRFlow showed you as
processed is lost**, whether the run was cancelled, the window closed or the
application crashed - and nothing already processed is read a second time.

## Why it works

Each sheet is saved to the project database as **one unit** the moment it is
read: its recognition result *and* the review conflicts it raises for that
sheet, in one transaction. Progress counts only saved sheets; a sheet that has
been read but not yet saved is shown as *saving*. So at any instant the
database holds every sheet you have seen counted, with everything Resolve needs
for it, and the rest are still marked as outstanding.

Checks that depend on the whole batch - duplicate Student IDs, set codes the
project does not define, a rejected scan imported again - are completed when a
run ends, and on the next open if a crash stopped the run first.

## What happens when you reopen the project

Before any stage is shown, OMRFlow:

1. returns sheets that were inside a worker to *pending* (never to *failed*);
2. re-checks the review conflicts of the sheets already saved, from what was
   stored - no image is read again;
3. completes the batch-wide checks;
4. marks the batch *interrupted*.

Then the **Scan** stage opens on the interrupted batch, with every scan listed,
the saved results shown, and the counts read from the database:

> 637 / 1,000 recognised · 4 failed (retryable) · 359 pending

and the **Resolve** stage opens with every decision you had confirmed and only
the unresolved conflicts in its queue - you do not need to visit Scan first.
Nothing is processed until you press **Resume**.

The batch keeps its scan session and its sealed or open state; reopening never
creates a new session, a new batch or a reprocess batch.

| Button | Use it when |
|---|---|
| **Resume Batch** | Continue the run. Sheets already saved are **not** read again |
| **Retry Failed** | Re-attempt only the sheets that could not be read |
| **Process All** | On a restored batch, also reads only what is not saved yet |
| **Reprocess All** | Read everything again, into a new batch that replaces this one |

## Closing while a batch runs

You are asked to confirm, and told that sheets already read are saved and the
batch can be resumed. On confirmation the batch is stopped **and waited for**,
its batch-wide checks are completed, and only then is the project released.

## Checking afterwards

**Application menu → Tools → Project Health / Recovery…** reports structural
integrity, foreign keys, stale jobs and batches left in a running state,
saved sheets missing their review state, duplicated detections, and missing or
changed source scans. It reports and never silently repairs; reopening the
project for editing is what performs the recovery above.

## Worker processes

Recognition runs in a pool of worker processes. If the coordinator dies, the
workers die with it - they are bound to its lifetime on Windows, so a crashed
run cannot leave processes consuming CPU in the background. Workers never have
database write access, so an abrupt end cannot corrupt the project.

## What is and is not tested

Recovery was tested by **killing a real OMRFlow process** from outside at
chosen points of Scan and Resolve, at 40 sheets and in a separate 1,000-sheet
run (0.1.1 phase 3). **A real loss of power was not tested.** That case is
covered only as far as SQLite's own transaction guarantees and your storage go;
a synchronised folder or a network share weakens them.

Recovery is designed against **process interruption**. It is not a defence
against storage failure or filesystem corruption; nothing in OMRFlow is. That is
what [backups](Backup-and-Data-Retention) are for.

## Related

- [Processing](Processing)
- [Review and Resolution](Review-and-Resolution)
- [Backup & Data Retention](Backup-and-Data-Retention)
- [Troubleshooting](Troubleshooting)
