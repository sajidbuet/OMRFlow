"""Which scan session intake files belong to (0.1.1 revised phase 5).

Sources are project-level and attached to one session at a time. A file is
intended for the session its source served when it was **first observed**;
re-attaching never moves a row, a registered row never changes session, a
closed session cannot be attached, and nothing reopens a session or makes a new
one.
"""

from __future__ import annotations

import pytest
from tests.intake_fakes import jpeg
from tests.integration.test_intake_ledger import ROOT_A, Rig

from omr_scanner.domain.intake import IntakeState
from omr_scanner.services import intake as intake_service
from omr_scanner.services import scan_sessions
from omr_scanner.services.intake import IntakeError


@pytest.fixture
def rig(project_session, answer_sheet_template):
    return Rig(project_session, answer_sheet_template)


def test_an_unattached_source_records_but_offers_nothing(rig):
    a = rig.source(ROOT_A, attach=False)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.settle(a)
    row = rig.row(a, "1.jpg")
    assert row.state is IntakeState.READY and row.scan_session_id is None
    assert rig.service.ready_items(scan_session_id=rig.session_id) == ()
    intake_service.attach_source(rig.database, a, rig.session_id, actor="op")
    assert rig.row(a, "1.jpg").scan_session_id == rig.session_id
    assert len(rig.service.ready_items(scan_session_id=rig.session_id)) == 1


def test_reattaching_moves_nothing_already_intended(rig):
    a = rig.source(ROOT_A)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.poll(a)  # observed for the first session
    other = scan_sessions.create_scan_session(rig.database, name="Resit").scan_session_id
    intake_service.attach_source(rig.database, a, other, actor="op")
    rig.fs.write(ROOT_A, "2.jpg", jpeg(2))
    rig.settle(a)
    assert rig.row(a, "1.jpg").scan_session_id == rig.session_id
    assert rig.row(a, "2.jpg").scan_session_id == other
    assert [i.relative_path for i in rig.service.ready_items(scan_session_id=other)] == ["2.jpg"]
    history = intake_service.source_sessions(rig.database, a)
    assert [owner for owner, _start, _end in history] == [rig.session_id, other]
    assert history[0][2] is not None and history[1][2] is None


def test_a_registered_row_never_changes_session(rig):
    a = rig.source(ROOT_A)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.settle(a)
    rig.register_all(a)
    other = scan_sessions.create_scan_session(rig.database, name="Resit").scan_session_id
    intake_service.attach_source(rig.database, a, other, actor="op")
    rig.poll(a, advance=10)
    row = rig.row(a, "1.jpg")
    assert row.state is IntakeState.REGISTERED and row.scan_session_id == rig.session_id
    with pytest.raises(IntakeError):
        rig.service.register(
            scan_session_id=other, source_id=a, intake_file_ids=[row.intake_file_id],
            identity=rig.identity,
        )


def test_a_closed_session_cannot_be_attached(rig):
    a = rig.source(ROOT_A, attach=False)
    closed = scan_sessions.create_scan_session(rig.database, name="Old").scan_session_id
    scan_sessions.close_scan_session(rig.database, closed, closed_by="op")
    with pytest.raises(IntakeError):
        intake_service.attach_source(rig.database, a, closed, actor="op")
    assert intake_service.get_source(rig.database, a).attached_session_id is None


def test_rows_for_a_session_closed_later_are_held_and_nothing_is_reopened(rig):
    a = rig.source(ROOT_A)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.poll(a)
    scan_sessions.close_scan_session(rig.database, rig.session_id, closed_by="op")
    rig.fs.write(ROOT_A, "2.jpg", jpeg(2))  # still intended for the closed session
    rig.settle(a)
    rig.poll(a, advance=10)
    assert {rig.state(a, name) for name in ("1.jpg", "2.jpg")} == {IntakeState.HELD}
    sessions = scan_sessions.list_scan_sessions(rig.database)
    assert [(s.scan_session_id, s.state.value) for s in sessions] == [(rig.session_id, "closed")]


def test_detaching_leaves_rows_where_they_are(rig):
    a = rig.source(ROOT_A)
    rig.fs.write(ROOT_A, "1.jpg", jpeg(1))
    rig.poll(a)
    intake_service.detach_source(rig.database, a, actor="op")
    rig.fs.write(ROOT_A, "2.jpg", jpeg(2))
    rig.settle(a)
    assert rig.row(a, "1.jpg").scan_session_id == rig.session_id
    assert rig.row(a, "2.jpg").scan_session_id is None


def test_the_manual_source_is_never_attached(rig):
    manual = intake_service.manual_source(rig.database)
    with pytest.raises(IntakeError):
        intake_service.attach_source(rig.database, manual.source_id, rig.session_id)
    with pytest.raises(IntakeError):
        rig.service.reconcile(manual.source_id)
