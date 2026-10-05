"""The operational scan-session panel on the Scan stage (0.1.1 revised phase 8).

Purpose:
    Make a continuous scan session's backend facts visible and controllable
    in one compact strip above the Scan stage's workspace: which session,
    open or closed, what it is doing, the **three separate progress lines**,
    the live counts, warnings, the scanner sources, and the session's actions.

    It renders values, nothing else: a
    :class:`~omr_scanner.gui.scan.session_poller.SessionView` (the session
    snapshot, the session's lifecycle and its configured sources) and the
    engine runner's state. Every count, every activity and every source
    state comes from :mod:`omr_scanner.services.session_snapshot`; the
    functions below only choose words for them.

Never one percentage:
    Recognition, conflict resolution and rescans measure different work and
    are drawn as three bars with their own counts. No combined or "overall"
    figure is computed or shown, and no bar shows a percentage - recognition
    can legitimately fall as files arrive, and a percentage invites reading
    it as "how finished the examination is", which none of them is.

"Caught up" is not "complete":
    The activity line is the snapshot's own label. An open session is never
    called complete, finished or done here, however quiet it is.

What does NOT belong here:
    Service calls and dialogs. The panel emits a signal per action; the
    session-mode controller (:mod:`omr_scanner.gui.scan.session_mode`) acts.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, SignalInstance
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.intake import Reachability, SourceKind
from omr_scanner.domain.session_controls import ProcessingIntent
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.gui.theme import Color, FontSize, Spacing
from omr_scanner.gui.ui_scale import scale_layout, scale_widget, set_relative_font
from omr_scanner.gui.widgets.collapsible import CollapsibleSection

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from omr_scanner.domain.session_snapshot import (
        Partition,
        Progress,
        SessionSnapshot,
        SourceSnapshot,
    )
    from omr_scanner.gui.scan.session_poller import SessionView
    from omr_scanner.services.intake import SourceInfo

ENGINE_IDLE = "idle"
ENGINE_RUNNING = "running"
ENGINE_STOPPING = "stopping"
ENGINE_STARTING = "starting"

SOURCE_TABLE_ROWS_VISIBLE = 4
"""The sources table's height in rows before it scrolls: secondary, compact."""

PROGRESS_LINES = ("recognition", "conflicts", "rescans")


# ----------------------------------------------------------------------
# Words for values (pure; tested directly)
# ----------------------------------------------------------------------
def _clock(moment: datetime | None) -> str:
    return f"{moment.astimezone():%H:%M}" if moment is not None else ""


def activity_text(view: SessionView) -> str:
    """The session's activity, as the snapshot states it.

    The label is the snapshot's own (``SessionActivity.label``). Waiting for a
    source names which, and since when it has been unreachable when the
    source's last listing says so - the snapshot decided *that* it waits; this
    only names it.
    """
    snapshot = view.snapshot
    if snapshot.activity is SessionActivity.WAITING_FOR_SOURCE and snapshot.unreachable_sources:
        since = {item.label: item.reachability_changed_at for item in view.sources}
        names = []
        for label in snapshot.unreachable_sources:
            moment = since.get(label)
            names.append(f"{label} (unreachable since {_clock(moment)})" if moment else label)
        return "Waiting for " + " and ".join(names)
    return snapshot.activity.label


def lifecycle_text(view: SessionView) -> str:
    """``Open``, ``Closed`` or ``Open · reopened``."""
    state = view.snapshot.session_state
    if state == "closed":
        return "Closed"
    if view.session is not None and view.session.reopen_count:
        return "Open · reopened"
    return "Open"


def processing_text(snapshot: SessionSnapshot) -> str:
    """The persisted recognition intent, in words."""
    return {
        ProcessingIntent.RUNNING: "Processing: running",
        ProcessingIntent.PAUSED: "Processing: paused",
        ProcessingIntent.STOPPED: "Processing: stopped",
    }[snapshot.controls.processing]


def intake_text(view: SessionView) -> str:
    """The persisted intake intent, session-wide and per source."""
    controls = view.snapshot.controls
    if controls.intake_paused:
        return "Intake: paused"
    attached = {item.source_id for item in view.sources}
    paused = len(attached & set(controls.paused_sources))
    if paused:
        return f"Intake: on · {paused} source(s) paused"
    return "Intake: on"


def progress_text(progress: Progress, line: str) -> str:
    """One progress line's count, e.g. ``334 / 341 read``. Never a percentage."""
    if line == "recognition":
        return f"{progress.done:,} / {progress.total:,} read" if progress.total else "nothing yet"
    if line == "conflicts":
        return (
            f"{progress.done:,} / {progress.total:,} resolved" if progress.total else "none"
        )
    return f"{progress.done:,} / {progress.total:,} replaced" if progress.total else "none needed"


def counts_text(snapshot: SessionSnapshot) -> str:
    """The primary live counts, in a fixed order."""
    partition = snapshot.partition
    waiting = partition.ready + partition.queued
    return " · ".join(
        (
            f"Discovered {snapshot.discovered_excluding_ignored:,}",
            f"Stabilizing {partition.stabilizing:,}",
            f"Waiting {waiting:,}",
            f"Being read {partition.processing:,}",
            f"Accepted {partition.accepted:,}",
            f"Conflict {partition.conflict:,}",
            f"Rescan required {partition.rescan_required:,}",
        )
    )


_SECONDARY = (
    ("duplicate", "Duplicate"),
    ("superseded", "Superseded"),
    ("held", "Held"),
    ("unreadable_pending_decision", "Unreadable / unsupported"),
    ("deferred", "Deferred"),
    ("excluded", "Excluded"),
    ("counted_elsewhere", "Counted in another session"),
    ("vanished", "Vanished"),
)


def secondary_counts_text(partition: Partition) -> str:
    """The other buckets - only those that are not zero (secondary weight)."""
    values = partition.as_dict()
    return " · ".join(
        f"{label} {values[name]:,}" for name, label in _SECONDARY if values.get(name)
    )


def waiting_tooltip(partition: Partition) -> str:
    """What *Waiting* is made of."""
    return (
        f"{partition.ready:,} file(s) ready to be registered, "
        f"{partition.queued:,} sheet(s) registered and not read yet."
    )


def source_status_text(item: SourceSnapshot, info: SourceInfo | None) -> str:
    """One source's state: *Watching*, *Paused*, *Disabled*, *Unreachable since 10:42*..."""
    if not item.enabled:
        return "Disabled"
    if item.intake_paused:
        return "Paused"
    if item.reachability == Reachability.UNREACHABLE.value:
        moment = info.reachability_changed_at if info is not None else None
        return f"Unreachable since {_clock(moment)}" if moment else "Unreachable"
    if item.reachable:
        return "Watching" if item.reconciled_recently else "Checking"
    if item.reachability == Reachability.UNKNOWN.value:
        return "Not checked yet"
    return item.reachability.replace("_", " ").capitalize()


def alarm_text(item: SourceSnapshot) -> str:
    """A raised registration-failure alarm as a warning - never a verdict."""
    alarm = item.alarm
    if alarm is None or not alarm.raised:
        return ""
    return (
        f"High registration-failure rate on {item.label}: {alarm.failures} of its last "
        f"{alarm.samples} sheets could not be aligned. Check scanner alignment and template "
        "selection. (An uncalibrated warning, not a diagnosis.)"
    )


def warning_lines(view: SessionView, *, extra: tuple[str, ...] = ()) -> list[str]:
    """Everything the operator should notice now, most urgent first."""
    snapshot = view.snapshot
    lines = [*extra]
    lines += [text for text in (alarm_text(item) for item in snapshot.sources) if text]
    if snapshot.outstanding_suggestions:
        lines.append(
            f"{snapshot.outstanding_suggestions:,} suggested rescan(s) await an operator on "
            "Resolve. Suggestions use an unvalidated default quality policy; nothing is "
            "rejected without a named operator's confirmation."
        )
    if snapshot.pending_decisions:
        lines.append(
            f"{snapshot.pending_decisions:,} file(s) are held, unreadable or unsupported and "
            "await a decision on Resolve."
        )
    if snapshot.retry_processing:
        lines.append(
            f"{snapshot.retry_processing:,} sheet(s) could not be read by the software "
            "(not a paper problem)."
        )
    session = view.session
    if (
        session is not None
        and snapshot.session_state != "closed"
        and session.final_outputs_stale_since is not None
    ):
        lines.append(
            "This session was reopened: any final export generated while it was closed "
            "is stale and must be regenerated after it is closed again."
        )
    return lines


def engine_text(engine_state: str, status: object | None, *, session_open: bool) -> str:
    """Whether this window's continuous engine is running, and what it holds."""
    if not session_open:
        # A closed session reads nothing; the engine still lists the sources so
        # that a late file is held for a decision rather than missed.
        if engine_state == ENGINE_RUNNING:
            return (
                "Still watching the sources in this window: a file that arrives now is "
                "held for a decision on Resolve, not read"
            )
        return ""
    if engine_state == ENGINE_STARTING:
        return "Starting continuous scanning..."
    if engine_state == ENGINE_STOPPING:
        return "Stopping: sheets already being read finish and are saved..."
    if engine_state != ENGINE_RUNNING:
        return "Continuous scanning is not running in this window."
    in_flight = int(getattr(status, "in_flight", 0) or 0)
    backlog = int(getattr(status, "writer_backlog", 0) or 0)
    units = int(getattr(status, "units_registered", 0) or 0)
    parts = [f"{in_flight} sheet(s) with workers"]
    if backlog:
        parts.append(f"{backlog} waiting to be saved")
    parts.append(f"{units} batch(es) registered since start")
    return "Continuous scanning is running in this window · " + " · ".join(parts)


# ----------------------------------------------------------------------
# The panel
# ----------------------------------------------------------------------
class SessionPanel(QFrame):
    """The session strip: status, three progress lines, counts, sources, actions.

    Signals (one per action; the controller acts, nothing here does):
        start_requested, pause_processing_requested, resume_processing_requested,
        finish_current_requested, cancel_queued_requested,
        pause_intake_requested, resume_intake_requested,
        finish_session_requested, reopen_session_requested,
        add_source_requested, edit_source_requested(str),
        set_source_enabled_requested(str, bool), set_source_paused_requested(str, bool),
        detach_source_requested(str), details_toggled(bool), sources_toggled(bool).
    """

    start_requested = Signal()
    pause_processing_requested = Signal()
    resume_processing_requested = Signal()
    finish_current_requested = Signal()
    cancel_queued_requested = Signal()
    pause_intake_requested = Signal()
    resume_intake_requested = Signal()
    finish_session_requested = Signal()
    reopen_session_requested = Signal()
    add_source_requested = Signal()
    edit_source_requested = Signal(str)
    set_source_enabled_requested = Signal(str, bool)
    set_source_paused_requested = Signal(str, bool)
    detach_source_requested = Signal(str)
    details_toggled = Signal(bool)
    sources_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sessionPanel")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.view: SessionView | None = None
        self._engine_state = ENGINE_IDLE
        self._engine_status: object | None = None
        self._extra_warnings: tuple[str, ...] = ()
        self._can_write = True

        layout = QVBoxLayout(self)
        scale_layout(
            layout, margins=(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS), spacing=Spacing.XS
        )
        layout.addLayout(self._build_status_row())
        layout.addLayout(self._build_actions_row())
        # The three progress lines are built when a session is first shown
        # (:meth:`_ensure_progress`): a finite Scan stage, which never shows
        # one, keeps exactly the progress indicators it always had - one bar
        # for the batch, none per sheet or per mode it is not in.
        self._progress_host = QWidget()
        self._progress_host.setObjectName("sessionProgressHost")
        host_layout = QVBoxLayout(self._progress_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_bars: dict[str, QProgressBar] = {}
        self.progress_labels: dict[str, QLabel] = {}
        layout.addWidget(self._progress_host)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(details_layout, spacing=Spacing.XXS)
        self.counts_label = QLabel("")
        self.counts_label.setObjectName("sessionCountsLabel")
        self.counts_label.setWordWrap(True)
        details_layout.addWidget(self.counts_label)
        self.secondary_counts_label = QLabel("")
        self.secondary_counts_label.setObjectName("sessionSecondaryCountsLabel")
        self.secondary_counts_label.setWordWrap(True)
        set_relative_font(self.secondary_counts_label, FontSize.SECONDARY)
        self.secondary_counts_label.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
        details_layout.addWidget(self.secondary_counts_label)
        self.state_line_label = QLabel("")
        self.state_line_label.setObjectName("sessionStateLineLabel")
        self.state_line_label.setWordWrap(True)
        details_layout.addWidget(self.state_line_label)
        self.warnings_label = QLabel("")
        self.warnings_label.setObjectName("sessionWarningsLabel")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextFormat(Qt.TextFormat.RichText)
        self.warnings_label.setVisible(False)
        details_layout.addWidget(self.warnings_label)
        details_layout.addWidget(self._build_sources())
        self.details = CollapsibleSection("Session details", details, expanded=True)
        self.details.setObjectName("sessionDetailsSection")
        self.details.toggled.connect(self.details_toggled)
        layout.addWidget(self.details)
        self.refresh_controls()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        scale_layout(row, spacing=Spacing.SM)
        self.name_label = QLabel("")
        self.name_label.setObjectName("sessionNameLabel")
        self.name_label.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(self.name_label)
        self.lifecycle_label = QLabel("")
        self.lifecycle_label.setObjectName("sessionLifecycleLabel")
        self.lifecycle_label.setToolTip(
            "Open: the session accepts new scans and its results are provisional. "
            "Closed: finished by an operator; its results are final."
        )
        row.addWidget(self.lifecycle_label)
        self.activity_label = QLabel("")
        self.activity_label.setObjectName("sessionActivityLabel")
        self.activity_label.setWordWrap(True)
        self.activity_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.activity_label.setToolTip(
            "What the session is doing now, from the project database. \"Caught up\" means "
            "nothing is waiting at this moment - not that the examination is finished; "
            "only Finish Scan Session closes it."
        )
        row.addWidget(self.activity_label, stretch=1)
        return row

    def _build_actions_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        scale_layout(row, spacing=Spacing.XS)

        # -- Processing -----------------------------------------------
        self.start_button = QPushButton("Start Continuous Scan")
        self.start_button.setObjectName("startContinuousScanButton")
        self.start_button.setToolTip(
            "Watch the session's sources, register new files once they are complete and read "
            "them as they arrive. The saved pause settings apply."
        )
        self.start_button.clicked.connect(self.start_requested)
        row.addWidget(self.start_button)

        self.processing_button = QPushButton("Pause Processing")
        self.processing_button.setObjectName("processingPauseButton")
        self.processing_button.clicked.connect(self._on_processing_clicked)
        row.addWidget(self.processing_button)

        self.stop_button = QToolButton()
        self.stop_button.setObjectName("sessionStopButton")
        self.stop_button.setText("Stop")
        self.stop_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.stop_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.stop_button.setAccessibleName("Stop continuous scanning")
        stop_menu = QMenu(self.stop_button)
        stop_menu.setObjectName("sessionStopMenu")
        self.finish_current_action = stop_menu.addAction("Finish Current and Stop")
        self.finish_current_action.setObjectName("finishCurrentAction")
        self.finish_current_action.setToolTip(
            "Stop taking new work, but safely finish and save the sheets already being read."
        )
        self.finish_current_action.triggered.connect(self.finish_current_requested)
        self.cancel_queued_action = stop_menu.addAction("Cancel Queued Work...")
        self.cancel_queued_action.setObjectName("cancelQueuedAction")
        self.cancel_queued_action.setToolTip(
            "Return sheets not yet started to pending. Sheets already being read still finish "
            "and are saved. Nothing is deleted."
        )
        self.cancel_queued_action.triggered.connect(self.cancel_queued_requested)
        self.stop_button.setMenu(stop_menu)
        self.stop_button.setToolTip("Finish current work and stop, or cancel queued work")
        row.addWidget(self.stop_button)

        self._actions_separator = _separator()
        row.addWidget(self._actions_separator)

        # -- Intake ---------------------------------------------------
        self.intake_button = QPushButton("Pause Intake")
        self.intake_button.setObjectName("intakePauseButton")
        self.intake_button.clicked.connect(self._on_intake_clicked)
        row.addWidget(self.intake_button)
        self.add_source_button = QPushButton("Add Source...")
        self.add_source_button.setObjectName("addSourceButton")
        self.add_source_button.setToolTip(
            "Add a scanner folder (local or network path) whose new files join this session"
        )
        self.add_source_button.clicked.connect(self.add_source_requested)
        row.addWidget(self.add_source_button)

        row.addStretch(1)

        # -- Session --------------------------------------------------
        self.finish_session_button = QPushButton("Finish Scan Session...")
        self.finish_session_button.setObjectName("finishScanSessionButton")
        self.finish_session_button.setToolTip(
            "Give every source a final check and close the session if nothing blocks it. "
            "Its results then become final. Every blocker is listed if it cannot close."
        )
        self.finish_session_button.clicked.connect(self.finish_session_requested)
        row.addWidget(self.finish_session_button)
        self.reopen_session_button = QPushButton("Reopen Session...")
        self.reopen_session_button.setObjectName("reopenScanSessionButton")
        self.reopen_session_button.setToolTip(
            "Make the closed session open again (audited). Results become provisional and "
            "final exports generated while it was closed become stale."
        )
        self.reopen_session_button.clicked.connect(self.reopen_session_requested)
        row.addWidget(self.reopen_session_button)
        return row

    def _ensure_progress(self) -> None:
        """Build the three progress lines once, the first time a session is shown."""
        if self.progress_bars:
            return
        layout = self._progress_host.layout()
        assert layout is not None
        layout.addWidget(self._build_progress())

    def _build_progress(self) -> QWidget:
        box = QWidget()
        box.setObjectName("sessionProgressBox")
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        scale_layout(grid, horizontal_spacing=Spacing.SM, vertical_spacing=Spacing.XXS)
        titles = {
            "recognition": (
                "Recognition",
                "Sheets read of those discovered (less duplicates and vanished files). It can "
                "fall when new files arrive - that is more work, not a fault.",
            ),
            "conflicts": (
                "Conflicts",
                "Required conflicts resolved on Resolve, of all that exist on counted sheets.",
            ),
            "rescans": (
                "Rescans",
                "Rejected sheets replaced by a confirmed rescan, of those that need or needed "
                "one (including unanswered suggestions).",
            ),
        }
        for column, line in enumerate(PROGRESS_LINES):
            title, tip = titles[line]
            heading = QLabel(title)
            heading.setObjectName(f"{line}ProgressTitle")
            heading.setToolTip(tip)
            bar = QProgressBar()
            bar.setObjectName(f"{line}ProgressBar")
            bar.setTextVisible(False)
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setAccessibleName(f"{title} progress")
            bar.setToolTip(tip)
            scale_widget(bar, fixed_height=8)
            label = QLabel("")
            label.setObjectName(f"{line}ProgressLabel")
            label.setToolTip(tip)
            grid.addWidget(heading, 0, column * 2)
            grid.addWidget(label, 0, column * 2 + 1, alignment=Qt.AlignmentFlag.AlignRight)
            grid.addWidget(bar, 1, column * 2, 1, 2)
            grid.setColumnStretch(column * 2 + 1, 1)
            self.progress_bars[line] = bar
            self.progress_labels[line] = label
        return box

    def _build_sources(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.XXS)
        self.sources_table = QTableWidget(0, 9)
        self.sources_table.setObjectName("sourcesTable")
        self.sources_table.setHorizontalHeaderLabels(
            [
                "Source",
                "Status",
                "Received",
                "Read",
                "Accepted",
                "Conflict",
                "Rescan",
                "Duplicate",
                "Last check · rate",
            ]
        )
        self.sources_table.verticalHeader().setVisible(False)
        self.sources_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sources_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sources_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sources_table.setWordWrap(False)
        header = self.sources_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(2, 9):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.sources_table.itemSelectionChanged.connect(self.refresh_controls)
        layout.addWidget(self.sources_table)

        buttons = QHBoxLayout()
        scale_layout(buttons, spacing=Spacing.XS)
        self.edit_source_button = QPushButton("Edit...")
        self.edit_source_button.setObjectName("editSourceButton")
        self.edit_source_button.setToolTip("Change the selected source's name or options")
        self.edit_source_button.clicked.connect(
            lambda: self._emit_for_selected(self.edit_source_requested)
        )
        buttons.addWidget(self.edit_source_button)
        self.source_enable_button = QPushButton("Disable")
        self.source_enable_button.setObjectName("sourceEnableButton")
        self.source_enable_button.clicked.connect(self._on_source_enable_clicked)
        buttons.addWidget(self.source_enable_button)
        self.source_pause_button = QPushButton("Pause Source")
        self.source_pause_button.setObjectName("sourcePauseButton")
        self.source_pause_button.clicked.connect(self._on_source_pause_clicked)
        buttons.addWidget(self.source_pause_button)
        self.detach_source_button = QPushButton("Remove from Session...")
        self.detach_source_button.setObjectName("detachSourceButton")
        self.detach_source_button.setToolTip(
            "Stop taking new files from this source into the session. Files and sheets it "
            "already delivered, and their history, are kept."
        )
        self.detach_source_button.clicked.connect(
            lambda: self._emit_for_selected(self.detach_source_requested)
        )
        buttons.addWidget(self.detach_source_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.sources_section = CollapsibleSection("Sources", content, expanded=False)
        self.sources_section.setObjectName("sessionSourcesSection")
        self.sources_section.toggled.connect(self.sources_toggled)
        return self.sources_section

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def set_can_write(self, writable: bool) -> None:
        """A read-only project shows everything and offers no action."""
        self._can_write = writable
        self.refresh_controls()

    def set_engine_state(self, state: str, status: object | None = None) -> None:
        """This window's engine runner: idle, starting, running or stopping."""
        self._engine_state = state
        if status is not None:
            self._engine_status = status
        if state == ENGINE_IDLE:
            self._engine_status = None
        self._render_activity()
        self._render_state_line()
        self.refresh_controls()

    def set_extra_warnings(self, lines: tuple[str, ...]) -> None:
        """Messages the controller adds (a refusal by the coordinator lease, an error)."""
        self._extra_warnings = lines
        self._render_warnings()

    def show_view(self, view: SessionView) -> None:
        """Show one session view. Every value comes from it; nothing is kept beyond it."""
        self._ensure_progress()
        self.view = view
        snapshot = view.snapshot
        name = view.session.name if view.session is not None else "(scan session)"
        self.name_label.setText(f"Session: <b>{html.escape(name)}</b>")
        lifecycle = lifecycle_text(view)
        self.lifecycle_label.setText(lifecycle.upper() if lifecycle != "Open · reopened" else
                                     "OPEN · REOPENED")
        closed = snapshot.session_state == "closed"
        self.lifecycle_label.setStyleSheet(
            f"color: {Color.TEXT_SECONDARY if closed else Color.STATUS_READY}; font-weight: 600;"
        )
        self._render_activity()
        for line, progress in (
            ("recognition", snapshot.recognition),
            ("conflicts", snapshot.conflicts),
            ("rescans", snapshot.rescans),
        ):
            bar = self.progress_bars[line]
            # Exact counts, no conversion: the bar's maximum is the current
            # total, so a total that grew moves the bar back - correctly.
            bar.setRange(0, max(progress.total, 1))
            bar.setValue(min(progress.done, progress.total) if progress.total else 0)
            self.progress_labels[line].setText(progress_text(progress, line))
        self.counts_label.setText(counts_text(snapshot))
        self.counts_label.setToolTip(waiting_tooltip(snapshot.partition))
        secondary = secondary_counts_text(snapshot.partition)
        self.secondary_counts_label.setText(secondary)
        self.secondary_counts_label.setVisible(bool(secondary))
        self._render_state_line()
        self._render_warnings()
        self._render_sources(view)
        self.refresh_controls()

    def _render_activity(self) -> None:
        """The snapshot's activity word - qualified when no engine here is doing it."""
        view = self.view
        if view is None:
            return
        snapshot = view.snapshot
        activity = activity_text(view)
        colour = {
            SessionActivity.CAUGHT_UP: Color.STATUS_READY,
            SessionActivity.PROCESSING: Color.STATUS_BUSY,
            SessionActivity.WAITING_FOR_SOURCE: Color.STATUS_ERROR,
        }.get(snapshot.activity, Color.TEXT_PRIMARY)
        if snapshot.activity is SessionActivity.PROCESSING and self._engine_state == ENGINE_IDLE:
            # Work is outstanding, but nothing in this window is reading it
            # (after reopening an interrupted session, say): never imply motion.
            activity += " (not running in this window)"
            colour = Color.TEXT_PRIMARY
        # "CLOSED Closed" says one thing twice: the lifecycle word is enough.
        shown = "" if activity.casefold() == lifecycle_text(view).casefold() else activity
        self.activity_label.setText(shown)
        self.activity_label.setStyleSheet(f"color: {colour}; font-weight: 600;")
        self.activity_label.setAccessibleName(f"Session activity: {activity}")

    def _render_state_line(self) -> None:
        view = self.view
        if view is None:
            self.state_line_label.setText("")
            self.state_line_label.setVisible(False)
            return
        snapshot = view.snapshot
        session_open = snapshot.session_state != "closed"
        parts = [processing_text(snapshot), intake_text(view)] if session_open else []
        engine = engine_text(self._engine_state, self._engine_status, session_open=session_open)
        if engine:
            parts.append(engine)
        self.state_line_label.setText(" · ".join(parts))
        self.state_line_label.setVisible(bool(parts))

    def _render_warnings(self) -> None:
        view = self.view
        lines = (
            warning_lines(view, extra=self._extra_warnings)
            if view is not None
            else list(self._extra_warnings)
        )
        self.warnings_label.setText(
            "<br>".join(
                f"<span style='color:{Color.STATUS_BUSY};'>⚠ {html.escape(line)}</span>"
                for line in lines
            )
        )
        self.warnings_label.setVisible(bool(lines))

    def _render_sources(self, view: SessionView) -> None:
        selected = self.selected_source_id()
        info_of = {item.source_id: item for item in view.sources}
        watched = [
            item
            for item in view.snapshot.sources
            if item.kind == SourceKind.WATCHED.value
        ]
        table = self.sources_table
        table.setRowCount(len(watched))
        for row, item in enumerate(watched):
            info = info_of.get(item.source_id)
            received = (
                item.registered + item.stabilizing + item.ready + item.held + item.unreadable
            )
            last = (
                f"{_clock(item.last_reconciled_at)}"
                if item.last_reconciled_at is not None
                else "never"
            )
            if item.rate_per_minute is not None:
                last += f" · {item.rate_per_minute:.1f}/min"
            status = source_status_text(item, info)
            if item.alarm is not None and item.alarm.raised:
                status += " · ⚠ alignment"
            values = (
                item.label,
                status,
                f"{received:,}",
                f"{item.processed:,}",
                f"{item.accepted:,}",
                f"{item.conflict:,}",
                f"{item.rescan_required:,}",
                f"{item.duplicate:,}",
                last,
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, item.source_id)
                if column == 0 and info is not None:
                    cell.setToolTip(
                        f"{info.root_path}\n"
                        + (f"Last file: {info.last_file_seen_path}" if info.last_file_seen_path
                           else "No file seen yet")
                    )
                elif column == 1:
                    cell.setToolTip(alarm_text(item) or status)
                table.setItem(row, column, cell)
        rows = max(1, min(len(watched), SOURCE_TABLE_ROWS_VISIBLE))
        header_height = table.horizontalHeader().sizeHint().height()
        row_height = table.verticalHeader().defaultSectionSize()
        table.setFixedHeight(header_height + rows * row_height + 2 * table.frameWidth() + 2)
        if selected is not None:
            self.select_source(selected)
        unreachable = sum(
            1 for item in watched if item.enabled and not item.intake_paused and not item.reachable
        )
        paused = sum(1 for item in watched if item.intake_paused and item.enabled)
        summary = [f"{len(watched)} source(s)"]
        if unreachable:
            summary.append(f"{unreachable} unreachable")
        if paused:
            summary.append(f"{paused} paused")
        alarms = sum(1 for item in watched if item.alarm is not None and item.alarm.raised)
        if alarms:
            summary.append(f"{alarms} alignment warning(s)")
        self.sources_section.header.setText(f"Sources ({len(watched)})")
        self.sources_section.set_summary(" · ".join(summary))

    # ------------------------------------------------------------------
    # Selection and enablement
    # ------------------------------------------------------------------
    def selected_source_id(self) -> str | None:
        """The source selected in the table, or ``None``."""
        items = self.sources_table.selectedItems()
        if not items:
            return None
        value = items[0].data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def select_source(self, source_id: str) -> bool:
        """Select a source's row by its id (stable across refreshes)."""
        for row in range(self.sources_table.rowCount()):
            item = self.sources_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == source_id:
                self.sources_table.blockSignals(True)
                self.sources_table.selectRow(row)
                self.sources_table.blockSignals(False)
                self.refresh_controls()
                return True
        return False

    def _source_snapshot(self, source_id: str | None) -> SourceSnapshot | None:
        if source_id is None or self.view is None:
            return None
        return next(
            (item for item in self.view.snapshot.sources if item.source_id == source_id), None
        )

    def refresh_controls(self) -> None:
        """Enable and word the actions from the last view and the runner - never optimistically."""
        view = self.view
        writable = self._can_write and view is not None
        session_open = view is not None and view.snapshot.session_state != "closed"
        running = self._engine_state in (ENGINE_RUNNING, ENGINE_STARTING)
        stopping = self._engine_state == ENGINE_STOPPING
        controls = view.snapshot.controls if view is not None else None

        self.start_button.setVisible(session_open and not running and not stopping)
        self.start_button.setEnabled(writable and session_open)
        processing = controls.processing if controls is not None else ProcessingIntent.RUNNING
        if processing is ProcessingIntent.RUNNING:
            self.processing_button.setText("Pause Processing")
            self.processing_button.setToolTip(
                "Claim no new sheets. Sheets already being read finish and are saved; "
                "nothing is killed. Intake (finding new files) is not paused by this."
            )
        else:
            self.processing_button.setText("Resume Processing")
            self.processing_button.setToolTip(
                "Claim and read sheets again. Starts continuous scanning in this window if "
                "it is not running."
            )
        self.processing_button.setVisible(session_open)
        self.processing_button.setEnabled(writable and session_open and not stopping)
        self.stop_button.setVisible(session_open)
        self.stop_button.setEnabled(writable and running)
        intake_paused = controls is not None and controls.intake_paused
        self.intake_button.setText("Resume Intake" if intake_paused else "Pause Intake")
        self.intake_button.setToolTip(
            "Look for and register new files from the sources again."
            if intake_paused
            else "Stop looking for and registering new files. Sheets already registered are "
            "still read unless processing is paused too. Nothing on disk is lost."
        )
        self.intake_button.setVisible(session_open)
        self.intake_button.setEnabled(writable and session_open)
        self._actions_separator.setVisible(session_open)
        self.add_source_button.setVisible(session_open)
        self.add_source_button.setEnabled(writable and session_open)
        self.finish_session_button.setVisible(session_open)
        self.finish_session_button.setEnabled(writable and session_open and not stopping)
        self.reopen_session_button.setVisible(view is not None and not session_open)
        self.reopen_session_button.setEnabled(writable and not session_open)

        selected = self._source_snapshot(self.selected_source_id())
        has_source = selected is not None
        self.edit_source_button.setEnabled(writable and has_source)
        self.detach_source_button.setEnabled(writable and has_source and session_open)
        enabled = selected.enabled if selected is not None else True
        self.source_enable_button.setText("Disable" if enabled else "Enable")
        self.source_enable_button.setToolTip(
            "Stop watching this source (configuration and history kept)."
            if enabled
            else "Watch this source again."
        )
        self.source_enable_button.setEnabled(writable and has_source)
        paused_source = (
            selected is not None
            and controls is not None
            and selected.source_id in controls.paused_sources
        )
        self.source_pause_button.setText("Resume Source" if paused_source else "Pause Source")
        self.source_pause_button.setToolTip(
            "Look for new files in this source again."
            if paused_source
            else "Stop looking for new files in this source only. A paused source is not an "
            "unreachable one."
        )
        self.source_pause_button.setEnabled(writable and has_source and session_open)

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------
    def _on_processing_clicked(self) -> None:
        view = self.view
        if view is not None and view.snapshot.controls.processing is ProcessingIntent.RUNNING:
            self.pause_processing_requested.emit()
        else:
            self.resume_processing_requested.emit()

    def _on_intake_clicked(self) -> None:
        view = self.view
        if view is not None and view.snapshot.controls.intake_paused:
            self.resume_intake_requested.emit()
        else:
            self.pause_intake_requested.emit()

    def _emit_for_selected(self, signal: SignalInstance) -> None:
        source_id = self.selected_source_id()
        if source_id is not None:
            signal.emit(source_id)

    def _on_source_enable_clicked(self) -> None:
        selected = self._source_snapshot(self.selected_source_id())
        if selected is not None:
            self.set_source_enabled_requested.emit(selected.source_id, not selected.enabled)

    def _on_source_pause_clicked(self) -> None:
        selected = self._source_snapshot(self.selected_source_id())
        view = self.view
        if selected is None or view is None:
            return
        paused = selected.source_id in view.snapshot.controls.paused_sources
        self.set_source_paused_requested.emit(selected.source_id, not paused)


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


__all__ = [
    "ENGINE_IDLE",
    "ENGINE_RUNNING",
    "ENGINE_STARTING",
    "ENGINE_STOPPING",
    "SessionPanel",
    "activity_text",
    "alarm_text",
    "counts_text",
    "engine_text",
    "intake_text",
    "lifecycle_text",
    "processing_text",
    "progress_text",
    "secondary_counts_text",
    "source_status_text",
    "warning_lines",
]
