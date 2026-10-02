# Revised phase 5 handoff — intake sources and ledger (roadmap 0.1.1-D)

> Naming: handoffs in this directory are lettered in revised-phase order
> (A = revised 1 … D = revised 4), so revised phase 5 is `PHASE_E_HANDOFF.md`.
> It implements **roadmap phase 0.1.1-D**, not roadmap phase E.

Branch `feat/0.1.1-phase5-intake-ledger`, from `main` at `fd063f8` (the
Phase 4 merge; Phase 4 tip `98dc522`). Worktree `C:\Research\OMRflow-p5`.
**Not merged, not tagged, nothing released.**

## Implementation plan (written before code changes)

### What the code does today (inventory, 2026-10-02, `fd063f8`)

* **Manual intake.** *Add Folder* (`ScanPage.add_scan_paths`) only lists paths
  (`scan_import.collect_scan_files`: glob, supported suffix, natural sort); it
  writes nothing. *Process All* registers the batch (`scan_sessions.start_batch`
  → `batch_store.insert_batch`, one `batch_scan` row per path with size and
  float mtime). The worker thread then hashes the batch
  (`scan_provenance.compute_hashes_for_batch` → `hash_file`, SHA-256, 1 MiB
  streaming) and links exact duplicates before recognition
  (`scan_lifecycle.link_exact_duplicates`, session-wide, status `duplicate`,
  `scan_rejection.state = duplicate_content`).
* **Decoding** is `alignment_service.load_scan_image` (`np.fromfile` +
  `cv2.imdecode`). OpenCV 5.0.0 is installed and rejects every truncated
  JPEG/PNG/TIFF/BMP probed (cut 2 bytes to 90 %); `pyproject` allows
  OpenCV ≥ 4.9, whose libjpeg is lenient with truncated JPEGs - so OpenCV
  alone is not a sufficient completeness check.
* **No source, ledger, stabilisation, reachability or copy ingest exists.**
  Schema is 15. Scans are referenced in place (ADR-0002).
* **Defect found while inspecting:** the schema-14 upgrade fixtures
  (`tests/fixtures/schema14/*/database.sqlite`) were never committed -
  `.gitignore` ignores `*.sqlite` and only re-includes schema 12/13 - so
  `test_session_scope_migration` cannot pass on a clean checkout.

### Design

1. **Pure vocabulary** `domain/intake.py`: states, the transition table,
   reason codes, the stabilisation policy (configuration, not constants), the
   name/exclusion classifier, the "due for verification" rule.
2. **Image completeness** `services/image_integrity.py`: one function that
   takes the bytes of one read and decides: structural end-of-image check
   (JPEG EOI, PNG chunk walk to IEND, TIFF strip/tile extents, BMP pixel
   extent), TIFF page count (multi-page refused), and a full decode through
   the same `cv2.imdecode` call recognition uses (refactored out of
   `load_scan_image`, not duplicated).
3. **Filesystem boundary** `services/intake_fs.py`: a small protocol
   (`list_source`, `read_snapshot`) with the real `os.scandir` implementation
   (UNC and long-path aware, sharing-violation classification) and the
   content-addressed project copy store with temp-file + verify + atomic
   rename. Tests use a fake filesystem and an injectable clock.
4. **Migration 16** (additive): `intake_source`, `intake_source_attachment`,
   `intake_file` (the ledger), `batch_scan.intake_file_id` /
   `registered_at`, `scan_batch.source_id`; indexes for the per-source
   current-path lookup, hash, state/ready order and session.
5. **`services/intake.py`**: source CRUD + attach/detach (audited), lazy
   built-in manual source, reconciliation (listing outside transactions,
   one short write transaction per pass, verification reads outside
   transactions, compare-and-set commits), reachability, restart recovery
   (no I/O: unstable rows re-observe, ready rows flagged for re-verification,
   order kept), held diversion for closed sessions, the registration API
   (explicit item list → one finite batch, copy-ingest per the ADR, links,
   then Phase 4's `link_exact_duplicates` for duplicate content), manual
   recording (*Add Folder → Process All*) converging on the same ledger and
   duplicate path.
6. **Manual convergence**: the Scan worker's hashing step calls
   `intake.record_manual_batch`, which hashes with `hash_file` (as before),
   writes the same `batch_scan.content_sha256`, records the built-in manual
   source's ledger rows, and then the unchanged `link_exact_duplicates`.
   No GUI change.
7. **ADR-0008**: copy for watched sources, reference in place for manual.
8. **Project Health** ledger invariants.
9. **Tests**: state-machine unit tests, fake-filesystem scenarios, real
   temporary-directory integration, real writer subprocess killed mid-file,
   real intake subprocess killed and restarted, three-source acceptance with
   exact identity sets, migration from real schema-15 fixtures (written by
   `main` `fd063f8`), measurements.

Not in this phase: unit scheduler, background runner, recognition of intake
items, quality decisions, held-file decisions, GUI.
