"""Background report generation, so a large cohort does not freeze the GUI.

Follows the pattern Phase 8's ``ScoringWorker`` established: a single
``QThread`` running one service call, one ``ready`` signal carrying the
result, no Qt object touched off the GUI thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from PySide6.QtCore import QObject

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.reporting.pdf import PdfExporter
    from omr_scanner.services import ProjectDatabase
    from omr_scanner.services.report_store import GenerationOutcome

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReportJob:
    """One set's worth of work for a generation run.

    :attr:`set_id` is what makes a job belong to a *defined* set: given one,
    the run resolves that set's own attendance roster and its own result
    template and can reach nothing else (Part 2 §13/§15). It is empty only
    for a set this project knows about from evidence alone - a code found on
    scored scripts or an answer key in a project that has never had sets
    defined - which is generated the pre-Part-2 way, against the project's
    single unscoped roster.
    """

    set_code: str
    kind: str  # "xlsx", "pdf_rollwise", "pdf_meritwise"
    set_id: str = ""


@dataclass(frozen=True, slots=True)
class ReportRunResult:
    """What one worker run produced - one outcome per requested job."""

    outcomes: tuple[GenerationOutcome, ...] = field(default_factory=tuple)
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether the run completed without an unexpected failure.

        Individual jobs can still have failed - that is what
        :attr:`~omr_scanner.services.report_store.GenerationOutcome.ok`
        reports per job (§33: one set's failure must never abort the rest).
        """
        return not self.error


class ReportGenerationWorker(QThread):
    """Generate one or more sets' reports without blocking the interface."""

    ready = Signal(object)
    """Emitted with a :class:`ReportRunResult` when the run finishes."""

    progressed = Signal(int, int)
    """Emitted with ``(done, total)`` as each job finishes."""

    def __init__(
        self,
        database: ProjectDatabase,
        roster_id: int | None,
        batch_id: str,
        template: OmrTemplate,
        jobs: list[ReportJob],
        *,
        project_name: str,
        output_dir: Path,
        pdf_exporter: PdfExporter,
        computed_by: str = "",
        final: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._roster_id = roster_id
        self._batch_id = batch_id
        self._template = template
        self._jobs = jobs
        self._project_name = project_name
        self._output_dir = output_dir
        self._pdf_exporter = pdf_exporter
        self._computed_by = computed_by
        self._final = final
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the run to stop after the job currently in progress."""
        self._cancelled = True

    def run(self) -> None:
        """Generate every requested job in order. Runs on the worker thread."""
        outcomes = []
        total = len(self._jobs)
        try:
            for index, job in enumerate(self._jobs, start=1):
                if self._cancelled:
                    break
                outcome = self._run_one(job)
                if outcome is None:
                    continue
                outcomes.append(outcome)
                self.progressed.emit(index, total)
        except OMRScannerError as exc:
            _LOGGER.warning("Report generation failed: %s", type(exc).__name__)
            self.ready.emit(
                ReportRunResult(
                    outcomes=tuple(outcomes), error=exc.user_message or str(exc)
                )
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.error("Unexpected failure during report generation: %s", type(exc).__name__)
            self.ready.emit(
                ReportRunResult(
                    outcomes=tuple(outcomes),
                    error=f"Report generation could not complete. ({exc})",
                )
            )
            return
        self.ready.emit(ReportRunResult(outcomes=tuple(outcomes)))

    def _run_one(self, job: ReportJob) -> GenerationOutcome | None:
        """Generate one job, against **its own set's** roster and template.

        A job carrying a ``set_id`` goes through
        :func:`~omr_scanner.services.report_store.generate_for_set`, which
        resolves that set's roster and template itself and refuses - with the
        reason - when either is missing. A job without one is a pre-Part-2
        evidence-only set, generated against the project's single unscoped
        roster exactly as before.

        Returns ``None`` only when a legacy job has no roster to run against,
        which the page already prevents by disabling the action.
        """
        from omr_scanner.services import report_store

        if job.kind == "xlsx" and job.set_id:
            return report_store.generate_for_set(
                self._database, job.set_id, self._batch_id, self._template,
                project_name=self._project_name, output_dir=self._output_dir,
                computed_by=self._computed_by, final=self._final,
            )

        roster_id, refusal = self._roster_id_for(job)
        if roster_id is None:
            if refusal:
                # §15: say which set cannot be generated and why, rather than
                # letting the job disappear from the run's summary.
                return report_store.GenerationOutcome(
                    report_id=0, set_code=job.set_code, report_type=job.kind,
                    status="blocked", warnings=(refusal,),
                )
            return None

        if job.kind == "xlsx":
            return report_store.generate_xlsx(
                self._database, roster_id, self._batch_id, self._template,
                job.set_code, project_name=self._project_name,
                output_dir=self._output_dir, computed_by=self._computed_by,
                final=self._final,
            )

        label = "Rollwise" if job.kind == "pdf_rollwise" else "Meritwise"
        return report_store.generate_pdf(
            self._database, roster_id, self._batch_id, self._template,
            job.set_code, project_name=self._project_name,
            output_dir=self._output_dir,
            sheet_name=self._sheet_name_for(job.set_code, label, job.set_id),
            report_label=label, pdf_exporter=self._pdf_exporter,
            computed_by=self._computed_by, final=self._final,
        )

    def _roster_id_for(self, job: ReportJob) -> tuple[int | None, str]:
        """The roster a job must read candidates from, and why it has none.

        For a defined set that is **that set's own** roster, resolved here
        rather than taken from the run, so a PDF job cannot read another
        set's candidates for the same reason an XLSX job cannot.

        Returns:
            ``(roster_id, refusal)`` - the refusal is the operator-facing
            reason when there is no roster, and ``""`` otherwise.
        """
        from omr_scanner.services import report_store

        if not job.set_id:
            return self._roster_id, ""
        try:
            return (
                report_store.resolve_set_sources(self._database, job.set_id).roster_id,
                "",
            )
        except report_store.SetGenerationRefusedError as exc:
            return None, exc.user_message or str(exc)

    def _sheet_name_for(self, set_code: str, label: str, set_id: str = "") -> str:
        """The sheet name a PDF job should export.

        Rollwise's sheet name comes from the set's own template (it can be
        anything); Meritwise is always this application's own fixed sheet.

        Resolved by ``set_id`` when the job has one, for the same reason
        every other template lookup is: a code-keyed lookup happens to be
        unambiguous today only because both ``project_set.code`` and
        ``report_template_association.set_code`` are unique, and relying on
        that here would make this the one place that breaks quietly if either
        constraint were ever relaxed.
        """
        from omr_scanner.reporting.excel import MERITWISE_SHEET_NAME
        from omr_scanner.services import report_store

        if label == "Meritwise":
            return MERITWISE_SHEET_NAME
        association = (
            report_store.get_template_association_for_set(
                self._database, set_id, set_code
            )
            if set_id
            else report_store.get_template_association(self._database, set_code)
        )
        return association.sheet_name if association else ""
