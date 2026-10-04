# Intake sources and the intake ledger

> **Status (2026-10-02): implemented and tested; headless.** Revised phase 5 of
> `0.1.1-alpha.0` (roadmap phase 0.1.1-D). There is **no operator-facing watched
> intake yet**: no source panel, no background engine, no continuous
> recognition - those are phases 6-8. *Add Folder -> Process All* works exactly
> as before and now also records into the built-in manual source.
> **Not** network-share validated, **not** real-scanner validated, **not**
> production-qualified. Local-disk tests say nothing about SMB `mtime` caching
> or share disconnects.

Code: `domain/intake.py` (vocabulary, rules), `services/intake.py` (the
service), `services/intake_fs.py` (filesystem boundary, copy store),
`services/image_integrity.py` (completeness of one read). Decision record:
[ADR-0008](decisions/ADR-0008-intake-copy-or-reference.md). Schema:
[DATA_MODEL.md](DATA_MODEL.md) (migration 16).

## The pipeline

```text
source folder --list--> ledger row --stabilise--> one read: SHA-256 + full decode + re-stat
      --> ready --register (phase 6 calls it)--> finite sealed batch --> batch_scan (pending)
                                    \--> phase 4 duplicate rule --> duplicate_content
```

Phase 5 ends at **ready + the registration API**. It never decides when to
register, never recognises a sheet, never runs in the background.

## Sources

* **Project-level**, outliving any one scan session. `kind`: `watched` (a folder,
  local or UNC) or `manual` (the built-in source, **created on first use** by the
  service - not by the migration - one per project).
* Configuration: label, folder (stored exactly as given), `recursive`,
  exclusions (file-name and folder `fnmatch` patterns, case-insensitive),
  enabled, stabilisation policy, ingest mode (ADR-0008). The root folder cannot
  be changed - a different folder is a new source (relative paths would lie).
* **Attachment**: a source serves at most one session at a time
  (`intake_source_attachment`, history kept). A file is intended for the session
  its source served **when it was first observed**. Re-attaching never moves an
  existing row; rows observed while unattached and not yet registered take the
  session attached next. Attaching to a closed session is refused.
* **Identity is not provenance.** A source and a path say where bytes were seen,
  never whose script they are. Three scanners all writing `000001.jpg` is
  normal.
* Audited (`audit_event`, `entity_type = intake_source`): created, updated,
  enabled, disabled, attached, detached. Polls are **not** audited.

## Reachability

`online`, `unreachable`, `permission_denied`, `disabled` (plus `unknown` before
the first listing), with detail, time of change, last attempt and last
**successful** reconciliation.

* A failed listing changes **source state only**: no file row is touched, none
  becomes `vanished`, other sources continue (`reconcile_all` isolates each).
* When the source returns, the next pass is a complete reconciliation: files
  that arrived meanwhile are found.
* A sub-folder that cannot be listed is recorded; rows under it are not
  observed that pass, so their absence is not evidence they vanished.

## The ledger (`intake_file`)

One row per **observed version** of a file: `(source, relative path, content)`
is unique once the hash is known; exactly one row per `(source, relative path)`
is *current*. `(size, mtime_ns)` is only an observation cache - path is never
identity. Field-by-field: [DATA_MODEL.md](DATA_MODEL.md), *IntakeFile*.

Once registered, the file's processing state **is** its `batch_scan.status`;
nothing about recognition is copied into the ledger.

## States

| State | Meaning | Next |
|---|---|---|
| `discovered` | Seen once | `stabilizing`, `ready`*, `ignored`, `vanished`, `unreadable`, `unsupported` |
| `stabilizing` | Not yet proven complete (changing, quiet period, empty, locked, decode retry) | same as above |
| `ready` | Proven complete and unchanged; verified hash | `registered`, `duplicate_content`, `stabilizing` (changed / re-verify failed), `vanished`, `held` |
| `registered` | Linked to the `batch_scan` it became | `duplicate_content` (phase 4's link, committed just after) |
| `duplicate_content` | Registered, bytes repeat an effective sheet of the session; never recognised | - |
| `ignored` | Temporary name, unsupported suffix, configured exclusion, or the same bytes at the same path as an existing row (`unchanged_content`) | - |
| `vanished` | Gone before registration; kept | `discovered` when it reappears |
| `unreadable` | Stable but never fully decodable after the bounded retries (an *intake* outcome, not a recognition failure) | - |
| `unsupported` | Complete but not admissible: a **multi-page TIFF**, refused at file level (never split, never page 1 only) | - |
| `held` | Ready for a **closed** session; never registered into it | - (phase 7 decides) |

\* `ready` straight from `discovered` only for a one-observation policy.
Terminal rows never change state; new bytes at their path are a **new** row,
flagged `path_reused` when the old row's content was registered. Every other
transition is refused (`require_transition`).

## Stabilisation - when a file becomes `ready`

All of:

1. **Name**: supported suffix (`.png .jpg .jpeg .tif .tiff .bmp`), not
   temporary (`*.tmp`, `*.part`, `*.partial`, `*.crdownload`, `~*`, `.*`), not
   excluded. Dot-folders are not descended into. The Windows *hidden* attribute
   is not consulted (manual import never did).
2. **Non-empty**.
3. **Quiet**: the same `(size, mtime_ns)` over at least *K* consecutive
   observations, the first at least *T* seconds ago.
4. **Openable**: a Windows sharing violation (another program still holds the
   file without read sharing) is *locked* - not yet, not an error, not an
   attempt. (The C runtime reports it as a plain permission error; intake
   re-probes with `CreateFileW` to tell *locked* from *access denied*.)
5. **One read**: the whole file is read once; that buffer is hashed (SHA-256,
   `scan_provenance.hash_bytes`) **and** fully decoded - structural end-of-image
   check (JPEG EOI, PNG chunks to `IEND`, TIFF strip/tile extents and page
   count, BMP pixel extent) plus the same `cv2.imdecode` recognition uses.
6. **Re-stat**: handle metadata before and after the read, and the path's
   metadata after closing, all equal to the observation; otherwise the result
   is discarded and the file observed afresh (`changed_during_read`).
7. **Retries**: a failed decode is retried after a back-off that doubles
   (`retry_backoff_seconds`); after `max_decode_attempts` on the same version
   the row is `unreadable`. Any change to size or time is a new version and
   resets the count.

Ready files whose session is closed become `held`.

### Parameters (configuration, starting values - **measured locally, not validated**)

| | Local | Network (UNC path) | Manual |
|---|---|---|---|
| *K* `min_observations` | 2 | 2 | 1 |
| *T* `quiet_seconds` | 5 | 15 | 0 |
| `max_decode_attempts` | 3 | 3 | 3 |
| `retry_backoff_seconds` | 5 | 15 | 5 |
| `stall_after_seconds` (reported, changes nothing) | 600 | 600 | 600 |
| `poll_interval_seconds` (advisory for phase 6) | 10 | 30 | - |

Per source in `intake_source.policy_json`. A mapped drive letter pointing at a
share gets the local values unless set explicitly. The handoff records the
measurements behind them (`scripts/benchmark_intake.py`).

## Collisions

| Situation | Behaviour |
|---|---|
| Same file name, different sources | Different rows; each registered |
| Same path, same observation | Nothing happens; no re-read |
| Same path, metadata changed, same bytes | A new observation row `ignored / unchanged_content`, pointing at the row that holds the bytes; nothing registered |
| Same path, **new bytes** after registration | A new row, `path_reused`, `previous_intake_file_id` = the old row; the old row and its scan are never changed |
| Same bytes, another path or source, same session | `duplicate_content`, linked to the effective sheet, never recognised - **phase 4's rule** (`scan_lifecycle.link_exact_duplicates`), the same one manual imports use |
| Same bytes in another session | Not a duplicate; both sessions register it |
| Same bytes twice in one registration call | The second is linked to the first, no second scan |
| A copy of a **rejected** scan | Phase 4/Reject & Rescan's re-import rule: `duplicate_content` naming the rejected scan (`reimport_of_rejected`) |

## Registration API (what phase 6 calls)

```python
service = IntakeService(database, project_root)        # restart recovery runs here
service.reconcile_all()                                # or reconcile(source_id)
items = service.ready_items(scan_session_id=..., source_id=..., limit=N)
outcome = service.register(scan_session_id=..., source_id=...,
                           intake_file_ids=[i.intake_file_id for i in items],
                           identity=BatchIdentity.of(template))
```

* `ready_items`: ready, verified (not awaiting re-verification), present,
  current, intended for the session; ordered by `ready_at`, then ledger id - an
  order that survives restart.
* `register`: one **finite, sealed** batch (`role = scan`, `source_id` set) of
  the given files of one source, in that order, each `batch_scan` `pending`
  with the verified hash and `intake_file_id`; copies per ADR-0008; then phase
  4's duplicate link. Idempotent (already-registered ids are reported, not
  repeated); refuses rows that are not ready, of another source, or intended for
  another session; a **closed** session diverts the rows to `held` and creates
  nothing - never reopens, never starts another session.
* Nothing is recognised: the batch is ordinary input for the continuous
  engine (below) or the unchanged `process_batch`.

## Processing registered units (revised phase 6)

The continuous engine (`services/continuous_engine.py`,
[ADR-0009](decisions/ADR-0009-continuous-engine-single-writer.md)) is the
caller of the API above. Headless in this phase - nothing in the GUI starts
it yet.

```python
engine = ContinuousEngine(database, scan_session_id=session_id, template=template,
                          recogniser=ProcessRecogniser(template, workers=8),
                          intake_factory=lambda: IntakeService(database, project_root),
                          limits=EngineLimits.for_workers(8),
                          unit_policy=UnitPolicy(max_unit_size=200, trickle_seconds=30))
engine.start()                       # scan recovery, then intake recovery
while running:
    engine.poll_intake()             # reconcile sources whose poll interval passed
    engine.form_units()              # register due units (finite, sealed, one source)
    engine.step(wait=0.2)            # collect, commit, finish units, claim, submit
engine.shutdown()                    # drain in-flight; leave nothing claimed
```

* **Unit rule** (`domain.processing.plan_units`): a source's oldest ready files,
  in `(ready_at, intake_file_id)` order; a unit as soon as `max_unit_size` are
  ready, or whatever is ready once the oldest has waited `trickle_seconds`.
  Sources are served by their oldest ready file. Starting values, **not
  validated**.
* **Processing**: sheets are claimed (`pending -> processing`, one
  transaction) before they reach a worker, read from the verified project copy
  (never the scanner's share), and committed through the phase 3 work unit;
  only then are they counted. A unit's batch-scope pass (session-wide duplicate
  Student IDs, undefined set codes, re-imports) runs when it has nothing
  unfinished, before it leaves `running`.
* **Source outage**: an unreachable source stops nothing - its registered
  copies are read as usual and other sources continue.
* **Caught up** (`EngineStatus.caught_up`): nothing in flight, nothing
  claimable, nothing ready - *as of this engine's view*. The session-level
  definition, with source reachability, is the session snapshot (below).
* Held, unreadable, unsupported and duplicate-content files are never claimed;
  an operator decides about the first three (below).
* **One coordinator per project** (revised phase 7): `engine.start()` takes
  the project's coordinator lease (`services/coordinator.py`) before its
  recovery touches a claim. While the engine holds it, *Process All* on the
  Scan stage is refused with a typed `CoordinatorBusyError` (and the other way
  round). See [ADR-0010](decisions/ADR-0010-quality-decisions-and-session-controls.md).

## Operator controls (revised phase 7)

Operator **intent**, stored in the project (`services/session_controls.py`,
migration 17) and read by the engine on every step and after every restart -
never reset by a restart:

| Control | Stored in | Effect |
|---|---|---|
| Intake paused (session) | `scan_session.intake_paused` | No source of the session is listed or registered from. Nothing on disk is lost; files keep arriving on the scanners' side |
| Intake paused (one source) | `intake_source.intake_paused` | That source is not listed or registered from. **Paused is not unreachable**: its reachability stays what the last listing found, and the snapshot never lists it as unreachable |
| Processing `running` / `paused` / `stopped` | `scan_session.processing_intent` | Paused or stopped: nothing new is claimed; sheets already handed to a worker finish **and are recorded**. A restart resumes recognition **only** when the stored intent is `running` |

Two stop policies on the engine, each persisted **first** (a crash while it
settles comes back stopped, never resumed):

* `finish_current_and_stop(actor)` - the default safe stop. **Current** work is
  every sheet this engine has already claimed and handed to the recogniser
  (queued in the bounded pool or inside a worker - at most
  `EngineLimits.max_in_flight`) and every result already read: all of it
  finishes and is committed. Nothing new is claimed or registered; the rest of
  a unit stays `pending`; units leave `running`; the engine shuts down. No row
  is left `processing` (unless the writer itself is failing - `faulted`, the
  claims left for recovery). A timeout releases what has not returned to
  `pending`.
* `cancel_queued_and_stop(actor)` - the deliberate stop, **named operator
  required**. Sheets submitted but not started are withdrawn at once (claims
  released to `pending`); sheets inside a worker finish and are recorded;
  committed work is untouched. Audited as `queue_cancelled`.

Every change of intent is an `audit_event` (`entity_type = session_control`).

## Files waiting for an operator: held, unreadable, unsupported (revised phase 7)

`services/intake_decisions.py`. A file **held** (it became ready for a session
that is closed - never silently added to it), **unreadable** (never decoded
after the bounded retries; the quality policy calls it a rescan) or
**unsupported** (a multi-page TIFF) waits for a decision. `pending_decisions()`
pages them with their provenance: source, path, arrival time, how long it has
waited, why (a sentence and a code), and the decisions that apply:

| State | Decisions |
|---|---|
| `held` | *release* into its own session once reopened, or into another **open** session the operator names (the file is re-read and re-verified before it is registered); *dismiss* |
| `unreadable` | *retry* (observed and read afresh); *dismiss* |
| `unsupported` | *dismiss* |

*Dismiss* keeps the record as `ignored` / `operator_dismissed` and never
registers it. Each decision is one transaction with its audit event
(`entity_type = intake_file`) and a named reviewer. These exits exist only as
operator decisions (`domain.intake.OPERATOR_TRANSITIONS`); reconciliation,
recovery and registration still never leave these states.

## The session snapshot (revised phase 7)

`services/session_snapshot.take_snapshot()` returns an immutable value for
phase 8 to poll: counts that **partition** everything discovered (stabilising,
ready, held, vanished, unreadable awaiting a decision, queued, processing,
accepted, conflict, rescan required, superseded, duplicate, excluded, deferred,
counted in another session - each file or sheet exactly once, checked against
an independently counted total), **three separate progress lines**
(recognition, which may fall as files arrive; conflict resolution; rescans),
per-source state (reachability, last successful reconciliation, stabilising /
ready / held counts, registered / processed, recent rate, a
registration-failure **rate** alarm) and the session's activity:

*Caught up - watching for new scans* only when the session is open, processing
is running, intake is on, nothing is stabilising, ready, queued or being read,
no unit is still `running`, and every enabled, unpaused source is reachable and
was reconciled within twice its poll interval plus 5 s (an **operational
heuristic, not calibrated**). An unreachable source makes it *waiting for a
source*; a paused one *intake paused*; a disabled one is listed by name and
not watched. **Caught up never means the examination is complete.**

Bounded: 11 SQL statements in one read transaction whatever the size; sheets
are classified in groups by the canonical rule. Measured (metadata rows, this
machine): median 35 ms at 10,200 rows, 86 ms at 51,000, 154 ms at 102,000.

## Finish scan session (revised phase 7)

`services/session_finish.finish_scan_session()` (or `engine.finish_session()`
from the coordinator): a **final reconciliation** of every enabled source
attached to the session - paused ones included - then either an audited close
or **every** blocker as a typed code with a count, never only the first:
files stabilising or ready, sheets queued or processing, units still running,
unresolved required conflicts, rejected sheets whose rescan has not arrived,
rejected sheets whose likely rescan has arrived but is unconfirmed, suggested
rescans nobody answered, files awaiting a decision, deferred sheets, an
enabled source unreachable or not reconciled, another coordinator processing.
Disabled sources are reported by name. Outstanding rescans and deferred sheets
may be closed past only by a named operator's explicit acceptance of incomplete
results, recorded in the close's audit event. The close itself is the existing
one-transaction close (seal every batch, state, audit event): a crash leaves
the session open or closed, never half-closed. After it, files for the session
are held. *Reopen* is named and audited and stales final outputs; closing
again runs every check again.

## In the application (revised phase 8)

The operational GUI renders and drives everything above; it decides nothing
itself (`PHASE_H_HANDOFF.md`):

* **Sources** are added, renamed, enabled/disabled, paused/resumed and removed
  from a session on the Scan stage's session panel (*Session → Add Scanner
  Source...* turns session mode on). The folder can be typed or pasted - a UNC
  path that cannot be listed right now is kept and shown *Unreachable*.
  *Remove from Session* ends the source's attachment (audited); its ledger
  rows, sheets and history stay.
* **Controls** are the buttons of the panel, each one service call
  (`session_controls`, or a command to the engine's own thread for *Finish
  Current and Stop* / *Cancel Queued Work*). Captions follow the stored
  intent - they are re-read from the snapshot, never flipped by the click.
* **The snapshot** is polled once a second in a worker thread (one read at a
  time, at most one owed); the panel shows its activity label, partition
  counts, the three progress lines and per-source state.
* **Files waiting for an operator** are Resolve's *Files awaiting decision*
  view: exactly the options of `intake_decisions.OPTIONS`, a named reviewer,
  and *Release* disabled while the file's session is closed (reopen it first).
* **Finish scan session** is the panel's *Finish Scan Session...*: the
  read-only blocker preview, then the attempt (final reconciliation) through
  `finish_scan_session` - in the engine's thread while it runs.

## Manual intake

*Add Folder -> Process All*: the Scan stage creates its batch exactly as before;
the worker's registration step (`intake.record_manual_batch`) hashes the files
with the same `hash_file` as before, records each in the manual source (its
path as chosen, its hash, `registered`), links the batch to the source, and the
unchanged phase 4 duplicate link follows (mirrored into the ledger). Manual
readiness evidence is the operator's choice of finished files (one observation;
decoding at recognition, as always). A *Reprocess All* re-reads link to the same
rows. No new operator step.

## Restart

`IntakeService` construction runs `recover_after_restart`, which reads no file:

* `discovered` / `stabilizing`: observations and quiet-period start discarded -
  re-observed for the full quiet period;
* `ready` (unregistered): flagged for re-verification - read, hashed, decoded
  again before registration; `ready_at` kept, so the order is unchanged;
  different bytes become ready anew;
* registrations whose duplicate link had not committed: completed;
* the copy store's own `.part` files: removed;
* `vanished`, terminal rows, unreachable sources: unchanged; the next pass
  updates reachability and finds files written while OMRFlow was closed.

A project whose writer was killed mid-commit now reopens (SQLite's hot-journal
recovery runs before the pre-migration probe - a defect fixed in this phase).

## Guarantees and non-guarantees

Guaranteed (tested; see the handoff): no partial, truncated, locked, changing or
header-only file becomes ready; the hash and decode describe the same bytes;
exactly-once registration across repeated calls, restarts and outages; path is
never identity; unreachable is never vanished; source files are only read;
copies are verified before they count.

**Not** guaranteed or not yet tested: SMB/NFS semantics, scanner-specific write
patterns beyond those simulated, power loss (process kills only), a directory
with more than 10,000 files per source (measured up to 10,000), cold-cache
listing on a real share.
