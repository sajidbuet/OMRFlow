# Reconciliation fixtures (Phase 7)

Small, deterministic candidate lists for the import and reconciliation tests.

**Every identifier and name here is fictional.** Roll numbers are in the
`15000000`/`10000` ranges nobody uses, and names are placeholders
(`CANDIDATE A`, `PRIVATE CANDIDATE`). Real candidate data must never reach this
repository - see `docs/TESTING.md` ("Fixture policy") and the privacy rule in
`docs/ARCHITECTURE.md`.

| File | What it is for |
| --- | --- |
| `roster_basic.csv` | The ordinary case: five candidates, a marks column, one `ABSENT` and one `ABS`. |
| `roster_tokens.csv` | Every spelling and spacing of the absence tokens, plus the near-misses (`ABSENTEE`, `ABSENCE`, `ABS123`) that must **not** be read as absences. |
| `roster_duplicates.csv` | The same candidate ID twice. Must be refused, not silently deduplicated. |
| `roster_blank_ids.csv` | Rows with an empty candidate ID. |
| `roster_no_id_column.csv` | No column that looks like a roll number. |
| `roster_ambiguous.csv` | Two columns that both look like candidate IDs. Must ask rather than guess. |
| `roster_reordered.csv` | The usual columns in an unusual order, with extra columns to ignore. |
| `roster_bom.csv` | UTF-8 with a byte-order mark, which Excel writes by default. |
| `roster_header_only.csv` | Headings and no candidates. |

XLSX fixtures are **generated in the test run** rather than committed, by
`tests/conftest.py`'s `make_workbook` helper. A binary fixture cannot be
reviewed in a pull request, and the structures under test (numeric roll
numbers, several worksheets, trailing blank rows) are exactly the things worth
seeing spelled out in the test that needs them.

The packaged sample workbook - the one the application offers to save - is a
different thing and lives at
`src/omr_scanner/resources/templates/candidate_attendance_sample.xlsx`.
`tests/unit/test_candidate_import.py` reads it to prove the shipped file is
importable, which is the one thing a generated fixture cannot check.
