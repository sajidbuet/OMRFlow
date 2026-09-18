"""Build the committed ``RecognitionResult`` fixtures.

Purpose:
    Produce one small, realistic, *stored* recognition result per interesting
    scenario, so that Phase 4 and Phase 5 can be developed and tested against
    recognition output without running recognition - or even having OpenCV
    installed.

    That is the point of the exercise: a review screen, a scoring engine or a
    conflict queue should be writable against the published shape of a result,
    and should keep working when Recognition Engine v2 replaces v1 underneath.

Usage::

    python scripts/build_recognition_fixtures.py
    python scripts/build_recognition_fixtures.py --output tests/fixtures/recognition

Each fixture is produced by *actually recognising* a sheet rendered for that
scenario, never by hand-writing JSON: a hand-written fixture drifts from what
the engine really emits, and then the tests written against it are testing a
fiction.

Regenerate these when the result schema changes, and review the diff - a
surprising change in a fixture is a change in a published contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT):
    if str(candidate) not in sys.path:  # pragma: no cover - script bootstrap
        sys.path.insert(0, str(candidate))

import cv2  # noqa: E402
from tests.conftest import build_answer_sheet_template  # noqa: E402

from omr_scanner.evaluation.synthetic_dataset import (  # noqa: E402
    MarkPlan,
    sheet_spec_from_template,
)
from omr_scanner.imaging.synthetic import (  # noqa: E402
    DistortionSpec,
    MarkStyle,
    apply_distortion,
    render_sheet,
)
from omr_scanner.services.recognition_service import RecognitionEngine  # noqa: E402
from omr_scanner.services.recognition_settings import RecognitionOptions  # noqa: E402

DEFAULT_OUTPUT = REPOSITORY_ROOT / "tests" / "fixtures" / "recognition"

ROLL = "120317"
"""A fictional roll number. Committed fixtures never carry a real one."""

OTHER_ROLL = "120318"
SET_CODE = "B"
ANSWER = "B"
"""The template's answer labels are upper case; using 'b' here would mark
nothing at all and every fixture would quietly become a blank sheet."""


def _answers(value: str = ANSWER) -> dict[str, dict[int, MarkPlan]]:
    """Mark every question with ``value`` and fill in the roll and set code.

    The zone ids are the synthetic answer sheet's own, from
    ``tests.conftest.build_answer_sheet_template``; this script is tied to that
    one template on purpose, because a fixture wants a small, stable sheet
    rather than whatever template happens to be lying around.
    """
    return {
        "roll_number": {
            position: MarkPlan(labels=(digit,)) for position, digit in enumerate(ROLL)
        },
        "set_code": {0: MarkPlan(labels=(SET_CODE,))},
        "questions_0": {index: MarkPlan(labels=(value,)) for index in range(10)},
        "questions_1": {index: MarkPlan(labels=(value,)) for index in range(10)},
    }


def _scenarios() -> dict[str, tuple[dict, dict]]:
    """Return ``{fixture name: (marks, page options)}`` for every scenario.

    Page options are keyword arguments for
    :func:`~omr_scanner.evaluation.synthetic_dataset.sheet_spec_from_template`
    plus an optional ``distortion``, so one table describes both what was
    marked and what was wrong with the page.
    """
    perfect = _answers()

    blanks = _answers()
    for index in range(4):
        del blanks["questions_0"][index]

    multiples = _answers()
    multiples["questions_0"][2] = MarkPlan(labels=("A", "C"))
    multiples["questions_1"][5] = MarkPlan(labels=("B", "D"))

    faint = _answers()
    for index in range(3):
        # Between the template's blank threshold (0.25) and its fill
        # threshold (0.55): the engine is *supposed* to refuse this rather
        # than guess, which is exactly the fixture a review screen needs.
        faint["questions_0"][index] = MarkPlan(
            labels=("B",), fill=0.38, intensity=0.55
        )

    bad_roll = _answers()
    del bad_roll["roll_number"][2]  # a digit column left empty

    bad_set = _answers()
    bad_set["set_code"][0] = MarkPlan(labels=("A", "C"))  # two set codes marked

    mixed = _answers()
    del mixed["questions_0"][0]
    mixed["questions_0"][1] = MarkPlan(labels=("A", "B"))
    mixed["questions_0"][2] = MarkPlan(labels=("C",), fill=0.38, intensity=0.55)
    mixed["questions_1"][9] = MarkPlan(labels=("D",), style=MarkStyle.CROSS, fill=0.9)

    duplicate = _answers(value="C")

    return {
        "perfect_scan": (perfect, {}),
        "blank_answers": (blanks, {}),
        "multiple_marks": (multiples, {}),
        "low_confidence": (faint, {}),
        "invalid_roll": (bad_roll, {}),
        "invalid_set": (bad_set, {}),
        "mixed_ambiguity": (mixed, {}),
        # Two sheets claiming one roll number: the pair Phase 3's naming rule
        # exists for, and the pair a future review screen has to present.
        "duplicate_roll_a": (perfect, {}),
        "duplicate_roll_b": (duplicate, {}),
        # A page that registers, but only just: no margin around it, so a
        # marker sits against the image border. Every synthetic page already
        # earns MULTIPLE_CORNER_CANDIDATES (its own printed graphics look like
        # markers, exactly as the real sample sheet's do); this one carries a
        # second, different reservation on top.
        "alignment_warning": (
            perfect,
            {"distortion": DistortionSpec(margin_px=2, rotation_degrees=1.5, seed=7)},
        ),
        # No orientation mark at all: which way up the page is cannot be
        # established, so nothing is read. Deliberately a *failure* fixture.
        "orientation_failure": (perfect, {"omit_orientation": True}),
    }


FLOAT_DIGITS = 6
"""Decimal places kept in a fixture's measurements.

Six is far beyond anything a fill ratio means physically, and it is the
difference between 850 KB and 200 KB of committed JSON: a full-precision dump
spends most of its bytes on the seventeenth digit of a number measured from
eight-bit pixels."""


def _round(value: object) -> object:
    """Recursively round floats, leaving everything else alone."""
    if isinstance(value, float):
        return round(value, FLOAT_DIGITS)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def _render(payload: dict, *, pretty: bool) -> str:
    """Serialise a fixture: compact by default, indented when a human asks."""
    rounded = _round(payload)
    if pretty:
        return json.dumps(rounded, indent=2, ensure_ascii=False) + "\n"
    return json.dumps(rounded, ensure_ascii=False, separators=(",", ":")) + "\n"


def build(output: Path, *, pretty: bool = False) -> list[Path]:
    """Render, recognise and write every fixture. Returns the paths written."""
    template = build_answer_sheet_template()
    engine = RecognitionEngine(RecognitionOptions(with_preview=False))
    output.mkdir(parents=True, exist_ok=True)
    scratch = output / "_scratch"
    scratch.mkdir(exist_ok=True)

    written: list[Path] = []
    for name, (marks, page_options) in _scenarios().items():
        distortion = page_options.pop("distortion", None)
        spec = sheet_spec_from_template(template, marks, **page_options)
        sheet = render_sheet(spec)
        image = (
            apply_distortion(sheet, distortion).image
            if distortion is not None
            else apply_distortion(sheet, DistortionSpec()).image
        )

        # Recognition reads a *file*, as it does in production; writing the
        # page to a scratch image keeps the fixture on the real code path.
        image_path = scratch / f"{name}.png"
        cv2.imwrite(str(image_path), image)
        result = engine.process(image_path, template)

        payload = result.to_dict()
        # The scratch directory is an implementation detail of this script, and
        # an absolute path from one developer's machine has no business in a
        # committed fixture.
        payload["source_path"] = f"{name}.png"
        payload["recognised_at"] = ""  # a timestamp would make every rebuild a diff

        destination = output / f"{name}.json"
        destination.write_text(_render(payload, pretty=pretty), encoding="utf-8")
        written.append(destination)
        print(
            f"{name:<22} outcome={result.outcome.value:<20} "
            f"roll={result.identifier_value or '-':<8} "
            f"status={','.join(result.status_codes)}"
        )
        image_path.unlink()

    scratch.rmdir()
    return written


def main(argv: list[str] | None = None) -> int:
    """Run the script and return a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Indent the JSON for reading. Roughly triples the committed size.",
    )
    arguments = parser.parse_args(argv)

    written = build(arguments.output, pretty=arguments.pretty)
    print(f"\n{len(written)} fixture(s) written to {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
