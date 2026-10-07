# Installed-build check: upgrade v0.1.0-alpha.2 project

**Result: PASS**

- executable: `C:\Users\Sajid\AppData\Local\Programs\OMRFlow\OMRFlow.exe`
- fixture: `C:\Research\OMRflow-p10\Scratch\phase10\upgrade\fixtures\alpha2-project`
- schema_before: `9`
- schema_expected: `17`
- title: `Qualification Exam - OMRFlow 0.1.1-alpha.0`
- migrated_seconds: `10.22`

## What the installed GUI showed

- scan / `scanSessionLabel`: Scan session: Legacy batch b7d6446f (2026-10-07 04:32) - open, 1 batch(es)
- scan / `workersLabel`: 16 CPU threads detected - no scans added yet
- scan / `batchStateLabel`: Processing
- resolve / `reviewBatchLabel`: Scan session Legacy batch b7d6446f (2026-10-07 04:32) · open · results provisional
- resolve / `reviewSummaryLabel`: 0 total   0 unresolved   0 resolved
- attendance / `attendanceBatchLabel`: Batch: b7d6446f · scan session Legacy batch b7d6446f (2026-10-07 04:32) (open - provisional)
- attendance / `activeRosterLabel`: candidates.csv · 6 candidate(s) · 5 expected present · 1 absent · Reconciled: no exceptions
- attendance / `reconciliationSummaryLabel`: Reconciliation complete. No attendance exceptions need review.
- answer_key / `answerKeySessionLabel`: Scan session Legacy batch b7d6446f (2026-10-07 04:32) · open · results provisional
- answer_key / `answerKeyReadinessLabel`: Answer keys: 1 of 1 verified
- results / `resultsBatchLabel`: PROVISIONAL - scan session 'Legacy batch b7d6446f (2026-10-07 04:32)' is open; results may change.
- results / `resultsSummaryLabel`: Candidates 6 · Scored 5 · Absent 1 · Cannot be scored 0 · Need recalculating 0

## Checked from outside

```json
{
 "schema_version": 17,
 "backups": [
  "backup_20261007T045444Z_before-migration-9-to-17.sqlite3"
 ],
 "backup_schema_version": 9,
 "backup_manifest": {
  "backup_file": "backup_20261007T045444Z_before-migration-9-to-17.sqlite3",
  "created_at": "2026-10-07T04:54:44.197940+00:00",
  "app_version": "0.1.1-alpha.0",
  "schema_version": 9,
  "reason": "before-migration-9-to-17",
  "source_size_bytes": 581632,
  "backup_sha256": "a30546142ddd4935c88485f30eeff6084a52b89ca237d1c042a9e7e5d7e8f00b"
 },
 "scan_sessions": [
  {
   "name": "Legacy batch b7d6446f (2026-10-07 04:32)",
   "state": "open",
   "origin": "backfill"
  }
 ],
 "batches": 1,
 "sets": [],
 "integrity": {
  "quick_check": [
   "ok"
  ],
  "integrity_check": [
   "ok"
  ],
  "foreign_key_check": [],
  "journal_mode": "delete"
 },
 "health": []
}
```

## Failures

none
