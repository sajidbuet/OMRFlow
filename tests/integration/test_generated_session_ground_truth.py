"""A generated multi-set session over several batches matches ground truth (ACCEPTANCE C10).

The stress generator renders a deterministic cohort - clean sheets, sheets of
several sets, blank answers, a duplicate roll on a different image, exact
duplicate images - with the ground truth it was drawn from. The same files are
read by the real recognition engine twice:

* **one batch** of one scan session;
* **three batches** of one scan session;

each with a sheet rejected and its rescan (re-encoded: other bytes, same
pixels) read later and confirmed. Both runs must agree sheet by sheet -
disposition, effective Student ID, effective answers and duplicate-ID groups -
and must match the generator's own ground truth where it is unambiguous.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np
import pytest
from sqlalchemy import select
from tests.conftest import build_answer_sheet_template

from omr_scanner.database.models import BatchStatus, ReviewConflict
from omr_scanner.domain.review import ConflictState, ConflictType
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.session_population import SheetDisposition
from omr_scanner.evaluation import stress_dataset
from omr_scanner.evaluation.stress_dataset import StressCaseKind, StressDatasetSpec
from omr_scanner.services import (
    batch_store,
    create_project,
    scan_lifecycle,
    scan_provenance,
    scan_recovery,
    scan_sessions,
    session_population,
)
from omr_scanner.services.batch_processor import BatchOptions, process_batch

if TYPE_CHECKING:
    pass

OPERATOR = "Ground Truth"
COUNT = 36
SPEC = StressDatasetSpec(
    seed=20261002,
    sheet_count=COUNT,
    distribution={
        StressCaseKind.CLEAN: 0.55,
        StressCaseKind.MULTIPLE_SETS: 0.12,
        StressCaseKind.NO_ANSWER: 0.08,
        StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE: 0.12,
        StressCaseKind.EXACT_DUPLICATE_SCAN: 0.13,
    },
)


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template(roll_digits=7)


@pytest.fixture(scope="module")
def cohort(template, tmp_path_factory):
    folder = tmp_path_factory.mktemp("cohort")
    sheets = {}
    for index in range(COUNT):
        rendered = stress_dataset.render_sheet_for_index(SPEC, template, index)
        path = folder / f"sheet_{index:03d}.png"
        path.write_bytes(rendered.png_bytes or rendered.malformed_bytes or b"")
        sheets[path.name] = (path, rendered.case, SPEC.kind_for_index(index))
    # The rescan of the rejected sheet: the same pixels, other bytes.
    rejected = next(name for name, (_p, _c, kind) in sheets.items() if kind is StressCaseKind.CLEAN)
    image = cv2.imdecode(np.frombuffer(sheets[rejected][0].read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    rescan = folder / "rescan.png"
    cv2.imwrite(str(rescan), image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    kinds = {kind for _p, _c, kind in sheets.values()}
    assert {
        StressCaseKind.EXACT_DUPLICATE_SCAN,
        StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE,
        StressCaseKind.MULTIPLE_SETS,
    } <= kinds, kinds
    return sheets, rejected, rescan


def _read(database, template, paths, *, batch_id: str | None = None) -> str:
    """One run, as the Scan stage does it: register, hash, link duplicates, read, record."""
    identity = batch_store.BatchIdentity.of(template)
    if batch_id is None:
        batch_id = scan_sessions.start_batch(database, paths, identity=identity)
    else:
        batch_store.add_scans_to_batch(database, batch_id, paths)
    scan_provenance.compute_hashes_for_batch(database, batch_id)
    skipped = {
        str(item.path) for item in scan_lifecycle.link_exact_duplicates(database, batch_id, paths)
    }
    todo = [path for path in paths if str(path) not in skipped]
    batch_store.mark_queued(database, batch_id, todo)
    batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id, template=template)
    process_batch(todo, template, options=BatchOptions(), on_result=recorder.record, workers=1)
    recorder.flush()
    scan_recovery.complete_batch_review_state(database, batch_id)
    batch_store.finalise_batch(database, batch_id)
    return batch_id


def _run(workspace, template, cohort, groups: int) -> dict[str, object]:
    sheets, rejected, rescan = cohort
    session = create_project(workspace, f"Ground truth {groups}")
    database = session.database
    try:
        names = sorted(sheets)
        paths = [sheets[name][0] for name in names]
        size = -(-len(paths) // groups)
        chunks = [paths[start : start + size] for start in range(0, len(paths), size)]
        batch = None
        for number, chunk in enumerate(chunks):
            batch = _read(database, template, chunk)
            if number == 0:
                ids = batch_store.scan_ids_by_path(database, batch)
                scan_lifecycle.reject_scan(
                    database, ids[sheets[rejected][0]], reviewer=OPERATOR,
                    reason=RejectionReason.FOLDED,
                )
        assert batch is not None
        if groups == 1:
            _read(database, template, [rescan], batch_id=batch)
        else:
            _read(database, template, [rescan])
        active = scan_sessions.active_scan_session(database)
        assert active is not None
        by_path = {}
        for info in scan_sessions.batches_of(database, active.scan_session_id):
            by_path.update(batch_store.scan_ids_by_path(database, info.batch_id))
        name_of = {scan_id: path.name for path, scan_id in by_path.items()}
        scan_lifecycle.confirm_replacement(
            database,
            next(sid for sid, name in name_of.items() if name == rejected),
            next(sid for sid, name in name_of.items() if name == "rescan.png"),
            reviewer=OPERATOR,
        )
        population = session_population.session_population(database, active.scan_session_id)
        identifiers = session_population.effective_identifiers(database, population)
        decided = session_population.effective_answers(database, population, template)
        results = session_population.results_by_scan(database, population)
        answers = {
            scan: {
                **{item.number: item.value for item in result.answers},
                **(dict(decided[scan].decided) if scan in decided else {}),
            }
            for scan, result in results.items()
        }
        with database.session() as db:
            duplicates = {
                name_of[int(scan)]: frozenset(
                    name_of[int(item)] for item in str(related).split(",") if item
                )
                for scan, related in db.execute(
                    select(ReviewConflict.scan_id, ReviewConflict.related_scan_ids)
                    .where(ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value)
                    .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
                ).all()
                if int(scan) in population.effective
            }
        return {
            "dispositions": {
                name_of[scan]: kind.value for scan, kind in population.dispositions.items()
            },
            "ids": {name_of[scan]: item.value for scan, item in identifiers.items()},
            "answers": {name_of[scan]: item for scan, item in answers.items()},
            "duplicates": duplicates,
            "batches": len(population.batch_ids),
        }
    finally:
        session.close()


@pytest.fixture(scope="module")
def runs(template, cohort, tmp_path_factory):
    one = _run(tmp_path_factory.mktemp("one"), template, cohort, 1)
    three = _run(tmp_path_factory.mktemp("three"), template, cohort, 3)
    return one, three


def test_one_batch_and_three_batches_agree_sheet_by_sheet(runs):
    one, three = runs
    assert one["batches"] == 1 and three["batches"] == 4  # three runs + the rescan's
    for key in ("dispositions", "ids", "answers", "duplicates"):
        assert one[key] == three[key], key


def test_the_session_matches_the_generators_ground_truth(runs, cohort):
    sheets, rejected, _rescan = cohort
    _one, three = runs
    dispositions, ids, answers = three["dispositions"], three["ids"], three["answers"]
    assert dispositions[rejected] == SheetDisposition.SUPERSEDED_BY_REPLACEMENT.value
    assert dispositions["rescan.png"] == SheetDisposition.EFFECTIVE.value
    assert ids["rescan.png"] == sheets[rejected][1].roll
    # The generator makes an "exact duplicate" byte-identical to the *clean*
    # rendering of the previous index, which is on disk only when that index
    # was itself drawn clean. The bytes on disk are the ground truth.
    seen: set[bytes] = set()
    repeats: set[str] = set()
    for name in sorted(sheets):
        content = sheets[name][0].read_bytes()
        if content in seen:
            repeats.add(name)
        seen.add(content)
    assert repeats, "the cohort has no byte-identical repeat"
    assert repeats <= {
        name for name, (_p, _c, kind) in sheets.items()
        if kind is StressCaseKind.EXACT_DUPLICATE_SCAN
    }
    for name, (_path, case, kind) in sheets.items():
        if name in repeats:
            # Byte-identical to an earlier sheet of the session: linked, never read.
            assert dispositions[name] == SheetDisposition.EXACT_DUPLICATE.value, name
            continue
        if name == rejected:
            continue
        assert dispositions[name] == SheetDisposition.EFFECTIVE.value, name
        assert ids[name] == case.roll, name
        if kind in (StressCaseKind.CLEAN, StressCaseKind.MULTIPLE_SETS):
            # Every question the generator marked reads back as marked.
            assert {
                number: answers[name].get(number) for number in case.answers
            } == case.answers, name
    # Every duplicate roll on a different image is in a group with the sheets
    # that really share its Student ID - and only those.
    for name, (_path, case, kind) in sheets.items():
        if kind is not StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE:
            continue
        sharing = {
            other
            for other, value in ids.items()
            if value == case.roll and other != name
            and dispositions[other] == SheetDisposition.EFFECTIVE.value
        }
        if sharing:
            assert three["duplicates"][name] == frozenset(sharing), name
