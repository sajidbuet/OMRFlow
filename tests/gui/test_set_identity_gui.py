"""Set identity in the real widgets (phase 0.1.1-A).

* Project Configuration -> Sets: *Printed on sheet as*, refusal of an
  ambiguous or unprintable mark, and how a legacy ``A`` / ``a`` collision is
  shown and resolved.
* Resolve: a sheet whose mark is a set's physical mark is described as
  *Set 10 (A on sheet)*, and the full set-code editor accepts the logical
  code ``10`` while recording the paper's ``A``.
* The Reject dialog offers mapped sets by their logical code.

Driven through the methods the buttons call (never through a modal), with a
real project, a real recognition engine and the database as the oracle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database.models import ProjectSet
from omr_scanner.domain.review import ConflictType, FieldKind, ReasonCode
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.project_config_dialog import MARK_COLUMN, NO_MARK_TEXT, ProjectConfigDialog
from omr_scanner.gui.review.page import ResolvePage
from omr_scanner.gui.review.rescan import RejectScanDialog
from omr_scanner.services import batch_store, create_project, project_sets, review_store
from omr_scanner.services.batch_processor import process_batch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

SHEET_TIMEOUT_MS = 120_000
REVIEWER = "Set Identity Reviewer"


# ----------------------------------------------------------------------
# Project Configuration
# ----------------------------------------------------------------------
@pytest.fixture
def session(workspace: Path):
    created = create_project(workspace, "Set Identity GUI")
    yield created
    if not created.is_closed:
        created.close()


@pytest.fixture
def dialog(qtbot, session: ProjectSession) -> ProjectConfigDialog:
    box = ProjectConfigDialog(session)
    # The project has no template file; give the dialog the A-D set field the
    # printability check is made against, as a project with one would.
    box._template = build_answer_sheet_template()
    qtbot.addWidget(box)
    return box


def _marks(box: ProjectConfigDialog) -> list[str]:
    return [
        box.sets_table.item(row, MARK_COLUMN).text() for row in range(box.sets_table.rowCount())
    ]


class TestPrintedOnSheetAs:
    def test_a_set_can_be_printed_as_another_mark(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        assert dialog.add_set("10", "Electrical", "A") is True
        assert dialog.add_set("11", "Civil") is True
        assert _marks(dialog) == ["A", NO_MARK_TEXT]
        sets = project_sets.list_sets(session.database)
        stored = {item.code: item.physical_mark for item in sets}
        assert stored == {"10": "A", "11": ""}

    def test_two_sets_printed_alike_are_refused(self, dialog: ProjectConfigDialog) -> None:
        assert dialog.add_set("10", "", "A") is True
        assert dialog.add_set("11", "", "a") is False
        assert "Set 10 (A on sheet)" in dialog.sets_status_label.text()
        assert [item.code for item in dialog.sets] == ["10"]

    def test_a_code_that_is_another_sets_mark_is_refused(
        self, dialog: ProjectConfigDialog
    ) -> None:
        assert dialog.add_set("10", "", "A") is True
        assert dialog.add_set("A") is False
        assert "already exists" in dialog.sets_status_label.text()

    def test_a_mark_the_template_cannot_print_is_refused(
        self, dialog: ProjectConfigDialog
    ) -> None:
        assert dialog.add_set("10", "", "Z") is False
        assert "cannot print 'Z'" in dialog.sets_status_label.text()

    def test_lower_case_beside_upper_case_is_refused(self, dialog: ProjectConfigDialog) -> None:
        assert dialog.add_set("A") is True
        assert dialog.add_set("a") is False
        assert "without regard to case" in dialog.sets_status_label.text()

    def test_editing_and_clearing_a_mark(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        assert dialog.add_set("10", "", "A") is True
        (ten,) = dialog.sets
        assert dialog.edit_set(ten.set_id, code="10", description="", physical_mark="B") is True
        assert _marks(dialog) == ["B"]
        assert dialog.edit_set(ten.set_id, code="10", description="", physical_mark="") is True
        assert _marks(dialog) == [NO_MARK_TEXT]
        assert project_sets.list_sets(session.database)[0].physical_mark == ""

    def test_an_existing_project_without_marks_looks_as_before(
        self, qtbot, session: ProjectSession
    ) -> None:
        project_sets.add_set(session.database, "10", "Electrical")
        box = ProjectConfigDialog(session)
        qtbot.addWidget(box)
        assert box.sets_table.item(0, 0).text() == "10"
        assert box.sets_table.item(0, 1).text() == "Electrical"
        assert _marks(box) == [NO_MARK_TEXT]
        assert not box.collision_label.isVisible()


class TestCollisionPresentation:
    @pytest.fixture
    def colliding(self, session: ProjectSession) -> ProjectSession:
        """A legacy pair an earlier build allowed, written as that build wrote it."""
        project_sets.add_set(session.database, "A", "Upper")
        moment = datetime.now(UTC)
        with session.database.session() as orm:
            orm.add(
                ProjectSet(
                    set_id="legacy-lower-a", code="a", description="Lower",
                    display_order=1, canonical_code=None, physical_mark="",
                    created_at=moment, updated_at=moment,
                )
            )
        return session

    def test_the_collision_is_named_and_the_rows_marked(
        self, qtbot, colliding: ProjectSession
    ) -> None:
        box = ProjectConfigDialog(colliding)
        qtbot.addWidget(box)
        box.show()
        assert box.collision_label.isVisible()
        assert "'A' and 'a'" in box.collision_label.text()
        assert "Rename or remove one" in box.collision_label.text()
        assert len(box.collisions) == 1
        assert "same set code" in box.sets_table.item(1, 0).toolTip()

    def test_renaming_one_resolves_it(self, qtbot, colliding: ProjectSession) -> None:
        box = ProjectConfigDialog(colliding)
        qtbot.addWidget(box)
        box.show()
        assert box.edit_set("legacy-lower-a", code="C", description="Lower") is True
        assert not box.collision_label.isVisible()
        assert box.collisions == ()

    def test_editing_only_the_description_of_a_colliding_set_is_allowed(
        self, qtbot, colliding: ProjectSession
    ) -> None:
        box = ProjectConfigDialog(colliding)
        qtbot.addWidget(box)
        assert box.edit_set("legacy-lower-a", code="a", description="Renamed later") is True
        assert len(box.collisions) == 1


# ----------------------------------------------------------------------
# Resolve
# ----------------------------------------------------------------------
def _sheet_marks(roll: str, set_symbol: str) -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: set_symbol},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }


@pytest.fixture
def mapped_page(qtbot, project_session: ProjectSession, tmp_path: Path):
    """Sets 10 -> A and 11 -> B; one sheet marked D (no set), one marked A."""
    template = build_answer_sheet_template()
    database = project_session.database
    project_sets.add_set(database, "10", "Electrical", physical_mark="A", template=template)
    project_sets.add_set(database, "11", "Civil", physical_mark="B", template=template)
    scans = tmp_path / "mapped"
    scans.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (roll, symbol) in enumerate((("170501", "D"), ("170502", "A")), start=1):
        path = scans / f"SYN_{index:06d}.png"
        cv2.imwrite(str(path), render_marked_sheet(template, _sheet_marks(roll, symbol)))
        paths.append(path)
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)
    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database, batch_id=batch_id, scan_id=ids[item.source_path],
            result=item.result, template=template,
        )
    review_store.sync_undefined_set_codes(database, batch_id)
    spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
    page = ResolvePage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(REVIEWER)
    assert page.load_batch(batch_id, template) is True
    yield page, batch_id, ids
    page.close()


def _select_undefined(qtbot, page: ResolvePage) -> None:
    for row, item in enumerate(page.state.conflicts):
        if item.conflict_type is ConflictType.SET_CODE_UNDEFINED:
            with qtbot.waitSignal(page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
                page.queue_table.selectRow(row)
            return
    raise AssertionError("no undefined-set conflict in the queue")


class TestResolveMappedSets:
    def test_only_the_sheet_marked_d_is_an_undefined_set(self, mapped_page) -> None:
        page, batch_id, _ids = mapped_page
        undefined = [
            item for item in review_store.list_conflicts(page.database, batch_id)
            if item.conflict_type is ConflictType.SET_CODE_UNDEFINED
        ]
        assert len(undefined) == 1
        assert "10 (printed A)" in undefined[0].observation.detail
        effective = review_store.effective_set_codes(page.database, batch_id)
        assert sorted((item.value, item.as_read) for item in effective.values()) == [
            ("10", "A"),
            ("D", "D"),
        ]

    def test_a_mark_is_described_as_its_logical_set(self, mapped_page) -> None:
        page, _batch_id, _ids = mapped_page
        assert page.describe_set_reading("A") == "Set 10 (A on sheet)"
        assert page.describe_set_reading("b") == "Set 11 (b on sheet)"
        assert page.describe_set_reading("D") == ""

    def test_typing_the_logical_code_records_the_physical_mark(
        self, qtbot, mapped_page
    ) -> None:
        page, batch_id, _ids = mapped_page
        _select_undefined(qtbot, page)
        page.reason_combo.setCurrentText(ReasonCode.MISCLASSIFICATION.label)
        assert page.open_field_editor(FieldKind.SET_CODE) is True
        page.field_edit_input.setText("10")
        status = page.field_edit_status.text()
        assert "Set 10 (A on sheet)" in status, status
        assert page.apply_field_edit() is True
        effective = review_store.effective_set_codes(page.database, batch_id)
        corrected = [item for item in effective.values() if item.machine_value == "D"]
        # The ledger keeps what the paper now says (A); downstream sees Set 10.
        assert [(item.value, item.as_read) for item in corrected] == [("10", "A")]

    def test_the_evidence_names_the_logical_set(self, qtbot, mapped_page) -> None:
        page, _batch_id, _ids = mapped_page
        _select_undefined(qtbot, page)
        conflict = page.current_conflict()
        assert conflict is not None
        assert "Reads as" not in page.evidence_label.text()  # "D" names no set
        html = page._set_reading_html("A")
        assert "Set 10 (A on sheet)" in html


class TestRejectDialog:
    def test_mapped_sets_are_offered_by_label_and_chosen_by_logical_code(self, qtbot) -> None:
        dialog = RejectScanDialog(
            "SYN_000002.png", "170502", "10", ["10", "11"],
            set_labels={"10": "Set 10 (A on sheet)", "11": "Set 11 (B on sheet)"},
        )
        qtbot.addWidget(dialog)
        texts = [dialog.set_combo.itemText(index) for index in range(dialog.set_combo.count())]
        assert texts[1:] == ["Set 10 (A on sheet)", "Set 11 (B on sheet)"]
        assert dialog.set_combo.currentData() == "10"

    def test_the_recognised_set_is_matched_canonically(self, qtbot) -> None:
        dialog = RejectScanDialog("x.png", "1", "a", ["A", "B"])
        qtbot.addWidget(dialog)
        assert dialog.set_combo.currentData() == "A"
