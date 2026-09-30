# Prompt 04 — `0.1.1-D`: Intake sources and ledger

You are working in the OMRFlow repository. This is **phase 0.1.1-D** of the
`0.1.1-alpha.0` line. Phases A and B must be merged (`PHASE_A_HANDOFF.md`,
`PHASE_B_HANDOFF.md` exist); phase C may or may not be merged. Otherwise stop
and say so. Branch: `feat/0.1.1-d-intake-ledger`.

Plan: `ROADMAP.md` §5 D; `ARCHITECTURE_NOTES.md` §§2.3, 3 (defect 4), 10, 15,
16 (Q7, Q11, Q12); `ACCEPTANCE_CRITERIA.md` §2 D. The code is the fact; record
discrepancies.

Files from named sources — scanner folders, computers, network shares, manual
imports — are discovered, stabilised, hashed and made ready for registration
**exactly once**, surviving restart. This phase is **headless**; creating
processing units and recognising files is phase E.

---

## 1. Inspect first

- Phase A, B (and C, if merged) handoffs and ADRs; `services/scan_sessions.py`
- `services/scan_import.py` (`collect_scan_files`, supported suffixes, natural
  sort), `services/scan_provenance.py` (`hash_file`, `duplicate_groups`,
  `check_availability`, `relink_scan`), `services/alignment_service.py`
  `load_scan_image` (how recognition decodes images today — reuse it for the
  decode check rather than adding a second decoder),
  `services/project_lock.py`, `database/engine.py`
- `gui/scan/worker.py` (how hashing is invoked today)
- `docs/decisions/ADR-0002-project-on-disk-layout.md` (scans referenced in place)
- `packaging/audit_dependencies.py`, `packaging/verify_frozen_imports.py` (only
  if you consider a new dependency)
- tests: `test_scan_import.py`, `test_scan_provenance.py`, phase A–C tests

Run the full suite first; record the baseline.

## 2. Preserve

- `collect_scan_files` behaviour for the finite workflow; *Add Folder* keeps
  working and records into the built-in manual source.
- `hash_file` (SHA-256, 1 MiB streaming) — reuse it.
- Source files are **never modified, moved or deleted**.
- Single writer; no transaction spans listing, hashing or copying.
- No Qt in services; the engine runs headlessly.
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **ADR** (next free number): ingest-by-copy vs reference-in-place
   (ARCHITECTURE_NOTES.md §10.5) — decide, with disk-space and
   synchronised-folder consequences.
2. **Migration** (next free number): `intake_source` (project-level, attached
   to sessions — Q11), `intake_file` (the ledger, §10.2), `batch_scan.
   intake_file_id` and `registered_at`, `scan_batch.source_id`. NULL for every
   existing row ("added before intake sources existed"). The built-in manual
   source is created on first use by the service, not by the migration.
3. **`services/intake.py`**: source CRUD (label, folder incl. UNC, kind,
   recursive — Q12, exclusions, enabled); authoritative periodic
   **reconciliation** (`os.scandir`), idempotent; diff by relative path and
   `(size, mtime_ns)`.
4. **Stabilisation state machine** (§10.2, §10.4) with injectable clock and
   filesystem: quiet period (*K*, *T*), openable (sharing violation = "not
   yet"), hashed **and fully decoded from one read** (a truncated file with a
   valid header must fail), re-stat unchanged, not a duplicate; stall ceiling;
   `unreadable` after bounded retries. Multi-page TIFF handled per Q12 and
   documented.
5. **Collision rules** (§10.3): same filename across sources distinct;
   `path_reused` for new content at a known path; `duplicate_content` for bytes
   equal to an effective scan of the session, linked, never submitted.
6. **Ingest** per the ADR: if copying, copy after ready, verify the copy's hash,
   record both paths.
7. **Reachability**: listing failure → `unreachable` / `permission_denied`
   with time and detail; never marks files vanished; other sources continue; on
   return a full reconciliation.
8. **Restart**: on read-write open, unstable files restart stabilisation;
   ready-but-unregistered files are re-verified (stat + hash); then every
   enabled source is reconciled.
9. **Registration API** that phase E's scheduler will call: register ready files
   of one source into a given sealed batch of a session (through
   `batch_store`), idempotently. Manual *Add Folder* converges on it.
10. **Engine object** with `tick(now)` (pure step) and a Qt-free runner that
    ticks on an interval in a background thread. Notifications only as an
    optional early-reconcile hint, dependency-audited; shipping without them is
    complete.

## 4. Out of scope

- Unit scheduling, recognition, conflict sync, quality decisions (E).
- Any GUI (F).
- The real SMB qualification (G) — but design for it.

## 5. Persistence expectations

- State transitions durable before the next step relies on them; a kill at any
  point leaves a state restart recovery handles.
- Writes batched per reconciliation pass, bounded, never held across I/O.
- Upgrade test from the prior schema and from a schema-12 fixture.

## 6. Tests required

- **Unit** (fake clock, fake filesystem): every transition; quiet-period edges;
  stepped growth with pauses longer than *T*; held-open; header-first;
  zero-byte; rename-into-place (`scan.tmp` → `scan.jpg`); deletion before
  ready; reappearance; path reuse; duplicates within and across sources;
  exclusions; unreachable → reachable; restart mid-stabilisation.
- **Integration** (real temporary directories, real writer threads/processes):
  three sources writing `000001.jpg…` concurrently; a writer killed mid-file;
  engine stopped while writers continue, then restarted — every file once, no
  partial file ready, source hashes unchanged; *Add Folder* and a watched
  source give identical ledger records.
- **Soak** (mark `stress`): randomised arrivals over ≥ 2,000 files.
- **Measurement** in the handoff: listing time for 1k / 10k-file directories;
  stabilisation latency distribution — the basis for the defaults.
- Whole suite unchanged; Phase 10 self-test passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m stress -k intake
```

## 8. Documentation updates

New `docs/intake.md` (sources, states, stabilisation rules and parameters,
collision rules, reachability, restart, guarantees and non-guarantees); the new
ADR; `docs/ARCHITECTURE.md`; `docs/DATA_MODEL.md`; `ROADMAP.md` §7; new
`PHASE_D_HANDOFF.md`; `CURRENT_STATE.md`; README Development/Testing status
(**network-share status: not yet validated**); `docs/wiki/Development-Roadmap.md`;
`CHANGELOG.md`.

## 9. Status reporting

Separately: implemented; tested; synthetic validation — local temporary-
directory soak only, not the campaign; **network-share validation: not
performed**; real scanners not performed; production not performed. Local-disk
tests say nothing about SMB `mtime` caching or share disconnects — state it.

Commit in small commits, push the branch, do not merge unless asked.
