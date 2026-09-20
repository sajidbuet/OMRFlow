"""Export a generated workbook to PDF, through whatever engine is available.

Purpose:
    Turn a finished ``.xlsx`` report into a PDF an examination office can
    print or file, without ever pretending a PDF exists when it does not
    (phase brief §27, §28).

Why an abstraction:
    Python has no first-class way to render an Excel workbook to PDF with
    its formulas recalculated - that is genuinely Excel's or LibreOffice's
    job. :class:`PdfExporter` is the seam: :func:`detect_exporter` picks
    whichever real engine this machine has, and every caller (the service
    layer, the GUI worker, every test) depends on the seam, never on a
    concrete engine - which is what makes PDF export testable by injecting a
    fake exporter (§44) on a machine that has neither Excel nor LibreOffice.

What this module will not do:
    * Require Microsoft Excel. LibreOffice's ``soffice --headless`` is the
      primary, cross-platform path; a Windows-only Excel COM adapter is a
      natural addition later (see :class:`UnavailablePdfExporter`'s
      docstring) but is not implemented here, because untested COM code is
      worse than an honest "PDF unavailable" message.
    * Silently produce an empty or corrupt file and call it success.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omr_scanner.errors import ReportingError

_LOGGER = logging.getLogger(__name__)

PDF_EXPORT_TIMEOUT_SECONDS = 120
"""Generous ceiling for one workbook: LibreOffice's headless startup alone
can take several seconds, and a report is at most a few sheets of text -
never large enough to need longer than this in practice."""


class PdfExportError(ReportingError):
    """A PDF could not be produced. Always carries a ``user_message``.

    The `reporting` package's own error, per its documented contract
    (`reporting/__init__.py`): every failure here is a
    :class:`~omr_scanner.errors.ReportingError`.
    """


@dataclass(frozen=True, slots=True)
class PdfExportResult:
    """What one PDF export produced."""

    output_path: Path
    engine: str


class PdfExporter(Protocol):
    """What every PDF backend must do - the seam other code depends on."""

    @property
    def name(self) -> str:
        """Short, human-readable name of the engine ('LibreOffice', ...)."""
        ...

    def is_available(self) -> bool:
        """Whether this engine can actually be used on this machine."""
        ...

    def export(self, workbook_path: Path, output_pdf_path: Path) -> PdfExportResult:
        """Render ``workbook_path`` to ``output_pdf_path``.

        Raises:
            PdfExportError: The export could not be completed. Never returns
                a result for a PDF that was not actually, verifiably written
                (§27: "Do not pretend a PDF was successfully generated").
        """
        ...


class UnavailablePdfExporter:
    """The engine used when nothing can actually export a PDF.

    :meth:`is_available` is always ``False``; :meth:`export` always raises,
    with a message naming what to install - never a silent no-op and never a
    fabricated success (§27: "If no supported PDF engine exists: XLSX
    generation must still work; disable or gracefully fail PDF export").

    A Windows-only adapter using Excel via COM automation
    (``win32com.client``) would be a reasonable addition where ``pywin32``
    and a licensed Excel installation are both available, following exactly
    this protocol; it is not implemented here because this project has
    neither dependency, and an untested COM automation path is a worse
    default than an honest "install LibreOffice" message.
    """

    @property
    def name(self) -> str:
        """This engine's display name."""
        return "none"

    def is_available(self) -> bool:
        """Always ``False`` - this is the fallback when nothing else works."""
        return False

    def export(self, _workbook_path: Path, _output_pdf_path: Path) -> PdfExportResult:
        """Always raise, naming what an operator needs to install.

        Arguments are accepted (and ignored) only to satisfy
        :class:`PdfExporter`'s shape - there is nothing to export to.
        """
        raise PdfExportError(
            "No PDF export engine is available",
            user_message=(
                "PDF export requires LibreOffice to be installed (Microsoft "
                "Excel is not required for XLSX reports, only for PDF "
                "export through it - which is not supported in this build). "
                "Install LibreOffice, or use the generated .xlsx file "
                "directly and print or export to PDF from within it."
            ),
        )


class LibreOfficePdfExporter:
    """Render a workbook to PDF using LibreOffice's headless conversion.

    Args:
        executable: Path to ``soffice`` (or ``soffice.exe``). Autodetected
            from ``PATH`` and the usual Windows install locations when
            omitted, so a test can inject a fake path without touching the
            real environment (§44).

    LibreOffice recalculates every formula when it opens a workbook headless
    - the same ``RANK.EQ`` cells this project writes - which is why this
    engine, not a "render the cached values" shortcut, is what §26 requires.
    """

    def __init__(self, executable: str | Path | None = None) -> None:
        self._executable = str(executable) if executable is not None else None

    @property
    def name(self) -> str:
        """This engine's display name."""
        return "LibreOffice"

    def _resolve_executable(self) -> str | None:
        """Find ``soffice``, trying the usual places in order."""
        if self._executable:
            return self._executable if Path(self._executable).is_file() else None
        found = shutil.which("soffice") or shutil.which("soffice.exe")
        if found:
            return found
        for candidate in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            "/usr/bin/soffice",
            "/opt/libreoffice/program/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ):
            if Path(candidate).is_file():
                return candidate
        return None

    def is_available(self) -> bool:
        """Whether ``soffice`` could be found on this machine."""
        return self._resolve_executable() is not None

    def export(self, workbook_path: Path, output_pdf_path: Path) -> PdfExportResult:
        """Convert ``workbook_path`` to PDF via ``soffice --headless``.

        LibreOffice's headless converter names its output after the input
        file and writes it into a directory it is given - it cannot be told
        an exact output filename - so this converts into a private temporary
        directory and then moves the result to ``output_pdf_path``, which is
        also where the "does the caller's destination already exist" check
        belongs (the caller's, via
        :func:`omr_scanner.services.report_store.unique_output_path` -
        this function only ever writes the exact path it is given).
        """
        executable = self._resolve_executable()
        if executable is None:
            raise PdfExportError(
                "LibreOffice (soffice) was not found",
                user_message=(
                    "PDF export requires LibreOffice. Install it, or add its "
                    "installation folder to your PATH."
                ),
            )
        if not workbook_path.is_file():
            raise PdfExportError(
                f"Workbook not found: {workbook_path}",
                user_message=f"'{workbook_path.name}' could not be found for PDF export.",
            )

        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="omrflow_pdf_") as scratch:
            try:
                completed = subprocess.run(  # fixed argv, no shell involved
                    [
                        executable,
                        "--headless",
                        "--norestore",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        scratch,
                        str(workbook_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=PDF_EXPORT_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PdfExportError(
                    f"LibreOffice did not finish within {PDF_EXPORT_TIMEOUT_SECONDS}s",
                    user_message=(
                        "PDF export timed out. Close any open LibreOffice "
                        "window and try again."
                    ),
                ) from exc
            except OSError as exc:
                raise PdfExportError(
                    f"Could not start LibreOffice: {exc}",
                    user_message="LibreOffice could not be started for PDF export.",
                ) from exc

            produced = list(Path(scratch).glob("*.pdf"))
            if completed.returncode != 0 or not produced:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise PdfExportError(
                    f"LibreOffice conversion failed (exit {completed.returncode}): {detail}",
                    user_message=(
                        "LibreOffice could not convert the report to PDF. The "
                        "generated .xlsx file is unaffected."
                    ),
                )
            produced_file = produced[0]
            if produced_file.stat().st_size == 0:
                raise PdfExportError(
                    "LibreOffice produced an empty PDF",
                    user_message="PDF export produced an empty file and was discarded.",
                )
            shutil.move(str(produced_file), str(output_pdf_path))

        _LOGGER.info("PDF exported via LibreOffice: bytes=%d", output_pdf_path.stat().st_size)
        return PdfExportResult(output_path=output_pdf_path, engine=self.name)


def detect_exporter(preferred: str | Path | None = None) -> PdfExporter:
    """Return the best available PDF exporter for this machine.

    Args:
        preferred: An explicit ``soffice`` path (from application settings),
            tried before autodetection.

    Returns:
        :class:`LibreOfficePdfExporter` when ``soffice`` can be found,
        otherwise :class:`UnavailablePdfExporter` - callers check
        :meth:`PdfExporter.is_available` and act on it (§27: never silently
        skip PDF export without saying so).
    """
    libreoffice = LibreOfficePdfExporter(preferred)
    if libreoffice.is_available():
        return libreoffice
    return UnavailablePdfExporter()
