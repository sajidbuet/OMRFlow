"""Deterministic, index-addressable synthetic sheets for the stress test.

Backs the mandatory 100,000-sheet production-hardening stress test (Phase
10, §19-§22).

Purpose:
    Let a 100,000-sheet examination exist and be processed without ever
    requiring 100,000 physical scan files to sit on disk at once, while still
    exercising the real recognition pipeline on real (synthetic) pixels -
    never a shortcut that inserts pre-computed recognition rows and calls
    that a stress test (§19's explicit prohibition).

Responsibilities:
    * :class:`StressDatasetSpec` - seed, sheet count and case-kind
      distribution: everything needed to regenerate any sheet independently.
    * :func:`plan_for_index` - pure function ``(spec, template, index) ->
      SheetCase``, using the same builder Phase 3's own synthetic-dataset
      generator uses, so a stress sheet is built from the identical,
      already-tested drawing primitives.
    * :func:`render_sheet_for_index` - plan plus pixels (or, for the
      deliberately-malformed case, corrupt bytes instead of pixels).
    * :func:`virtual_source_path` / :func:`is_stress_source` /
      :func:`parse_virtual_source_path` - the addressing scheme that lets a
      100,000-row batch exist in :mod:`omr_scanner.services.batch_store`
      with 100,000 distinct, unique ``source_path`` values that are never
      real files until the moment a worker needs one.

What does NOT belong here:
    * Anything Qt, database or batch-orchestration. This module only ever
      answers "what is sheet number N", deterministically and in isolation -
      :mod:`omr_scanner.services.stress_runner` is what turns that into an
      actual project and an actual batch.
    * A curated, hand-picked demonstration dataset. That is
      :mod:`omr_scanner.evaluation.case_plans` and
      :mod:`omr_scanner.evaluation.synthetic_dataset`, a *different*, already
      complete subsystem this module reuses the rendering primitives of but
      does not replace: that generator answers "make me a good regression
      dataset"; this one answers "make me sheet number 78,431 of a
      100,000-sheet run, cheaply, and independently of every other sheet."

Why every sheet is derived from ``seed + index`` alone:
    So that sheet 78,431 can be regenerated after an interruption without
    remembering anything about the other 99,999 - :func:`plan_for_index` and
    :func:`render_sheet_for_index` read nothing but their own arguments, and
    two calls with the same arguments are guaranteed to produce the same
    :class:`~omr_scanner.evaluation.test_cases.SheetCase` and the same pixels
    (the case-kind draw, the identifier, the answers and the geometric
    distortion are each seeded from ``(spec.seed, index)`` and nothing else).

Scope, stated honestly:
    Every case kind below produces a real, decodable (or, for
    :attr:`StressCaseKind.MALFORMED`, deliberately not decodable) image that
    the actual recognition pipeline reads exactly as it would a real scan.
    Two categories the phase brief also names - "absentee reconciliation"
    and "unknown candidate" - describe a *roster* relationship (a registered
    candidate with no script, or a script belonging to nobody registered),
    not a property of one image; they are represented in
    :mod:`omr_scanner.services.stress_runner`'s roster generation instead of
    here, and :attr:`StressCaseKind.UNKNOWN_CANDIDATE` here only supplies the
    *sheet* half of that pair (a roll number deliberately outside the
    intended roster range).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from omr_scanner.evaluation.synthetic_dataset import GeneratedSheet, render_case
from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder, SheetCase, TestCaseTag
from omr_scanner.imaging.synthetic import DistortionSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from omr_scanner.domain.template import OmrTemplate

STRESS_SOURCE_PREFIX = "stress:"
"""See :mod:`omr_scanner.services.scan_provenance`'s identical constant for
why this carries no ``/`` or ``\\`` - it must survive a round trip through
:class:`pathlib.Path` on Windows unchanged."""


class StressCaseKind(StrEnum):
    """What kind of sheet one stress-test index represents."""

    CLEAN = "clean"
    NO_ANSWER = "no_answer"
    MULTIPLE_ANSWER = "multiple_answer"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS_MARK = "ambiguous_mark"
    SKEWED = "skewed"
    BLANK_ID = "blank_id"
    AMBIGUOUS_ID = "ambiguous_id"
    DUPLICATE_ID = "duplicate_id"
    EXACT_DUPLICATE_SCAN = "exact_duplicate_scan"
    DUPLICATE_ROLL_DIFFERENT_IMAGE = "duplicate_roll_different_image"
    UNKNOWN_CANDIDATE = "unknown_candidate"
    MULTIPLE_SETS = "multiple_sets"
    MALFORMED = "malformed"
    CONFLICT_GENERATING = "conflict_generating"

    @property
    def label(self) -> str:
        """Operator-facing wording, for the benchmark report's distribution table."""
        return self.value.replace("_", " ")


DEFAULT_STRESS_DISTRIBUTION: dict[StressCaseKind, float] = {
    StressCaseKind.CLEAN: 0.72,
    StressCaseKind.NO_ANSWER: 0.05,
    StressCaseKind.MULTIPLE_ANSWER: 0.03,
    StressCaseKind.LOW_CONFIDENCE: 0.02,
    StressCaseKind.AMBIGUOUS_MARK: 0.02,
    StressCaseKind.SKEWED: 0.03,
    StressCaseKind.BLANK_ID: 0.01,
    StressCaseKind.AMBIGUOUS_ID: 0.01,
    StressCaseKind.DUPLICATE_ID: 0.01,
    StressCaseKind.EXACT_DUPLICATE_SCAN: 0.01,
    StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE: 0.01,
    StressCaseKind.UNKNOWN_CANDIDATE: 0.02,
    StressCaseKind.MULTIPLE_SETS: 0.03,
    StressCaseKind.MALFORMED: 0.02,
    StressCaseKind.CONFLICT_GENERATING: 0.01,
}
"""Sums to 1.0. A deliberately curated proportion, not a claim about a real
examination's error rate - see the phase brief §21, which asks only for a
"configurable deterministic distribution", not a validated one."""

_MULTIPLE_SET_CODES: tuple[str, ...] = ("A", "B", "C")


def _seeded_rng(*parts: object) -> random.Random:
    """A :class:`random.Random` seeded deterministically from ``parts``.

    ``random.Random`` accepts only ``None``, ``int``, ``float``, ``str``,
    ``bytes`` or ``bytearray`` as a seed - not a tuple - so every part is
    joined into one string. A string seed is hashed by ``random`` itself
    (SHA-512-based, not the process-randomised ``hash()`` builtin), so this
    is stable across processes and across Python runs, which is the whole
    property this module depends on.
    """
    return random.Random(":".join(str(part) for part in parts))


@dataclass(frozen=True, slots=True)
class StressDatasetSpec:
    """Everything needed to regenerate any sheet of a stress run, independently.

    Attributes:
        seed: Master seed. The same ``(seed, sheet_count, distribution)``
            always produces the same 100,000 sheets.
        sheet_count: How many logical sheets this run has.
        distribution: Case-kind weights, defaulting to
            :data:`DEFAULT_STRESS_DISTRIBUTION`. Need not sum to exactly
            1.0 - :meth:`kind_for_index` falls back to
            :attr:`StressCaseKind.CLEAN` for any residual probability mass.
    """

    seed: int
    sheet_count: int
    distribution: Mapping[StressCaseKind, float] = field(
        default_factory=lambda: dict(DEFAULT_STRESS_DISTRIBUTION)
    )

    def kind_for_index(self, index: int) -> StressCaseKind:
        """Return which case kind sheet ``index`` is, deterministically.

        A per-index seeded draw against the cumulative distribution - O(1),
        independent of every other index, and stable for a given
        ``(seed, distribution)`` pair regardless of ``sheet_count`` (adding
        more sheets to a run never reassigns an existing sheet's kind).
        """
        draw = _seeded_rng(self.seed, index, "kind").random()
        cumulative = 0.0
        for kind, weight in self.distribution.items():
            cumulative += weight
            if draw < cumulative:
                return kind
        return StressCaseKind.CLEAN


def natural_roll(spec: StressDatasetSpec, layout: FieldLayout, index: int) -> str:
    """Return the roll number sheet ``index`` would carry absent any twist.

    Deterministic and independent of :meth:`StressDatasetSpec.kind_for_index`
    - other functions in this module deliberately call it with a
    *different* index than the one being rendered to construct a partner
    sheet (a duplicate roll, an exact duplicate image), without needing to
    know or reproduce that partner's own assigned kind.
    """
    digits = layout.identifier_columns or 7
    symbols = layout.identifier_symbols or tuple("0123456789")
    base = len(symbols) ** digits
    number = (spec.seed + index) % max(base, 1)
    characters: list[str] = []
    remainder = number
    for _ in range(digits):
        characters.append(symbols[remainder % len(symbols)])
        remainder //= len(symbols)
    return "".join(reversed(characters))


def _out_of_range_roll(spec: StressDatasetSpec, layout: FieldLayout, index: int) -> str:
    """A roll deliberately outside any index this run would naturally assign.

    Used for :attr:`StressCaseKind.UNKNOWN_CANDIDATE`: a roster built for
    indices ``0 .. sheet_count - 1`` (see
    :mod:`omr_scanner.services.stress_runner`) will never contain this roll.
    """
    return natural_roll(spec, layout, index + spec.sheet_count * 7 + 1_000_003)


def plan_for_index(
    spec: StressDatasetSpec, template: OmrTemplate, index: int
) -> SheetCase:
    """Return sheet ``index``'s complete definition, before anything is drawn.

    Pure and O(1): reads only ``spec``, ``template`` and ``index``. Calling
    this twice with the same arguments returns an equal
    :class:`~omr_scanner.evaluation.test_cases.SheetCase` every time.
    """
    layout = FieldLayout.of(template)
    kind = spec.kind_for_index(index)

    if kind is StressCaseKind.EXACT_DUPLICATE_SCAN:
        # Byte-identical to the *natural, undistorted* rendering of the
        # previous index - not to whatever kind that index was itself
        # independently assigned, which this deliberately never consults.
        # `index` fields on `SheetCase` affect only ground-truth metadata,
        # never pixels, so replacing it after the fact keeps the case
        # correctly labelled without touching the image it will produce.
        partner_index = index - 1 if index > 0 else index + 1
        partner_case = _clean_case(spec, layout, partner_index)
        return replace(
            partner_case,
            index=index,
            duplicate_group=f"exact-duplicate-{partner_index}",
        )

    partner_index = index - 1 if index > 0 else index + 1
    rng = _seeded_rng(spec.seed, index)
    builder = SheetBuilder(layout, index, rng)

    if kind in (StressCaseKind.DUPLICATE_ID, StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE):
        roll = natural_roll(spec, layout, partner_index)
    elif kind is StressCaseKind.UNKNOWN_CANDIDATE:
        roll = _out_of_range_roll(spec, layout, index)
    else:
        roll = natural_roll(spec, layout, index)

    if kind is not StressCaseKind.BLANK_ID:
        builder.identifier(roll)

    if kind is StressCaseKind.AMBIGUOUS_ID and layout.has_identifier:
        symbols = layout.identifier_symbols
        if len(symbols) >= 2:
            builder.identifier_column(0, symbols[:2])

    if layout.has_set_code:
        set_code = (
            _MULTIPLE_SET_CODES[index % len(_MULTIPLE_SET_CODES)]
            if kind is StressCaseKind.MULTIPLE_SETS
            else _MULTIPLE_SET_CODES[0]
        )
        builder.set_code(set_code)

    distortion = DistortionSpec(seed=index)
    expect_failure = False
    duplicate_group = ""

    if kind in (StressCaseKind.CLEAN, StressCaseKind.MALFORMED):
        builder.answer_all()
        builder.tag(TestCaseTag.BASELINE)
    elif kind is StressCaseKind.NO_ANSWER:
        builder.blank_all()
    elif kind is StressCaseKind.MULTIPLE_ANSWER:
        builder.answer_all()
        _mark_first_question(builder, layout, count=2)
    elif kind is StressCaseKind.LOW_CONFIDENCE:
        builder.answer_all(fill=0.5, intensity=0.55)
    elif kind is StressCaseKind.AMBIGUOUS_MARK:
        builder.answer_all()
        _mark_first_question(builder, layout, count=1, fill=0.5, ambiguous=True)
    elif kind is StressCaseKind.SKEWED:
        builder.answer_all()
        distortion = DistortionSpec(rotation_degrees=6.0, seed=index)
    elif kind is StressCaseKind.BLANK_ID or kind is StressCaseKind.AMBIGUOUS_ID:
        builder.answer_all()
    elif kind in (StressCaseKind.DUPLICATE_ID, StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE):
        builder.answer_all()
        duplicate_group = f"duplicate-roll-{roll}"
    elif kind is StressCaseKind.UNKNOWN_CANDIDATE or kind is StressCaseKind.MULTIPLE_SETS:
        builder.answer_all()
    elif kind is StressCaseKind.CONFLICT_GENERATING:
        builder.answer_all()
        _mark_first_question(builder, layout, count=1, fill=0.5, ambiguous=True)
        if layout.has_identifier and layout.identifier_columns > 1:
            symbols = layout.identifier_symbols
            if len(symbols) >= 2:
                builder.identifier_column(1, symbols[:2])

    return builder.build(
        distortion=distortion,
        expect_failure=expect_failure,
        duplicate_group=duplicate_group,
        notes=f"stress:{kind.value}",
    )


def _clean_case(spec: StressDatasetSpec, layout: FieldLayout, index: int) -> SheetCase:
    """The plain, undistorted "clean" rendering of ``index`` - no case-kind draw."""
    rng = _seeded_rng(spec.seed, index)
    builder = SheetBuilder(layout, index, rng)
    builder.identifier(natural_roll(spec, layout, index))
    if layout.has_set_code:
        builder.set_code(_MULTIPLE_SET_CODES[0])
    builder.answer_all()
    return builder.build(distortion=DistortionSpec(seed=index), notes="stress:clean")


def _mark_first_question(
    builder: SheetBuilder, layout: FieldLayout, *, count: int, **mark_kwargs: object
) -> None:
    """Overwrite the first question with ``count`` marked options, if any exist."""
    if not layout.questions:
        return
    question = layout.questions[0]
    labels = layout.labels_for(question)
    if len(labels) >= count:
        builder.answer(question, labels[:count], **mark_kwargs)


@dataclass(frozen=True, slots=True)
class RenderedStressSheet:
    """One sheet's rendered bytes, ready to be written to a file.

    Attributes:
        case: What was planned.
        png_bytes: PNG-encoded image bytes, or ``None`` for a deliberately
            malformed sheet.
        malformed_bytes: Deliberately non-image bytes, for
            :attr:`StressCaseKind.MALFORMED` - written verbatim so
            recognition genuinely meets an undecodable file, the same
            failure a damaged real scan produces, rather than a flag that
            merely claims one.
    """

    case: SheetCase
    png_bytes: bytes | None
    malformed_bytes: bytes | None = None

    @property
    def is_malformed(self) -> bool:
        """Whether this sheet is deliberately not a decodable image."""
        return self.malformed_bytes is not None


def render_sheet_for_index(
    spec: StressDatasetSpec, template: OmrTemplate, index: int
) -> RenderedStressSheet:
    """Render sheet ``index`` to bytes, deterministically.

    Args:
        spec: The stress dataset this sheet belongs to.
        template: The template to render against.
        index: Which sheet.

    Returns:
        Encoded bytes ready to write to a file - real PNG bytes for every
        ordinary case, or deliberately corrupt bytes for
        :attr:`StressCaseKind.MALFORMED`, which never reaches the image
        encoder at all.
    """
    import cv2

    case = plan_for_index(spec, template, index)
    if case.notes == "stress:malformed":
        # Genuinely undecodable - the same failure mode a damaged real file
        # produces, not a simulated one. The exact bytes are irrelevant; they
        # must simply not be a valid image, deterministically, by index.
        payload = f"MALFORMED STRESS SHEET {spec.seed}:{index}".encode()
        return RenderedStressSheet(case=case, png_bytes=None, malformed_bytes=payload)

    generated: GeneratedSheet = render_case(template, case, seed=spec.seed)
    ok, buffer = cv2.imencode(".png", generated.image)
    if not ok:  # pragma: no cover - cv2 encoding failure is not exercised
        raise RuntimeError(f"Could not encode synthetic stress sheet {index}")
    return RenderedStressSheet(case=case, png_bytes=buffer.tobytes())


def virtual_source_path(spec: StressDatasetSpec, index: int) -> str:
    """Return the unique, Windows-safe identity string for sheet ``index``.

    Stored as a :class:`~omr_scanner.database.models.BatchScan.source_path`,
    where it satisfies that column's ``(batch_id, source_path)`` uniqueness
    constraint without a single physical file existing anywhere - see
    :mod:`omr_scanner.services.scan_provenance` for why this carries no path
    separator characters.
    """
    return f"{STRESS_SOURCE_PREFIX}{spec.seed}-{index:07d}"


def is_stress_source(source_path: str) -> bool:
    """Whether ``source_path`` names a virtual stress sheet."""
    return source_path.startswith(STRESS_SOURCE_PREFIX)


def parse_virtual_source_path(source_path: str) -> tuple[int, int]:
    """Return ``(seed, index)`` encoded in a virtual source path.

    Raises:
        ValueError: ``source_path`` is not a virtual stress source.
    """
    if not is_stress_source(source_path):
        raise ValueError(f"Not a stress source path: {source_path!r}")
    remainder = source_path[len(STRESS_SOURCE_PREFIX) :]
    seed_text, _, index_text = remainder.rpartition("-")
    return int(seed_text), int(index_text)


__all__ = [
    "DEFAULT_STRESS_DISTRIBUTION",
    "STRESS_SOURCE_PREFIX",
    "RenderedStressSheet",
    "StressCaseKind",
    "StressDatasetSpec",
    "is_stress_source",
    "natural_roll",
    "parse_virtual_source_path",
    "plan_for_index",
    "render_sheet_for_index",
    "virtual_source_path",
]
