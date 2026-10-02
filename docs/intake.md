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
* Nothing is recognised: the batch is ordinary, ready for the unchanged
  `process_batch` (phase 6).

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
