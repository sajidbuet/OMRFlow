# ADR-0005: Project → ScanSession → finite ScanBatch

- **Status:** Accepted
- **Date:** 2026-10-01
- **Phase:** 0.1.1-alpha.0, revised phase 2 (roadmap phase 0.1.1-B, lifecycle part)
- **Supersedes:** nothing; refines Phase 5's batch model

## Context

Until `0.1.1`, a project's scans lived in **batches**, and every downstream stage
(Attendance, Results, Reports) read *one* batch: `list_batches(limit=1)`, the
most recently **updated** one. Two latent defects followed (roadmap §3):

1. **Late scans split the cohort.** Reopening a project and pressing *Process
   All* again created an unrelated second batch; downstream read one of the two
   and reported the other half of the examination as absent.
2. **What Results read could change silently.** `updated_at` moves when an old
   batch is retried or `recover_interrupted` runs on open, so "latest" could
   become a different batch with no action by the operator.

At the same time `add_scans_to_batch` let **any** batch grow at any time
(rescans were read into the original's batch), and *Reprocess All* abandoned the
stored batch and read the same files into a new one with nothing recording that
the new one replaced the old.

The `0.1.1` line adds several scanners, watched folders, hours-long intake and
rescans much later. A model where the unit of aggregation is also the unit of
processing cannot stay correct under that.

## Decision

### Three levels

```text
Project → ScanSession → one or more finite ScanBatch objects → sheets
```

| Level | Role |
|---|---|
| **Project** | The persistent container (unchanged). |
| **ScanSession** (`scan_session`) | The examination-level **aggregation** unit. Holds every batch of one examination sitting: several scanners, folders, computers, later rescans and reprocessing, across project reopenings. |
| **ScanBatch** (`scan_batch`) | The **finite processing and provenance** unit: one import from one source, read with one template identity, resumable and fingerprint-checked exactly as in Phase 5. |

### A batch is finite

A batch has a **membership state** orthogonal to its processing status:

| Membership | Meaning |
|---|---|
| **OPEN** (`sealed_at IS NULL`) | May receive members. |
| **SEALED** (`sealed_at` set) | May **never** receive another member. `total_scans` is final. Processing, resume, retry, review, rejection and replacement of its existing members continue: **SEALED is not COMPLETED**. |

`add_scans_to_batch` raises `BatchSealedError` for a sealed batch when offered a
file that is not already a member (offering a sealed batch its own members, as a
resume does, is fine).

**Rejected alternative: one indefinitely growing batch per examination.** It
would fix defect 1 but makes a batch's membership, manifest and
fingerprint-checked identity meaningless (a batch read over six hours from
three scanners with two templates is not one processing unit), turns every
resume into a scan of an unbounded list, and leaves nothing to which "this
reading replaced that one" can attach. Aggregation belongs one level up.

### The finite-workflow seal trigger

Derived test-first from what the Scan stage did on `main`: a batch receives
members **exactly while it is the Scan page's current batch** (files added to the
list, a rescan imported in the same sitting); once the page moves to another
batch, nothing ever adds to the old one except a later rescan import. So a batch
is sealed:

* when **another batch is started in its session** (`scan_sessions.start_batch`,
  used by *Process All*) - "the operator starting another batch";
* when **its session is closed**;
* when ***Reprocess All*** supersedes it;
* when the **upgrade backfill** assigns it to a session (pre-session batches are
  history).

Every seal is an audit event and writes a processing manifest. Finishing a run
does **not** seal: a batch held by the page still takes the next files added to
the list, exactly as before.

### Roles

`scan` (an import), `rescan` (replacements for rejected sheets of a sealed
batch), `reprocess` (a re-reading of the same files, superseding the batch it
re-reads), `legacy` (backfilled). A `rescan` batch's replacements still count in
the original's batch through the confirmed Reject & Rescan link, exactly as a
cross-batch replacement already did.

### Sessions

| State | Meaning |
|---|---|
| **OPEN** | Receives new batches. Results are provisional. |
| **CLOSED** | Receives no batch, rescan, reprocess or intake. Closing seals every open batch (refused while one is running). |
| *reopen* | An explicit, audited **transition** CLOSED → OPEN, recorded with `reopened_at/by`, `reopen_count`, and `final_outputs_stale_since` (outputs generated while closed are stale). Sealed batches stay sealed. |

### Implicit session and the active pointer

`project_setting['active_scan_session']` names the session the operator is
working in. The **first** *Process All* in a project with no active session
creates one **silently** (named after the exam and the date) and makes it
active; nobody is asked anything. Reopening the project re-adopts it, and the
next *Process All* creates a **new batch in the same session** - the cause of
defect 1 removed. A minimal *Scan session* menu on the Scan stage offers New,
Rename, Close, Reopen and Combine; a single-folder examination never needs it.

### Template pinning

A session pins the template identity (template id, geometry and recognition
fingerprints, engine version) of its first batch. A later batch that differs:

* from the **Scan stage** - refused unless the operator acknowledges the change
  in a dialog (audited as `template_ack` against the batch), or cancels and
  starts a new session;
* from a **programmatic caller** (`batch_store.create_batch`: tools, the stress
  runner, tests) - goes into a **new implicit session**, because there is
  nobody to ask and mixing silently is not allowed.

### Supersession is first-class

`batch_supersession` records *batch B supersedes batch A*: reason, actor, time,
and an audited **reversal** (`reversed_at/by/reason`). It is never a
`supersedes_batch_id` column and never deletes anything. Rules
(`domain.scan_sessions.supersession_problem`): never self; same session only;
only a sealed batch can be superseded; at most one live superseding batch; no
cycle. Chains are legal. *Reprocess All* uses it today; whole-batch rescans and
algorithmic re-reads use the same record later. Sheet-level supersession stays
Reject & Rescan's `scan_rejection` link, unchanged.

### Upgrade backfill

Migration 14 adds structure only. On the first **writable** open after it,
`scan_sessions.backfill_legacy_batches` (audited, idempotent):

* puts **each** pre-session batch in its own one-batch `legacy` session, sealed;
* groups two batches into one session **only** when they are joined by
  confirmed cross-batch rescans and the relationship is **unambiguous**:
  exactly two batches connected, all links one direction, the replacement batch
  newer, the same template identity;
* leaves anything else (three or more batches chained, links both ways, an
  older replacement, a template difference) separate and lists it in the
  upgrade report (`project_setting['scan_session_backfill']`, Project Health);
* **never** combines unrelated batches, however alike they look;
* makes the session of the most recently created batch active.

A schema-13 project opened **read-only** is not migrated or backfilled. The new
`scan_batch` columns are mapped `deferred`, so ordinary reads work, and each batch
is presented as a **virtual** one-batch session.

*Combine into one session* is the operator's explicit, audited repair for a
project split by defect 1: same project, open target, matching template
identity, and refused when an effective sheet (same path or content hash) would
be counted twice.

### Downstream until session-level results

Session **aggregation** - which sheets of which batches count for attendance,
reconciliation, scoring, results and reports - is **not** decided here; it is
the effective-scan-set service of revised phase 4. Until then Attendance,
Results and Reports read **one** batch, chosen by
`scan_sessions.downstream_batch_id`: the active session's newest batch **by
creation** that has a primary role (`scan`, `legacy`, `reprocess`), is not
superseded, and is not still `new`/`running`. Never `updated_at`. A
multi-batch session is therefore **not yet authoritative** for scoring.

> **Superseded by [ADR-0007](ADR-0007-session-effective-sheet-set.md)
> (revised phase 4).** Downstream stages now read the whole session's
> effective sheet set; `downstream_batch_id` returns the session's stable
> population key.

### Crash safety is not in this decision

Crash-safe Scan/Resolve persistence (gaps S1, S2, S3, R1; real-kill testing) is
revised phase 3. This ADR makes the lifecycle correct and persistent under
normal operation and leaves `recover_interrupted`, grouped commits and Resume
unchanged. Recovery must preserve lifecycle state (an OPEN batch stays OPEN, a
SEALED one SEALED) and must never create a session, batch or supersession.

## Consequences

* Defect 1's cause and defect 2's cause are removed; defect 6
  (`processing_manifest` never written) is closed: manifests are written at every
  seal and at the end of every run.
* A single-folder examination behaves exactly as before: one session, one
  batch, no new question.
* A rescan after a project reopen goes into a `rescan` batch instead of the
  original's batch; results are unchanged because cross-batch replacements were
  already counted in the original's batch.
* Regressions this ADR is meant to stop: a downstream stage choosing "the
  latest batch"; a code path that appends to a sealed batch; a re-reading that
  overwrites or deletes the batch it replaces; an automatic combine.

## References

`development/releases/0.1.1-alpha.0/ARCHITECTURE_NOTES.md` §§5-6, §13;
`ROADMAP.md` §2, §5 B; `PHASE_B_HANDOFF.md`.
