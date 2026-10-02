"""The real Scan stage records *Add Folder -> Process All* into the manual source.

0.1.1 revised phase 5: no new operator step and no GUI change. The worker's
registration step writes the built-in manual source's ledger rows through the
same hash and duplicate rule as watched intake; the scans are read exactly as
before.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import cv2
import pytest
from sqlalchemy import select
from tests.conftest import build_answer_sheet_template, render_marked_sheet
from tests.gui.test_exact_duplicates_gui import TIMEOUT_MS, _marks

from omr_scanner.database.models import BatchScan
from omr_scanner.domain.intake import IntakeState, SourceKind
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import intake, save_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui


def test_add_folder_then_process_all_fills_the_manual_ledger(
    qtbot, project_session: ProjectSession, tmp_path: Path
) -> None:
    template = build_answer_sheet_template()
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    template_file = save_template(template, path)
    folder = tmp_path / "exam scans"
    folder.mkdir()
    first = folder / "000001.png"
    cv2.imwrite(str(first), render_marked_sheet(template, _marks("170501")))
    second = folder / "000002.png"
    cv2.imwrite(str(second), render_marked_sheet(template, _marks("170502")))
    shutil.copyfile(first, folder / "000003.png")  # an exact duplicate
    before = {item: item.read_bytes() for item in folder.iterdir()}

    page = ScanPage(next(item for item in WORKFLOW_PAGES if item.key == "scan"))
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    assert page.load_template_from(template_file) is True
    page.add_scan_paths([folder])  # Add Folder
    with qtbot.waitSignal(page.batch_finished, timeout=TIMEOUT_MS) as finished:
        assert page.process_all() is True
    assert len(finished.args[0].processed) == 2

    database = project_session.database
    (source,) = intake.list_sources(database)
    assert source.kind is SourceKind.MANUAL
    rows = {row.file_name: row for row in intake.ledger(database)}
    assert rows["000001.png"].state is IntakeState.REGISTERED
    assert rows["000002.png"].state is IntakeState.REGISTERED
    assert rows["000003.png"].state is IntakeState.DUPLICATE_CONTENT
    with database.session() as session:
        statuses = dict(session.execute(select(BatchScan.filename, BatchScan.status)).all())
    assert statuses["000003.png"] == "duplicate"
    assert {statuses["000001.png"], statuses["000002.png"]} <= {"completed", "warning"}
    assert {item: item.read_bytes() for item in folder.iterdir()} == before  # untouched
    page.shutdown_batch()
    page.on_project_changed(None)
