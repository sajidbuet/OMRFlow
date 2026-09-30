"""A deterministic multi-set examination for the Results dashboard.

Used by ``tests/gui/test_results_dashboard.py`` (small cohorts) and by
``scripts/acceptance_results_dashboard.py`` (the manual-acceptance project:
three sets, 100 questions, 60-90 candidates a set).

Shape:
    One processed batch holding every set's scripts - as a real session
    produces - with one attendance workbook and one candidate list per set,
    a *different* verified answer key per set, real reconciliation and real
    scoring. No sheet images: recognition results are stored directly, the
    same way the other service-level suites store them, because nothing on
    the dashboard reads an image.

What makes it checkable rather than merely plausible:
    Each candidate has a fixed ability, and every question a fixed
    difficulty; a candidate answers a question correctly when a seeded
    logistic draw says so. Eight questions are then *designed* so that each
    dashboard feature has something definite to show - the easiest, the
    hardest, a negative-discrimination item, a high blank rate, a strong and
    a weak distractor, and a distractor more popular than the key. Their
    numbers are in :data:`DESIGNED` and the tests assert on them.

Privacy:
    Every roll number and name is fictional.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import openpyxl
from tests.conftest import build_answer_sheet_template

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    BubbleGrid,
    FieldType,
    OmrTemplate,
    QuestionBlockFieldDefinition,
    SymbolAxis,
    Zone,
)
from omr_scanner.services import (
    batch_store,
    candidate_import,
    project_sets,
    reconciliation_store,
    save_template,
    scoring_store,
    set_active_template,
    set_attendance,
)
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.recognition_models import (
    AnswerView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from omr_scanner.services import ProjectSession

OPERATOR = "Dr. Dashboard Operator"
LABELS = ("A", "B", "C", "D")
HEADERS = ("Sl.No.", "Roll No.", "Name", "Total", "Merit")
HEADER_ROW = 3

DESIGNED: dict[str, int] = {
    "very_easy": 7,
    "easy": 14,
    "negative_discrimination": 37,
    "high_blank": 51,
    "strong_distractor": 62,
    "weak_distractor": 75,
    "very_difficult": 83,
    "distractor_over_key": 90,
}
"""Question numbers with a deliberate behaviour, on a 100-question paper.

On a shorter paper each is taken modulo the question count (see
:func:`designed_numbers`)."""


def designed_numbers(question_count: int) -> dict[str, int]:
    """:data:`DESIGNED`, folded onto a paper of ``question_count`` questions."""
    if question_count >= 100:
        return dict(DESIGNED)
    used: set[int] = set()
    found: dict[str, int] = {}
    for name, number in DESIGNED.items():
        folded = (number - 1) % question_count + 1
        while folded in used:
            folded = folded % question_count + 1
        used.add(folded)
        found[name] = folded
    return found


def build_template(question_count: int = 100, *, name: str = "Dashboard sheet") -> OmrTemplate:
    """A template with ``question_count`` questions in five compact columns.

    The identifier and set-code fields are the synthetic sheet's; only the
    question grid is packed tighter, so a 100-question paper fits the page.
    """
    base = build_answer_sheet_template(
        name=name, roll_digits=8, set_symbols=("10", "11", "12"),
        question_blocks=1, questions_per_block=10,
    )
    blocks = 5
    per_block = math.ceil(question_count / blocks)
    column_pitch, row_pitch, gap = 0.030, 0.021, 0.17
    bubble = NormalizedSize(width=0.020, height=0.014)
    zones = [zone for zone in base.zones if not zone.id.startswith("questions_")]
    first = 1
    for index in range(blocks):
        count = min(per_block, question_count - first + 1)
        if count <= 0:
            break
        origin = NormalizedPoint(x=0.06 + index * gap, y=0.50)
        zones.append(
            Zone(
                id=f"questions_{index}",
                label=f"Questions {first}-{first + count - 1}",
                bounds=NormalizedRect(
                    x=origin.x - bubble.width,
                    y=origin.y - bubble.height,
                    width=column_pitch * (len(LABELS) - 1) + 2 * bubble.width,
                    height=row_pitch * (count - 1) + 2 * bubble.height,
                ),
                field=QuestionBlockFieldDefinition(
                    type=FieldType.QUESTION_BLOCK, first_question=first,
                    question_count=count, answer_labels=LABELS,
                    symbol_axis=SymbolAxis.HORIZONTAL, group_id="questions",
                ),
                grid=BubbleGrid(
                    origin=origin, column_pitch=column_pitch, row_pitch=row_pitch,
                    bubble_size=bubble,
                ),
            )
        )
        first += count
    data = base.model_dump()
    data["zones"] = [zone.model_dump() for zone in zones]
    return OmrTemplate.model_validate(data)


@dataclass
class SetDesign:
    """One set: its code, cohort size, ability shift and key."""

    code: str
    size: int
    shift: float = 0.0
    absent_every: int = 15
    key: str = ""
    rolls: list[str] = field(default_factory=list)
    absent: set[str] = field(default_factory=set)


@dataclass
class DashboardExam:
    """What was built, for assertions."""

    template: OmrTemplate
    batch_id: str
    rosters: dict[str, int]
    sets: dict[str, SetDesign]
    designed: dict[str, int]
    scored: dict[str, int]
    """Per set, how many candidates the builder expects to be scored."""

    @property
    def question_count(self) -> int:
        """Questions on the paper."""
        return plan_for(self.template).question_count


def _key_for(code: str, question_count: int) -> str:
    """A different, deterministic key per set."""
    rng = random.Random(f"key-{code}")
    return "".join(rng.choice(LABELS) for _ in range(question_count))


def _difficulties(question_count: int, designed: dict[str, int]) -> dict[int, float]:
    """A fixed difficulty per question, from easy to hard, then the designed ones."""
    rng = random.Random("difficulty")
    found = {number: rng.uniform(-2.4, 0.9) for number in range(1, question_count + 1)}
    found[designed["very_easy"]] = -4.0
    found[designed["easy"]] = -2.2
    found[designed["very_difficult"]] = 3.2
    return found


def answers_for(
    design: SetDesign, index: int, ability: float, question_count: int,
    difficulties: dict[int, float], designed: dict[str, int],
) -> str:
    """One candidate's answer string: key letters where right, a distractor where not."""
    rng = random.Random(f"{design.code}-{index}")
    key = design.key
    out: list[str] = []
    for number in range(1, question_count + 1):
        expected = key[number - 1]
        others = [label for label in LABELS if label != expected]
        p = 1.0 / (1.0 + math.exp(-1.1 * (ability - difficulties[number])))
        right = rng.random() < p
        if number == designed["negative_discrimination"]:
            right = ability < -0.3
        if number == designed["high_blank"] and ability < -0.35:
            out.append("")
            continue
        if number == designed["distractor_over_key"]:
            draw = rng.random()
            out.append(expected if draw < 0.25 else others[0] if draw < 0.80 else others[1])
            continue
        if right:
            out.append(expected)
        elif number == designed["strong_distractor"]:
            out.append(others[1])
        elif number == designed["weak_distractor"]:
            out.append(others[0] if rng.random() < 0.5 else others[1])
        else:
            out.append(rng.choice(others))
    return "|".join(out)


def _write_attendance(path: Path, design: SetDesign) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = f"Set {design.code}"
    for column, header in enumerate(HEADERS, start=1):
        sheet.cell(row=HEADER_ROW, column=column, value=header)
    for offset, roll in enumerate(design.rolls):
        row = HEADER_ROW + 1 + offset
        sheet.cell(row=row, column=1, value=offset + 1)
        sheet.cell(row=row, column=2, value=roll)
        sheet.cell(row=row, column=3, value=f"CANDIDATE {roll}")
        if roll in design.absent:
            sheet.cell(row=row, column=4, value="ABSENT")
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    workbook.close()
    return path


def _result(path: Path, roll: str, code: str, answers: list[str], numbers) -> ScanResult:
    return ScanResult(
        source_path=path,
        outcome=RecognitionOutcome.COMPLETE,
        registration=RegistrationStatus.REGISTERED,
        fields=(
            FieldView(
                zone_id="roll_number", label="Roll", field_type="numeric", value=roll,
                status="complete", needs_review=False, characters=(),
            ),
            FieldView(
                zone_id="set_code", label="Set", field_type="set_code", value=code,
                status="complete", needs_review=False, characters=(),
            ),
        ),
        answers=tuple(
            AnswerView(
                number=number, zone_id="q", value=value,
                status="resolved" if value else "blank", needs_review=False,
                top_fill=0.9 if value else 0.05, margin=0.4, confidence=0.9,
            )
            for number, value in zip(numbers, answers, strict=True)
        ),
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )


def build_dashboard_exam(
    session: ProjectSession,
    tmp_path: Path,
    *,
    sizes: dict[str, int] | None = None,
    question_count: int = 100,
    empty_sets: tuple[str, ...] = (),
    score: bool = True,
) -> DashboardExam:
    """Build, reconcile, key and (by default) score the examination.

    Args:
        session: An open project.
        tmp_path: Where the attendance workbooks and sheet files go.
        sizes: Candidates per set; the defaults are the acceptance sizes.
        question_count: Questions on the paper.
        empty_sets: Extra sets defined with no candidates or key at all.
        score: Whether to run scoring at the end.
    """
    from omr_scanner.database.models import BatchScan, ScanBatch

    sizes = sizes or {"10": 60, "11": 75, "12": 90}
    template = build_template(question_count)
    written = save_template(template, session.project.layout.templates_dir / "dashboard.omrt")
    set_active_template(session, written)
    plan = plan_for(template)
    designed = designed_numbers(question_count)
    difficulties = _difficulties(question_count, designed)
    shifts = {"10": 0.15, "11": -0.2, "12": 0.0}

    designs: dict[str, SetDesign] = {}
    rosters: dict[str, int] = {}
    for position, (code, size) in enumerate(sizes.items()):
        design = SetDesign(code=code, size=size, shift=shifts.get(code, 0.0))
        design.key = _key_for(code, question_count)
        design.rolls = [f"{code}{position + 1}{index:05d}" for index in range(1, size + 1)]
        design.absent = {
            roll for index, roll in enumerate(design.rolls, start=1)
            if index % design.absent_every == 0
        }
        exam_set = project_sets.add_set(session.database, code, f"Paper {code}")
        attendance = _write_attendance(tmp_path / f"set{code}_attendance.xlsx", design)
        assignment = set_attendance.assign_attendance_workbook(
            session.database, exam_set.set_id, attendance,
            candidate_import.read_roster(attendance), imported_by=OPERATOR,
        )
        rosters[code] = assignment.roster_id
        designs[code] = design
    for code in empty_sets:
        project_sets.add_set(session.database, code, f"Paper {code} (no candidates)")

    scans_dir = session.project.layout.scans_original_dir
    scans_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    scored: dict[str, int] = {}
    with session.database.session() as db:
        total = sum(len(d.rolls) - len(d.absent) for d in designs.values())
        db.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(scans_dir), status="completed", total_scans=total,
            )
        )
        db.flush()
        batch_index = 0
        for design in designs.values():
            present = [roll for roll in design.rolls if roll not in design.absent]
            scored[design.code] = len(present)
            for index, roll in enumerate(present):
                # Abilities evenly spread, shifted per set.
                ability = -1.4 + 2.8 * (index + 0.5) / len(present) + design.shift
                answers = answers_for(
                    design, index, ability, question_count, difficulties, designed
                ).split("|")
                name = f"set{design.code}_{index:04d}.png"
                path = scans_dir / name
                path.write_bytes(f"scan:{name}".encode())
                result = _result(path, roll, design.code, answers, plan.numbers)
                db.add(
                    BatchScan(
                        batch_id=batch_id, batch_index=batch_index, source_path=str(path),
                        filename=name, status="completed", identifier_value=roll,
                        set_code_value=design.code,
                        result_json=json.dumps(result.to_dict()),
                    )
                )
                batch_index += 1

    for code, design in designs.items():
        stored = scoring_store.save_key(
            session.database, read_key(design.key, plan, code).to_key(), template=template
        )
        scoring_store.verify_key(
            session.database, stored.key_id, verified_by=OPERATOR, plan=plan
        )
        reconciliation_store.reconcile_batch(session.database, rosters[code], batch_id)
        if score:
            scoring_store.score_batch(
                session.database, rosters[code], batch_id, template, computed_by=OPERATOR
            )
    return DashboardExam(
        template=template, batch_id=batch_id, rosters=rosters, sets=designs,
        designed=designed, scored=scored,
    )
