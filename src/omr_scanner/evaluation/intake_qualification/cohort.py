"""The deterministic examination and arrival plan, with its ground truth (revised phase 9).

Purpose:
    Decide - from the campaign's two seeds and nothing else - who sits which
    set, what every candidate marked, which papers are folded, rescanned,
    scanned twice or wrongly bubbled, which scanner each image arrives at,
    under which file name, when, written how, and what the scripted operator
    will decide about each of them. Everything the evaluator later expects is
    derived from this plan by :mod:`.reference`; nothing here consults an
    OMRFlow service, a database or a recognition result.

Two seeds:
    ``config.seed`` decides the *logical* cohort and the source / file-name
    allocation (so a failure names the same sheet when rerun);
    ``config.timing_seed`` decides arrival times and write patterns. The
    uninterrupted control run reuses the cohort and redraws only the timing.

Content and arrival:
    A :class:`Content` is one distinct image (one set of bytes once rendered);
    an :class:`Arrival` is one file a scanner writes. Several arrivals may carry
    the same content - the planted byte copies, within and across sources -
    and every source names its files with its own counter, so the same file
    name arrives at every source (``Scanner_A/000001.png``,
    ``Scanner_B/000001.png``, ...): independent files whose provenance differs.

Image conventions (measured against the qualification template, see
``docs/intake_qualification.md``): a clean script reads exactly; a blank or
double-bubbled identifier column gives ``identifier_blank`` /
``identifier_multiple``; a fold covering a registration marker gives
``registration_failed`` and the (unvalidated) quality policy's
``RESCAN_REQUIRED``; a rescan is the same marks with other pixel noise, so other
bytes. These are expectations the campaign *checks*, not results it assumes.
"""

from __future__ import annotations

import hashlib
import json
import random
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from omr_scanner.evaluation.intake_qualification.config import (
    RELEASE_MIN_ARRIVALS,
    CampaignConfig,
    Mode,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.template import OmrTemplate

PLAN_VERSION = 1


class ContentKind(StrEnum):
    """What one distinct image is."""

    SCRIPT = "script"
    """A candidate's answer sheet, cleanly scanned."""
    RESCAN = "rescan"
    """Another scan of a candidate's sheet: the same marks, other bytes."""
    FOLDED = "folded"
    """A candidate's sheet with a corner folded over a registration marker."""
    BLANK_PAGE = "blank_page"
    """An empty page fed by mistake - no candidate's script."""


class IdDefect(StrEnum):
    """How the candidate bubbled the Student ID (the written ID is always right)."""

    NONE = "none"
    BLANK = "blank"
    MULTIPLE = "multiple"
    WRONG = "wrong"
    """Bubbled another present candidate's roll."""


class Category(StrEnum):
    """The one planted case a candidate belongs to."""

    NORMAL = "normal"
    ABSENT = "absent"
    """Marked absent on the attendance list; no script."""
    MISSING_SCRIPT = "missing_script"
    """Marked present; no script ever arrives. The operator records absence."""
    LATE_FOUND = "late_found"
    """Marked present; the script arrives only after the session was closed."""
    ABSENT_WITH_SCRIPT = "absent_with_script"
    """Marked absent on the list, but a script arrives (the list was wrong)."""
    DUPLICATE_SCRIPT = "duplicate_script"
    """The same paper scanned twice (other bytes, no rejection)."""
    WRONG_ID = "wrong_id"
    """Bubbled the roll of the paired :attr:`WRONG_ID_VICTIM`."""
    WRONG_ID_VICTIM = "wrong_id_victim"
    ID_BLANK = "id_blank"
    ID_MULTIPLE = "id_multiple"
    FOLDED = "folded"
    """Folded original, suggested for rescan, rejected, replaced by a rescan."""
    CHAIN = "chain"
    """Folded original -> rescan 1 (confirmed, then rejected) -> rescan 2."""
    UNKNOWN = "unknown"
    """Not on any attendance list, yet a script bearing this roll arrives."""


class WritePattern(StrEnum):
    """How a writer puts one file on disk."""

    ATOMIC = "atomic"
    STEPPED = "stepped"
    HEADER_FIRST = "header_first"
    HELD_OPEN = "held_open"
    LONG_PAUSE = "long_pause"
    """One pause longer than the stability quiet period, mid-file."""
    RENAME = "rename"
    """Written under a temporary ``.part`` name, then renamed into place."""


class When(StrEnum):
    """When the scripted operator acts on a planned item."""

    LIVE = "live"
    """As soon as the item is visible (after a seeded delay), during intake."""
    LATE = "late"
    """Only once the run is well advanced - while intake still continues."""
    FINAL = "final"
    """Only in the endgame, after the deliberately refused closure attempt."""


class TaskKind(StrEnum):
    """One scripted operator decision, by production service."""

    CORRECT_ID = "correct_id"
    """``review_store.correct_value`` on the sheet's Student-ID conflict."""
    ACCEPT_DUPLICATE = "accept_duplicate"
    """``review_store.accept_machine_value`` on a duplicate-ID conflict."""
    CONFIRM_RESCAN = "confirm_rescan"
    """``quality_decisions.confirm_suggestion`` - the suggested rejection."""
    DISMISS_SUGGESTION = "dismiss_suggestion"
    """``quality_decisions.dismiss_suggestion``."""
    ACKNOWLEDGE_UNREADABLE = "acknowledge_unreadable"
    """``review_store.accept_machine_value`` on a dismissed page's sheet conflict."""
    CONFIRM_REPLACEMENT = "confirm_replacement"
    """``scan_lifecycle.confirm_replacement`` (original -> ``value`` content)."""
    REJECT_REPLACEMENT = "reject_replacement"
    """``scan_lifecycle.reject_scan`` on a confirmed rescan that is itself bad."""
    LIST_ABSENT = "list_absent"
    """The office corrects the attendance list: this candidate was absent
    (``set_attendance.assign_attendance_workbook`` with the corrected list)."""
    LIST_PRESENT = "list_present"
    """The office corrects the attendance list: this candidate sat the paper."""
    EXCLUDE_SCRIPT = "exclude_script"
    """``reconciliation_store.set_script_excluded``: an accidental second scan."""
    DISMISS_UNKNOWN = "dismiss_unknown"
    """``reconciliation_store.dismiss_entry``: a script of nobody on the list."""


# ----------------------------------------------------------------------
# Plan values
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Candidate:
    """One roster row and what that candidate marked."""

    roll: str
    set_code: str
    name: str
    row: int
    """Position on the set's attendance list (0-based)."""
    absent_on_roster: bool
    category: Category
    answers: tuple[str, ...]
    """Per question, engine convention: ``"B"``, ``""`` (blank), ``"A-C"`` (two marks)."""
    on_roster: bool = True
    """``False`` for :attr:`Category.UNKNOWN`: on no attendance list."""


@dataclass(frozen=True, slots=True)
class Content:
    """One distinct image, and what reading it must give.

    Attributes:
        key: Stable id (``c00042``).
        kind: :class:`ContentKind`.
        candidate: Whose paper (roll), ``""`` for a blank page. For an
            unknown-candidate script, a roll on no attendance list.
        set_code: The set marked on the paper.
        written_roll: What the candidate wrote above the bubbles.
        bubbled_roll: What the bubbles say, engine convention - what a correct
            reading returns (``"______"`` blank, ``"1?0042"`` multiple).
        id_defect: :class:`IdDefect`.
        render_index: Seeds the drawing and the pixel noise.
        rotation: Degrees of page rotation (small).
        offset: ``(dx, dy)`` pixels the page is displaced by (rescans).
        fold: ``(corner, depth_x, depth_y)`` for a folded sheet, else ``None``.
        registers: Whether registration is expected to succeed.
        expected_conflicts: Sheet-local conflict types a correct reading gives.
        expected_quality: ``accept`` or ``rescan_required``.
    """

    key: str
    kind: ContentKind
    candidate: str
    set_code: str
    written_roll: str
    bubbled_roll: str
    id_defect: IdDefect
    render_index: int
    rotation: float = 0.0
    offset: tuple[float, float] = (0.0, 0.0)
    """Page displacement in pixels: a rescan lies a little differently on the platen."""
    fold: tuple[str, float, float] | None = None
    registers: bool = True
    expected_conflicts: tuple[str, ...] = ()
    expected_quality: str = "accept"


@dataclass(frozen=True, slots=True)
class Arrival:
    """One file one simulated scanner writes.

    Attributes:
        seq: Global sequence number (stable).
        source: Source label (``A``, ``B``, ...).
        name: File name on that source (its own counter).
        content: :attr:`Content.key` of the bytes it carries.
        at: Planned start of the write, seconds from the writer's start.
        pattern: :class:`WritePattern`.
        pauses: Seconds paused between successive chunks (``len`` = chunks - 1).
        hold_after: Seconds a held-open handle stays open after the last chunk.
        late: Written only after the session is closed (the held-file case).
    """

    seq: int
    source: str
    name: str
    content: str
    at: float
    pattern: WritePattern = WritePattern.ATOMIC
    pauses: tuple[float, ...] = ()
    hold_after: float = 0.0
    late: bool = False


@dataclass(frozen=True, slots=True)
class OperatorTask:
    """One planned operator decision.

    Attributes:
        kind: :class:`TaskKind`.
        content: The content the decision is about (its scan).
        value: The corrected roll, the replacement's content key, ...
        when: :class:`When`.
        delay: Seconds after the item becomes visible (``LIVE``).
        candidate: The roster candidate concerned (reconciliation tasks).
        set_code: The set concerned.
        phase: ``"first_close"`` / ``"reopen"`` for reconciliation tasks.
    """

    kind: TaskKind
    content: str = ""
    value: str = ""
    when: When = When.LIVE
    delay: float = 0.0
    candidate: str = ""
    set_code: str = ""
    phase: str = ""


@dataclass(frozen=True, slots=True)
class ScoringRules:
    """The campaign's scoring policy, as plain exact data (the reference's input)."""

    correct: Fraction = Fraction(1)
    incorrect_penalty: Fraction = Fraction(1, 4)
    multiple_penalty: Fraction = Fraction(1, 4)
    blank: Fraction = Fraction(0)
    minimum: Fraction = Fraction(0)
    wrong_questions: dict[str, tuple[int, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    """The whole plan. Built by :func:`plan_campaign`; never mutated."""

    config: CampaignConfig
    template_fingerprint: str
    labels: tuple[str, ...]
    question_count: int
    keys: dict[str, str]
    scoring: ScoringRules
    candidates: tuple[Candidate, ...]
    contents: tuple[Content, ...]
    arrivals: tuple[Arrival, ...]
    tasks: tuple[OperatorTask, ...]
    unknown_rolls: tuple[str, ...]
    _content_index: dict[str, Content] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _candidate_index: dict[str, Candidate] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    # --- lookups ------------------------------------------------------
    def content(self, key: str) -> Content:
        """The planned content ``key``."""
        if self._content_index is None:
            object.__setattr__(
                self, "_content_index", {item.key: item for item in self.contents}
            )
        assert self._content_index is not None
        return self._content_index[key]

    def candidate(self, roll: str) -> Candidate | None:
        """The planned candidate with ``roll`` (rostered or not), or ``None``."""
        if self._candidate_index is None:
            object.__setattr__(
                self, "_candidate_index", {item.roll: item for item in self.candidates}
            )
        assert self._candidate_index is not None
        return self._candidate_index.get(roll)

    @property
    def main_arrivals(self) -> tuple[Arrival, ...]:
        """Arrivals of the main intake timeline (not the post-closure late file)."""
        return tuple(item for item in self.arrivals if not item.late)

    @property
    def late_arrivals(self) -> tuple[Arrival, ...]:
        """Arrivals of the post-closure late file."""
        return tuple(item for item in self.arrivals if item.late)

    def to_json(self) -> dict[str, Any]:
        """The machine-readable manifest (independent of any OMRFlow output)."""
        return {
            "plan_version": PLAN_VERSION,
            "config": self.config.to_json(),
            "template_fingerprint": self.template_fingerprint,
            "labels": list(self.labels),
            "question_count": self.question_count,
            "answer_keys": dict(self.keys),
            "scoring": {
                "correct": str(self.scoring.correct),
                "incorrect_penalty": str(self.scoring.incorrect_penalty),
                "multiple_penalty": str(self.scoring.multiple_penalty),
                "blank": str(self.scoring.blank),
                "minimum": str(self.scoring.minimum),
                "wrong_questions": {k: list(v) for k, v in self.scoring.wrong_questions.items()},
            },
            "candidates": [
                {**asdict(item), "category": item.category.value, "answers": list(item.answers)}
                for item in self.candidates
            ],
            "contents": [
                {**asdict(item), "kind": item.kind.value, "id_defect": item.id_defect.value,
                 "expected_conflicts": list(item.expected_conflicts),
                 "fold": list(item.fold) if item.fold else None,
                 "offset": list(item.offset)}
                for item in self.contents
            ],
            "arrivals": [
                {**asdict(item), "pattern": item.pattern.value, "pauses": list(item.pauses)}
                for item in self.arrivals
            ],
            "operator_tasks": [
                {**asdict(item), "kind": item.kind.value, "when": item.when.value}
                for item in self.tasks
            ],
            "unknown_rolls": list(self.unknown_rolls),
        }

    def digest(self) -> str:
        """SHA-256 of the canonical manifest - names this exact plan in a report."""
        text = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------
def _rng(*parts: object) -> random.Random:
    """A stream seeded from ``parts`` (stable across processes; see stress_dataset)."""
    return random.Random(":".join(str(part) for part in parts))


def _quota(fraction: float, population: int, minimum: int) -> int:
    return max(minimum, round(fraction * population))


def template_fingerprint(template: OmrTemplate) -> str:
    """The template's geometry identity, as the project records it."""
    from omr_scanner.services import batch_store

    return batch_store.BatchIdentity.of(template).geometry_fingerprint


def _answer_key(seed: int, set_code: str, labels: Sequence[str], count: int) -> str:
    rng = _rng(seed, "key", set_code)
    return "".join(rng.choice(list(labels)) for _ in range(count))


def _draw_answers(
    rng: random.Random, key: str, labels: Sequence[str], *, blanks: int, multiples: int
) -> tuple[str, ...]:
    """One candidate's marks: mostly the key at a sampled ability, some blanks / doubles."""
    ability = rng.uniform(0.30, 0.95)
    answers: list[str] = []
    for correct in key:
        if rng.random() < ability:
            answers.append(correct)
        else:
            answers.append(rng.choice([label for label in labels if label != correct]))
    numbers = list(range(len(key)))
    rng.shuffle(numbers)
    for index in numbers[:blanks]:
        answers[index] = ""
    for index in numbers[blanks : blanks + multiples]:
        pair = sorted(rng.sample(list(labels), 2), key=list(labels).index)
        answers[index] = "-".join(pair)
    return tuple(answers)


def _multiple_reading(roll: str, column: int) -> str:
    return roll[:column] + "?" + roll[column + 1 :]


def plan_campaign(config: CampaignConfig, template: OmrTemplate) -> CampaignPlan:
    """Plan the whole campaign. Pure and deterministic in ``config``.

    Raises:
        ValueError: A release plan that would have fewer than
            :data:`RELEASE_MIN_ARRIVALS` arrivals, fewer sources than three,
            or a template without the fields this cohort needs.
    """
    from omr_scanner.evaluation.test_cases import FieldLayout

    layout = FieldLayout.of(template)
    if not (layout.has_identifier and layout.has_set_code and layout.has_questions):
        raise ValueError("the qualification template needs an identifier, a set code and questions")
    missing_sets = [code for code in config.sets if code not in layout.set_symbols]
    if missing_sets:
        raise ValueError(f"the template cannot mark set(s) {missing_sets}")
    labels = layout.option_labels
    count = len(layout.questions)
    digits = layout.identifier_columns
    seed = config.seed
    keys = {code: _answer_key(seed, code, labels, count) for code in config.sets}
    wrong_questions: dict[str, tuple[int, ...]] = (
        {config.sets[1]: (7,)} if len(config.sets) > 1 else {}
    )
    scoring = ScoringRules(wrong_questions=wrong_questions)
    sources = config.source_labels

    candidates: list[Candidate] = []
    contents: list[Content] = []
    tasks: list[OperatorTask] = []
    # Each item: (content key, source, anchor content key or "", delay kind)
    placements: list[tuple[str, str, str, str]] = []
    counter = iter(range(1, 10_000_000))
    content_of: dict[str, Content] = {}

    offset_rng = _rng(seed, "offsets")

    def new_content(**kwargs: Any) -> Content:
        number = next(counter)
        if kwargs.get("kind") is ContentKind.RESCAN:
            kwargs.setdefault("offset", (
                round(offset_rng.choice((-1, 1)) * offset_rng.uniform(2.0, 5.0), 2),
                round(offset_rng.choice((-1, 1)) * offset_rng.uniform(1.0, 4.0), 2),
            ))
        item = Content(key=f"c{number:06d}", render_index=seed % 100_000 * 1_000 + number,
                       **kwargs)
        contents.append(item)
        content_of[item.key] = item
        return item

    when_rng = _rng(seed, "when")

    def when() -> tuple[When, float]:
        draw = when_rng.random()
        if draw < 0.6:
            return When.LIVE, round(when_rng.uniform(0.0, 6.0), 2)
        if draw < 0.85:
            return When.LATE, 0.0
        return When.FINAL, 0.0

    source_rng = _rng(seed, "sources")
    weights = [1.0 + 0.2 * ((index * 7) % 3) for index in range(len(sources))]

    def pick_source(*, other_than: str = "") -> str:
        choices = [s for s in sources if s != other_than] or list(sources)
        return source_rng.choices(choices, weights=[weights[sources.index(s)] for s in choices])[0]

    total_candidates = config.candidates_per_set * len(config.sets)
    late_assigned = False
    for set_index, code in enumerate(config.sets):
        rng = _rng(seed, "set", code)
        n = config.candidates_per_set
        rolls = [f"{set_index + 1}{number:0{digits - 1}d}" for number in range(1, n + 1)]
        quota = {
            Category.ABSENT: _quota(0.03, n, 1),
            Category.MISSING_SCRIPT: _quota(0.005, n, 1),
            Category.ABSENT_WITH_SCRIPT: _quota(0.005, n, 1),
            Category.DUPLICATE_SCRIPT: _quota(0.003, n, 1),
            Category.WRONG_ID: _quota(0.005, n, 1),
            Category.ID_BLANK: _quota(0.004, n, 1),
            Category.ID_MULTIPLE: _quota(0.004, n, 1),
            Category.FOLDED: _quota(0.005, n, 1),
            Category.CHAIN: 1 if set_index in (0, 2) else 0,
        }
        needed = sum(quota.values()) + quota[Category.WRONG_ID]  # victims too
        if needed * 2 > n:
            raise ValueError(f"{n} candidates per set is too few for the planted cases")
        order = list(range(n))
        rng.shuffle(order)
        assigned: dict[int, Category] = {}
        cursor = 0
        for category, number in quota.items():
            for _ in range(number):
                assigned[order[cursor]] = category
                cursor += 1
        for _ in range(quota[Category.WRONG_ID]):
            assigned[order[cursor]] = Category.WRONG_ID_VICTIM
            cursor += 1
        if not late_assigned:
            # One missing script turns up after the session was closed.
            missing = next(i for i in order if assigned.get(i) is Category.MISSING_SCRIPT)
            assigned[missing] = Category.LATE_FOUND
            late_assigned = True
        victims = [i for i in order if assigned.get(i) is Category.WRONG_ID_VICTIM]
        writers = [i for i in order if assigned.get(i) is Category.WRONG_ID]
        partner = dict(zip(writers, victims, strict=True))

        set_candidates: list[Candidate] = []
        for row in range(n):
            category = assigned.get(row, Category.NORMAL)
            blanks = 0
            multiples = 0
            draw = rng.random()
            if draw < 0.06:
                blanks = rng.randint(1, 3)
            elif draw < 0.10:
                multiples = rng.randint(1, 2)
            answers = _draw_answers(
                rng, keys[code], labels, blanks=blanks, multiples=multiples
            )
            candidate = Candidate(
                roll=rolls[row], set_code=code, name=f"CANDIDATE {rolls[row]}", row=row,
                absent_on_roster=category in (Category.ABSENT, Category.ABSENT_WITH_SCRIPT),
                category=category, answers=answers,
            )
            set_candidates.append(candidate)
        candidates.extend(set_candidates)

        for row, candidate in enumerate(set_candidates):
            category = candidate.category
            roll = candidate.roll
            rotation = round(rng.uniform(-1.5, 1.5), 2) if rng.random() < 0.15 else 0.0

            def script(
                *, _roll: str = roll, _code: str = code, _rotation: float = rotation,
                **extra: Any,
            ) -> Content:
                values: dict[str, Any] = {
                    "kind": ContentKind.SCRIPT, "candidate": _roll, "set_code": _code,
                    "written_roll": _roll, "bubbled_roll": _roll, "id_defect": IdDefect.NONE,
                    "rotation": _rotation,
                }
                values.update(extra)
                return new_content(**values)

            if category in (Category.ABSENT, Category.MISSING_SCRIPT):
                if category is Category.MISSING_SCRIPT:
                    tasks.append(OperatorTask(TaskKind.LIST_ABSENT, candidate=roll,
                                              set_code=code, when=When.FINAL,
                                              phase="first_close"))
                continue
            if category is Category.LATE_FOUND:
                item = script()
                placements.append((item.key, sources[0], "", "late"))
                tasks.append(OperatorTask(TaskKind.LIST_ABSENT, candidate=roll,
                                          set_code=code, when=When.FINAL, phase="first_close"))
                tasks.append(OperatorTask(TaskKind.LIST_PRESENT, candidate=roll,
                                          set_code=code, when=When.FINAL, phase="reopen"))
                continue
            if category is Category.WRONG_ID:
                victim = set_candidates[partner[row]].roll
                item = script(bubbled_roll=victim, id_defect=IdDefect.WRONG)
                placements.append((item.key, pick_source(), "", "base"))
                moment, delay = when()
                tasks.append(OperatorTask(TaskKind.CORRECT_ID, content=item.key, value=roll,
                                          when=moment, delay=delay))
                continue
            if category is Category.ID_BLANK:
                item = script(bubbled_roll="_" * digits, id_defect=IdDefect.BLANK,
                              expected_conflicts=("identifier_blank",))
                placements.append((item.key, pick_source(), "", "base"))
                moment, delay = when()
                tasks.append(OperatorTask(TaskKind.CORRECT_ID, content=item.key, value=roll,
                                          when=moment, delay=delay))
                continue
            if category is Category.ID_MULTIPLE:
                column = 1 + row % (digits - 1)
                item = script(bubbled_roll=_multiple_reading(roll, column),
                              id_defect=IdDefect.MULTIPLE,
                              expected_conflicts=("identifier_multiple",))
                placements.append((item.key, pick_source(), "", "base"))
                moment, delay = when()
                tasks.append(OperatorTask(TaskKind.CORRECT_ID, content=item.key, value=roll,
                                          when=moment, delay=delay))
                continue
            if category in (Category.FOLDED, Category.CHAIN):
                fold = _fold_for(template, row + set_index)
                original = new_content(
                    kind=ContentKind.FOLDED, candidate=roll, set_code=code, written_roll=roll,
                    bubbled_roll="", id_defect=IdDefect.NONE, fold=fold, registers=False,
                    expected_conflicts=("registration_failed",),
                    expected_quality="rescan_required",
                )
                source = pick_source()
                placements.append((original.key, source, "", "base"))
                moment, delay = when()
                tasks.append(OperatorTask(TaskKind.CONFIRM_RESCAN, content=original.key,
                                          value=roll, set_code=code, when=moment, delay=delay))
                first = new_content(
                    kind=ContentKind.RESCAN, candidate=roll, set_code=code, written_roll=roll,
                    bubbled_roll=roll, id_defect=IdDefect.NONE, rotation=rotation,
                )
                cross = rng.random() < 0.5
                placements.append((
                    first.key,
                    pick_source(other_than=source) if cross else source,
                    original.key,
                    "soon" if rng.random() < 0.5 else "later",
                ))
                tasks.append(OperatorTask(TaskKind.CONFIRM_REPLACEMENT, content=original.key,
                                          value=first.key, when=When.LIVE,
                                          delay=round(rng.uniform(0.0, 4.0), 2)))
                if category is Category.CHAIN:
                    second = new_content(
                        kind=ContentKind.RESCAN, candidate=roll, set_code=code,
                        written_roll=roll, bubbled_roll=roll, id_defect=IdDefect.NONE,
                        rotation=rotation,
                    )
                    placements.append((second.key, pick_source(other_than=source), first.key,
                                       "later"))
                    tasks.append(OperatorTask(TaskKind.REJECT_REPLACEMENT, content=first.key,
                                              value=roll, set_code=code, when=When.LIVE,
                                              delay=round(rng.uniform(0.0, 3.0), 2)))
                    tasks.append(OperatorTask(TaskKind.CONFIRM_REPLACEMENT, content=first.key,
                                              value=second.key, when=When.LIVE,
                                              delay=round(rng.uniform(0.0, 3.0), 2)))
                continue
            item = script()
            placements.append((item.key, pick_source(), "", "base"))
            if category is Category.ABSENT_WITH_SCRIPT:
                tasks.append(OperatorTask(TaskKind.LIST_PRESENT, candidate=roll,
                                          set_code=code, when=When.FINAL,
                                          phase="first_close"))
            if category is Category.DUPLICATE_SCRIPT:
                again = new_content(
                    kind=ContentKind.RESCAN, candidate=roll, set_code=code, written_roll=roll,
                    bubbled_roll=roll, id_defect=IdDefect.NONE, rotation=rotation,
                )
                placements.append((again.key, pick_source(other_than=placements[-1][1]),
                                   item.key, "later"))
                moment, delay = when()
                for key in (item.key, again.key):
                    tasks.append(OperatorTask(TaskKind.ACCEPT_DUPLICATE, content=key,
                                              when=moment, delay=delay))
                tasks.append(OperatorTask(TaskKind.EXCLUDE_SCRIPT, content=again.key,
                                          candidate=roll, set_code=code, when=When.FINAL,
                                          phase="first_close"))

    # --- scripts belonging to nobody, and stray blank pages ---------------
    unknown_rng = _rng(seed, "unknown")
    unknown_rolls: list[str] = []
    for number in range(_quota(0.001, total_candidates, 1)):
        roll = f"9{number + 1:0{digits - 1}d}"
        code = unknown_rng.choice(list(config.sets))
        unknown_rolls.append(roll)
        candidates.append(Candidate(
            roll=roll, set_code=code, name=f"UNKNOWN {roll}", row=-1, absent_on_roster=False,
            category=Category.UNKNOWN,
            answers=_draw_answers(unknown_rng, keys[code], labels, blanks=0, multiples=0),
            on_roster=False,
        ))
        item = new_content(
            kind=ContentKind.SCRIPT, candidate=roll, set_code=code, written_roll=roll,
            bubbled_roll=roll, id_defect=IdDefect.NONE,
        )
        placements.append((item.key, pick_source(), "", "base"))
        tasks.append(OperatorTask(TaskKind.DISMISS_UNKNOWN, content=item.key, candidate=roll,
                                  set_code=code, when=When.FINAL, phase="first_close"))
    for _number in range(_quota(0.001, total_candidates, 2)):
        page = new_content(
            kind=ContentKind.BLANK_PAGE, candidate="", set_code="", written_roll="",
            bubbled_roll="", id_defect=IdDefect.NONE, registers=False,
            expected_conflicts=("registration_failed",), expected_quality="rescan_required",
        )
        placements.append((page.key, pick_source(), "", "base"))
        moment, delay = when()
        tasks.append(OperatorTask(TaskKind.DISMISS_SUGGESTION, content=page.key, when=moment,
                                  delay=delay))
        tasks.append(OperatorTask(TaskKind.ACKNOWLEDGE_UNREADABLE, content=page.key,
                                  when=moment, delay=delay))

    arrivals = _schedule(config, placements, config.timing_seed, config.duration_seconds)
    plan = CampaignPlan(
        config=config,
        template_fingerprint=template_fingerprint(template),
        labels=tuple(labels),
        question_count=count,
        keys=keys,
        scoring=scoring,
        candidates=tuple(candidates),
        contents=tuple(contents),
        arrivals=arrivals,
        tasks=tuple(tasks),
        unknown_rolls=tuple(unknown_rolls),
    )
    if config.mode is Mode.RELEASE:
        main = len(plan.main_arrivals)
        if main < RELEASE_MIN_ARRIVALS:
            raise ValueError(
                f"a release plan must write at least {RELEASE_MIN_ARRIVALS:,} files; "
                f"this one writes {main:,}"
            )
        if config.sources < 3:
            raise ValueError("a release plan needs at least three sources")
    return plan


def answers_of(plan: CampaignPlan, content: Content) -> tuple[str, ...]:
    """The marks drawn on ``content`` (empty for a blank page)."""
    if content.kind is ContentKind.BLANK_PAGE:
        return ()
    candidate = plan.candidate(content.candidate)
    if candidate is None:  # pragma: no cover - every script belongs to a planned candidate
        raise KeyError(content.candidate)
    return candidate.answers


def _fold_for(template: OmrTemplate, index: int) -> tuple[str, float, float]:
    """A fold that covers a registration marker entirely (registration fails)."""
    from omr_scanner.evaluation import fold_plans
    from omr_scanner.imaging.folds import CORNER_ORDER

    width = float(template.page.canonical_width_px)
    height = float(template.page.canonical_height_px)
    outlines = fold_plans.marker_outlines(template, width=width, height=height)
    corner = CORNER_ORDER[index % len(CORNER_ORDER)]
    spec = fold_plans.solve_fold(
        corner, fold_plans.FoldCoverage.FULL_MARKER, outlines, width=width, height=height
    )
    if spec is None:  # pragma: no cover - the qualification template admits one everywhere
        raise ValueError(f"no marker-covering fold exists at {corner.value}")
    return (corner.value, round(spec.depth_x, 6), round(spec.depth_y, 6))


# ----------------------------------------------------------------------
# Timing: when each file arrives, and how it is written
# ----------------------------------------------------------------------
def _intensity(rng: random.Random, duration: float) -> list[float]:
    """Per-second arrival intensity: bursts, normal stretches and idle gaps."""
    seconds = max(1, int(duration))
    profile: list[float] = []
    while len(profile) < seconds:
        draw = rng.random()
        length = rng.randint(max(2, seconds // 60), max(4, seconds // 12))
        level = 3.0 if draw < 0.2 else (0.05 if draw < 0.35 else 1.0)
        profile.extend([level] * length)
    profile = profile[:seconds]
    tail = max(2, seconds // 30)
    for index in range(seconds - tail, seconds):
        profile[index] = 3.0  # end in a burst: the 99 % kill finds work in flight
    return profile


def _times(rng: random.Random, profile: list[float], count: int) -> list[float]:
    cumulative: list[float] = []
    total = 0.0
    for value in profile:
        total += value
        cumulative.append(total)
    out = []
    for _ in range(count):
        target = rng.uniform(0.0, total)
        second = bisect_left(cumulative, target)
        out.append(min(len(profile) - 0.001, second + rng.random()))
    return sorted(out)


def _pattern(rng: random.Random, quiet: float) -> tuple[WritePattern, tuple[float, ...], float]:
    draw = rng.random()
    if draw < 0.30:
        return WritePattern.ATOMIC, (), 0.0
    if draw < 0.55:
        chunks = rng.randint(3, 6)
        return WritePattern.STEPPED, tuple(
            round(rng.uniform(0.05, 0.5) * quiet, 3) for _ in range(chunks - 1)
        ), 0.0
    if draw < 0.65:
        return WritePattern.HEADER_FIRST, (round(rng.uniform(0.3, 0.7) * quiet, 3),), 0.0
    if draw < 0.80:
        chunks = rng.randint(2, 5)
        return WritePattern.HELD_OPEN, tuple(
            round(rng.uniform(0.1, 0.6) * quiet, 3) for _ in range(chunks - 1)
        ), round(rng.uniform(0.1, 0.4) * quiet, 3)
    if draw < 0.85:
        return WritePattern.LONG_PAUSE, (round(rng.uniform(1.3, 1.8) * quiet, 3),), 0.0
    chunks = rng.randint(1, 4)
    return WritePattern.RENAME, tuple(
        round(rng.uniform(0.05, 0.4) * quiet, 3) for _ in range(chunks - 1)
    ), 0.0


def _schedule(
    config: CampaignConfig,
    placements: Sequence[tuple[str, str, str, str]],
    timing_seed: int,
    duration: float,
) -> tuple[Arrival, ...]:
    """Times, names and write patterns for every placement, plus the byte copies."""
    rng = _rng(timing_seed, "schedule")
    sources = config.source_labels
    profiles = {source: _intensity(_rng(timing_seed, "profile", source), duration)
                for source in sources}
    base = [item for item in placements if item[3] == "base"]
    by_source: dict[str, list[str]] = {source: [] for source in sources}
    for key, source, _anchor, _kind in base:
        by_source[source].append(key)
    at: dict[str, float] = {}
    where: dict[str, str] = {}
    for source, keys in by_source.items():
        order = list(keys)
        _rng(timing_seed, "order", source).shuffle(order)
        for key, moment in zip(order, _times(rng, profiles[source], len(order)), strict=True):
            at[key] = moment
            where[key] = source
    late: list[tuple[str, str]] = []
    pending = [item for item in placements if item[3] != "base"]
    while pending:
        progress = False
        for item in list(pending):
            key, source, anchor, kind = item
            if kind == "late":
                late.append((key, source))
                pending.remove(item)
                progress = True
                continue
            if anchor not in at:
                continue
            gap = (rng.uniform(1.0, 0.05 * duration) if kind == "soon"
                   else rng.uniform(0.08 * duration, 0.35 * duration))
            at[key] = min(duration * 0.97, at[anchor] + gap)
            where[key] = source
            pending.remove(item)
            progress = True
        if not progress:  # pragma: no cover - placements are acyclic by construction
            raise ValueError(f"unanchored placements: {pending}")

    # Byte copies: the same bytes again, within a source and across sources.
    copy_rng = _rng(timing_seed, "copies")
    originals = sorted(at, key=lambda key: (at[key], key))
    copies: list[tuple[str, str, float]] = []
    for number in range(_quota(0.02, len(originals), 4)):
        key = copy_rng.choice(originals)
        source = where[key] if number % 2 == 0 else copy_rng.choice(
            [s for s in sources if s != where[key]] or list(sources)
        )
        moment = min(duration * 0.97, at[key] + copy_rng.uniform(0.5, 0.25 * duration))
        copies.append((key, source, moment))

    entries: list[tuple[float, str, str, bool]] = [
        (at[key], where[key], key, False) for key in originals
    ]
    entries += [(moment, source, key, False) for key, source, moment in copies]
    entries.sort(key=lambda item: (item[0], item[1], item[2]))
    counters = dict.fromkeys(sources, 0)
    pattern_rng = _rng(timing_seed, "patterns")
    quiet = config.stability.quiet_seconds
    arrivals: list[Arrival] = []
    for seq, (moment, source, key, _late) in enumerate(entries, start=1):
        counters[source] += 1
        pattern, pauses, hold = _pattern(pattern_rng, quiet)
        arrivals.append(Arrival(
            seq=seq, source=source, name=f"{counters[source]:06d}.png", content=key,
            at=round(moment, 3), pattern=pattern, pauses=pauses, hold_after=hold,
        ))
    # Some cross-source copies keep the original's file name: the same name
    # *and* the same bytes is a duplicate; the same name alone never is.
    renamed: list[Arrival] = []
    first_name: dict[str, str] = {}
    for arrival in arrivals:
        first_name.setdefault(arrival.content, arrival.name)
    taken = {(arrival.source, arrival.name) for arrival in arrivals}
    for index, arrival in enumerate(arrivals):
        original_name = first_name[arrival.content]
        if (
            arrival.name != original_name
            and index % 3 == 0
            and (arrival.source, original_name) not in taken
        ):
            taken.add((arrival.source, original_name))
            arrival = Arrival(**{**asdict(arrival), "name": original_name})
        renamed.append(arrival)
    for number, (key, source) in enumerate(late, start=1):
        renamed.append(Arrival(
            seq=len(renamed) + 1, source=source, name=f"late_{number:03d}.png", content=key,
            at=0.0, pattern=WritePattern.STEPPED, pauses=(0.2, 0.2), late=True,
        ))
    return tuple(renamed)


def retime(plan: CampaignPlan, timing_seed: int, duration: float) -> CampaignPlan:
    """The same logical cohort with a fresh arrival timeline (the control run).

    Contents, candidates, operator plan and source / name allocation are kept;
    only *when* each file arrives is redrawn and compressed into ``duration``.
    """
    from dataclasses import replace

    scale = duration / max(1.0, plan.config.duration_seconds)
    arrivals = tuple(
        item if item.late else replace(item, at=round(item.at * scale, 3))
        for item in plan.arrivals
    )
    # Scaling keeps every file in the order it had; the seed is recorded.
    return replace(plan, arrivals=arrivals,
                   config=replace(plan.config, timing_seed=timing_seed,
                                  duration_seconds=duration))


__all__ = [
    "PLAN_VERSION",
    "Arrival",
    "CampaignPlan",
    "Candidate",
    "Category",
    "Content",
    "ContentKind",
    "IdDefect",
    "OperatorTask",
    "ScoringRules",
    "TaskKind",
    "When",
    "WritePattern",
    "answers_of",
    "plan_campaign",
    "retime",
    "template_fingerprint",
]
