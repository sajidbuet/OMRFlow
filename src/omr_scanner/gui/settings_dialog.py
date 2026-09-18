"""The File > Settings dialog.

Purpose:
    Let a user change the preferences that belong to *them and their machine*
    rather than to an examination - currently how much of the computer OMRFlow
    is allowed to use while it reads a batch of sheets.

Responsibilities:
    * Present :class:`~omr_scanner.config.processing.ProcessingSettings` in
      plain language, and show what the current choice actually amounts to on
      this machine ("Detected CPU threads: 16 / Parallel workers: 8").
    * Return the edited configuration. Saving it is the main window's job, which
      already owns the configuration file.

What does NOT belong here:
    * The worker-count rules. Every number shown here comes from
      :meth:`ProcessingSettings.configured_worker_count`, the same function the
      batch processor calls, so the label and the run can never disagree.
    * Any processing. A settings dialog starts nothing.

Vocabulary:
    The user interface says "parallel workers" and "CPU threads". It never says
    "process pool", "spawn" or "multiprocessing": the person setting this is an
    examination officer deciding how much of their computer to lend OMRFlow, not
    a Python programmer.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.config import AppConfig
from omr_scanner.config.processing import (
    ProcessingMode,
    ProcessingSettings,
    detected_cpu_count,
)

MODE_LABELS: tuple[tuple[ProcessingMode, str], ...] = (
    (ProcessingMode.AUTOMATIC, "Automatic"),
    (ProcessingMode.SINGLE_CORE, "Single core"),
    (ProcessingMode.CUSTOM, "Custom"),
)
"""The offered modes, in the order they appear in the combo box."""

MODE_EXPLANATIONS: dict[ProcessingMode, str] = {
    ProcessingMode.AUTOMATIC: (
        "OMRFlow chooses how many sheets to read at once: enough to use the "
        "computer well, never so many that the window stops responding or "
        "memory runs short."
    ),
    ProcessingMode.SINGLE_CORE: (
        "One sheet at a time. Slower, but the least demanding on memory and the "
        "easiest to follow when investigating a problem."
    ),
    ProcessingMode.CUSTOM: (
        "Read this many sheets at once. More workers need more memory; there is "
        "no benefit in more workers than there are sheets."
    ),
}


class SettingsDialog(QDialog):
    """Edit the per-user application settings.

    Args:
        config: The configuration to edit. It is never modified; the edited copy
            is available from :meth:`result_config` after the dialog is
            accepted.
        parent: Owning widget, typically the main window.
        cpu_count: Logical CPUs to present. Defaults to this machine's; tests
            pass a fixed number so that the dialog behaves identically on a
            two-core runner and a thirty-two-core workstation.
    """

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        cpu_count: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("Settings")
        self.setModal(True)

        self._config = config
        self._cpu_count = max(cpu_count if cpu_count is not None else detected_cpu_count(), 1)

        self._diagnostics_dir: Path | None = config.processing.diagnostics_dir

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(self._build_processing_group())
        layout.addWidget(self._build_diagnostics_group())

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("settingsButtonBox")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._load(config.processing)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_processing_group(self) -> QGroupBox:
        """Build the Processing section."""
        box = QGroupBox("Processing")
        box.setObjectName("processingSettingsGroup")
        form = QFormLayout(box)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("processingModeCombo")
        for mode, label in MODE_LABELS:
            self.mode_combo.addItem(label, mode.value)
        self.mode_combo.setToolTip(
            "How many scanned sheets OMRFlow reads at the same time."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Processing mode:", self.mode_combo)

        self.worker_spin = QSpinBox()
        self.worker_spin.setObjectName("workerCountSpinBox")
        # The offered range is this machine's: asking for more workers than the
        # computer has logical CPUs cannot make it faster, and a spin box that
        # refuses the value is clearer than one that accepts it and quietly
        # clamps it later.
        self.worker_spin.setRange(1, self._cpu_count)
        self.worker_spin.setToolTip(
            "How many sheets to read at the same time when the mode is Custom."
        )
        self.worker_spin.valueChanged.connect(self._on_worker_count_changed)
        form.addRow("Parallel workers:", self.worker_spin)

        self.detected_label = QLabel(str(self._cpu_count))
        self.detected_label.setObjectName("detectedCpuLabel")
        form.addRow("Detected CPU threads:", self.detected_label)

        self.active_label = QLabel("")
        self.active_label.setObjectName("activeWorkersLabel")
        form.addRow("Workers this setting uses:", self.active_label)

        self.explanation_label = QLabel("")
        self.explanation_label.setObjectName("processingExplanationLabel")
        self.explanation_label.setWordWrap(True)
        form.addRow(self.explanation_label)
        return box

    def _build_diagnostics_group(self) -> QGroupBox:
        """Build the Diagnostics section.

        Deliberately a second, smaller section rather than another row in
        Processing: how much of the computer to use is an everyday choice, and
        writing several full-page images per sheet is a troubleshooting one.
        """
        box = QGroupBox("Diagnostics")
        box.setObjectName("diagnosticsSettingsGroup")
        layout = QVBoxLayout(box)

        self.diagnostics_checkbox = QCheckBox(
            "Save diagnostic images while processing"
        )
        self.diagnostics_checkbox.setObjectName("diagnosticsCheckBox")
        self.diagnostics_checkbox.setToolTip(
            "Writes the corrected page and an annotated overlay for every sheet. "
            "Useful when investigating a misread; large, so leave it off otherwise."
        )
        self.diagnostics_checkbox.toggled.connect(self._on_diagnostics_toggled)
        layout.addWidget(self.diagnostics_checkbox)

        self.diagnostics_folder_button = QPushButton("Diagnostics Folder...")
        self.diagnostics_folder_button.setObjectName("diagnosticsFolderButton")
        self.diagnostics_folder_button.clicked.connect(self._prompt_diagnostics_folder)
        layout.addWidget(self.diagnostics_folder_button)

        self.diagnostics_folder_label = QLabel("")
        self.diagnostics_folder_label.setObjectName("diagnosticsFolderLabel")
        self.diagnostics_folder_label.setWordWrap(True)
        layout.addWidget(self.diagnostics_folder_label)
        return box

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def set_diagnostics_directory(self, directory: Path | None) -> None:
        """Set - or clear - the folder diagnostic images are written to.

        Separate from the file dialog for the reason every command in this
        application is: the dialog cannot be driven offscreen, the behaviour
        can.
        """
        self._diagnostics_dir = directory
        self._refresh()

    def _prompt_diagnostics_folder(self) -> None:
        """Ask for the diagnostics folder, then set it."""
        start = self._diagnostics_dir or Path.home()
        directory = QFileDialog.getExistingDirectory(
            self, "Select the diagnostics folder", str(start)
        )
        if directory:
            self.set_diagnostics_directory(Path(directory))

    def _on_diagnostics_toggled(self, _checked: bool) -> None:
        self._refresh()

    def _load(self, processing: ProcessingSettings) -> None:
        """Put a stored setting into the widgets."""
        index = self.mode_combo.findData(processing.mode.value)
        self.mode_combo.setCurrentIndex(max(index, 0))
        self.diagnostics_checkbox.setChecked(processing.diagnostics_enabled)
        # A stored count from a machine with more CPUs than this one is clamped
        # into range rather than rejected - the configuration file travels with
        # the user, the hardware does not.
        self.worker_spin.setValue(min(processing.worker_count, self._cpu_count))
        self._refresh()

    @property
    def selected_mode(self) -> ProcessingMode:
        """The mode currently chosen in the combo box."""
        return ProcessingMode(self.mode_combo.currentData())

    def processing_settings(self) -> ProcessingSettings:
        """The processing settings the dialog currently describes.

        Deliberately the *section* rather than a whole rebuilt
        :class:`~omr_scanner.config.app_config.AppConfig`: the dialog holds a
        snapshot taken when it opened, and returning that snapshot with one
        field changed would quietly undo anything else - a newly opened project
        landing in the recent list, say - that changed meanwhile. The window
        merges this into whatever the current configuration is.
        """
        return ProcessingSettings(
            mode=self.selected_mode,
            worker_count=self.worker_spin.value(),
            diagnostics_enabled=self.diagnostics_checkbox.isChecked(),
            diagnostics_dir=self._diagnostics_dir,
        )

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    def _on_mode_changed(self, _index: int) -> None:
        self._refresh()

    def _on_worker_count_changed(self, _value: int) -> None:
        self._refresh()

    def _refresh(self) -> None:
        """Keep the enabled state and the two computed labels truthful."""
        mode = self.selected_mode
        # The number only means anything in Custom mode; showing it greyed out
        # in the others keeps the dialog's shape stable while making clear that
        # OMRFlow, not the user, is deciding.
        self.worker_spin.setEnabled(mode is ProcessingMode.CUSTOM)
        active = self.processing_settings().configured_worker_count(self._cpu_count)
        self.active_label.setText(str(active))
        self.explanation_label.setText(MODE_EXPLANATIONS[mode])

        wants_diagnostics = self.diagnostics_checkbox.isChecked()
        self.diagnostics_folder_button.setEnabled(wants_diagnostics)
        if self._diagnostics_dir is not None:
            self.diagnostics_folder_label.setText(str(self._diagnostics_dir))
        elif wants_diagnostics:
            # Switched on with nowhere to write is a real state a user can
            # reach, and saying so beats silently writing nothing.
            self.diagnostics_folder_label.setText(
                "Choose a folder - diagnostics stay off until you do."
            )
        else:
            self.diagnostics_folder_label.setText("No diagnostics folder selected")


__all__ = ["MODE_EXPLANATIONS", "MODE_LABELS", "SettingsDialog"]
