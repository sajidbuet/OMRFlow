"""The golden one-batch scenario: same cohort, same configuration, on two builds.

Purpose:
    ROADMAP Phase C / ACCEPTANCE C9: a single-batch session must reproduce the
    pre-phase-4 reconciliation rows and workbook cell values exactly. This
    module builds one deterministic examination - three sets, one batch of
    one scan session, an absent candidate, a missing script, varied marks -
    reconciles, scores, closes the session and generates each set's final
    workbook, and captures the values that matter:

    * every reconciliation entry: candidate, status, attendance, scripts
      (by file name), exception and review flags;
    * every stored result: candidate, status, scores, counts, scan file;
    * every cell value of the result workbook's Rollwise and Meritwise
      sheets (the Summary and Processing Log sheets carry generation
      timestamps and are not compared).

    It uses only APIs present both on ``main`` before phase 4 (``0e94d67``)
    and after, so the same code runs against both builds:

        python tests/golden_one_batch.py --repo C:/path/to/checkout OUT.json

    puts that checkout's ``src`` and ``tests`` first on ``sys.path``. The
    fixture ``tests/fixtures/golden_one_batch/golden.json`` was produced that
    way from ``main`` at ``0e94d67``; ``tests/integration/test_golden_one_batch.py``
    compares the current build against it.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OPERATOR = "Golden Operator"

ROSTERS: dict[str, list[tuple[str, bool]]] = {
    "1": [(f"1{n:05d}", n == 4) for n in range(1, 9)],
    "2": [(f"2{n:05d}", False) for n in range(1, 9)],
    "3": [(f"3{n:05d}", n == 2) for n in range(1, 7)],
}
"""Per set: ``(roll, marked absent)``."""

MISSING = {"200008"}
"""Present on the list, no script."""


def _scripts() -> list[tuple[str, str, str, int]]:
    rows: list[tuple[str, str, str, int]] = []
    for code, roster in ROSTERS.items():
        for index, (roll, absent) in enumerate(roster):
            if absent or roll in MISSING:
                continue
            rows.append((f"g{code}_{index:02d}.png", roll, code, 4 + (index * 3 + int(code)) % 17))
    return rows


def _write_roster(path: Path, code: str) -> Path:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = f"Set {code}"
    for column, header in enumerate(("Sl.No.", "Roll No.", "Name", "Total", "Merit"), 1):
        sheet.cell(row=3, column=column, value=header)
    for offset, (roll, absent) in enumerate(ROSTERS[code]):
        row = 4 + offset
        sheet.cell(row=row, column=1, value=offset + 1)
        sheet.cell(row=row, column=2, value=roll)
        sheet.cell(row=row, column=3, value=f"CANDIDATE {roll}")
        if absent:
            sheet.cell(row=row, column=4, value="ABSENT")
    workbook.save(path)
    workbook.close()
    return path


def capture(workdir: Path) -> dict[str, Any]:
    """Build the examination in ``workdir`` and return the comparable values."""
    import openpyxl
    from sqlalchemy import update
    from tests.conftest import build_answer_sheet_template
    from tests.integration.test_reject_and_rescan import make_result

    from omr_scanner.database.models import BatchScan
    from omr_scanner.services import (
        batch_store,
        candidate_import,
        create_project,
        project_sets,
        reconciliation_store,
        report_store,
        review_store,
        scan_sessions,
        scoring_store,
        set_attendance,
    )
    from omr_scanner.services.answer_key import plan_for, read_key

    template = build_answer_sheet_template()
    plan = plan_for(template)
    session = create_project(workdir / "workspace", "Golden")
    database = session.database
    try:
        set_ids: dict[str, str] = {}
        rosters: dict[str, int] = {}
        for code in ROSTERS:
            set_ids[code] = project_sets.add_set(database, code, f"Paper {code}").set_id
            path = _write_roster(workdir / f"set{code}.xlsx", code)
            assignment = set_attendance.assign_attendance_workbook(
                database, set_ids[code], path, candidate_import.read_roster(path),
                imported_by=OPERATOR,
            )
            rosters[code] = assignment.roster_id
            stored = scoring_store.save_key(
                database, read_key("A" * plan.question_count, plan, code).to_key()
            )
            scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)

        scans_dir = session.project.layout.scans_original_dir
        scripts = _scripts()
        paths = []
        for name, _roll, _code, _correct in scripts:
            path = scans_dir / name
            path.write_bytes(f"golden:{name}".encode())
            paths.append(path)
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        by_path = batch_store.scan_ids_by_path(database, batch_id)
        with database.session() as db:
            for path, (_name, roll, code, correct) in zip(paths, scripts, strict=True):
                db.execute(
                    update(BatchScan)
                    .where(BatchScan.scan_id == by_path[path])
                    .values(
                        status="completed", identifier_value=roll, set_code_value=code,
                        result_json=json.dumps(make_result(path, roll, code, correct).to_dict()),
                    )
                )
        batch_store.finalise_batch(database, batch_id)
        review_store.sync_duplicate_identifiers(database, batch_id)
        review_store.sync_undefined_set_codes(database, batch_id)

        names = {scan_id: path.name for path, scan_id in by_path.items()}
        captured: dict[str, Any] = {"entries": {}, "results": {}, "workbooks": {}}
        for code, roster_id in rosters.items():
            reconciliation_store.reconcile_batch(database, roster_id, batch_id)
            scoring_store.score_batch(
                database, roster_id, batch_id, template, computed_by=OPERATOR
            )
            captured["entries"][code] = [
                {
                    "candidate": entry.candidate_id,
                    "status": str(entry.status.value),
                    "attendance": str(getattr(entry, "attendance", "")),
                    "scripts": sorted(
                        names.get(view.script.scan_id, "?") for view in entry.scripts
                    ),
                    "script_count": entry.script_count,
                }
                for entry in reconciliation_store.list_entries(database, roster_id, batch_id)
            ]
            captured["results"][code] = [
                {
                    "candidate": item.candidate_id,
                    "status": item.status.value,
                    "scan": names.get(item.scan_id) if item.scan_id else None,
                    "correct": item.correct_count,
                    "incorrect": item.incorrect_count,
                    "blank": item.blank_count,
                    "raw": str(item.raw_score),
                    "final": str(item.final_score),
                }
                for item in scoring_store.list_results(database, roster_id, batch_id, template)
            ]

        active = scan_sessions.active_scan_session(database)
        assert active is not None
        scan_sessions.close_scan_session(database, active.scan_session_id, closed_by=OPERATOR)
        exports = workdir / "exports"
        for code, set_id in set_ids.items():
            # A preview of the closed session: the result sheets are the same
            # as a final export's, and set 2's deliberate missing script does
            # not stop the workbook being written on either build.
            outcome = report_store.generate_for_set(
                database, set_id, batch_id, template, project_name="Golden",
                output_dir=exports, computed_by=OPERATOR, final=False,
            )
            assert outcome.output_path is not None, (code, outcome.warnings)
            workbook = openpyxl.load_workbook(outcome.output_path)
            try:
                captured["workbooks"][code] = {
                    sheet.title: [
                        [None if value is None else str(value) for value in row]
                        for row in sheet.iter_rows(values_only=True)
                    ]
                    for sheet in workbook.worksheets
                    if sheet.title not in ("Summary", "Processing Log")
                }
            finally:
                workbook.close()
        return captured
    finally:
        session.close()


def main(argv: list[str]) -> int:
    import tempfile

    repo = Path(argv[argv.index("--repo") + 1]).resolve() if "--repo" in argv else None
    output = Path(argv[-1])
    if repo is not None:
        sys.path.insert(0, str(repo / "src"))
        sys.path.insert(0, str(repo))
    with tempfile.TemporaryDirectory(prefix="omrflow_golden_") as scratch:
        captured = capture(Path(scratch))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(captured, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - fixture generator
    raise SystemExit(main(sys.argv[1:]))
