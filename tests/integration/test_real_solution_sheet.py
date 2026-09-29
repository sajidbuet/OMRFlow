"""The committed real solution sheet, read as an answer key.

Scope:
    ``Sample-Project/1.Template/ECE-0000.png`` is a real scanned OMR sheet
    (2480x3508, ~300 dpi) filled in pen as a solution sheet, with its template
    ``BUET100q.omrt``. It is read through the production solution-sheet path
    (:func:`~omr_scanner.services.solution_sheet.read_solution_sheet`) and
    compared question by question with ground truth.

Where the ground truth comes from:
    A visual transcription of the rectified page, made from crops *without*
    the engine's overlay, on 2026-09-29. It is not an examiner's answer key -
    none exists for this sheet - and is labelled as a transcription for that
    reason. The sheet's marks follow an obvious pattern, which makes the
    transcription easy to check by eye.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.services import load_template
from omr_scanner.services.answer_key import SetCodeCheck, plan_for, read_key
from omr_scanner.services.solution_sheet import read_solution_sheet

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPOSITORY_ROOT / "Sample-Project" / "1.Template"
SHEET = SAMPLE / "ECE-0000.png"
TEMPLATE = SAMPLE / "BUET100q.omrt"

_PATTERN = "ABCDABCDABCDABCDABCD"
TRANSCRIBED_KEY = (
    "AAAABBBBCCCCDDDDABCD" + _PATTERN * 3 + "ABCDABCDABCDAAABBBBB"
)
"""Visual transcription of ECE-0000 (see module docstring). Q89 is a wide mark
over A spilling towards B; Q96-98 are B marks sitting off-centre."""

pytestmark = pytest.mark.skipif(
    not (SHEET.is_file() and TEMPLATE.is_file()),
    reason="committed sample sheet not present in this checkout",
)


@pytest.fixture(scope="module")
def reading():
    template = load_template(TEMPLATE)
    return read_solution_sheet(
        SHEET, template, plan_for(template), selected_set="10", defined_sets=("10", "11")
    )


def test_it_registers(reading):
    assert reading.registered


def test_every_answer_matches_the_transcription(reading):
    read = reading.scanned.answers
    mismatches = [
        (number, TRANSCRIBED_KEY[number - 1], read[number - 1])
        for number in range(1, 101)
        if read[number - 1] != TRANSCRIBED_KEY[number - 1]
    ]
    assert mismatches == []


def test_nothing_is_flagged_that_is_not_ambiguous(reading):
    assert reading.scanned.blanks == ()
    assert reading.scanned.multiples == ()
    assert reading.scanned.is_clean


def test_the_set_field_reads_ten(reading):
    assert reading.scanned.set_code == "10"
    assert reading.verdict.check is SetCodeCheck.MATCH


def test_it_is_a_complete_valid_key(reading):
    draft = read_key(reading.scanned.answers, reading.scanned.plan, "10")
    assert draft.is_valid
