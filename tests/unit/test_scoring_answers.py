"""What an ambiguous answer is worth, once it is no longer a conflict.

Scope:
    :func:`omr_scanner.services.scoring.build_candidate_answers` and the guard
    beneath it, :func:`~omr_scanner.services.scoring._scorable_answer`.

Why this exists:
    Removing answers from the conflict system removed the thing that used to
    stop an undecided reading being marked: the scoring block. An ``UNCERTAIN``
    group with one faint mark still *has* a value - the engine's ``"B"`` - and
    marking that as a clean ``B`` would award or deny credit on evidence the
    engine itself refused to stand behind. Calling it blank would be the
    opposite mistake, quietly awarding the blank mark for a question the
    candidate may well have answered.

    These tests pin the third option, which is the one the export and the
    on-screen value have always shown: it is not one determinable answer, so it
    is marked as a multiple.
"""

from __future__ import annotations

import pytest
from tests.conftest import build_answer_sheet_template
from tests.unit.test_recognition_contract import make_answer, make_result

from omr_scanner.domain.scoring import BLANK, MULTIPLE
from omr_scanner.services.answer_key import plan_for
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
)
from omr_scanner.services.scoring import build_candidate_answers


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def plan(template):
    return plan_for(template)


def result_with(answers):
    """A registered result carrying exactly these answers."""
    return make_result(
        outcome=RecognitionOutcome.COMPLETE,
        registration=RegistrationStatus.REGISTERED,
        warnings=(),
        fields=(),
        answers=tuple(answers),
        bubbles=(),
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )


def first_character(result, plan) -> str:
    """The canonical answer string's first character."""
    return build_candidate_answers(result, plan).answers[0]


class TestADecidedAnswerIsUnchanged:
    def test_a_clean_single_mark_scores_as_itself(self, plan):
        result = result_with([make_answer(1, "B", "resolved", needs_review=False)])
        assert first_character(result, plan) == "B"

    def test_a_blank_stays_blank(self, plan):
        result = result_with([make_answer(1, "", "blank", needs_review=False)])
        assert first_character(result, plan) == BLANK

    def test_a_confirmed_double_mark_is_a_multiple(self, plan):
        result = result_with([make_answer(1, "B-D", "multiple")])
        assert first_character(result, plan) == MULTIPLE

    def test_a_resolved_but_low_confidence_mark_is_still_that_mark(self, plan):
        # The engine decided this one: exactly one bubble crossed the fill
        # threshold, with the separation the template demands. It fell below
        # `min_confidence`, which is a reason to have flagged it - not a reason
        # to disbelieve which bubble it was. The CSV shows `b` for it too.
        result = result_with(
            [make_answer(1, "B", "resolved", needs_review=True, confidence=0.4)]
        )
        assert first_character(result, plan) == "B"


class TestAnUndecidedAnswerIsNeverMarkedAsOne:
    def test_an_uncertain_mark_is_not_scored_as_the_option_it_nearly_said(self, plan):
        # The regression this guards. `value` is "B" - one bubble was darkest,
        # but not clearly enough for the engine to call it.
        result = result_with([make_answer(1, "B", "uncertain", needs_review=True)])
        assert first_character(result, plan) == MULTIPLE

    def test_an_uncertain_mark_is_not_scored_as_a_blank_either(self, plan):
        result = result_with([make_answer(1, "", "uncertain", needs_review=True)])
        assert first_character(result, plan) == MULTIPLE

    def test_an_unreadable_group_is_not_scored_as_a_blank(self, plan):
        result = result_with([make_answer(1, "", "unreadable", needs_review=True)])
        assert first_character(result, plan) == MULTIPLE

    def test_it_is_not_reported_as_a_value_the_template_cannot_name(self, plan):
        # `?` is this module's own symbol. Reporting it as unnameable would
        # block the candidate for a value scoring itself produced.
        result = result_with([make_answer(1, "B", "uncertain", needs_review=True)])
        assert build_candidate_answers(result, plan).unnameable == ()


class TestTheRecognitionResultIsUntouched:
    def test_the_machine_string_carries_the_same_projection(self, plan):
        result = result_with([make_answer(1, "B", "uncertain", needs_review=True)])
        found = build_candidate_answers(result, plan)
        assert found.machine_answers[0] == MULTIPLE

    def test_the_result_object_itself_is_not_modified(self, plan):
        answer = make_answer(1, "B", "uncertain", needs_review=True)
        result = result_with([answer])
        build_candidate_answers(result, plan)
        assert result.answers[0].value == "B"
        assert result.answers[0].status == "uncertain"

    def test_a_reviewers_decision_still_wins(self, plan):
        # A decision recorded before answers stopped being conflicts is still
        # applied; it is not overridden by the projection.
        result = result_with([make_answer(1, "B", "uncertain", needs_review=True)])
        found = build_candidate_answers(result, plan, decided={1: "C"})
        assert found.answers[0] == "C"
        assert found.machine_answers[0] == MULTIPLE
        assert found.corrected == (1,)


class TestNothingBlocksOnAnAmbiguousAnswer:
    def test_unresolved_is_empty_when_nothing_is_passed(self, plan):
        result = result_with(
            [make_answer(n, "A-C", "multiple") for n in range(1, 11)]
        )
        assert build_candidate_answers(result, plan).unresolved == ()
