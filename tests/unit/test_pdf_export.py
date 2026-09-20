"""PDF export abstraction (Phase 9, §27, §44).

Scope:
    :mod:`omr_scanner.reporting.pdf` - the exporter protocol, the
    unavailable-engine fallback, and (when LibreOffice is genuinely present
    on the machine running the tests) a real conversion.

Testable without Excel or LibreOffice (§44):
    Every orchestration-style test here injects a fake exporter rather than
    depending on a real engine, so the suite never fails merely because
    neither Office product is installed. The one test that needs a real
    engine is guarded by ``shutil.which`` and skips, loudly, when absent.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from omr_scanner.reporting.pdf import (
    LibreOfficePdfExporter,
    PdfExportError,
    PdfExportResult,
    UnavailablePdfExporter,
    detect_exporter,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakePdfExporter:
    """A dependency-injected stand-in used by orchestration tests."""

    available: bool = True
    should_fail: bool = False
    calls: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        """Give ``calls`` a fresh list rather than sharing a mutable default."""
        if self.calls is None:
            self.calls = []

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return self.available

    def export(self, workbook_path, output_pdf_path) -> PdfExportResult:
        self.calls.append((str(workbook_path), str(output_pdf_path)))
        if self.should_fail:
            raise PdfExportError("fake failure", user_message="fake failure")
        output_pdf_path.write_bytes(b"%PDF-1.4 fake\n%%EOF")
        return PdfExportResult(output_path=output_pdf_path, engine=self.name)


# ----------------------------------------------------------------------
class TestUnavailableExporter:
    def test_is_never_available(self):
        assert UnavailablePdfExporter().is_available() is False

    def test_exporting_raises_with_an_actionable_message(self):
        exporter = UnavailablePdfExporter()
        with pytest.raises(PdfExportError) as excinfo:
            exporter.export(None, None)  # type: ignore[arg-type]
        assert "LibreOffice" in excinfo.value.user_message

    def test_never_produces_a_file(self, tmp_path: Path):
        exporter = UnavailablePdfExporter()
        output = tmp_path / "out.pdf"
        with pytest.raises(PdfExportError):
            exporter.export(tmp_path / "in.xlsx", output)
        assert not output.exists()


# ----------------------------------------------------------------------
class TestDetectExporter:
    def test_returns_unavailable_when_soffice_cannot_be_found(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        exporter = LibreOfficePdfExporter("/definitely/not/a/real/path")
        assert exporter.is_available() is False

    def test_a_missing_explicit_path_falls_back_to_autodetection(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        exporter = detect_exporter(preferred="/does/not/exist/soffice")
        # Either genuinely unavailable, or LibreOffice really is installed on
        # this machine at a standard location - both are correct outcomes;
        # what matters is it never crashes and never claims a fake path works.
        assert exporter.name in ("none", "LibreOffice")


# ----------------------------------------------------------------------
class TestFakeExporterContract:
    """Confirm the test double behaves like a real exporter.

    The fake used by report_store's orchestration tests must satisfy the
    same contract these tests care about: writing a real file on success and
    nothing at all on failure.
    """

    def test_a_successful_export_writes_a_non_empty_file(self, tmp_path: Path):
        exporter = FakePdfExporter()
        output = tmp_path / "out.pdf"
        result = exporter.export(tmp_path / "in.xlsx", output)
        assert output.exists()
        assert output.stat().st_size > 0
        assert result.output_path == output

    def test_a_forced_failure_raises_and_writes_nothing(self, tmp_path: Path):
        exporter = FakePdfExporter(should_fail=True)
        output = tmp_path / "out.pdf"
        with pytest.raises(PdfExportError):
            exporter.export(tmp_path / "in.xlsx", output)
        assert not output.exists()


# ----------------------------------------------------------------------
@pytest.mark.skipif(
    shutil.which("soffice") is None and shutil.which("soffice.exe") is None,
    reason="LibreOffice (soffice) is not installed in this environment",
)
class TestRealLibreOfficeConversion:
    """Runs only where LibreOffice is genuinely present (§44)."""

    def test_a_real_workbook_converts_to_a_nonempty_pdf(self, tmp_path: Path):
        import openpyxl

        workbook = openpyxl.Workbook()
        workbook.active.append(["Roll", "Marks"])
        workbook.active.append(["1", 90])
        source = tmp_path / "sheet.xlsx"
        workbook.save(source)

        exporter = LibreOfficePdfExporter()
        output = tmp_path / "sheet.pdf"
        result = exporter.export(source, output)

        assert output.exists()
        assert output.stat().st_size > 0
        assert output.read_bytes().startswith(b"%PDF")
        assert result.engine == "LibreOffice"
