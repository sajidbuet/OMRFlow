"""Persist report configuration, and generate reports from canonical data.

Purpose:
    The repository and orchestration layer for Phase 9: which template and
    layout an operator chose for each set, the append-only generation audit,
    and the top-level "generate this set's report" operation that reads
    Phase 7's reconciliation and Phase 8's scoring - **never re-deriving
    either** - and drives :mod:`omr_scanner.reporting.excel`.

Scope:
    Database access and orchestration. The spreadsheet mechanics are
    :mod:`omr_scanner.reporting.excel`'s; the readiness rules are
    :mod:`omr_scanner.services.report_readiness`'s; this module wires them
    together and is where the safe-output-naming and never-silently-
    overwrite rules (§24, §25) live.

**Regenerate, never patch** (§23, following Phase 8's own rule exactly): a
report is an output. Every generation reads the roster, the reconciliation
state and the scoring results fresh from the database and rebuilds the whole
workbook; nothing here opens a previously generated report and edits a cell.

Privacy:
    No candidate ID, name, answer string or mark is logged. The processing
    log this module builds for the generated workbook is not a log line - it
    is examination-report content the operator asked for - but it still
    carries counts and hashes only, never a name (§21, §49).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from typing import TYPE_CHECKING

from sqlalchemy import select

from omr_scanner import __version__
from omr_scanner.database.models import (
    GeneratedReport,
    ReportLayoutConfig,
    ReportTemplateAssociation,
)
from omr_scanner.domain.reconciliation import AttendanceState
from omr_scanner.domain.reporting import (
    ReadinessIssueKind,
    ReadinessReport,
    header_matches_maximum,
    safe_filename_component,
    total_header_for,
)
from omr_scanner.domain.scoring import ResultStatus
from omr_scanner.errors import OMRScannerError
from omr_scanner.reporting import excel as rx
from omr_scanner.services import reconciliation_store, scoring_store
from omr_scanner.services.answer_key import plan_for
from omr_scanner.services.report_readiness import block_stale_results_for_final_export
from omr_scanner.services.report_readiness import evaluate as evaluate_readiness_report
from omr_scanner.services.report_template import (
    ReportColumnMapping,
    ReportTemplateError,
    TemplateRoster,
    read_template,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.reconciliation import ReconciliationEntry
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.reporting.pdf import PdfExporter
    from omr_scanner.services.scoring_store import StoredResult

_LOGGER = logging.getLogger(__name__)


class ReportStoreError(OMRScannerError):
    """A report operation was refused. Always carries a ``user_message``."""


def _now() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# Sets known to this project
# ----------------------------------------------------------------------
def known_sets(database: ProjectDatabase, roster_id: int, batch_id: str) -> tuple[str, ...]:
    """Every set code this project has evidence of.

    The union of sets with a verified key, sets a script was actually read
    as, and sets an operator has already associated a template with - a
    template associated *before* any script of that set was scanned is still
    a set worth listing (an operator preparing report templates ahead of
    scoring).
    """
    from_keys = set(scoring_store.known_set_codes(database))
    from_results = {
        item.set_code
        for item in scoring_store.list_results(database, roster_id, batch_id)
        if item.set_code
    }
    from_associations = set(list_template_associations(database))
    return tuple(sorted(from_keys | from_results | from_associations))


@dataclass(frozen=True, slots=True)
class SetOverview:
    """One set's row in the Result Management set list (§6).

    Attributes:
        set_code: The set.
        script_count: How many scripts were read as this set - always
            knowable from Phase 8 alone, even before a template exists.
        scored_count / blocked_count: From the same scripts.
        has_verified_key / key_revision: Answer-key status.
        policy_revision: The active scoring-policy revision.
        template: The associated template, if any.

    **What this deliberately does not claim:** a true absentee has no
    script and therefore no set code anywhere in Phase 1-8's data - "who was
    assigned Set A" is knowable only from that set's own result template
    (§3, §8), which is exactly why the phase brief makes associating one
    mandatory before a set's *candidate roster* (as opposed to its script
    count) is known. See ``docs/reporting.md`` "Where a set's roster comes
    from".
    """

    set_code: str
    script_count: int = 0
    scored_count: int = 0
    blocked_count: int = 0
    has_verified_key: bool = False
    key_revision: int = 0
    policy_revision: int = 0
    template: StoredTemplateAssociation | None = None

    @property
    def report_readiness_label(self) -> str:
        """A one-word status for the set list's "Report readiness" column."""
        if self.template is None:
            return "Template required"
        if not self.has_verified_key:
            return "Key not verified"
        return "Ready"


def set_overview(
    database: ProjectDatabase, roster_id: int, batch_id: str
) -> tuple[SetOverview, ...]:
    """Build the Result Management set list, cheaply (§6).

    One read of the batch's results, grouped in Python - deliberately not
    one query per set, so the page costs the same whether the project has
    two sets or twenty.
    """
    results = scoring_store.list_results(database, roster_id, batch_id)
    keys = scoring_store.verified_keys(database)
    policy = scoring_store.active_policy(database)
    associations = list_template_associations(database)

    by_set: dict[str, list[StoredResult]] = {}
    for item in results:
        if item.set_code:
            by_set.setdefault(item.set_code, []).append(item)

    codes = set(by_set) | set(keys) | set(associations)
    overviews = []
    for code in sorted(codes):
        rows = by_set.get(code, ())
        stored_key = keys.get(code)
        overviews.append(
            SetOverview(
                set_code=code,
                script_count=len(rows),
                scored_count=sum(1 for r in rows if r.status is ResultStatus.SCORED),
                blocked_count=sum(1 for r in rows if r.status is ResultStatus.BLOCKED),
                has_verified_key=stored_key is not None,
                key_revision=stored_key.revision if stored_key else 0,
                policy_revision=policy.revision,
                template=associations.get(code),
            )
        )
    return tuple(overviews)


# ----------------------------------------------------------------------
# Template association
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredTemplateAssociation:
    """One set's chosen result template, as stored."""

    association_id: int
    set_code: str
    template_path: str
    template_sha256: str
    sheet_name: str
    mapping: ReportColumnMapping
    updated_at: datetime
    updated_by: str = ""


def _to_association(row: ReportTemplateAssociation) -> StoredTemplateAssociation:
    return StoredTemplateAssociation(
        association_id=row.association_id,
        set_code=row.set_code,
        template_path=row.template_path,
        template_sha256=row.template_sha256,
        sheet_name=row.sheet_name,
        mapping=ReportColumnMapping(
            roll=row.roll_column,
            marks=row.marks_column,
            serial=row.serial_column,
            name=row.name_column,
            rank=row.rank_column,
        ),
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def associate_template(
    database: ProjectDatabase,
    set_code: str,
    template_path: Path,
    mapping: ReportColumnMapping,
    *,
    sheet_name: str,
    updated_by: str = "",
) -> StoredTemplateAssociation:
    """Record (or replace) the result template for one set.

    Raises:
        ReportStoreError: The template cannot be hashed (typically: it has
            disappeared since it was chosen).

    Not revisioned (see :class:`~omr_scanner.database.models.
    ReportTemplateAssociation`'s own docstring): this updates the one row for
    ``set_code`` in place. Every *generation* still records its own snapshot
    on :class:`GeneratedReport`, which is where reproducibility actually
    lives.
    """
    digest = _hash_file(template_path)
    moment = _now()
    with database.session() as session:
        row = session.scalars(
            select(ReportTemplateAssociation).where(
                ReportTemplateAssociation.set_code == set_code
            )
        ).first()
        if row is None:
            row = ReportTemplateAssociation(set_code=set_code, created_at=moment)
            session.add(row)
        row.template_path = str(template_path)
        row.template_sha256 = digest
        row.sheet_name = sheet_name
        row.header_row = 0  # informational only; the roster is re-read fresh each time
        row.roll_column = mapping.roll
        row.marks_column = mapping.marks
        row.serial_column = mapping.serial
        row.name_column = mapping.name
        row.rank_column = mapping.rank
        row.updated_at = moment
        row.updated_by = updated_by.strip()
        session.flush()
        stored = _to_association(row)
    _LOGGER.info("Report template associated: set=%s sheet=%r", set_code, sheet_name)
    return stored


def _hash_file(path: Path) -> str:
    """Return the SHA-256 of a file's bytes.

    Used both to record and to later verify that a template was not
    modified between association and generation.
    """
    import hashlib

    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ReportStoreError(
            f"Could not read template to hash it: {exc}",
            user_message=f"'{path.name}' could not be read.",
        ) from exc


def get_template_association(
    database: ProjectDatabase, set_code: str
) -> StoredTemplateAssociation | None:
    """Return the stored template association for one set, or ``None``."""
    with database.session() as session:
        row = session.scalars(
            select(ReportTemplateAssociation).where(
                ReportTemplateAssociation.set_code == set_code
            )
        ).first()
        return _to_association(row) if row is not None else None


def list_template_associations(
    database: ProjectDatabase,
) -> dict[str, StoredTemplateAssociation]:
    """Every set's template association, keyed by set code."""
    with database.session() as session:
        rows = session.scalars(select(ReportTemplateAssociation)).all()
        return {row.set_code: _to_association(row) for row in rows}


def load_roster_for_set(
    database: ProjectDatabase, set_code: str
) -> TemplateRoster:
    """Read the associated template fresh, for validation or generation.

    Raises:
        ReportStoreError: No template is associated with ``set_code``.
        ReportTemplateError: The template cannot be read (moved, corrupted,
            or its mapped columns no longer fit the sheet).

    Always reads the file again rather than trusting a cached roster -
    consistent with "regenerate, never patch": if the operator has edited
    their own template since associating it, the next validation or
    generation sees the current file, not a snapshot of the day it was
    chosen.
    """
    from pathlib import Path as _Path

    association = get_template_association(database, set_code)
    if association is None:
        raise ReportStoreError(
            f"No template associated with set {set_code}",
            user_message=f"Select a result template for Set {set_code} first.",
        )
    return read_template(
        _Path(association.template_path), association.mapping, sheet=association.sheet_name
    )


# ----------------------------------------------------------------------
# Layout configuration
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredLayoutConfig:
    """One layout configuration row, with its settings and provenance."""

    config_id: int
    set_code: str
    settings: rx.LayoutSettings
    updated_at: datetime
    updated_by: str = ""


_LAYOUT_COLUMNS: tuple[str, ...] = (
    "title_text", "subtitle_text", "examination_name", "footer_text",
    "auto_update_marks_header", "logo_path", "logo_max_width_px",
    "logo_max_height_px", "font_family", "title_font_size", "header_font_size",
    "body_font_size", "bold_headers", "page_size", "orientation",
    "margin_top_mm", "margin_bottom_mm", "margin_left_mm", "margin_right_mm",
    "fit_to_width", "scale_percent", "center_horizontally", "repeat_header_row",
)


def _to_layout_settings(row: ReportLayoutConfig) -> rx.LayoutSettings:
    import json

    overrides = json.loads(row.column_header_overrides_json or "{}")
    return rx.LayoutSettings(
        title_text=row.title_text,
        subtitle_text=row.subtitle_text,
        examination_name=row.examination_name,
        footer_text=row.footer_text,
        column_header_overrides=overrides,
        auto_update_marks_header=row.auto_update_marks_header,
        logo_path=row.logo_path,
        logo_max_width_px=row.logo_max_width_px,
        logo_max_height_px=row.logo_max_height_px,
        font_family=row.font_family,
        title_font_size=row.title_font_size,
        header_font_size=row.header_font_size,
        body_font_size=row.body_font_size,
        bold_headers=row.bold_headers,
        page_size=row.page_size,
        orientation=row.orientation,
        margin_top_mm=row.margin_top_mm,
        margin_bottom_mm=row.margin_bottom_mm,
        margin_left_mm=row.margin_left_mm,
        margin_right_mm=row.margin_right_mm,
        fit_to_width=row.fit_to_width,
        scale_percent=row.scale_percent,
        center_horizontally=row.center_horizontally,
        repeat_header_row=row.repeat_header_row,
    )


def get_layout_config(database: ProjectDatabase, set_code: str = "") -> StoredLayoutConfig:
    """Return one set's layout configuration, falling back to the project default.

    Creates the project-wide default row (``set_code=""``) with the class
    defaults the first time it is asked for - the same "conservative
    default, created on first read" pattern
    :func:`omr_scanner.services.scoring_store.active_policy` uses - so a
    project that has never configured anything still generates a report
    using sensible, explicit settings rather than an implicit absence of
    configuration.
    """
    with database.session() as session:
        row = session.scalars(
            select(ReportLayoutConfig).where(ReportLayoutConfig.set_code == set_code)
        ).first()
        if row is not None:
            return StoredLayoutConfig(
                config_id=row.config_id, set_code=row.set_code,
                settings=_to_layout_settings(row), updated_at=row.updated_at,
                updated_by=row.updated_by,
            )
        if set_code:
            # No per-set override - fall back to the project default without
            # creating a redundant duplicate row for this set.
            default = get_layout_config(database, "")
            return StoredLayoutConfig(
                config_id=default.config_id, set_code=set_code,
                settings=default.settings, updated_at=default.updated_at,
                updated_by=default.updated_by,
            )
        moment = _now()
        if database.read_only:
            # Phase 10, §42: never write from a read-only session, including
            # this function's own "create the project default on first read"
            # behaviour. The unpersisted default (`config_id=0`) is what a
            # freshly-created project would show anyway.
            return StoredLayoutConfig(
                config_id=0, set_code="", settings=rx.LayoutSettings(), updated_at=moment,
            )
        created = ReportLayoutConfig(set_code="", created_at=moment, updated_at=moment)
        session.add(created)
        session.flush()
        return StoredLayoutConfig(
            config_id=created.config_id, set_code="",
            settings=_to_layout_settings(created), updated_at=moment,
        )


def save_layout_config(
    database: ProjectDatabase,
    set_code: str,
    settings: rx.LayoutSettings,
    *,
    updated_by: str = "",
) -> StoredLayoutConfig:
    """Store layout settings for the project (``set_code=""``) or one set.

    Updated in place - layout is presentation, not a score-affecting input,
    so it does not need Phase 8's revision-and-stale machinery. What matters
    for reproducibility is captured at generation time, on
    :class:`GeneratedReport`.
    """
    import json

    moment = _now()
    with database.session() as session:
        row = session.scalars(
            select(ReportLayoutConfig).where(ReportLayoutConfig.set_code == set_code)
        ).first()
        if row is None:
            row = ReportLayoutConfig(set_code=set_code, created_at=moment)
            session.add(row)
        row.title_text = settings.title_text
        row.subtitle_text = settings.subtitle_text
        row.examination_name = settings.examination_name
        row.footer_text = settings.footer_text
        row.column_header_overrides_json = json.dumps(dict(settings.column_header_overrides))
        row.auto_update_marks_header = settings.auto_update_marks_header
        row.logo_path = settings.logo_path
        row.logo_max_width_px = settings.logo_max_width_px
        row.logo_max_height_px = settings.logo_max_height_px
        row.font_family = settings.font_family
        row.title_font_size = settings.title_font_size
        row.header_font_size = settings.header_font_size
        row.body_font_size = settings.body_font_size
        row.bold_headers = settings.bold_headers
        row.page_size = settings.page_size
        row.orientation = settings.orientation
        row.margin_top_mm = settings.margin_top_mm
        row.margin_bottom_mm = settings.margin_bottom_mm
        row.margin_left_mm = settings.margin_left_mm
        row.margin_right_mm = settings.margin_right_mm
        row.fit_to_width = settings.fit_to_width
        row.scale_percent = settings.scale_percent
        row.center_horizontally = settings.center_horizontally
        row.repeat_header_row = settings.repeat_header_row
        row.updated_at = moment
        row.updated_by = updated_by.strip()
        session.flush()
        return StoredLayoutConfig(
            config_id=row.config_id, set_code=set_code, settings=settings,
            updated_at=moment, updated_by=row.updated_by,
        )


# ----------------------------------------------------------------------
# Safe output naming (§24, §25)
# ----------------------------------------------------------------------
def unique_output_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a path in ``directory`` guaranteed not to already exist.

    Tries ``stem.suffix`` first, then ``stem_1.suffix``, ``stem_2.suffix``,
    ... - the incremented-filename half of §25's no-silent-overwrite rule.
    An explicit overwrite (the operator confirming "replace it") is a
    separate, deliberate caller decision - see :func:`generate_xlsx`'s
    ``allow_overwrite`` - never this function's default.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def default_output_stem(project_name: str, set_code: str, report_label: str) -> str:
    """``<Project>_<Set>_<Label>``, every component sanitised for a filename."""
    parts = (
        safe_filename_component(project_name, fallback="Project"),
        safe_filename_component(f"Set{set_code}", fallback="Set"),
        safe_filename_component(report_label, fallback="Result"),
    )
    return "_".join(parts)


# ----------------------------------------------------------------------
# Gathering canonical inputs (never re-derived)
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SetGenerationInputs:
    """Every canonical fact one set's generation reads, gathered once."""

    entries: tuple[ReconciliationEntry, ...]
    results_by_candidate: dict[str, StoredResult]
    verified_key: scoring_store.StoredKey | None
    policy: scoring_store.StoredPolicy
    question_plan_count: int


def gather_set_inputs(
    database: ProjectDatabase, roster_id: int, batch_id: str, template: OmrTemplate, set_code: str
) -> SetGenerationInputs:
    """Read every Phase 7/8 fact one set's report needs, in one pass.

    This, and only this, is where Phase 9 is allowed to look at Phase 7/8
    state - every later step in generation works from what this function
    returned, never from a second, possibly inconsistent read.
    """
    entries = reconciliation_store.list_entries(database, roster_id, batch_id)
    results = scoring_store.list_results(database, roster_id, batch_id, template)
    results_by_candidate = {item.candidate_id: item for item in results}
    keys = scoring_store.verified_keys(database)
    policy = scoring_store.active_policy(database)
    plan = plan_for(template)
    return SetGenerationInputs(
        entries=entries,
        results_by_candidate=results_by_candidate,
        verified_key=keys.get(set_code),
        policy=policy,
        question_plan_count=plan.question_count,
    )


def _decisions_for(
    roster: TemplateRoster, inputs: SetGenerationInputs
) -> dict[str, rx.CandidateReportRow]:
    """Turn canonical Phase 7/8 state into per-roll Rollwise decisions.

    The one place attendance/score facts become "what goes in this cell" -
    every rule here is read from :class:`~omr_scanner.domain.reconciliation.
    ReconciliationEntry` and :class:`~omr_scanner.services.scoring_store.
    StoredResult` directly, never guessed from the template's own leftover
    marks column.
    """
    entries_by_id = {entry.candidate_id: entry for entry in inputs.entries}
    decisions: dict[str, rx.CandidateReportRow] = {}
    for row in roster.rows:
        if not row.roll:
            continue
        entry = entries_by_id.get(row.roll)
        if entry is None or not entry.is_registered:
            continue  # reported by readiness; nothing to decide here
        if entry.effective_attendance is AttendanceState.ABSENT:
            decisions[row.roll] = rx.CandidateReportRow(roll=row.roll, is_absent=True)
            continue
        result = inputs.results_by_candidate.get(row.roll)
        score = result.final_score if result and result.has_mark else None
        decisions[row.roll] = rx.CandidateReportRow(
            roll=row.roll, is_absent=False, final_score=score
        )
    return decisions


def _maximum_score(inputs: SetGenerationInputs) -> Fraction | None:
    """The best score a candidate could obtain on this set's paper."""
    if inputs.verified_key is None:
        return None
    return inputs.policy.policy.correct_mark * inputs.verified_key.key.question_count


# ----------------------------------------------------------------------
# Readiness (public entry point)
# ----------------------------------------------------------------------
def check_readiness(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    template: OmrTemplate,
    set_code: str,
    *,
    for_final_export: bool,
) -> ReadinessReport:
    """The full readiness report for one set (§32).

    Args:
        database: The open project database.
        roster_id: The active candidate roster.
        batch_id: The batch whose scores are being reported on.
        template: The template the batch was read with.
        set_code: The set to check.
        for_final_export: When ``True``, a stale result becomes a blocking
            issue rather than a warning - the distinction §8 draws between
            what a preview may show and what a final export must never
            present as finished.
    """
    try:
        roster = load_roster_for_set(database, set_code)
    except (ReportStoreError, ReportTemplateError) as exc:
        kind = (
            ReadinessIssueKind.NO_TEMPLATE
            if isinstance(exc, ReportStoreError)
            else ReadinessIssueKind.TEMPLATE_UNREADABLE
        )
        from omr_scanner.domain.reporting import ReadinessIssue

        return ReadinessReport(
            set_code=set_code,
            issues=(ReadinessIssue(kind, exc.user_message or str(exc)),),
        )

    inputs = gather_set_inputs(database, roster_id, batch_id, template, set_code)
    report = evaluate_readiness_report(
        set_code=set_code,
        roster=roster,
        entries=inputs.entries,
        results_by_candidate=inputs.results_by_candidate,
        has_verified_key=inputs.verified_key is not None,
    )
    return block_stale_results_for_final_export(report) if for_final_export else report


# ----------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """What one generation attempt produced (or why it did not)."""

    report_id: int
    set_code: str
    report_type: str
    status: str
    output_path: Path | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    readiness: ReadinessReport | None = None

    @property
    def ok(self) -> bool:
        """Whether a report was actually written."""
        return self.status == "success" and self.output_path is not None


def generate_xlsx(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    template: OmrTemplate,
    set_code: str,
    *,
    project_name: str,
    output_dir: Path,
    computed_by: str = "",
    final: bool = True,
    should_cancel: Callable[[], bool] | None = None,
) -> GenerationOutcome:
    """Generate one set's XLSX report from canonical stored data.

    Args:
        database: The open project database.
        roster_id: The active candidate roster.
        batch_id: The batch whose scores are being reported on.
        template: The template the batch was read with.
        set_code: The set to generate a report for.
        project_name: The open project's display name, used in the output
            filename and the Summary sheet.
        output_dir: Where the workbook is written - typically the project's
            ``exports`` directory.
        computed_by: Who ran the generation, if known - recorded on the
            audit row and in the Processing Log.
        final: When ``True`` (Final Export), a blocking readiness issue
            refuses generation outright. When ``False`` (Preview), generation
            proceeds and every issue - including ones that would block a
            final export - is returned as a warning, so an operator can see
            what a candidate row will look like *and* what needs fixing
            before it is official (§8).
        should_cancel: Polled once before the (fast, in-memory) work begins;
            reporting has no long per-candidate loop worth checking mid-way,
            unlike Phase 8's batch scoring.

    Returns:
        The outcome, always backed by a :class:`GeneratedReport` audit row -
        including on failure, so "we tried to generate Set C and it failed
        because X" is itself part of the permanent record (§33: one set's
        failure must be diagnosable without corrupting anything else's).
    """
    moment = _now()
    if should_cancel is not None and should_cancel():
        return GenerationOutcome(
            report_id=0, set_code=set_code, report_type="xlsx", status="cancelled"
        )

    try:
        roster = load_roster_for_set(database, set_code)
    except (ReportStoreError, ReportTemplateError) as exc:
        return _record_failure(database, set_code, "xlsx", exc, moment)

    inputs = gather_set_inputs(database, roster_id, batch_id, template, set_code)
    readiness = evaluate_readiness_report(
        set_code=set_code,
        roster=roster,
        entries=inputs.entries,
        results_by_candidate=inputs.results_by_candidate,
        has_verified_key=inputs.verified_key is not None,
    )
    if final:
        readiness = block_stale_results_for_final_export(readiness)
        if not readiness.is_ready:
            return GenerationOutcome(
                report_id=0, set_code=set_code, report_type="xlsx", status="blocked",
                warnings=readiness.describe(), readiness=readiness,
            )

    warnings: list[str] = list(readiness.describe()) if readiness.has_warnings else []

    try:
        association = get_template_association(database, set_code)
        assert association is not None  # load_roster_for_set already required this
        layout = get_layout_config(database, set_code).settings

        decisions = _decisions_for(roster, inputs)
        maximum = _maximum_score(inputs)
        header_override = None
        if layout.auto_update_marks_header and maximum is not None:
            current_header = _current_marks_header(roster)
            if not header_matches_maximum(current_header, maximum):
                header_override = total_header_for(maximum)

        stem = default_output_stem(project_name, set_code, "Result")
        output_path = unique_output_path(output_dir, stem, ".xlsx")

        rx.copy_into(association_path(association), output_path)
        rollwise_result = rx.populate_rollwise(
            output_path, roster, decisions, marks_header_override=header_override
        )
        warnings.extend(
            f"Roll {roll} could not be resolved to a decision and was left unchanged."
            for roll in rollwise_result.unresolved_rows
        )

        scored_marks = [
            item.final_score
            for item in decisions.values()
            if not item.is_absent and item.final_score is not None
        ]
        merit_candidates: list[rx.MeritCandidate] = []
        for row in roster.rows:
            decision = decisions.get(row.roll)
            if decision is None or decision.is_absent or decision.final_score is None:
                continue
            merit_candidates.append(
                rx.MeritCandidate(
                    roll=row.roll, name=row.name, final_score=decision.final_score
                )
            )
        rx.add_meritwise_sheet(output_path, merit_candidates)

        present_count = sum(1 for item in decisions.values() if not item.is_absent)
        absent_count = sum(1 for item in decisions.values() if item.is_absent)
        summary = rx.SummaryData(
            project_name=project_name, set_code=set_code, registered=len(roster.rows),
            present=present_count, absent=absent_count, scored=len(scored_marks),
            unresolved=len(rollwise_result.unresolved_rows), maximum_score=maximum,
            highest_score=max(scored_marks) if scored_marks else None,
            lowest_score=min(scored_marks) if scored_marks else None,
            mean_score=(
                Fraction(sum(scored_marks), len(scored_marks)) if scored_marks else None
            ),
            median_score=_median(scored_marks),
            scoring_policy_summary=inputs.policy.policy.describe(),
            answer_key_revision=inputs.verified_key.revision if inputs.verified_key else None,
            generated_at=moment, application_version=__version__,
        )
        rx.add_summary_sheet(output_path, summary)

        if inputs.verified_key is not None:
            rx.add_answer_key_sheet(
                output_path, set_code=set_code, key=inputs.verified_key.key,
                key_id=inputs.verified_key.key_id,
            )

        log_entries = _processing_log_entries(
            set_code=set_code, association=association, summary=summary,
            policy_revision=inputs.policy.revision,
            answer_key_revision=inputs.verified_key.revision if inputs.verified_key else 0,
            warnings=warnings, computed_by=computed_by,
        )
        rx.add_processing_log_sheet(output_path, log_entries)

        layout_warnings = rx.apply_layout(
            output_path, [roster.sheet, rx.MERITWISE_SHEET_NAME], layout,
            primary_header_row=roster.header_row_number,
        )
        warnings.extend(layout_warnings)

        output_hash = _hash_file(output_path)
    except Exception as exc:
        return _record_failure(database, set_code, "xlsx", exc, moment)

    report_id = _record_success(
        database, set_code=set_code, report_type="xlsx", association=association,
        policy_revision=inputs.policy.revision,
        answer_key_revision=inputs.verified_key.revision if inputs.verified_key else 0,
        layout=layout, summary=summary, output_path=output_path,
        output_hash=output_hash, warnings=warnings, moment=moment, computed_by=computed_by,
    )
    _LOGGER.info(
        "Report generated: set=%s type=xlsx status=success candidates=%d",
        set_code, len(roster.rows),
    )
    return GenerationOutcome(
        report_id=report_id, set_code=set_code, report_type="xlsx", status="success",
        output_path=output_path, warnings=tuple(warnings), readiness=readiness,
    )


def _current_marks_header(roster: TemplateRoster) -> str:
    """Best-effort read of the template's own current marks-column header.

    Falls back to an empty string (never matches, so an override is always
    offered) if the header cell cannot be read for any reason - a defensive
    read, since this only affects whether an already-correct header is left
    alone or rewritten to the exact same text.
    """
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(roster.path, read_only=True, data_only=True)
        try:
            sheet = workbook[roster.sheet]
            value = sheet.cell(
                row=roster.header_row_number, column=roster.mapping.marks + 1
            ).value
            return str(value) if value is not None else ""
        finally:
            workbook.close()
    except Exception:
        # A display nicety only - never worth failing a whole generation.
        return ""


def association_path(association: StoredTemplateAssociation) -> Path:
    """The template file an association points to, as a ``Path``."""
    from pathlib import Path as _Path

    return _Path(association.template_path)


def _median(values: Sequence[Fraction]) -> Fraction | None:
    """Exact median of a list of exact marks - never a float approximation."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _processing_log_entries(
    *,
    set_code: str,
    association: StoredTemplateAssociation,
    summary: rx.SummaryData,
    policy_revision: int,
    answer_key_revision: int,
    warnings: Sequence[str],
    computed_by: str,
) -> list[rx.ProcessingLogEntry]:
    """Build the Processing Log's rows: counts and hashes, never a name."""
    entries = [
        rx.ProcessingLogEntry("Operation", "Generate result report"),
        rx.ProcessingLogEntry("Set", set_code),
        rx.ProcessingLogEntry("Timestamp", summary.generated_at.isoformat()),
        rx.ProcessingLogEntry("Source template hash (SHA-256)", association.template_sha256),
        rx.ProcessingLogEntry("Scoring policy revision", str(policy_revision)),
        rx.ProcessingLogEntry("Answer-key revision", str(answer_key_revision or "N/A")),
        rx.ProcessingLogEntry("Candidate count", str(summary.registered)),
        rx.ProcessingLogEntry("Present", str(summary.present)),
        rx.ProcessingLogEntry("Absent", str(summary.absent)),
        rx.ProcessingLogEntry("Scored", str(summary.scored)),
        rx.ProcessingLogEntry("Unresolved", str(summary.unresolved)),
        rx.ProcessingLogEntry("Application version", summary.application_version),
        rx.ProcessingLogEntry("Generated by", computed_by or "N/A"),
        rx.ProcessingLogEntry("Warning count", str(len(warnings))),
    ]
    entries.extend(rx.ProcessingLogEntry("Warning", text) for text in warnings)
    return entries


def _record_success(
    database: ProjectDatabase,
    *,
    set_code: str,
    report_type: str,
    association: StoredTemplateAssociation,
    policy_revision: int,
    answer_key_revision: int,
    layout: rx.LayoutSettings,
    summary: rx.SummaryData,
    output_path: Path,
    output_hash: str,
    warnings: Sequence[str],
    moment: datetime,
    computed_by: str,
) -> int:
    import json

    with database.session() as session:
        row = GeneratedReport(
            set_code=set_code, report_type=report_type,
            template_path=association.template_path,
            template_sha256=association.template_sha256,
            answer_key_revision=answer_key_revision, policy_revision=policy_revision,
            layout_config_snapshot_json=layout.to_json(),
            candidate_count=summary.registered, present_count=summary.present,
            absent_count=summary.absent, scored_count=summary.scored,
            unresolved_count=summary.unresolved,
            warnings_json=json.dumps(list(warnings), ensure_ascii=False),
            output_path=str(output_path), output_sha256=output_hash,
            status="success", generated_at=moment, generated_by=computed_by.strip(),
            application_version=__version__,
        )
        session.add(row)
        session.flush()
        return row.report_id


def _record_failure(
    database: ProjectDatabase,
    set_code: str,
    report_type: str,
    exc: Exception,
    moment: datetime,
) -> GenerationOutcome:
    """Write a failed :class:`GeneratedReport` row and return the outcome.

    The failure is recorded either way (§33: a set's failure must be
    diagnosable), but the *message* returned to the caller is the
    ``user_message`` an :class:`~omr_scanner.errors.OMRScannerError` carries
    when there is one - never a bare Python exception string reaching an
    operator's screen.
    """
    message = getattr(exc, "user_message", "") or str(exc)
    with database.session() as session:
        row = GeneratedReport(
            set_code=set_code, report_type=report_type, status="failed",
            error_message=message, generated_at=moment,
            application_version=__version__,
        )
        session.add(row)
        session.flush()
        report_id = row.report_id
    _LOGGER.warning(
        "Report generation failed: set=%s type=%s error_type=%s",
        set_code, report_type, type(exc).__name__,
    )
    return GenerationOutcome(
        report_id=report_id, set_code=set_code, report_type=report_type,
        status="failed", warnings=(message,),
    )


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------
def generate_pdf(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    template: OmrTemplate,
    set_code: str,
    *,
    project_name: str,
    output_dir: Path,
    sheet_name: str,
    report_label: str,
    pdf_exporter: PdfExporter,
    computed_by: str = "",
    final: bool = True,
) -> GenerationOutcome:
    """Generate a fresh XLSX, then export one of its sheets to PDF.

    Args:
        database: The open project database.
        roster_id: The active candidate roster.
        batch_id: The batch whose scores are being reported on.
        template: The template the batch was read with.
        set_code: The set to generate a report for.
        project_name: The open project's display name.
        output_dir: Where both the intermediate workbook and the PDF are
            written.
        sheet_name: Which sheet of the *freshly generated* workbook to
            export - the Rollwise sheet's own name, or
            :data:`~omr_scanner.reporting.excel.MERITWISE_SHEET_NAME`.
        report_label: Human-readable label for the output filename
            ("Rollwise", "Meritwise").
        pdf_exporter: Injected so this is testable without a real PDF engine
            (§44) - see :func:`omr_scanner.reporting.pdf.detect_exporter`.
        computed_by: Who ran the generation, if known.
        final: Forwarded to the XLSX generation this performs first - see
            :func:`generate_xlsx`.

    PDF export always regenerates the XLSX first (§23: never patch an old
    file), then converts a temporary single-sheet copy of *that* fresh
    workbook - so the PDF and a simultaneously generated XLSX are guaranteed
    to agree, and a multi-sheet workbook never leaks Meritwise data into a
    Rollwise PDF or vice versa.
    """
    import tempfile
    from pathlib import Path as _Path

    xlsx_outcome = generate_xlsx(
        database, roster_id, batch_id, template, set_code,
        project_name=project_name, output_dir=output_dir, computed_by=computed_by,
        final=final,
    )
    if not xlsx_outcome.ok:
        return GenerationOutcome(
            report_id=xlsx_outcome.report_id, set_code=set_code,
            report_type=f"pdf_{report_label.lower()}", status=xlsx_outcome.status,
            warnings=xlsx_outcome.warnings, readiness=xlsx_outcome.readiness,
        )

    assert xlsx_outcome.output_path is not None
    moment = _now()
    if not pdf_exporter.is_available():
        return _record_failure(
            database, set_code, f"pdf_{report_label.lower()}",
            _pdf_unavailable_error(pdf_exporter), moment,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="omrflow_pdf_src_") as scratch:
            single_sheet = _Path(scratch) / "sheet.xlsx"
            _extract_single_sheet(xlsx_outcome.output_path, sheet_name, single_sheet)
            stem = default_output_stem(project_name, set_code, report_label)
            output_pdf = unique_output_path(output_dir, stem, ".pdf")
            pdf_exporter.export(single_sheet, output_pdf)
        output_hash = _hash_file(output_pdf)
    except Exception as exc:
        return _record_failure(database, set_code, f"pdf_{report_label.lower()}", exc, moment)

    association = get_template_association(database, set_code)
    with database.session() as session:
        row = GeneratedReport(
            set_code=set_code, report_type=f"pdf_{report_label.lower()}",
            template_path=association.template_path if association else "",
            template_sha256=association.template_sha256 if association else "",
            output_path=str(output_pdf), output_sha256=output_hash,
            status="success", generated_at=moment, generated_by=computed_by.strip(),
            application_version=__version__,
        )
        session.add(row)
        session.flush()
        report_id = row.report_id

    return GenerationOutcome(
        report_id=report_id, set_code=set_code, report_type=f"pdf_{report_label.lower()}",
        status="success", output_path=output_pdf, readiness=xlsx_outcome.readiness,
    )


def _pdf_unavailable_error(exporter: PdfExporter) -> OMRScannerError:
    from omr_scanner.reporting.pdf import PdfExportError

    return PdfExportError(
        f"PDF exporter {exporter.name!r} is not available",
        user_message=(
            "PDF export requires LibreOffice to be installed. The .xlsx "
            "report was generated successfully; only the PDF step was skipped."
        ),
    )


def _extract_single_sheet(source: Path, sheet_name: str, destination: Path) -> None:
    """Copy ``source`` and delete every sheet except ``sheet_name``.

    Guarantees a Rollwise PDF cannot contain Meritwise rows or vice versa -
    the PDF converter renders whatever sheets a workbook has, so this is
    where "export only this sheet" is actually enforced.
    """
    import shutil

    import openpyxl

    shutil.copyfile(source, destination)
    workbook = openpyxl.load_workbook(destination)
    try:
        if sheet_name not in workbook.sheetnames:
            raise ReportStoreError(
                f"Sheet {sheet_name!r} not found for PDF export",
                user_message=f"'{sheet_name}' could not be found in the generated report.",
            )
        for name in list(workbook.sheetnames):
            if name != sheet_name:
                del workbook[name]
        workbook.save(destination)
    finally:
        workbook.close()
