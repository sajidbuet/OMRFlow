"""The answer key of a synthetic examination, and the solution sheets it implies.

Purpose:
    Decide, once per question-paper set, what the correct answers are - and
    derive from that single decision everything that states them: the solution
    OMR sheet, the answer-key text file and the manifest. A synthetic
    examination without a key can test recognition but not scoring; with one,
    the whole path from scan to mark is testable end to end.

Responsibilities:
    * :class:`SyntheticAnswerKey` - the canonical key for one set.
    * :func:`generate_answer_keys` - one key per set, from the dataset seed.
    * :func:`serialise_answer_key` - the text file, in the format OMRFlow's own
      Answer Key stage reads (:func:`omr_scanner.services.answer_key.read_key`).
    * :func:`solution_case` - the solution sheet as an ordinary
      :class:`~omr_scanner.evaluation.test_cases.SheetCase`, so it is drawn by
      the same renderer as every candidate's script.
    * :func:`solution_file_names` / :func:`clear_stale_solutions` - where the
      files go, and making sure an earlier run's files do not survive into
      this one.

What does NOT belong here:
    * Drawing or encoding images (:mod:`omr_scanner.evaluation.synthetic_dataset`).
    * How well candidates do against the key
      (:mod:`omr_scanner.evaluation.performance`).

One key, three representations:
    ::

        SyntheticAnswerKey (one per set)
                │
                ├── solution OMR sheet   solution_case -> render_case
                ├── answer-key text      serialise_answer_key
                └── manifest / truth     describe

    Nothing downstream re-derives the answers; each representation reads the
    same object, so they cannot disagree.

The text format is not invented here:
    OMRFlow has no answer-key *file* format - keys are typed or pasted into the
    Answer Key stage and parsed by
    :func:`~omr_scanner.services.answer_key.read_key`, which takes one option
    label per question in question order and ignores whitespace, commas,
    semicolons and bars. The file written here is exactly that string on one
    line, so its contents can be pasted into the stage unchanged, and the test
    suite round-trips it through ``read_key``. The set code is not inside the
    file, because ``read_key`` would report it as a stray character; it is in
    the file name and the manifest.

Why each set has its own random stream:
    ``Random(f"{seed}:answer-key:{set_code}")``, never the case planner's
    generator. Drawing a key must not move any other decision the seed makes -
    who is absent, which sheets fold, what each candidate's identifier is - and
    a set's key must not change because another set was added beside it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omr_scanner.errors import OMRScannerError
from omr_scanner.evaluation.test_cases import SheetBuilder, SheetCase, TestCaseTag
from omr_scanner.recognition.models import BLANK_CHARACTER, UNRESOLVED_CHARACTER
from omr_scanner.services.answer_key import QuestionPlan, plan_for
from omr_scanner.services.filename_manager import sanitise_stem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.test_cases import FieldLayout

SOLUTION_DIRNAME = "solution"
"""The folder, at the dataset root, holding one solution sheet and one key per set."""

SOLUTION_ROLE = "solution"
"""What a solution sheet's ground truth and manifest entry say it is.

Recorded explicitly so no consumer has to infer it from a folder name: a
solution sheet is the examiner's key, never a candidate's script."""

SOLUTION_IMAGE_SUFFIX = "_Solution"
ANSWER_KEY_SUFFIX = "_Answer_Key"
SOLUTION_TRUTH_SUFFIX = "_Solution"

UNSET_STEM = "Unset"
"""File-name stand-in for the one key of a paper that has no set code.

Only reached when the template has no set-code field and no roster names a
set: the examination then has exactly one paper, and its files still need a
name."""


@dataclass(frozen=True, slots=True)
class SyntheticAnswerKey:
    """The correct answers to one question-paper set.

    Attributes:
        set_code: The set this key answers, exactly as the roster and the sheet
            spell it - ``"10"``, ``"A"`` and ``"X1"`` alike. ``""`` only for a
            single-paper examination with no set-code field.
        answers: The correct option for every question, keyed by printed
            question number, **in the template's own spelling** - the label a
            candidate's sheet is marked with, which is what the renderer and the
            ground truth use.
        plan: The template's questions and uppercased labels, as the Answer Key
            stage reads them.
    """

    set_code: str
    answers: dict[int, str]
    plan: QuestionPlan

    @property
    def key_string(self) -> str:
        """The key as OMRFlow stores it: one uppercased label per question."""
        return "".join(self.answers[number].strip().upper() for number in self.plan.numbers)

    def describe(self) -> dict[str, Any]:
        """The key as manifest data."""
        return {
            "set_code": self.set_code,
            "first_question": self.plan.first_question,
            "question_count": self.plan.question_count,
            "answers": self.key_string,
        }


def question_plan(template: OmrTemplate) -> QuestionPlan | None:
    """The template's questions, or ``None`` when it has none at all.

    Returns ``None`` for a template with no question regions - an identifier
    sheet has no key to generate, and that is not an error. Everything else the
    Answer Key stage would refuse is refused here too, and **before** anything is
    drawn: a key over non-contiguous numbering or disagreeing option labels
    would be a key OMRFlow itself cannot hold, so generating one would test
    nothing.

    Raises:
        ValueError: The question regions are malformed, or an option label is
            longer than one character (a key is one character per question).
    """
    from omr_scanner.domain.template import QuestionBlockFieldDefinition

    if not any(isinstance(zone.field, QuestionBlockFieldDefinition) for zone in template.zones):
        return None
    try:
        plan = plan_for(template)
    except OMRScannerError as exc:
        raise ValueError(
            f"Cannot generate an answer key for this template: {exc.user_message or exc}"
        ) from exc
    long_labels = [label for label in plan.labels if len(label) != 1]
    if long_labels:
        raise ValueError(
            "Cannot generate an answer key for this template: an answer key holds "
            "one character per question, and these options are longer: "
            + ", ".join(long_labels)
        )
    return plan


def generate_answer_keys(
    template: OmrTemplate,
    layout: FieldLayout,
    set_codes: Sequence[str],
    *,
    seed: int,
) -> dict[str, SyntheticAnswerKey]:
    """Return one independently drawn key per set, in ``set_codes`` order.

    Args:
        template: The template the examination is printed from.
        layout: Its field layout; the options each question offers come from
            here, never from an assumption of four.
        set_codes: The sets to key. Duplicates are keyed once.
        seed: The dataset's master seed.

    Returns:
        ``{set code: key}``, empty when the template has no questions.

    Raises:
        ValueError: The template's questions are malformed (see
            :func:`question_plan`), or a question offers no options.
    """
    plan = question_plan(template)
    if plan is None:
        return {}
    keys: dict[str, SyntheticAnswerKey] = {}
    for set_code in set_codes:
        if set_code in keys:
            continue
        rng = random.Random(f"{seed}:answer-key:{set_code}")
        answers: dict[int, str] = {}
        for number in plan.numbers:
            options = layout.labels_for(number)
            if not options:
                raise ValueError(f"Question {number} offers no options to key")
            answers[number] = rng.choice(list(options))
        keys[set_code] = SyntheticAnswerKey(set_code=set_code, answers=answers, plan=plan)
    return keys


def serialise_answer_key(key: SyntheticAnswerKey) -> str:
    """The answer-key text file's contents: the key string and a newline.

    Exactly what :func:`~omr_scanner.services.answer_key.read_key` accepts and
    what the Answer Key stage displays, so the file can be pasted straight into
    it. One line, one newline, no header - see the module docstring for why the
    set code is not inside the file.
    """
    return key.key_string + "\n"


def is_resolved_set(value: str) -> bool:
    """Whether a planned sheet's set code names one set.

    ``False`` for an empty value and for any containing the engine's blank or
    unresolved character - a set-code test case, not a paper.
    """
    return bool(value) and BLANK_CHARACTER not in value and UNRESOLVED_CHARACTER not in value


def planned_set_codes(cases: Iterable[SheetCase]) -> tuple[str, ...]:
    """The distinct sets a dataset without a roster actually prints.

    Without a roster the examination's sets are whatever the planned sheets
    carry, so those are the sets that get a key. Sorted, so the answer does not
    depend on case order.
    """
    return tuple(sorted({case.set_code for case in cases if is_resolved_set(case.set_code)}))


def set_code_markable(layout: FieldLayout, set_code: str) -> bool:
    """Whether ``set_code`` can be marked, exactly, on the template's set-code field.

    ``True`` for one whole-code bubble on a single-position field (``"10"``
    among symbols ``"10"``, ``"11"``) and for one symbol per position on a
    positional field (``"103"`` on three digit columns). ``False`` for anything
    the field cannot spell - ``"10"`` on a field offering ``A``-``D`` - and for
    every code when the template has no set-code field at all.

    Not a refusal. A roster's set codes and a template's set-code field are
    chosen separately, and existing datasets pair ``10, 11, 12`` with an
    ``A``-``D`` field; the candidate sheets have always carried a blank set code
    in that case, and the solution sheet does the same rather than drawing some
    *other* set. The manifest records which it was (``set_code_marked``).
    """
    if not layout.has_set_code or not set_code:
        return False
    single = layout.set_columns == 1 and set_code in layout.set_symbols
    positional = len(set_code) == layout.set_columns and all(
        character in layout.set_symbols for character in set_code
    )
    return single or positional


def solution_case(key: SyntheticAnswerKey, layout: FieldLayout, *, index: int) -> SheetCase:
    """The solution sheet for ``key``, as a case the ordinary renderer draws.

    Every question marked with the key's answer, the set code marked, and the
    candidate identifier **left blank**: the Answer Key stage reads a solution
    sheet's answers and set code and nothing else, and a blank identifier is
    what keeps a solution sheet from ever being matched to a candidate if it is
    scanned into a batch by mistake - it resolves to no roll at all. The page is
    clean: no degradation, no folds.

    Args:
        key: The canonical key.
        layout: The template's field layout.
        index: Recorded as the case index. Solution sheets are numbered apart
            from candidate sheets and never share their file names.
    """
    builder = SheetBuilder(layout, index, random.Random(0))
    if layout.has_identifier:
        for position in range(layout.identifier_columns):
            builder.blank_identifier_column(position)
    if key.set_code:
        builder.set_code(key.set_code)
    for number, label in key.answers.items():
        builder.answer(number, (label,))
    builder.tag(TestCaseTag.BASELINE, TestCaseTag.ALL_ANSWERED)
    return builder.build(notes=f"Solution sheet for set {key.set_code or '(none)'}.")


def solution_stem(set_code: str) -> str:
    """``Set_<code>`` with the code made safe for a file name.

    The same shape as the attendance workbooks' ``Set_<code>_Attendance.xlsx``.
    The set code itself is preserved unaltered in the manifest; only the file
    name is sanitised.
    """
    safe = sanitise_stem(set_code) if set_code else ""
    return f"Set_{safe or UNSET_STEM}"


@dataclass(frozen=True, slots=True)
class SolutionFiles:
    """Where one set's solution files are written, relative to ``solution/``."""

    sheet: str
    answer_key: str
    ground_truth: str


def solution_file_names(
    set_codes: Sequence[str], image_suffix: str
) -> dict[str, SolutionFiles]:
    """File names for every set's solution files.

    Raises:
        ValueError: Two set codes sanitise to the same file name (``"A/1"`` and
            ``"A-1"``). Writing one over the other would leave a key file that
            describes a different set from the one its name claims.
    """
    names: dict[str, SolutionFiles] = {}
    seen: dict[str, str] = {}
    for code in set_codes:
        stem = solution_stem(code)
        folded = stem.casefold()
        if folded in seen and seen[folded] != code:
            raise ValueError(
                f"Set codes '{seen[folded]}' and '{code}' would share the solution "
                f"file name '{stem}'"
            )
        seen[folded] = code
        names[code] = SolutionFiles(
            sheet=f"{stem}{SOLUTION_IMAGE_SUFFIX}{image_suffix}",
            answer_key=f"{stem}{ANSWER_KEY_SUFFIX}.txt",
            ground_truth=f"{stem}{SOLUTION_TRUTH_SUFFIX}.json",
        )
    return names


_SOLUTION_SUFFIXES = (
    f"{SOLUTION_IMAGE_SUFFIX}.png",
    f"{SOLUTION_IMAGE_SUFFIX}.jpg",
    f"{ANSWER_KEY_SUFFIX}.txt",
    f"{SOLUTION_TRUTH_SUFFIX}.json",
)


def clear_stale_solutions(directory: Path) -> tuple[Path, ...]:
    """Remove every solution file an earlier run left in ``directory``.

    The generator overwrites a dataset in place, and for images that is
    enough - an image is only ever read through the manifest. A key file is
    different: somebody will open ``solution/`` and read what is there, and a
    ``Set_4_Answer_Key.txt`` left over from a four-set run would look exactly
    as valid as this run's three. Only files this module names are removed;
    anything else a person put in the folder is left alone.

    Returns:
        The files removed.
    """
    if not directory.is_dir():
        return ()
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        if (
            path.is_file()
            and path.name.startswith("Set_")
            and path.name.endswith(_SOLUTION_SUFFIXES)
        ):
            path.unlink()
            removed.append(path)
    return tuple(removed)


def describe_solutions(
    keys: Mapping[str, SyntheticAnswerKey],
    files: Mapping[str, SolutionFiles],
    layout: FieldLayout,
) -> dict[str, Any]:
    """The manifest's ``solutions`` block: set -> key -> the files stating it."""
    return {
        "directory": SOLUTION_DIRNAME,
        "role": SOLUTION_ROLE,
        "text_format": "omrflow-answer-string",
        "sets": [
            {
                **key.describe(),
                "set_code_marked": set_code_markable(layout, code),
                "sheet": f"{SOLUTION_DIRNAME}/{files[code].sheet}",
                "answer_key": f"{SOLUTION_DIRNAME}/{files[code].answer_key}",
                "ground_truth": f"{SOLUTION_DIRNAME}/{files[code].ground_truth}",
            }
            for code, key in keys.items()
        ],
    }


__all__ = [
    "ANSWER_KEY_SUFFIX",
    "SOLUTION_DIRNAME",
    "SOLUTION_ROLE",
    "SolutionFiles",
    "SyntheticAnswerKey",
    "clear_stale_solutions",
    "describe_solutions",
    "generate_answer_keys",
    "is_resolved_set",
    "planned_set_codes",
    "question_plan",
    "serialise_answer_key",
    "set_code_markable",
    "solution_case",
    "solution_file_names",
    "solution_stem",
]
