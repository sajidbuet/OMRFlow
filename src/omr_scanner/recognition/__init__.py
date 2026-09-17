"""Interpretation of bubble measurements as OMR values.

Purpose:
    Turn the per-bubble measurements :mod:`omr_scanner.imaging.metrics` produces
    into logical values - a roll number, a set code, an answer per question -
    each with an explicit representation of "missing" and "multiple" marks and
    the evidence behind the decision.

Modules:
    * ``models.py``  - the result vocabulary: statuses, readings, decisions,
      field and sheet results.
    * ``decide.py``  - the single decision rule for one response group.
    * ``fields.py``  - grid arithmetic and assembly of groups into fields.

What does NOT belong here:
    * Pixel access or OpenCV calls; this layer consumes measurements, it does
      not produce them.
    * Database writes or GUI code. Persisting or displaying a recognition
      result is a service or GUI responsibility, and
      :mod:`omr_scanner.services.recognition_service` is the seam that joins
      alignment, measurement and interpretation into one operation.

Guarantees:
    * A result never silently discards an ambiguity. Two marks in one group are
      both reported (``"B-D"``) and the group is flagged
      :attr:`~omr_scanner.recognition.models.MarkStatus.MULTIPLE`; a mark too
      faint to accept produces no value at all rather than a guess.
    * Thresholds come from the template
      (:class:`~omr_scanner.domain.template.RecognitionSettings`), never from
      module-level constants in this package.
    * Nothing here assumes a set code is a single character, or that answer
      labels are ``A``-``D``: both come from the template.
"""

from omr_scanner.recognition.decide import (
    decide_group,
    reading_from_measurement,
    readings_from_measurements,
)
from omr_scanner.recognition.fields import (
    ZoneGroup,
    choose_identifier_zone,
    choose_set_code_zone,
    field_status,
    recognise_grid_zone,
    recognise_question_zone,
    recognise_template,
    zone_groups,
)
from omr_scanner.recognition.models import (
    BLANK_CHARACTER,
    BLANK_VALUE,
    MULTIPLE_MARK_SEPARATOR,
    UNRESOLVED_CHARACTER,
    BubbleReading,
    FieldResult,
    FieldStatus,
    GroupDecision,
    MarkStatus,
    QuestionAnswer,
    SheetRecognition,
)

__all__ = [
    "BLANK_CHARACTER",
    "BLANK_VALUE",
    "MULTIPLE_MARK_SEPARATOR",
    "UNRESOLVED_CHARACTER",
    "BubbleReading",
    "FieldResult",
    "FieldStatus",
    "GroupDecision",
    "MarkStatus",
    "QuestionAnswer",
    "SheetRecognition",
    "ZoneGroup",
    "choose_identifier_zone",
    "choose_set_code_zone",
    "decide_group",
    "field_status",
    "reading_from_measurement",
    "readings_from_measurements",
    "recognise_grid_zone",
    "recognise_question_zone",
    "recognise_template",
    "zone_groups",
]
