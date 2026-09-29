"""Inspect one script's scan and correct its whole Student ID or set code.

Purpose:
    The evidence-and-correction panel a stage embeds when it needs a human to
    look at a particular sheet: the **original scan** as it arrived, the sheet
    as recognition read it (every bubble, the fields outlined, any manual
    decision drawn in), and a field editor for the complete Student ID and set
    code.

    Built from the Resolve stage's own parts rather than beside them - the
    same background loader (:class:`~omr_scanner.gui.review.worker.SheetWorker`),
    the same image view, the same decision lanes, the same override warning -
    and every correction goes through
    :func:`~omr_scanner.services.field_edit.commit_field_edit`, the shared
    path the Resolve stage's own field editor uses. There is no second
    correction mechanism: a Student ID corrected here is corrected in the
    review ledger exactly as it would be on Resolve, with the machine's value
    kept, the operator's value effective, the change audited and one
    :meth:`undo_last` away from being taken back.

What does NOT belong here:
    * Deciding which script to show. The embedding stage does that; this
      widget shows the one it is given.
    * Attendance, reconciliation or anything else about *why* the operator is
      looking. The stage passes a sentence of context, which is written into
      the ledger beside the correction.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.review import FieldKind, ReasonCode
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.review.lanes import bounds_of, build_lanes
from omr_scanner.gui.review.worker import SheetBundle, SheetWorker
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.theme import Color, Spacing
from omr_scanner.services import (
    ConflictFilter,
    field_shape,
    join_field_value,
    list_conflicts,
    provenance_for_scan,
    scan_lifecycle,
    scan_source_path,
    undo_field_edit,
)
from omr_scanner.services.field_edit import commit_field_edit as commit
from omr_scanner.services.field_edit import (
    current_field_values,
    machine_characters,
    override_warning_text,
    plan_field_edit,
    validate_field_value,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.review import Provenance
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import ConflictRecord, FieldEdit, FieldShape, ProjectDatabase
    from omr_scanner.services.field_edit import FieldEditPlan

_LOGGER = logging.getLogger(__name__)

ORIGINAL_TAB = 0
READ_TAB = 1

EDITOR_KINDS: tuple[FieldKind, ...] = (FieldKind.IDENTIFIER, FieldKind.SET_CODE)


class ScriptInspector(QWidget):
    """Show one script's evidence, and correct its Student ID or set code.

    Signals:
        loaded: The sheet has been read and drawn.
        corrected: ``int`` scan id, after a correction (or its undo) has been
            stored - the embedding stage re-reads whatever depends on it.

    The embedding stage calls :meth:`set_context` once it has a project, then
    :meth:`show_script` for each sheet the operator asks about.
    """

    loaded = Signal()
    corrected = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scriptInspector")
        self._database: ProjectDatabase | None = None
        self._batch_id: str | None = None
        self._scan_batch_id: str | None = None
        self._template: OmrTemplate | None = None
        self._reviewer = ""
        self._context = ""
        self._scan_id: int | None = None
        self._bundle: SheetBundle | None = None
        self._records: list[ConflictRecord] = []
        self._provenance: dict[int, Provenance] = {}
        self._last_edit: FieldEdit | None = None
        self._worker: SheetWorker | None = None
        self._workers: list[SheetWorker] = []
        self._editors: dict[FieldKind, tuple[QLabel, QLineEdit, QPushButton]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        self.title_label = QLabel("")
        self.title_label.setObjectName("inspectorTitleLabel")
        self.title_label.setTextFormat(Qt.TextFormat.RichText)
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.view_tabs = QTabWidget()
        self.view_tabs.setObjectName("inspectorViewTabs")
        self.original_view = ScanPreviewView()
        self.original_view.setObjectName("inspectorOriginalView")
        self.view_tabs.addTab(self.original_view, "Original scan")
        self.read_view = ScanPreviewView()
        self.read_view.setObjectName("inspectorReadView")
        self.view_tabs.addTab(self.read_view, "As read (ID and set code)")
        self.view_tabs.setMinimumHeight(280)
        self.view_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self.view_tabs, stretch=1)

        layout.addWidget(self._build_editors())

        self.status_label = QLabel("")
        self.status_label.setObjectName("inspectorStatusLabel")
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.clear()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_editors(self) -> QWidget:
        """One row per identity field: what it reads, and a box to correct it."""
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(Spacing.SM)
        grid.setVerticalSpacing(Spacing.XXS)
        for row, (kind, name) in enumerate(
            ((FieldKind.IDENTIFIER, "Identifier"), (FieldKind.SET_CODE, "SetCode"))
        ):
            reading = QLabel("")
            reading.setObjectName(f"inspector{name}ReadingLabel")
            reading.setTextFormat(Qt.TextFormat.RichText)
            edit = QLineEdit()
            edit.setObjectName(f"inspector{name}Edit")
            edit.setPlaceholderText("Correct value")
            edit.textChanged.connect(lambda _text, k=kind: self._refresh_preview(k))
            edit.returnPressed.connect(lambda k=kind: self.apply_correction(k))
            apply = QPushButton("Apply")
            apply.setObjectName(f"inspector{name}ApplyButton")
            apply.clicked.connect(lambda _checked=False, k=kind: self.apply_correction(k))
            grid.addWidget(reading, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(apply, row, 2)
            self._editors[kind] = (reading, edit, apply)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)

        reason_row = QHBoxLayout()
        reason_row.setContentsMargins(0, 0, 0, 0)
        reason_row.addWidget(QLabel("Reason"))
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("inspectorReasonCombo")
        for code in ReasonCode:
            if code is ReasonCode.MACHINE_CONFIRMED:
                continue  # A correction is never "the machine was right".
            self.reason_combo.addItem(code.label, userData=code.value)
        reason_row.addWidget(self.reason_combo, stretch=3)
        self.note_edit = QLineEdit()
        self.note_edit.setObjectName("inspectorNoteEdit")
        self.note_edit.setPlaceholderText("Note (required for 'Other')")
        reason_row.addWidget(self.note_edit, stretch=3)
        grid.addLayout(reason_row, 2, 0, 1, 3)

        self.preview_label = QLabel("")
        self.preview_label.setObjectName("inspectorPreviewLabel")
        self.preview_label.setTextFormat(Qt.TextFormat.RichText)
        self.preview_label.setWordWrap(True)
        grid.addWidget(self.preview_label, 3, 0, 1, 2)
        self.undo_button = QPushButton("Undo Correction")
        self.undo_button.setObjectName("inspectorUndoButton")
        self.undo_button.setToolTip(
            "Take back the last correction made here, as one action. It stays "
            "in the history."
        )
        self.undo_button.clicked.connect(self.undo_last)
        grid.addWidget(self.undo_button, 3, 2, Qt.AlignmentFlag.AlignTop)
        return holder

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------
    def set_context(
        self,
        database: ProjectDatabase | None,
        batch_id: str | None,
        template: OmrTemplate | None,
        reviewer: str,
    ) -> None:
        """Adopt the project, batch, template and reviewer corrections are made in."""
        self._database = database
        self._batch_id = batch_id
        self._template = template
        self._reviewer = reviewer.strip()
        self._refresh_enabled()

    def set_reason_context(self, text: str) -> None:
        """Say, in the ledger, why the operator is looking at this sheet."""
        self._context = text.strip()

    @property
    def scan_id(self) -> int | None:
        """The script being shown, or ``None``."""
        return self._scan_id

    @property
    def bundle(self) -> SheetBundle | None:
        """The loaded sheet, once it has arrived."""
        return self._bundle

    @property
    def last_edit(self) -> FieldEdit | None:
        """The most recent correction made here, until it is undone."""
        return self._last_edit

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Show nothing, and say how to show something."""
        self._scan_id = None
        self._bundle = None
        self._records = []
        self._provenance = {}
        self.title_label.setText(
            "<i>Choose a script and press <b>Inspect</b> to see its scan here.</i>"
        )
        for view in (self.original_view, self.read_view):
            view.clear()
            view.set_placeholder("No script selected.")
        for reading, edit, _apply in self._editors.values():
            reading.setText("")
            edit.clear()
        self.preview_label.setText("")
        self.status_label.setText("")
        self._refresh_enabled()

    def show_script(self, scan_id: int, *, title: str = "") -> bool:
        """Load one script's scan in the background.

        Returns:
            Whether loading started. ``False`` when there is no project, no
            template to read the sheet with, or no file behind the scan.
        """
        database = self._database
        if database is None or self._batch_id is None:
            return False
        self._scan_id = scan_id
        # The scan's own batch, which is where its review records live. Not
        # always the page's: a confirmed rescan read into a later batch is
        # reconciled in its original's batch, and a correction to it must be
        # filed where the rest of its ledger is.
        self._scan_batch_id = scan_lifecycle.batch_of(database, scan_id) or self._batch_id
        self._bundle = None
        self._last_edit = None
        self._reload_records()
        self.title_label.setText(f"<b>{html.escape(title or f'Scan {scan_id}')}</b>")
        if self._template is None:
            message = (
                "The project's template is not loaded, so this sheet cannot be "
                "re-read. Choose the template on the Template stage."
            )
            for view in (self.original_view, self.read_view):
                view.clear()
                view.set_placeholder(message)
            self._refresh_enabled()
            return False
        source = scan_source_path(database, scan_id)
        if not source:
            for view in (self.original_view, self.read_view):
                view.clear()
                view.set_placeholder("This scan has no file recorded.")
            self._refresh_enabled()
            return False
        for view in (self.original_view, self.read_view):
            view.clear()
            view.set_placeholder("Loading the scan...")
        self._start_worker(Path(source))
        self._refresh_enabled()
        return True

    def _start_worker(self, path: Path) -> None:
        """Start the shared sheet loader, superseding any running one."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.ready.disconnect()
        assert self._template is not None
        worker = SheetWorker(path, self._template, self)
        worker.ready.connect(self._on_ready)
        self._worker = worker
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()

    def _on_ready(self, bundle: SheetBundle) -> None:
        """Adopt the loaded sheet. Runs on the GUI thread."""
        self._bundle = bundle
        self._draw()
        self._refresh_readings()
        self._refresh_enabled()
        self.loaded.emit()

    def _reload_records(self) -> None:
        """Re-read this sheet's review records and where their values come from."""
        database = self._database
        batch_id = self._scan_batch_id or self._batch_id
        if database is None or batch_id is None or self._scan_id is None:
            self._records, self._provenance = [], {}
            return
        self._records = list(
            list_conflicts(
                database,
                batch_id,
                filters=ConflictFilter(scan_id=self._scan_id, include_withdrawn=True),
            )
        )
        self._provenance = provenance_for_scan(database, batch_id, self._scan_id)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _draw(self) -> None:
        """Draw the original scan and the read sheet."""
        bundle = self._bundle
        if bundle is None:
            return
        if bundle.original is not None:
            self.original_view.set_placeholder("")
            self.original_view.set_page(
                bundle.original,
                canonical_width=bundle.original.width,
                canonical_height=bundle.original.height,
            )
            self.original_view.set_overlay((), (), ())
            self.original_view.set_scan_quality(None)
            self.original_view.set_overlay_visible(zones=False, bubbles=False, empty=False)
            self.original_view.fit_to_window()
        else:
            self.original_view.clear()
            self.original_view.set_placeholder(
                bundle.error or "The original scan could not be decoded."
            )

        result = bundle.result
        if result is None or result.preview is None:
            self.read_view.clear()
            self.read_view.set_placeholder(
                bundle.error
                or "This sheet did not register, so there is no reliable map from "
                "the template's fields to the paper. The original scan is still "
                "shown; the Student ID cannot be edited from here."
            )
            return
        zones = {zone for zone in (result.identifier_zone_id, result.set_code_zone_id) if zone}
        highlighted = tuple(item for item in result.bubbles if item.zone_id in zones)
        self.read_view.set_placeholder("")
        self.read_view.set_page(
            result.preview,
            canonical_width=result.canonical_width,
            canonical_height=result.canonical_height,
            preview_scale=result.preview_scale,
        )
        self.read_view.set_overlay(result.zones, highlighted, ())
        self.read_view.set_scan_quality(result.scan_quality)
        self.read_view.set_overlay_visible(
            zones=True,
            bubbles=True,
            empty=True,
            centers=True,
            status_symbols=False,
            status_colors=False,
        )
        self._redraw_lanes()
        if highlighted:
            left, top, right, bottom = bounds_of(highlighted)
            self.read_view.focus_on(QRectF(left, top, right - left, bottom - top))
        else:
            self.read_view.fit_to_window()

    def _redraw_lanes(self) -> None:
        """Draw every decision on this sheet's identity fields.

        The same lanes the Resolve stage draws, so an override made here looks
        the same there.
        """
        bundle = self._bundle
        if bundle is None or bundle.result is None or self._template is None:
            return
        records = [item for item in self._records if item.field.kind.is_record_identity]
        self.read_view.set_lanes(
            build_lanes(bundle.result, self._template, records, self._provenance)
        )

    # ------------------------------------------------------------------
    # The fields
    # ------------------------------------------------------------------
    def field_shape_for(self, kind: FieldKind) -> FieldShape | None:
        """The shape of one identity field on the loaded sheet, or ``None``.

        ``None`` until the sheet has loaded, and for a sheet that never
        registered - the same guard the Resolve stage applies.
        """
        bundle = self._bundle
        if self._template is None or bundle is None or bundle.result is None:
            return None
        result = bundle.result
        if result.preview is None:
            return None
        zone_id = (
            result.identifier_zone_id if kind is FieldKind.IDENTIFIER else result.set_code_zone_id
        )
        if not zone_id:
            return None
        return field_shape(self._template, zone_id)

    def machine_value(self, kind: FieldKind) -> str:
        """What recognition read for one field, as it assembled it."""
        bundle = self._bundle
        result = bundle.result if bundle is not None else None
        if result is None:
            return ""
        zone_id = (
            result.identifier_zone_id if kind is FieldKind.IDENTIFIER else result.set_code_zone_id
        )
        found = next((item for item in result.fields if item.zone_id == zone_id), None)
        return found.value if found is not None else ""

    def current_values(self, kind: FieldKind) -> list[str] | None:
        """One field as it currently reads, per position, or ``None``."""
        shape = self.field_shape_for(kind)
        if shape is None:
            return None
        return current_field_values(
            shape, self._records, self._provenance, self._machine(shape)
        )

    def effective_value(self, kind: FieldKind) -> str:
        """One field as it now reads, after every decision."""
        values = self.current_values(kind)
        return join_field_value(values) if values is not None else ""

    def _machine(self, shape: FieldShape) -> dict[int, str]:
        bundle = self._bundle
        return machine_characters(bundle.result if bundle is not None else None, shape)

    def _refresh_readings(self) -> None:
        """Say what each field was read as and what it reads as now."""
        for kind, (reading, edit, _apply) in self._editors.items():
            shape = self.field_shape_for(kind)
            if shape is None:
                reading.setText(
                    f"<b>{'Student ID' if kind is FieldKind.IDENTIFIER else 'Set code'}"
                    "</b>: not available on this sheet"
                )
                edit.clear()
                continue
            machine = self.machine_value(kind) or "(not read)"
            now = self.effective_value(kind)
            changed = now != self.machine_value(kind)
            reading.setText(
                f"<b>{html.escape(shape.label)}</b>: read "
                f"<b>{html.escape(machine)}</b>"
                + (
                    f" &rarr; now <b style='color:{Color.PRIMARY};'>"
                    f"{html.escape(now)}</b>"
                    if changed
                    else ""
                )
            )
            reading.setToolTip(
                f"Machine reading: {machine}\nEffective value: {now}\n"
                f"{shape.length} position(s) on the template"
            )
            edit.blockSignals(True)
            edit.setText(now)
            edit.blockSignals(False)
        self.preview_label.setText("")

    def editor(self, kind: FieldKind) -> QLineEdit:
        """The correction box for one field, for a test or a shortcut."""
        return self._editors[kind][1]

    def apply_button(self, kind: FieldKind) -> QPushButton:
        """The Apply button for one field."""
        return self._editors[kind][2]

    def plan(self, kind: FieldKind) -> tuple[FieldEditPlan | None, str]:
        """What the typed value for one field would do, or why it cannot."""
        shape = self.field_shape_for(kind)
        if shape is None:
            return None, "This field cannot be edited on this sheet."
        values, problem = validate_field_value(shape, self.editor(kind).text())
        if values is None:
            return None, problem
        return (
            plan_field_edit(shape, values, self._records, self._provenance, self._machine(shape)),
            "",
        )

    def _refresh_preview(self, kind: FieldKind) -> None:
        """Validate what is typed and say, before anything is written, what it does."""
        plan, problem = self.plan(kind)
        _reading, _edit, apply = self._editors[kind]
        if plan is None:
            self.preview_label.setText(
                f"<span style='color:{Color.DESTRUCTIVE};'>{html.escape(problem)}</span>"
            )
            apply.setText("Apply")
            self._refresh_enabled()
            return
        if plan.is_empty:
            self.preview_label.setText(
                f"<span style='color:{Color.TEXT_TERTIARY};'>Already recorded; "
                "nothing to change.</span>"
            )
            apply.setText("Apply")
            self._refresh_enabled()
            return
        parts = [
            f"<b>{html.escape(join_field_value(plan.current))}</b> &rarr; "
            f"<b>{html.escape(join_field_value(plan.proposed))}</b>"
        ]
        parts.extend(
            f"<span style='color:{Color.STATUS_BUSY};'>Position {position + 1} "
            f"&mdash; confident machine read ({html.escape(machine or '(blank)')} "
            f"&rarr; {html.escape(typed or '(blank)')})</span>"
            for position, (machine, typed) in sorted(plan.overrides.items())
        )
        parts.append(
            "Changed positions: <b>"
            + ", ".join(str(item + 1) for item in plan.positions)
            + "</b>"
        )
        self.preview_label.setText(
            f"<span style='color:{Color.TEXT_TERTIARY};'>"
            + " &middot; ".join(parts)
            + "</span>"
        )
        apply.setText("Apply Override..." if plan.overrides else "Apply")
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def confirm_override(self, shape: FieldShape, overrides: dict[int, tuple[str, str]]) -> bool:
        """Ask before a typed value overrules a confident machine reading.

        The same warning, wording and default (Cancel) as the Resolve stage's.
        """
        box = QMessageBox(self)
        box.setObjectName("fieldOverrideConfirmation")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Manual field override")
        box.setText(override_warning_text(shape.label, overrides))
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        apply = box.addButton("Apply override", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is apply

    def _reason(self) -> ReasonCode:
        return ReasonCode(self.reason_combo.currentData())

    def select_reason(self, reason: ReasonCode) -> None:
        """Pre-select the reason most likely to apply."""
        index = self.reason_combo.findData(reason.value)
        if index >= 0:
            self.reason_combo.setCurrentIndex(index)

    def apply_correction(self, kind: FieldKind) -> bool:
        """Store the typed value for one field. One audited, undoable action.

        Returns:
            Whether anything was stored.
        """
        database = self._database
        shape = self.field_shape_for(kind)
        if (
            database is None
            or self._batch_id is None
            or self._scan_id is None
            or shape is None
            or not self.apply_button(kind).isEnabled()
        ):
            return False
        plan, problem = self.plan(kind)
        if plan is None or plan.is_empty:
            _LOGGER.info("Correction refused: %s", problem or "nothing to change")
            return False
        if plan.overrides and not self.confirm_override(shape, plan.overrides):
            return False
        bundle = self._bundle
        try:
            edit = commit(
                database,
                batch_id=self._scan_batch_id or self._batch_id,
                scan_id=self._scan_id,
                shape=shape,
                kind=kind,
                plan=plan,
                records=self._records,
                result=bundle.result if bundle is not None else None,
                reviewer=self._reviewer,
                reason=self._reason(),
                reason_text=self.note_edit.text(),
                context=self._context,
            )
        except OMRScannerError as exc:
            QMessageBox.warning(self, "Correction not stored", exc.user_message or str(exc))
            return False
        self._last_edit = edit
        self.note_edit.clear()
        self._after_write()
        self.status_label.setText(
            f"<span style='color:{Color.STATUS_READY};'>{html.escape(shape.label)} "
            f"recorded as <b>{html.escape(edit.value)}</b>. The machine's reading "
            "is kept in the history.</span>"
        )
        _LOGGER.info(
            "Scan %d: %s corrected from the inspector (%d position(s), %d override(s))",
            self._scan_id,
            kind.value,
            edit.conflict_count,
            len(edit.overridden),
        )
        self.corrected.emit(self._scan_id)
        return True

    def undo_last(self) -> bool:
        """Take back the last correction made here, as one action."""
        database = self._database
        edit = self._last_edit
        if database is None or self._batch_id is None or edit is None or self._scan_id is None:
            return False
        try:
            undone = undo_field_edit(
                database,
                batch_id=self._scan_batch_id or self._batch_id,
                group=edit.group,
                reviewer=self._reviewer,
            )
        except OMRScannerError as exc:
            QMessageBox.warning(self, "Nothing undone", exc.user_message or str(exc))
            return False
        self._last_edit = None
        self._after_write()
        self.status_label.setText(
            f"<span style='color:{Color.TEXT_TERTIARY};'>Correction undone "
            f"({len(undone)} position(s)). It remains in the history.</span>"
        )
        self.corrected.emit(self._scan_id)
        return bool(undone)

    def _after_write(self) -> None:
        """Re-read what a write changed and redraw it."""
        self._reload_records()
        self._redraw_lanes()
        self._refresh_readings()
        self._refresh_enabled()

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_enabled(self) -> None:
        named = bool(self._reviewer)
        for kind, (_reading, edit, apply) in self._editors.items():
            shape = self.field_shape_for(kind)
            editable = shape is not None and self._database is not None
            edit.setEnabled(editable)
            plan, _problem = self.plan(kind) if editable else (None, "")
            apply.setEnabled(editable and named and plan is not None and not plan.is_empty)
            apply.setToolTip(
                ""
                if named
                else "Add a reviewer name in File > Settings - a correction is "
                "recorded against the person who made it."
            )
        self.undo_button.setEnabled(self._last_edit is not None and named)
        self.reason_combo.setEnabled(self._scan_id is not None)
        self.note_edit.setEnabled(self._scan_id is not None)

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Wait for every loader this widget started."""
        workers = self._workers
        self._worker = None
        self._workers = []
        for worker in workers:
            if worker.isRunning():
                worker.wait(10_000)

    def closeEvent(self, event: object) -> None:
        """Join the loaders before the widget goes away."""
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]


__all__ = ["EDITOR_KINDS", "ScriptInspector"]
