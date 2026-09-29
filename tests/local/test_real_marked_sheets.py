"""Real scanned candidate scripts, read through the solution-sheet path.

Scope:
    Four real scans (``Scratch/Project1/raw``, OMR-Scan template) that are
    candidate scripts, not solution sheets. They are used because they are
    the only real scanner-produced sheets with genuine blank and double marks:
    the answer-key reader reads marks, and does not care who made them.

    ``Scratch/`` is git-ignored and never committed, so these skip elsewhere.

Ground truth:
    Visual transcriptions of the rectified pages, made from crops without the
    engine's overlay (2026-09-29). ``_`` is a question with no mark, ``?`` a
    question with more than one mark. Not an examiner's key.

The assertion that matters most is **no false confident read**: a question
the engine reports as one clear answer must be that answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.services import load_template
from omr_scanner.services.answer_key import plan_for
from omr_scanner.services.solution_sheet import read_solution_sheet

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPOSITORY_ROOT / "Scratch" / "Project1"
TEMPLATE = PROJECT / "templates" / "OMR-Scan.omrt"

TRANSCRIBED = {
    "C3--01268": "D___DADBDA__ACDDCBAD" "BBBABBCAC_CCDAB_D___" "BABBADBAADAC?___B__B"
    "__B___B___BBBCCB_BB_" "_DABBACBBCBB_D_____C",
    "C8--03369": "BBCAABCBCBBACCBB_CCB" "ABCACABDCADCBDCBDADC" "DBBDACDDDCCC__B_BC_D"
    "B_BCABBDBCB_BB____BB" "BBDBCBBABBBACBBCBBCC",
    "C1--00023": "____B__DBA_B_BCBBBBA" "CCCBB_D_CBAC_BBCBCBD" "CADCBBBDAC_BDABCDDDB"
    "CCDCB_BACCBB_CD_ADC_" "BBB_ABDBABCBBB_CCBAB",
    "C5--02293": "DADCDACBCAACCCAACBBD" "BAACCADBBCBDACCBBBCC" "DCCCDADCADBAADBCACAA"
    "ACBDDDDACCDCDCCCCDDA" "CDBDCABCDACCCCDDDADC",
}
READ_SET = {"C3--01268": "11", "C8--03369": "16", "C1--00023": "12", "C5--02293": "13"}

pytestmark = pytest.mark.skipif(
    not (TEMPLATE.is_file() and all((PROJECT / "raw" / f"{n}.png").is_file() for n in TRANSCRIBED)),
    reason="SKIPPED - local real-world fixture unavailable (Scratch/Project1)",
)


@pytest.fixture(scope="module")
def readings():
    template = load_template(TEMPLATE)
    plan = plan_for(template)
    return {
        name: read_solution_sheet(
            PROJECT / "raw" / f"{name}.png", template, plan, selected_set=READ_SET[name]
        )
        for name in TRANSCRIBED
    }


@pytest.mark.parametrize("name", sorted(TRANSCRIBED))
def test_no_false_confident_read(readings, name):
    read = readings[name].scanned.answers
    truth = TRANSCRIBED[name]
    wrong = [
        (number, truth[number - 1], read[number - 1])
        for number in range(1, 101)
        if read[number - 1] not in "_?" and read[number - 1] != truth[number - 1]
    ]
    assert wrong == []


@pytest.mark.parametrize("name", sorted(TRANSCRIBED))
def test_every_question_matches_including_blanks_and_multiples(readings, name):
    assert readings[name].scanned.answers == TRANSCRIBED[name]


def test_the_double_mark_is_named(readings):
    reading = readings["C3--01268"]
    assert reading.scanned.multiples == (53,)
    by_number = {item.number: item for item in reading.scanned.readings}
    assert by_number[53].describe == "Multiple marks B + C"


@pytest.mark.parametrize("name", sorted(TRANSCRIBED))
def test_the_set_field_reads_as_printed(readings, name):
    assert readings[name].scanned.set_code == READ_SET[name]
