# Installed-build check: upgrade schema-12 colliding_sets

**Result: PASS**

- executable: `C:\Users\Sajid\AppData\Local\Programs\OMRFlow\OMRFlow.exe`
- fixture: `C:\Research\OMRflow-p10\tests\fixtures\schema12\colliding_sets`
- schema_before: `12`
- schema_expected: `17`
- title: `colliding_sets - OMRFlow 0.1.1-alpha.0`
- migrated_seconds: `7.17`

## What the installed GUI showed

- scan / `scanSessionLabel`: Scan session: Legacy batch 5a5df819 (2026-10-01 02:56) - open, 1 batch(es)
- scan / `workersLabel`: 16 CPU threads detected - no scans added yet
- scan / `batchStateLabel`: Processing
- resolve / `reviewBatchLabel`: Scan session Legacy batch 5a5df819 (2026-10-01 02:56) · open · results provisional
- resolve / `reviewSummaryLabel`: 0 total   0 unresolved   0 resolved
- attendance / `attendanceBatchLabel`: Batch: 5a5df819 · scan session Legacy batch 5a5df819 (2026-10-01 02:56) (open - provisional)
- attendance / `activeRosterLabel`: Set A · attendance.xlsx (Sheet) · 2 candidate(s) · 2 expected present · 0 absent · Reconciled: 1 exception(s)
- attendance / `reconciliationSummaryLabel`: 1 attendance exception(s) still need review.
- answer_key / `answerKeySessionLabel`: Scan session Legacy batch 5a5df819 (2026-10-01 02:56) · open · results provisional
- answer_key / `answerKeyReadinessLabel`: Answer keys: 1 of 2 verified
- results / `resultsBatchLabel`: PROVISIONAL - scan session 'Legacy batch 5a5df819 (2026-10-01 02:56)' is open; results may change.
- results / `resultsSummaryLabel`: Candidates 2 · Scored 1 · Absent 0 · Cannot be scored 1 · Need recalculating 0

## Checked from outside

```json
{
 "schema_version": 17,
 "backups": [
  "backup_20261007T045659Z_before-migration-12-to-17.sqlite3"
 ],
 "backup_schema_version": 12,
 "backup_manifest": {
  "backup_file": "backup_20261007T045659Z_before-migration-12-to-17.sqlite3",
  "created_at": "2026-10-07T04:56:59.623491+00:00",
  "app_version": "0.1.1-alpha.0",
  "schema_version": 12,
  "reason": "before-migration-12-to-17",
  "source_size_bytes": 282624,
  "backup_sha256": "dfd40058fa17900fbf475cf151e21b00daeb51fe717f60ac8c17b99252956b05"
 },
 "scan_sessions": [
  {
   "name": "Legacy batch 5a5df819 (2026-10-01 02:56)",
   "state": "open",
   "origin": "backfill"
  }
 ],
 "batches": 1,
 "sets": [
  {
   "code": "A",
   "canonical_code": "A"
  },
  {
   "code": "a",
   "canonical_code": null
  },
  {
   "code": "B",
   "canonical_code": "B"
  }
 ],
 "sets_kept_as_collisions": [
  "a"
 ],
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
 "health": [
  {
   "level": "warning",
   "code": "SOURCE_SCANS_MISSING"
  },
  {
   "level": "warning",
   "code": "UNRESOLVED_RECONCILIATION_EXCEPTIONS"
  },
  {
   "level": "warning",
   "code": "SET_CODE_COLLISION"
  }
 ]
}
```

## Failures

none
