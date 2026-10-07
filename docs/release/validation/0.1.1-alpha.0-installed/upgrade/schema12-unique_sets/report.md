# Installed-build check: upgrade schema-12 unique_sets

**Result: PASS**

- executable: `C:\Users\Sajid\AppData\Local\Programs\OMRFlow\OMRFlow.exe`
- fixture: `C:\Research\OMRflow-p10\tests\fixtures\schema12\unique_sets`
- schema_before: `12`
- schema_expected: `17`
- title: `unique_sets - OMRFlow 0.1.1-alpha.0`
- migrated_seconds: `6.25`

## What the installed GUI showed

- scan / `scanSessionLabel`: Scan session: Legacy batch 0e55457a (2026-10-01 02:56) - open, 1 batch(es)
- scan / `workersLabel`: 16 CPU threads detected - no scans added yet
- scan / `batchStateLabel`: Processing
- resolve / `reviewBatchLabel`: Scan session Legacy batch 0e55457a (2026-10-01 02:56) · open · results provisional
- resolve / `reviewSummaryLabel`: 0 total   0 unresolved   0 resolved
- attendance / `attendanceBatchLabel`: Batch: 0e55457a · scan session Legacy batch 0e55457a (2026-10-01 02:56) (open - provisional)
- attendance / `activeRosterLabel`: Set 10 · attendance.xlsx (Sheet) · 2 candidate(s) · 2 expected present · 0 absent · Reconciled: no exceptions
- attendance / `reconciliationSummaryLabel`: Reconciliation complete. No attendance exceptions need review.
- answer_key / `answerKeySessionLabel`: Scan session Legacy batch 0e55457a (2026-10-01 02:56) · open · results provisional
- answer_key / `answerKeyReadinessLabel`: Answer keys: 1 of 2 verified
- results / `resultsBatchLabel`: PROVISIONAL - scan session 'Legacy batch 0e55457a (2026-10-01 02:56)' is open; results may change.
- results / `resultsSummaryLabel`: Candidates 2 · Scored 2 · Absent 0 · Cannot be scored 0 · Need recalculating 0

## Checked from outside

```json
{
 "schema_version": 17,
 "backups": [
  "backup_20261007T045518Z_before-migration-12-to-17.sqlite3"
 ],
 "backup_schema_version": 12,
 "backup_manifest": {
  "backup_file": "backup_20261007T045518Z_before-migration-12-to-17.sqlite3",
  "created_at": "2026-10-07T04:55:18.692614+00:00",
  "app_version": "0.1.1-alpha.0",
  "schema_version": 12,
  "reason": "before-migration-12-to-17",
  "source_size_bytes": 282624,
  "backup_sha256": "f85469bebc8b89ad79825c551a0b8ad945ed1d53c4dac6a1e851703e6caa31b6"
 },
 "scan_sessions": [
  {
   "name": "Legacy batch 0e55457a (2026-10-01 02:56)",
   "state": "open",
   "origin": "backfill"
  }
 ],
 "batches": 1,
 "sets": [
  {
   "code": "10",
   "canonical_code": "10"
  },
  {
   "code": "11",
   "canonical_code": "11"
  }
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
  }
 ]
}
```

## Failures

none
