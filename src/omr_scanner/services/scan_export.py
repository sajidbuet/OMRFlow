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
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.domain.template import QuestionBlockFieldDefinition
from omr_scanner.errors import ReportingError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.batch_processor import ProcessedScan

_LOGGER = logging.getLogger(__name__)

BASE_COLUMNS: tuple[str, ...] = (
    "original_filename",
    "output_filename",
    "roll",
    "set_code",
    "registration_status",
    "recognition_status",
    "warning_count",
)
"""The non-question columns, in order. Stable: later phases and external tools
read this file by column name, and reordering would break them silently."""

CSV_ENCODING = "utf-8"
"""The file's text encoding. Values may contain any script the template does -
the repository's own sample sheet is labelled in Bengali."""

BOM_ENCODING = "utf-8-sig"
"""UTF-8 with a byte-order mark.

The default, because the overwhelmingly common consumer is Excel on Windows,
which reads a BOM-less UTF-8 CSV as the local code page and mangles every
non-ASCII label. The BOM is still UTF-8; a reader that does not want it opens
the file as ``utf-8-sig``."""


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
    processed: Sequence[ProcessedScan], template: OmrTemplate
) -> tuple[tuple[str, ...], ...]:
    """Build the header row and one row per scan.

    Args:
        processed: The batch's outcomes, in the order they should appear.
        template: The template, which fixes the question columns - so two
            batches of the same examination export the same columns even when
            one of them happens to contain no answer to Q57.

    Returns:
        The header followed by one row per scan, every cell already a string.
    """
    numbers = question_numbers(template)
    header = (*BASE_COLUMNS, *(f"Q{number}" for number in numbers))

    rows: list[tuple[str, ...]] = [header]
    for item in processed:
        result = item.result
        answers = {answer.number: answer for answer in result.answers}
        rows.append(
            (
                result.source_path.name,
                item.output_name,
                result.identifier_value,
                result.set_code_value,
                result.registration.value,
                result.outcome.value,
                str(result.warning_count),
                *(
                    answers[number].display_value if number in answers else ""
                    for number in numbers
                ),
            )
        )
    return tuple(rows)


def write_scan_results(
    processed: Sequence[ProcessedScan], template: OmrTemplate, stream: io.TextIOBase
) -> None:
    """Write the CSV body into an already-open text stream.

    Args:
        processed: The batch's outcomes.
        template: The template that fixes the columns.
        stream: A text stream opened with ``newline=""``; the csv module
            supplies line endings itself, and letting Python translate them too
            produces blank lines between records on Windows.
    """
    writer = csv.writer(stream)
    writer.writerows(build_rows(processed, template))


def export_scan_results(
    processed: Sequence[ProcessedScan],
    template: OmrTemplate,
    path: Path,
    *,
    include_bom: bool = True,
) -> Path:
    """Write a batch's results to ``path``.

    Args:
        processed: The batch's outcomes, in export order.
        template: The template that fixes the question columns.
        path: Destination file. A ``.csv`` suffix is appended when missing.
        include_bom: Write UTF-8 with a byte-order mark. See
            :data:`BOM_ENCODING` for why that is the default.

    Returns:
        The path actually written.

    Raises:
        ReportingError: The file could not be written.
    """
    destination = path if path.suffix else path.with_suffix(".csv")
    encoding = BOM_ENCODING if include_bom else CSV_ENCODING
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding=encoding, newline="") as stream:
            write_scan_results(processed, template, stream)
    except OSError as exc:
        raise ReportingError(
            f"Could not write the results CSV to '{destination}': {exc}",
            user_message=f"The results could not be saved to '{destination.name}'.",
        ) from exc

    _LOGGER.info("Exported %d scan result(s) to %s", len(processed), destination)
    return destination


def render_scan_results(
    processed: Sequence[ProcessedScan], template: OmrTemplate
) -> str:
    """Return the CSV as a string, for tests and for previewing before saving."""
    buffer = io.StringIO(newline="")
    write_scan_results(processed, template, buffer)
    return buffer.getvalue()


__all__ = [
    "BASE_COLUMNS",
    "BOM_ENCODING",
    "CSV_ENCODING",
    "build_rows",
    "export_scan_results",
    "question_numbers",
    "render_scan_results",
    "write_scan_results",
]
