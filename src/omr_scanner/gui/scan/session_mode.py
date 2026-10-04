"""Session mode on the Scan stage: the operational controls, over the services (revised phase 8).

Purpose:
    Connect the session panel (:mod:`omr_scanner.gui.scan.session_panel`),
    the paged sheet list (:mod:`omr_scanner.gui.scan.session_table_model`),
    the engine runner (:mod:`omr_scanner.gui.scan.session_runner`) and the
    snapshot poller (:mod:`omr_scanner.gui.scan.session_poller`) to the
    phase 7 services - and to nothing else. Every state shown comes from a
    service; every action is one service call (or one command to the engine
    thread); every rule is the service's.

When it appears:
    Only for a session that uses intake sources - one with a watched source
    attached, or while this window's engine runs. A finite project (*Add
    Folder -> Process All*) never sees it: no banner, no setup step, no
    change to *Process All*.

The split every command keeps:
    A public method does the work and never opens a modal (tests drive these);
    a ``_prompt_*`` method asks the question and calls it. A failure becomes
    an inline notice on the panel - never a traceback, never a dialog opened
    from a signal a worker emitted. Results of background work (the engine,
    the finish check) arrive as signals; when the operator started them
    interactively, the follow-up dialog is opened by a ``_prompt_*`` method
    scheduled on the GUI thread's next turn, never inside the signal.

Coordinator ownership:
    One coordinator per project (the phase 7 lease). The engine takes it when
    it starts; if the finite Scan stage holds it, the refusal's own message is
    shown and nothing is retried, reset or stopped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from omr_scanner.domain.intake import SourceKind
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.domain.session_finish import IncompleteAcceptance
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui import session_close
from omr_scanner.gui.scan.session_dialogs import (
    FinishChoice,
    FinishSessionDialog,
    SourceDialog,
    SourceDraft,
)
from omr_scanner.gui.scan.session_panel import (
    ENGINE_IDLE,
    ENGINE_RUNNING,
    ENGINE_STARTING,
    ENGINE_STOPPING,
    SessionPanel,
)
from omr_scanner.gui.scan.session_poller import SessionPoller, SessionView, read_session_view
from omr_scanner.gui.scan.session_runner import EngineFactory, SessionRunner, StopMode
from omr_scanner.services import intake as intake_service
from omr_scanner.services import scan_sessions, session_controls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.config.processing import ProcessingSettings
    from omr_scanner.domain.session_finish import FinishOutcome
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.scan.session_table_model import SessionSheetList
    from omr_scanner.services import ProjectSession
    from omr_scanner.services.intake import SourceInfo

_LOGGER = logging.getLogger(__name__)

SHEET_LIST_REFRESH_SECONDS = 2.0
"""The sheet list re-reads its page at most this often while counts change."""

FINISH_REASON = "Finish Scan Session (Scan stage)"


def production_engine_factory(
    project: ProjectSession,
    scan_session_id: str,
    template: OmrTemplate,
    template_path: Path | None,
    processing: ProcessingSettings,
    operator: str,
) -> EngineFactory:
    """The engine the application runs: a warm worker-process pool and the real disk."""

    def build() -> object:
        from omr_scanner.services.continuous_engine import ContinuousEngine
        from omr_scanner.services.recognition_pool import ProcessRecogniser
        from omr_scanner.services.recognition_settings import RecognitionOptions

        recogniser = ProcessRecogniser(
            template,
            workers=processing.configured_worker_count(),
            options=RecognitionOptions(with_preview=False, keep_bubble_measurements=False),
            opencv_threads=processing.opencv_threads,
            recycle_after=processing.worker_recycle_after,
        )
        return ContinuousEngine(
            project.database,
            scan_session_id=scan_session_id,
            template=template,
            recogniser=recogniser,
            intake_factory=lambda: intake_service.IntakeService(project.database, project.root),
            started_by=operator or "continuous engine",
            template_path=template_path,
        )

    return build  # type: ignore[return-value]


class _FinishThread(QThread):
    """*Finish scan session* outside the engine: final reconciliation off the GUI thread."""

    outcome = Signal(object)
    refused = Signal(str)

    def __init__(
        self,
        project: ProjectSession,
        scan_session_id: str,
        *,
        operator: str,
        accept_incomplete: bool,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("finishSessionThread")
        self._project = project
        self._session_id = scan_session_id
        self._operator = operator
        self._accept = accept_incomplete

    def run(self) -> None:
        try:
            result = session_close.finish(
                self._project,
                self._session_id,
                operator=self._operator,
                accept_incomplete=self._accept,
                reason=FINISH_REASON,
            )
        except OMRScannerError as exc:
            self.refused.emit(exc.user_message or str(exc))
            return
        except Exception as exc:  # deliberately broad: reported, never a crash
            _LOGGER.exception("Finish scan session failed")
            self.refused.emit(str(exc) or type(exc).__name__)
            return
        self.outcome.emit(result)


class SessionModeController(QObject):
    """The Scan stage's session mode: commands, prompts and the background workers.

    Args:
        parent_widget: The Scan page - parent of every dialog.
        panel: The session panel it drives.
        sheet_list: The paged session sheet list it keeps current.
        template: Returns the template (and its path) the Scan stage reads with.
        processing: Returns the application's processing settings.

    Signals:
        mode_changed: ``bool`` - session mode shown or hidden.
        view_changed: each new :class:`SessionView` (the window relays it to
            Resolve, so the queues refresh without visiting Scan).
        session_changed: the session's lifecycle changed (closed, reopened) or
            the active session was switched.
        finish_completed: the ``FinishOutcome`` of a finish attempt.
        navigate_requested: a destination key from the finish dialog.
        running_changed: ``bool`` - this window's engine started or stopped.

    Attributes:
        engine_factory: Builds the engine for :meth:`start`; ``None`` uses
            :func:`production_engine_factory`. Tests inject an engine on a fake
            disk with inline recognition.
        operator: The configured operator name.
        notices: Inline messages currently shown on the panel.
        last_finish_outcome: The latest finish outcome.
    """

    mode_changed = Signal(bool)
    view_changed = Signal(object)
    session_changed = Signal()
    finish_completed = Signal(object)
    navigate_requested = Signal(str)
    running_changed = Signal(bool)

    def __init__(
        self,
        parent_widget: QWidget,
        panel: SessionPanel,
        sheet_list: SessionSheetList,
        *,
        template: Callable[[], tuple[OmrTemplate | None, Path | None]],
        processing: Callable[[], ProcessingSettings],
    ) -> None:
        super().__init__(parent_widget)
        self._widget = parent_widget
        self.panel = panel
        self.sheet_list = sheet_list
        self._template = template
        self._processing = processing
        self.project: ProjectSession | None = None
        self.scan_session_id = ""
        self.operator = ""
        self.engine_factory: EngineFactory | None = None
        self.notices: tuple[str, ...] = ()
        self.last_finish_outcome: FinishOutcome | None = None
        self._active = False
        self._finish_thread: _FinishThread | None = None
        self._finishing = False
        self._interactive_finish = False
        self._last_signature: tuple[object, ...] | None = None
        self._last_list_refresh = 0.0
        self._starting = False

        self.runner = SessionRunner(self)
        self.poller = SessionPoller(self)
        self.poller.view_ready.connect(self._on_view)
        self.poller.failed.connect(self._on_poll_failed)
        self.runner.engine_started.connect(self._on_engine_started)
        self.runner.status_changed.connect(self._on_engine_status)
        self.runner.busy.connect(self._on_engine_busy)
        self.runner.failed.connect(self._on_engine_failed)
        self.runner.stopped.connect(self._on_engine_stopped)
        self.runner.finish_outcome.connect(self._on_finish_outcome)
        self.runner.finish_failed.connect(self._on_finish_refused)
        self.runner.running_changed.connect(self.running_changed)

        panel.start_requested.connect(self.start)
        panel.pause_processing_requested.connect(self.pause_processing)
        panel.resume_processing_requested.connect(self.resume_processing)
        panel.finish_current_requested.connect(self.finish_current)
        panel.cancel_queued_requested.connect(self._prompt_cancel_queued)
        panel.pause_intake_requested.connect(self.pause_intake)
        panel.resume_intake_requested.connect(self.resume_intake)
        panel.add_source_requested.connect(self._prompt_add_source)
        panel.edit_source_requested.connect(self._prompt_edit_source)
        panel.set_source_enabled_requested.connect(self.set_source_enabled)
        panel.set_source_paused_requested.connect(self.set_source_paused)
        panel.detach_source_requested.connect(self._prompt_detach_source)
        panel.finish_session_requested.connect(self._prompt_finish_session)
        panel.reopen_session_requested.connect(self._prompt_reopen_session)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """Whether session mode is shown."""
        return self._active

    @property
    def running(self) -> bool:
        """Whether this window's engine thread is running."""
        return self.runner.is_running

    @property
    def finishing(self) -> bool:
        """Whether a finish attempt is in progress."""
        return self._finishing

    @property
    def view(self) -> SessionView | None:
        """The latest session view rendered."""
        return self.panel.view

    @property
    def database(self) -> object | None:
        """The open project's database."""
        return self.project.database if self.project is not None else None

    def adopt(self, project: ProjectSession | None) -> None:
        """Follow a newly opened (or closed) project.

        The previous project's engine and poller must already be stopped
        (:meth:`shutdown`) - the main window does that before it releases the
        previous database.
        """
        if self.project is not None and self.project is not project:
            self.shutdown()
        self.project = project
        self.scan_session_id = ""
        self._last_signature = None
        self.notices = ()
        self.panel.set_extra_warnings(())
        self.panel.set_engine_state(ENGINE_IDLE)
        self.panel.set_can_write(project is not None and not project.read_only)
        self.refresh_mode()

    def refresh_mode(self) -> None:
        """Re-decide whether session mode shows, for the project's active session.

        Shown for a session that uses intake sources (a watched source attached)
        or while this window's engine runs. The rule names *which session the
        GUI shows*; every fact about that session is read from the services.
        """
        project = self.project
        info = None
        if project is not None:
            try:
                info = scan_sessions.active_scan_session(project.database)
            except OMRScannerError:
                _LOGGER.exception("Could not read the active scan session")
        session_id = info.scan_session_id if info is not None and not info.virtual else ""
        wanted = bool(session_id) and (self.running or self._uses_sources(session_id))
        changed_session = session_id != self.scan_session_id
        self.scan_session_id = session_id
        if not wanted:
            self.poller.watch(None, "")
            self.sheet_list.set_session(None, "")
            self._set_active(False)
            return
        assert project is not None
        if changed_session or not self._active:
            self._last_signature = None
            # Rendered once now, from committed rows, so a reopened project shows
            # its reconstructed state at once - before any Resume or Start.
            try:
                clock = self.poller.clock
                self.panel.show_view(
                    read_session_view(
                        project.database,
                        session_id,
                        now=clock() if clock is not None else None,
                    )
                )
            except Exception as exc:  # the poller keeps trying
                _LOGGER.exception("Initial session view failed")
                self._notice(f"Session status could not be read: {exc}")
            self.sheet_list.set_session(project.database, session_id)
        self.poller.watch(project.database, session_id)
        self._set_active(True)

    def _uses_sources(self, session_id: str) -> bool:
        project = self.project
        if project is None or not intake_service.has_intake_schema(project.database):
            return False
        return any(
            item.kind is SourceKind.WATCHED and item.attached_session_id == session_id
            for item in intake_service.list_sources(project.database)
        )

    def _set_active(self, active: bool) -> None:
        self.panel.setVisible(active)
        if active != self._active:
            self._active = active
            self.mode_changed.emit(active)

    def shutdown(self) -> None:
        """Stop the engine safely, wait for it, and stop polling - before the database closes.

        Order: stop polling (wait for the read in flight), ask the engine to
        drain and settle (it releases the coordinator lease itself), wait for
        its thread, wait for a finish check in progress. The session stays
        open; its persisted intent is left as it was.
        """
        self.poller.stop()
        if not self.runner.shutdown():
            _LOGGER.error("The continuous engine did not stop in time during shutdown")
        thread = self._finish_thread
        if thread is not None:
            thread.wait()
            thread.deleteLater()
            self._finish_thread = None
        self._finishing = False
        self.panel.set_engine_state(ENGINE_IDLE)

    # ------------------------------------------------------------------
    # Notices (inline, never modal)
    # ------------------------------------------------------------------
    def _notice(self, text: str) -> None:
        self.notices = (text,)
        self.panel.set_extra_warnings(self.notices)

    def _clear_notices(self) -> None:
        if self.notices:
            self.notices = ()
            self.panel.set_extra_warnings(())

    def _refused(self, exc: OMRScannerError, what: str) -> bool:
        _LOGGER.info("%s refused: %s", what, exc)
        self._notice(f"{what}: {exc.user_message or exc}")
        return False

    def _writable(self) -> bool:
        project = self.project
        if project is None or not self.scan_session_id:
            return False
        if project.read_only:
            self._notice("This project is open read-only: nothing can be changed.")
            return False
        return True

    # ------------------------------------------------------------------
    # Processing commands (no dialogs)
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """Start continuous scanning in this window. ``False`` if refused (said on the panel)."""
        if not self._writable() or self.running:
            return False
        project = self.project
        assert project is not None
        info = scan_sessions.get_scan_session(project.database, self.scan_session_id)
        if info is None or info.state is not ScanSessionState.OPEN:
            self._notice("Only an open scan session can be scanned into. Reopen it first.")
            return False
        template, template_path = self._template()
        if template is None:
            self._notice("Load the examination's template on the Scan stage first.")
            return False
        factory = self.engine_factory or production_engine_factory(
            project,
            self.scan_session_id,
            template,
            template_path,
            self._processing(),
            self.operator,
        )
        self._clear_notices()
        self._starting = True
        self.panel.set_engine_state(ENGINE_STARTING)
        started = self.runner.start(factory)
        if not started:
            self._starting = False
            self.panel.set_engine_state(ENGINE_IDLE)
        self.refresh_mode()
        return started

    def pause_processing(self) -> bool:
        """Persist *paused*: nothing new is claimed; sheets being read finish and are saved."""
        if not self._writable():
            return False
        try:
            session_controls.pause_processing(
                self.project.database, self.scan_session_id, actor=self.operator  # type: ignore[union-attr]
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Pause processing")
        self._clear_notices()
        self.poller.refresh()
        return True

    def resume_processing(self) -> bool:
        """Persist *running*; start the engine here if it is not running."""
        if not self._writable():
            return False
        try:
            session_controls.resume_processing(
                self.project.database, self.scan_session_id, actor=self.operator  # type: ignore[union-attr]
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Resume processing")
        self._clear_notices()
        self.poller.refresh()
        if not self.running:
            return self.start()
        return True

    def finish_current(self) -> bool:
        """*Finish current and stop*: no new work; what is being read finishes and is saved."""
        if not self._writable() or not self.running:
            return False
        self.runner.stop(StopMode.FINISH_CURRENT, actor=self.operator, reason="Scan stage")
        self.panel.set_engine_state(ENGINE_STOPPING)
        return True

    def cancel_queued(self) -> bool:
        """*Cancel queued work* (named): queued sheets return to pending; running ones finish."""
        if not self._writable() or not self.running:
            return False
        if not self.operator.strip():
            self._notice(
                "Cancel queued work: set your operator name in File > Settings first - it "
                "is recorded."
            )
            return False
        self.runner.stop(StopMode.CANCEL_QUEUED, actor=self.operator, reason="Scan stage")
        self.panel.set_engine_state(ENGINE_STOPPING)
        return True

    def pause_intake(self) -> bool:
        """Persist *intake paused* for the whole session (registered work still runs)."""
        return self._set_intake(True)

    def resume_intake(self) -> bool:
        """Persist *intake on* for the session."""
        return self._set_intake(False)

    def _set_intake(self, paused: bool) -> bool:
        if not self._writable():
            return False
        try:
            session_controls.set_intake_paused(
                self.project.database, self.scan_session_id, paused, actor=self.operator  # type: ignore[union-attr]
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Pause intake" if paused else "Resume intake")
        self._clear_notices()
        self.poller.refresh()
        return True

    # ------------------------------------------------------------------
    # Sources (no dialogs)
    # ------------------------------------------------------------------
    def add_source(self, draft: SourceDraft) -> SourceInfo:
        """Create a watched source and attach it to the active session. Audited.

        With no active session yet, one is created first (only then - never a
        second one for the same sitting). Raises the service's error with its
        user message (the prompt shows it in the dialog).
        """
        project = self.project
        if project is None:
            raise OMRScannerError("no project", user_message="Open a project first.")
        if project.read_only:
            raise OMRScannerError("read only", user_message="This project is open read-only.")
        database = project.database
        info = scan_sessions.active_scan_session(database)
        if info is None or info.virtual:
            info = scan_sessions.create_scan_session(
                database, name="", created_by=self.operator, origin="operator"
            )
        if info.state is not ScanSessionState.OPEN:
            raise OMRScannerError(
                "session closed",
                user_message=(
                    f"Scan session '{info.name}' is closed; it receives no new scans. "
                    "Reopen it first."
                ),
            )
        source = intake_service.create_source(
            database,
            label=draft.label,
            root_path=draft.root_path,
            recursive=draft.recursive,
            created_by=self.operator,
        )
        attached = intake_service.attach_source(
            database, source.source_id, info.scan_session_id, actor=self.operator
        )
        self._clear_notices()
        self.refresh_mode()
        self.session_changed.emit()
        self.poller.refresh()
        return attached

    def update_source(self, source_id: str, draft: SourceDraft) -> SourceInfo:
        """Change a source's name and options (never its folder). Audited."""
        project = self.project
        if project is None:
            raise OMRScannerError("no project", user_message="Open a project first.")
        updated = intake_service.update_source(
            project.database,
            source_id,
            label=draft.label,
            recursive=draft.recursive,
            updated_by=self.operator,
        )
        self.poller.refresh()
        return updated

    def set_source_enabled(self, source_id: str, enabled: bool) -> bool:
        """Enable or disable a source (history and files kept). Audited."""
        if not self._writable():
            return False
        try:
            intake_service.set_source_enabled(
                self.project.database, source_id, enabled, actor=self.operator  # type: ignore[union-attr]
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Enable source" if enabled else "Disable source")
        self._clear_notices()
        self.poller.refresh()
        return True

    def set_source_paused(self, source_id: str, paused: bool) -> bool:
        """Pause or resume intake from one source. Audited; paused is not unreachable."""
        if not self._writable():
            return False
        try:
            session_controls.set_source_paused(
                self.project.database, source_id, paused, actor=self.operator  # type: ignore[union-attr]
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Pause source" if paused else "Resume source")
        self._clear_notices()
        self.poller.refresh()
        return True

    def detach_source(self, source_id: str) -> bool:
        """Stop a source feeding this session. Audited; nothing it delivered is removed."""
        if not self._writable():
            return False
        try:
            intake_service.detach_source(
                self.project.database, source_id, actor=self.operator  # type: ignore[union-attr]
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Remove source")
        self._clear_notices()
        self.refresh_mode()
        self.poller.refresh()
        return True

    # ------------------------------------------------------------------
    # Finish / reopen (no dialogs)
    # ------------------------------------------------------------------
    def finish_session(self, *, accept_incomplete: bool = False, interactive: bool = False) -> bool:
        """Attempt *Finish scan session* through the one finish policy. Asynchronous.

        With this window's engine running, the engine runs it as the
        coordinator (its final reconciliation is the ledger writer's own);
        otherwise a background thread runs it under the session-finish lease.
        The outcome arrives as :attr:`finish_completed` - every blocker, or
        closed. ``accept_incomplete``: the named operator's audited acceptance
        (only outstanding rescans, unmatched replacements and deferred sheets
        can be accepted; the service decides).
        """
        project = self.project
        if not self._writable() or self._finishing:
            return False
        assert project is not None
        if not self.operator.strip():
            self._notice(
                "Finish scan session: set your operator name in File > Settings first - "
                "closing a session is recorded against a named operator."
            )
            return False
        self._finishing = True
        self._interactive_finish = interactive
        self.panel.finish_session_button.setEnabled(False)
        if self.running:
            self.runner.finish_session(
                closed_by=self.operator,
                acknowledge=(
                    IncompleteAcceptance(self.operator, "(incomplete results accepted)")
                    if accept_incomplete
                    else None
                ),
                reason=FINISH_REASON,
            )
            return True
        thread = _FinishThread(
            project,
            self.scan_session_id,
            operator=self.operator,
            accept_incomplete=accept_incomplete,
            parent=self,
        )
        thread.outcome.connect(self._on_finish_outcome)
        thread.refused.connect(self._on_finish_refused)
        old = self._finish_thread
        if old is not None:
            old.wait()
            old.deleteLater()
        self._finish_thread = thread
        thread.start()
        return True

    def reopen_session(self, reason: str = "") -> bool:
        """Reopen the closed session - named operator, audited (one adapter for every page)."""
        project = self.project
        if not self._writable():
            return False
        assert project is not None
        try:
            session_close.reopen(
                project, self.scan_session_id, operator=self.operator, reason=reason
            )
        except OMRScannerError as exc:
            return self._refused(exc, "Reopen scan session")
        self._clear_notices()
        self.poller.refresh()
        self.session_changed.emit()
        return True

    # ------------------------------------------------------------------
    # Background results (GUI thread; never a modal here)
    # ------------------------------------------------------------------
    def _on_view(self, view: object) -> None:
        if not isinstance(view, SessionView):
            return
        if view.snapshot.scan_session_id != self.scan_session_id:
            return
        previous = self.panel.view
        self.panel.show_view(view)
        if (
            previous is not None
            and previous.snapshot.session_state != view.snapshot.session_state
        ):
            self.session_changed.emit()
        self._refresh_sheet_list_if_changed(view)
        self.view_changed.emit(view)

    def _refresh_sheet_list_if_changed(self, view: SessionView) -> None:
        import time

        snapshot = view.snapshot
        signature = (
            tuple(snapshot.partition.as_dict().values()),
            snapshot.conflicts,
            snapshot.rescans,
            snapshot.outstanding_suggestions,
        )
        if signature == self._last_signature:
            return
        now = time.monotonic()
        if now - self._last_list_refresh < SHEET_LIST_REFRESH_SECONDS:
            return  # the next poll catches it; the signature stays "changed"
        self._last_signature = signature
        self._last_list_refresh = now
        if self.sheet_list.isVisible():
            self.sheet_list.refresh()

    def _on_poll_failed(self, message: str) -> None:
        self._notice(f"Session status could not be refreshed ({message}); retrying.")

    def _on_engine_started(self, _report: object) -> None:
        self._starting = False
        self.panel.set_engine_state(ENGINE_RUNNING)
        self.refresh_mode()
        self.poller.refresh()

    def _on_engine_status(self, status: object) -> None:
        state = ENGINE_STOPPING if self.runner.stopping is not None else ENGINE_RUNNING
        self.panel.set_engine_state(state, status)
        error = str(getattr(status, "last_error", "") or "")
        if error:
            self._notice(f"Continuous scanning: {error}")

    def _on_engine_busy(self, message: str) -> None:
        self._starting = False
        self.panel.set_engine_state(ENGINE_IDLE)
        self._notice(message)

    def _on_engine_failed(self, message: str) -> None:
        self._notice(f"Continuous scanning stopped: {message}")

    def _on_engine_stopped(self, _status: object) -> None:
        self._starting = False
        self.panel.set_engine_state(ENGINE_IDLE)
        self.refresh_mode()
        self.poller.refresh()

    def _on_finish_outcome(self, outcome: object) -> None:
        self._finishing = False
        self.last_finish_outcome = outcome  # type: ignore[assignment]
        self.panel.refresh_controls()
        self.poller.refresh()
        if getattr(outcome, "closed", False):
            self._clear_notices()
            self.session_changed.emit()
        self.finish_completed.emit(outcome)
        if self._interactive_finish:
            self._interactive_finish = False
            QTimer.singleShot(0, lambda: self._prompt_finish_result(outcome))  # type: ignore[arg-type]

    def _on_finish_refused(self, message: str) -> None:
        self._finishing = False
        self._interactive_finish = False
        self.panel.refresh_controls()
        self._notice(f"Finish scan session: {message}")

    # ------------------------------------------------------------------
    # Prompts (the only methods that open dialogs)
    # ------------------------------------------------------------------
    def _prompt_add_source(self) -> None:
        error = ""
        count = len(self.view.sources) if self.view is not None else 0
        suggested = f"Scanner {chr(ord('A') + count)}" if count < 26 else ""
        while True:
            dialog = SourceDialog(self._widget, suggested_label=suggested, error=error)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            draft = dialog.draft()
            suggested = draft.label
            try:
                self.add_source(draft)
            except OMRScannerError as exc:
                error = exc.user_message or str(exc)
                continue
            return

    def _prompt_edit_source(self, source_id: str) -> None:
        project = self.project
        if project is None:
            return
        source = intake_service.get_source(project.database, source_id)
        if source is None:
            return
        error = ""
        while True:
            dialog = SourceDialog(self._widget, source=source, error=error)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                self.update_source(source_id, dialog.draft())
            except OMRScannerError as exc:
                error = exc.user_message or str(exc)
                continue
            return

    def _prompt_detach_source(self, source_id: str) -> None:
        project = self.project
        source = intake_service.get_source(project.database, source_id) if project else None
        if source is None:
            return
        answer = QMessageBox.question(
            self._widget,
            "Remove source from session",
            f"Stop taking new files from '{source.label}' into this scan session?\n\n"
            "Files and sheets it already delivered stay in the session with their history. "
            "The source itself is kept and can be attached again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.detach_source(source_id)

    def _prompt_cancel_queued(self) -> None:
        answer = QMessageBox.question(
            self._widget,
            "Cancel queued work",
            "Queued sheets will return to Pending.\n"
            "Sheets already running will still finish and be saved.\n"
            "No scanned files or completed results will be deleted.\n\n"
            "Cancel the queued work and stop continuous scanning?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.cancel_queued()

    def _session_name(self) -> str:
        view = self.view
        if view is not None and view.session is not None:
            return view.session.name
        return "the scan session"

    def _prompt_finish_session(self) -> None:
        project = self.project
        if project is None or not self.scan_session_id:
            return
        try:
            blockers = session_close.preview_blockers(project, self.scan_session_id)
        except OMRScannerError as exc:
            self._refused(exc, "Finish scan session")
            return
        dialog = FinishSessionDialog(
            self._session_name(),
            blockers,
            attempted=False,
            operator=self.operator,
            parent=self._widget,
        )
        dialog.exec()
        self._act_on_finish_choice(dialog)

    def _prompt_finish_result(self, outcome: FinishOutcome) -> None:
        if outcome.closed:
            accepted = session_close.blocker_messages(outcome.accepted)
            QMessageBox.information(
                self._widget,
                "Scan session closed",
                f"'{self._session_name()}' is closed. Its batches are sealed and its results "
                "are final."
                + (
                    "\n\nClosed with incomplete results, recorded against "
                    f"{self.operator}:\n" + "\n".join(f"• {item}" for item in accepted)
                    if accepted
                    else ""
                ),
            )
            return
        dialog = FinishSessionDialog(
            self._session_name(),
            outcome.blockers,
            attempted=True,
            operator=self.operator,
            disabled_sources=outcome.disabled_sources,
            parent=self._widget,
        )
        dialog.exec()
        self._act_on_finish_choice(dialog)

    def _act_on_finish_choice(self, dialog: FinishSessionDialog) -> None:
        if dialog.choice is FinishChoice.ATTEMPT:
            self.finish_session(interactive=True)
        elif dialog.choice is FinishChoice.ACCEPT_INCOMPLETE:
            self.finish_session(accept_incomplete=True, interactive=True)
        elif dialog.choice is FinishChoice.NAVIGATE:
            self.navigate_requested.emit(dialog.destination)

    def _prompt_reopen_session(self) -> None:
        if not self.operator.strip():
            QMessageBox.warning(
                self._widget,
                "Reopen scan session",
                "Reopening is recorded against a named operator. Set your name in "
                "File > Settings first.",
            )
            return
        box = QMessageBox(self._widget)
        box.setObjectName("reopenSessionConfirmation")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Reopen scan session")
        box.setText(
            f"Reopen '{self._session_name()}'?\n\n"
            "• The session becomes open and editable again.\n"
            "• Its results become provisional.\n"
            "• Final exports generated while it was closed become stale and must be "
            "regenerated after it is closed again.\n"
            "• Its sources can feed it again, as their saved pause settings say.\n\n"
            f"Recorded against: {self.operator}"
        )
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        reopen = box.addButton("Reopen Session", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is reopen:
            self.reopen_session()


__all__ = ["SessionModeController", "production_engine_factory"]
