"""The Tools > Project Health / Recovery dialog (Phase 10, §6, §7, §57).

Purpose:
    One coherent place for the things an operator needs when wondering "is
    this project OK?" - a database health check, and a backup/recovery
    snapshot - rather than several disconnected dialogs for closely related
    concerns (§57's explicit instruction).

Responsibilities:
    * Show the result of an on-demand comprehensive health check
      (:func:`omr_scanner.services.project_health.full_check`), coloured by
      severity, in plain language.
    * Create a backup on request
      (:func:`omr_scanner.services.project_backup.create_backup`) and list
      the backups that already exist.

What does NOT belong here:
    * Repairing anything the health check finds. This dialog only ever
      shows what :mod:`omr_scanner.services.project_health` reports; per the
      phase brief §7, there is deliberately no "fix it" button that would
      silently rewrite examination data.
    * Restoring a backup over the live project database while it is open -
      that is a recovery action for a closed project, and out of this
      dialog's scope; the dialog can only copy a backup out to a new
      location for inspection.

A known limitation, stated rather than hidden:
    The full check runs synchronously on the GUI thread. For an ordinary
    project this is a handful of SQL queries and finishes in well under a
    second; for a project with an extremely large database, ``PRAGMA
    integrity_check`` walks every page and could take noticeably longer,
    during which the window will not repaint. Moving this specific check to
    a background thread (the same pattern
    :class:`omr_scanner.gui.scan.worker.BatchWorker` already establishes) is
    a reasonable follow-up; it was not done here so that this on-demand,
    infrequently-used dialog did not have to carry a second worker-thread
    implementation this phase's time did not allow verifying as carefully as
    the ones on the critical batch-processing path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.services import project_backup, project_health

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    # Re-exported by services.project_service specifically so a GUI page can
    # name this type without importing omr_scanner.database directly, which
    # tests/unit/test_architecture.py forbids for this layer.
    from omr_scanner.services import ProjectDatabase

_LEVEL_COLOR = {
    project_health.HealthLevel.OK: "#1b7f3a",
    project_health.HealthLevel.WARNING: "#9a6a00",
    project_health.HealthLevel.ERROR: "#b3261e",
}

_ISSUE_TEXT_COLOR = {
    project_health.HealthLevel.OK: Qt.GlobalColor.darkGreen,
    project_health.HealthLevel.WARNING: Qt.GlobalColor.darkYellow,
    project_health.HealthLevel.ERROR: Qt.GlobalColor.red,
}


class ProjectHealthDialog(QDialog):
    """Project Health & Recovery: one window for both concerns.

    Args:
        database: The open project database.
        project_root: The project's root directory (for backups and free
            disk space).
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        database: ProjectDatabase,
        project_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("projectHealthDialog")
        self.setWindowTitle("Project Health & Recovery")
        self.setModal(True)
        self.resize(560, 520)

        self._database = database
        self._project_root = project_root
        self._last_report: project_health.HealthReport | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_health_group())
        layout.addWidget(self._build_backup_group())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.refresh_backups()

    # ------------------------------------------------------------------
    # Health section
    # ------------------------------------------------------------------
    def _build_health_group(self) -> QGroupBox:
        box = QGroupBox("Database health")
        box.setObjectName("healthGroup")
        layout = QVBoxLayout(box)

        self.health_summary_label = QLabel(
            "Run a comprehensive check to see the project's current health."
        )
        self.health_summary_label.setObjectName("healthSummaryLabel")
        self.health_summary_label.setWordWrap(True)
        layout.addWidget(self.health_summary_label)

        self.health_list = QListWidget()
        self.health_list.setObjectName("healthIssueList")
        layout.addWidget(self.health_list)

        row = QHBoxLayout()
        self.run_check_button = QPushButton("Run Full Check")
        self.run_check_button.setObjectName("runFullCheckButton")
        self.run_check_button.clicked.connect(self.run_full_check)
        row.addWidget(self.run_check_button)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def run_full_check(self) -> project_health.HealthReport:
        """Run the comprehensive check and update the display.

        Returns:
            The report, for a test to assert against directly without
            re-reading it back off the widgets.
        """
        report = project_health.full_check(self._database, self._project_root)
        self._show_report(report)
        return report

    def _show_report(self, report: project_health.HealthReport) -> None:
        self._last_report = report
        self.health_list.clear()
        if not report.issues:
            self.health_summary_label.setText("No issues found.")
            self.health_summary_label.setStyleSheet(
                f"color: {_LEVEL_COLOR[project_health.HealthLevel.OK]};"
            )
            return

        counts = {
            level: len(report.by_level(level))
            for level in (project_health.HealthLevel.ERROR, project_health.HealthLevel.WARNING)
        }
        summary_parts = [
            f"{count} {level.value}(s)" for level, count in counts.items() if count
        ]
        self.health_summary_label.setText(", ".join(summary_parts) + ".")
        self.health_summary_label.setStyleSheet(f"color: {_LEVEL_COLOR[report.level]};")

        for issue in report.issues:
            item = QListWidgetItem(f"[{issue.level.value.upper()}] {issue.message}")
            item.setForeground(_ISSUE_TEXT_COLOR[issue.level])
            self.health_list.addItem(item)

    @property
    def last_report(self) -> project_health.HealthReport | None:
        """The most recent full-check result shown, or ``None`` before the first run."""
        return self._last_report

    # ------------------------------------------------------------------
    # Backup section
    # ------------------------------------------------------------------
    def _build_backup_group(self) -> QGroupBox:
        box = QGroupBox("Backups")
        box.setObjectName("backupGroup")
        layout = QVBoxLayout(box)

        self.backup_list = QListWidget()
        self.backup_list.setObjectName("backupList")
        layout.addWidget(self.backup_list)

        row = QHBoxLayout()
        self.create_backup_button = QPushButton("Create Backup Now")
        self.create_backup_button.setObjectName("createBackupButton")
        self.create_backup_button.clicked.connect(self.prompt_create_backup)
        row.addWidget(self.create_backup_button)

        self.restore_backup_button = QPushButton("Restore Selected To...")
        self.restore_backup_button.setObjectName("restoreBackupButton")
        self.restore_backup_button.clicked.connect(self.prompt_restore_backup)
        row.addWidget(self.restore_backup_button)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _backups_dir(self) -> Path:
        return self._project_root / project_backup.BACKUP_DIR_NAME

    def refresh_backups(self) -> tuple[project_backup.BackupEntry, ...]:
        """Reload the backup list from disk. Testable without any dialog."""
        entries = project_backup.list_backups(self._backups_dir())
        self.backup_list.clear()
        for entry in entries:
            label = entry.backup_path.name
            if entry.is_complete and entry.manifest is not None:
                label += f"  ({entry.manifest.reason}, {entry.manifest.created_at})"
            else:
                label += "  (incomplete - not usable)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.backup_list.addItem(item)
        return entries

    def create_backup(self, *, reason: str) -> project_backup.BackupManifest:
        """Create a backup, no dialog. Testable directly."""
        manifest = project_backup.create_backup(
            self._database.path,
            self._backups_dir(),
            reason=reason,
            schema_version=self._database.schema_version,
        )
        self.refresh_backups()
        return manifest

    def prompt_create_backup(self) -> None:
        """Create a manual backup and confirm it. Owns the confirmation dialog."""
        try:
            manifest = self.create_backup(reason="manual")
        except project_backup.BackupError as exc:
            QMessageBox.warning(self, "Backup failed", exc.user_message)
            return
        QMessageBox.information(
            self, "Backup created", f"Backup written: {manifest.backup_file}"
        )

    def selected_backup(self) -> project_backup.BackupEntry | None:
        """The backup entry currently selected in the list, if any.

        Driven from the row index rather than ``currentItem()``: PySide6's
        generated stubs declare that method non-optional although it returns
        ``None`` for an empty selection, and a row index of ``-1`` says the
        same thing without arguing with the type checker (see the identical
        note in ``gui.attendance.page._selected_scan_id``).
        """
        row = self.backup_list.currentRow()
        if row < 0:
            return None
        entry = self.backup_list.item(row).data(Qt.ItemDataRole.UserRole)
        return entry if isinstance(entry, project_backup.BackupEntry) else None

    def restore_backup_to(self, entry: project_backup.BackupEntry, destination: Path) -> Path:
        """Restore ``entry`` to ``destination``, no dialog. Testable directly."""
        return project_backup.restore_backup(entry, destination)

    def prompt_restore_backup(self) -> None:
        """Ask where to restore the selected backup, then do it. Owns the dialogs."""
        entry = self.selected_backup()
        if entry is None:
            QMessageBox.information(self, "No backup selected", "Select a backup to restore.")
            return
        destination, _filter = QFileDialog.getSaveFileName(
            self, "Restore backup to", str(self._project_root / "database_restored.sqlite")
        )
        if not destination:
            return
        try:
            restored = self.restore_backup_to(entry, Path(destination))
        except project_backup.BackupError as exc:
            QMessageBox.warning(self, "Restore failed", exc.user_message)
            return
        QMessageBox.information(self, "Backup restored", f"Restored to: {restored}")


__all__ = ["ProjectHealthDialog"]
