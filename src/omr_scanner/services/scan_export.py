"""Exporting a batch of recognition results as CSV.

Purpose:
    Write what a batch read into a file a spreadsheet, a statistics package or
    the next phase's importer can consume - deterministically, so that
    re-exporting the same batch produces a byte-identical file.

Responsibilities:
    * :func:`question_numbers` - the question columns a template implies.
    * :func:`build_rows` - the header and body as plain strings, with no I/O,
      which is what makes the layout testable without a temporary directory.
    * :func:`export_scan_results` - write those rows to a file.

What does NOT belong here:
    * Excel or PDF generation; those are Phase 9 and belong in
      :mod:`omr_scanner.reporting`. This module lives in ``services`` because it
      serialises a service's own result type
      (:class:`~omr_scanner.services.batch_processor.ProcessedScan`) and because
      writing the file is a side effect, which is what the services layer owns.
    * Marking, scoring or comparing against an answer key. A Phase 3 export says
      what was on the paper, nothing more.

Column layout::

    original_filename, output_filename, roll, set_code,
    registration_status, recognition_status, warning_count, Q1, Q2, ... QN

How a question cell reads:
    * ``b``    - one mark.
    * ``""``   - no mark.
    * ``b-d``  - two marks; **both** are kept, never reduced to one.
    * ``b?``   - a mark that was too faint or too close to its runner-up.
    * ``?``    - something was there but nothing could be called an answer, or
      the bubbles could not be measured at all.

    So the value column alone distinguishes every outcome, and the separate
    ``recognition_status`` column says whether the sheet as a whole needs
    attention. That is the "avoid making the default CSV unnecessarily complex"
    compromise: one cell per question, self-describing.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.domain.template import QuestionBlockFieldDefinition
from omr_scanner.errors import ReportingError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.batch_processor import ProcessedScan
    from omr_scanner.services.recognition_models import AnswerView

_LOGGER = logging.getLogger(__name__)

BASE_COLUMNS: tuple[str, ...] = (
    "original_filename",
    "output_filename",
    "roll",
    "set_code",
    "registration_status",
    "recognition_status",
    "warning_count",
    "value_source",
    "unresolved_conflicts",
)
"""The non-question columns, in order. Stable: later phases and external tools
read this file by column name, and reordering would break them silently.

``value_source`` and ``unresolved_conflicts`` were appended in Phase 6, at the
end, so a reader that indexes the earlier columns positionally still works.
``value_source`` is ``machine`` for a sheet nobody has reviewed and ``human``
once any value on it has been decided by a named reviewer; ``unresolved_conflicts``
is how many of that sheet's disputes are still waiting. Together they stop an
export presenting unreviewed ambiguity as finished data."""

CSV_ENCODING = "utf-8"
"""The file's text encoding. Values may contain any script the template does -
the repository's own sample sheet is labelled in Bengali."""

BOM_ENCODING = "utf-8-sig"
"""UTF-8 with a byte-order mark.

The default, because the overwhelmingly common consumer is Excel on Windows,
which reads a BOM-less UTF-8 CSV as the local code page and mangles every
non-ASCII label. The BOM is still UTF-8; a reader that does not want it opens
the file as ``utf-8-sig``."""


@dataclass(frozen=True, slots=True)
class SheetResolution:
    """The human decisions that apply to one sheet's exported row (Phase 6).

    Built by :func:`~omr_scanner.services.review_service.sheet_resolutions`
    from the project's conflict ledger, and read here. Nothing in this module
    knows how a decision was made or by whom - only what the effective value
    now is, which is the whole point of having one resolution layer rather
    than teaching every consumer about conflicts.

    Attributes:
        identifier: The decided candidate identifier, or ``""`` to keep the
            machine's.
        set_code: The decided set code, or ``""``.
        answers: Decided answers, keyed by printed question number.
        unresolved: How many of this sheet's conflicts still await a decision.
        reviewed: Whether a named reviewer decided anything on this sheet.
    """

    identifier: str = ""
    set_code: str = ""
    answers: Mapping[int, str] = field(default_factory=dict)
    unresolved: int = 0
    reviewed: bool = False

    @property
    def source(self) -> str:
        """``"human"`` when a reviewer decided anything here, else ``"machine"``."""
        return "human" if self.reviewed else "machine"

    def answer_for(self, number: int, machine: Mapping[int, AnswerView]) -> str:
        """The value to export for one question.

        A human decision wins; otherwise the machine's own display value, which
        keeps every convention Phase 3 established - ``""`` for blank, ``"b-d"``
        for a double mark, ``"?"`` for something that could not be called.
        """
        if number in self.answers:
            return self.answers[number]
        found = machine.get(number)
        return found.display_value if found is not None else ""


EMPTY_RESOLUTION = SheetResolution()
"""What a sheet nobody has reviewed resolves to: the machine's own values."""


def question_numbers(template: OmrTemplate) -> tuple[int, ...]:
    """Return every question number the template defines, in ascending order.

    Args:
        template: The template the batch was read with.

    Returns:
        The numbers, de-duplicated. Order comes from the numbers themselves
        rather than from zone order, so a template whose printed columns are
        stored out of order still exports ``Q1`` before ``Q2``.
    """
    numbers: set[int] = set()
    for zone in template.zones:
        field = zone.field
        if isinstance(field, QuestionBlockFieldDefinition):
            numbers.update(range(field.first_question, field.last_question + 1))
    return tuple(sorted(numbers))


def build_rows(
    processed: Sequence[ProcessedScan],
    template: OmrTemplate,
    *,
    resolutions: Mapping[Path, SheetResolution] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Build the header row and one row per scan.

    Args:
        processed: The batch's outcomes, in the order they should appear.
        template: The template, which fixes the question columns - so two
            batches of the same examination export the same columns even when
            one of them happens to contain no answer to Q57.
        resolutions: Human decisions to apply, keyed by source path. Omitted
            (the default) exports exactly what the machine read, which is what
            a caller with no project open gets.

    Returns:
        The header followed by one row per scan, every cell already a string.

    **A correction replaces the exported value, never the machine's record.**
    ``resolutions`` is read, never written; the ``ProcessedScan`` objects are
    untouched, and what recognition read stays retrievable in the project
    database whatever this file says. That is the whole point of routing
    corrections through :class:`SheetResolution` rather than editing results
    before export.
    """
    numbers = question_numbers(template)
    header = (*BASE_COLUMNS, *(f"Q{number}" for number in numbers))
    overrides = resolutions or {}

    rows: list[tuple[str, ...]] = [header]
    for item in processed:
        result = item.result
        answers = {answer.number: answer for answer in result.answers}
        decided = overrides.get(result.source_path, EMPTY_RESOLUTION)
        rows.append(
            (
                result.source_path.name,
                item.output_name,
                decided.identifier or result.identifier_value,
                decided.set_code or result.set_code_value,
                result.registration.value,
                result.outcome.value,
                str(result.warning_count),
                decided.source,
                str(decided.unresolved),
                *(
                    decided.answer_for(number, answers)
                    for number in numbers
                ),
            )
        )
    return tuple(rows)


def write_scan_results(
    processed: Sequence[ProcessedScan],
    template: OmrTemplate,
    stream: io.TextIOBase,
    *,
    resolutions: Mapping[Path, SheetResolution] | None = None,
) -> None:
    """Write the CSV body into an already-open text stream.

    Args:
        processed: The batch's outcomes.
        template: The template that fixes the columns.
        stream: A text stream opened with ``newline=""``; the csv module
            supplies line endings itself, and letting Python translate them too
            produces blank lines between records on Windows.
        resolutions: Human decisions to apply; see :func:`build_rows`.
    """
    writer = csv.writer(stream)
    writer.writerows(build_rows(processed, template, resolutions=resolutions))


def export_scan_results(
    processed: Sequence[ProcessedScan],
    template: OmrTemplate,
    path: Path,
    *,
    include_bom: bool = True,
    resolutions: Mapping[Path, SheetResolution] | None = None,
) -> Path:
    """Write a batch's results to ``path``.

    Args:
        processed: The batch's outcomes, in export order.
        template: The template that fixes the question columns.
        path: Destination file. A ``.csv`` suffix is appended when missing.
        include_bom: Write UTF-8 with a byte-order mark. See
            :data:`BOM_ENCODING` for why that is the default.
        resolutions: Human decisions to apply; see :func:`build_rows`.

    Returns:
        The path actually written.

    Raises:
        ReportingError: The file could not be written.
    """
    destination = path if path.suffix else path.with_suffix(".csv")
    encoding = BOM_ENCODING if include_bom else CSV_ENCODING
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary file in the same directory and moved into
        # place only once it is complete (the same "an interrupted save
        # cannot truncate an existing file" guarantee
        # `omr_scanner.utils.json_io.write_json_atomic` gives project.json -
        # re-exporting a results CSV over a previous one must not leave a
        # half-written file where a good one used to be if the write is
        # interrupted, e.g. by a full disk).
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
                write_scan_results(processed, template, stream, resolutions=resolutions)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise ReportingError(
            f"Could not write the results CSV to '{destination}': {exc}",
            user_message=f"The results could not be saved to '{destination.name}'.",
        ) from exc

    _LOGGER.info("Exported %d scan result(s) to %s", len(processed), destination)
    return destination


def render_scan_results(
    processed: Sequence[ProcessedScan],
    template: OmrTemplate,
    *,
    resolutions: Mapping[Path, SheetResolution] | None = None,
) -> str:
    """Return the CSV as a string, for tests and for previewing before saving."""
    buffer = io.StringIO(newline="")
    write_scan_results(processed, template, buffer, resolutions=resolutions)
    return buffer.getvalue()


__all__ = [
    "BASE_COLUMNS",
    "BOM_ENCODING",
    "CSV_ENCODING",
    "EMPTY_RESOLUTION",
    "SheetResolution",
    "build_rows",
    "export_scan_results",
    "question_numbers",
    "render_scan_results",
    "write_scan_results",
]
