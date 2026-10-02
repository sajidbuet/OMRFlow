# ADR-0008: Watched sources are ingested by verified copy; manual imports are read in place

- **Status:** Accepted
- **Date:** 2026-10-02
- **Phase:** 0.1.1-alpha.0, revised phase 5 / roadmap Phase D (intake sources and
  ledger)
- **Builds on:** ADR-0002 (project folder; scans referenced in place), ADR-0006
  (crash-safe work units), ADR-0007 (session effective set; exact duplicates)
- **Schema:** **16** (migration 16: `intake_source`, `intake_source_attachment`,
  `intake_file`; `batch_scan.intake_file_id` / `registered_at`;
  `scan_batch.source_id`)
- **Answers:** ARCHITECTURE_NOTES §10.5 and §16 Q7

## Context

ADR-0002 references original scans **in place**: a batch records each file's
path and OMRFlow reads it there for recognition and again whenever Resolve
shows the sheet. For a finite, operator-chosen local folder that is right: the
files are the institution's own archive, copying them doubles disk use and
import time, and the folder outlives the project.

Watched intake is different. The files arrive on a **scanner workstation's**
folder or share, while the session runs, and Resolve may need a sheet hours
later:

* the share can go offline (machine asleep, network down, PC shut at the end
  of the day);
* scanner software and operators clear their output folders to free space;
* counters reset and overwrite `000001.jpg` with another candidate's script
  (path reuse);
* a share's metadata (`mtime`) caching makes "has this changed" weaker than on
  a local disk.

A project whose evidence lives on someone else's transient folder cannot be
reviewed, re-read or audited later.

## Decision

| Source kind | Default | What recognition and Resolve read | Provenance kept |
|---|---|---|---|
| **Watched** (`kind = watched`) | **Ingest by copy** (`ingest_mode = copy`) | The project copy | Source, relative path, absolute path as observed, size, `mtime_ns`, hash |
| **Manual** (built-in source; *Add Folder / Add Files*) | **Reference in place** (`ingest_mode = reference`) - today's behaviour, unchanged | The original file | The path as chosen, size, `mtime_ns`, hash |

A watched source may be configured `reference` explicitly (e.g. a local,
stable, archived folder); it is then read in place and the source must stay
available, as for a manual import.

### Storage layout

```text
<project>/scans_original/intake/<first two hex>/<sha256><suffix>
```

**Content-addressed.** The name is the SHA-256 of the bytes, so identical bytes
from any source share one copy (an exact duplicate costs no extra space), a
copy's name proves what it must contain, and a re-attempt after a crash lands
on the same file. `intake_file.ingest_path` records the copy relative to the
project root; `batch_scan.source_path` holds its absolute path (as every
existing scan row does - see Consequences).

### When and how a copy becomes authoritative

A file is copied only **after** it is `ready` - stable, read once, hashed and
fully decoded from that one read, unchanged after the read - and only when it
is **registered** (`IntakeService.register`). Each step is safe to interrupt:

1. **Read** the source once more; its bytes must hash to the `ready` row's
   verified SHA-256, and size and `mtime_ns` must still match. Otherwise the row
   returns to `stabilizing` (`source_changed`) or `vanished`; nothing is
   written.
2. **Existing destination**: if the content-addressed file exists and hashes to
   the expected value, it is reused (identical bytes from another source, or a
   crash after step 5 below). If it exists with **different** bytes it is
   moved aside to `<name>.mismatch-<n>` - never deleted - and replaced.
3. **Write** `<name>.ingest-<random>.part` in the destination folder, flush,
   `fsync`, close.
4. **Verify the copy**: re-read the temporary file and hash it. A mismatch (a
   failing disk, a corrupting writer) deletes the temporary file and leaves the
   row `ready`; registration reports the failure.
5. **Atomic rename** (`os.replace`, same volume) onto the final name.
6. **Commit** one transaction: the finite batch, its `batch_scan` rows pointing
   at the copies and carrying the verified hash, the ledger rows `registered`
   with `ingest_path`, the batch sealed.

The copy is **never** considered valid because a copy call returned; it is
valid because its bytes were re-read and hashed after writing.

### Crash and recovery semantics

| Interrupted | What reopening finds | What recovery does |
|---|---|---|
| During steps 1-3 | At most a `.part` file; the row `ready` | `IngestStore.clean_temporaries` (run by `recover_after_restart`) deletes the store's own `.part` files; the row is re-verified, then registered on the next call |
| Between 4 and 5 | A verified `.part`; the row `ready` | As above (the `.part` is removed, the copy is redone) |
| Between 5 and 6 | A correct, unreferenced copy; the row `ready` | Re-verification, then registration reuses the existing copy (step 2) - no second copy |
| After 6, before phase 4's duplicate link | Registered rows whose duplicate decision is not recorded | `recover_after_restart` runs `link_exact_duplicates` for those batches and mirrors it into the ledger |

A copy that ends up referenced by nothing (a file registered nowhere, e.g. a
`ready` row that later changed) is harmless content-addressed data; it is not
deleted automatically in this phase.

### Source files

**Sources are read-only inputs.** Intake opens source files for reading only
and never renames, truncates, moves or deletes them - not after a successful
copy either. Clearing a scanner folder remains the scanner operator's
decision. (Tests: hash-and-metadata snapshots of every source tree before and
after intake.)

### After registration, the source disappears

* **Copy:** nothing downstream is affected; the ledger row stays `registered`
  (history); the source path is provenance only.
* **Reference:** the existing availability check and relink
  (`scan_provenance.check_availability` / `relink_scan`) apply, as for every
  manual import.

## Consequences

**Disk space.** A watched session stores one copy of every unique page in the
project: roughly the size of the scans themselves (a 300 dpi A4 greyscale JPEG
is typically 0.2–1 MB; 10,000 sheets ≈ 2–10 GB). Exact duplicates add nothing.
Project Health's existing low-disk warning applies; registration reports
*no space left* per file and leaves those files `ready`.

**Project portability.** Copies live inside the project, so a project folder
moved or archived with ordinary tools carries its evidence. `ingest_path` is
project-relative. `batch_scan.source_path` is absolute, as for every scan row
since Phase 5; moving a project still needs the existing relink for scans whose
recorded path no longer exists (unchanged limitation, not made worse).

**Synchronised and cloud folders.** A project kept in OneDrive / Dropbox /
a synced share now receives large binary files during a session. The sync
client uploads them and may briefly hold them open (harmless: each copy is
written and verified once, before the sync client sees it), and on a second
machine may present them as placeholders not yet downloaded - a project opened
there could find copies it cannot read until they arrive. Recommendation (docs): keep
an active session's project on a local disk and back it up, rather than run a
live session inside a synchronised folder. The database's rollback-journal
choice (ADR-0002) is unchanged.

**Backups.** `project_backup` backs up the database, not the scan folders; the
copies are ordinary files to back up with the project folder.

**Read-only projects.** A read-only open never ingests: the intake service
refuses to start on a read-only database.

**Duplicate storage.** Content addressing means a second source delivering the
same bytes writes nothing new; its ledger row is `duplicate_content` and its
registered scan points at the same copy.

**Identity is not changed by the copy.** Duplicate detection, the effective
set and Reject & Rescan see the same SHA-256 whichever path the bytes took.

## Alternatives considered

* **Reference in place for watched sources.** Rejected: Resolve and re-reading
  would depend on a scanner PC's folder that is routinely cleared or offline.
* **Copy every manual import too.** Rejected for 0.1.1: doubles disk use and
  import time for the finite workflow, contradicting ADR-0002 and the "no new
  operator step" rule, for files that already live in the institution's own
  folder.
* **Name copies by ledger id or original name.** Rejected: identical bytes
  would be stored repeatedly, and a crash-retry could not recognise its own
  earlier copy.
* **Move (rename) source files into the project.** Rejected: OMRFlow never
  mutates source files.

## Not decided here

* Removing unreferenced copies, or a per-session storage quota (later).
* Real SMB behaviour - `mtime` caching, oplocks, share disconnects during a
  copy - is **not** validated; only phase 10's real network-share
  qualification can.
