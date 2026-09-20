"""Configure a report's header text, logo, fonts and page setup.

The dialog for phase brief §16. Two things worth keeping:

* **Every field left blank/default changes nothing.** The default is to
  preserve whatever the operator's own template already has - see
  :mod:`omr_scanner.reporting.excel`'s "layout preservation" note - so this
  dialog is safe to open and cancel out of without side effects.
* **A logo is never distorted.** The width/height limits here are a bounding
  box; the actual embedded size is computed by
  :mod:`omr_scanner.reporting.excel` from the image's own aspect ratio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.reporting.excel import LayoutSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class ReportLayoutDialog(QDialog):
    """Edit one project's (or one set's) report layout settings."""

    def __init__(self, settings: LayoutSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("reportLayoutDialog")
        self.setWindowTitle("Report Layout Settings")
        self.setModal(True)
        self.resize(560, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.setObjectName("reportLayoutTabs")
        tabs.addTab(self._build_text_tab(), "Header && Footer")
        tabs.addTab(self._build_logo_tab(), "Logo")
        tabs.addTab(self._build_font_tab(), "Fonts")
        tabs.addTab(self._build_page_tab(), "Page Setup")
        layout.addWidget(tabs, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("reportLayoutButtons")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.load(settings)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_text_tab(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("reportTitleEdit")
        self.title_edit.setToolTip(
            "Left blank, the template's own header is used unchanged. Only "
            "written if the template has a blank row above its own header - "
            "candidate rows are never disturbed to make room."
        )
        form.addRow("Report title:", self.title_edit)

        self.examination_edit = QLineEdit()
        self.examination_edit.setObjectName("reportExaminationEdit")
        form.addRow("Examination name:", self.examination_edit)

        self.subtitle_edit = QLineEdit()
        self.subtitle_edit.setObjectName("reportSubtitleEdit")
        form.addRow("Subtitle:", self.subtitle_edit)

        self.footer_edit = QLineEdit()
        self.footer_edit.setObjectName("reportFooterEdit")
        form.addRow("Footer text:", self.footer_edit)

        self.auto_header_check = QCheckBox(
            "Automatically correct the marks-column header if it does not "
            "match the maximum score"
        )
        self.auto_header_check.setObjectName("autoUpdateMarksHeaderCheck")
        form.addRow("", self.auto_header_check)
        return box

    def _build_logo_tab(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        self.logo_edit = QLineEdit()
        self.logo_edit.setObjectName("reportLogoPathEdit")
        self.logo_edit.setReadOnly(True)
        form.addRow("Logo file:", self.logo_edit)

        from PySide6.QtWidgets import QHBoxLayout, QPushButton

        buttons = QHBoxLayout()
        self.choose_logo_button = QPushButton("Choose...")
        self.choose_logo_button.setObjectName("chooseLogoButton")
        self.choose_logo_button.clicked.connect(self._prompt_logo)
        buttons.addWidget(self.choose_logo_button)
        self.clear_logo_button = QPushButton("Remove")
        self.clear_logo_button.setObjectName("removeLogoButton")
        self.clear_logo_button.clicked.connect(lambda: self.logo_edit.setText(""))
        buttons.addWidget(self.clear_logo_button)
        form.addRow("", buttons)

        self.logo_width_spin = QSpinBox()
        self.logo_width_spin.setObjectName("logoMaxWidthSpin")
        self.logo_width_spin.setRange(16, 2000)
        form.addRow("Maximum width (px):", self.logo_width_spin)

        self.logo_height_spin = QSpinBox()
        self.logo_height_spin.setObjectName("logoMaxHeightSpin")
        self.logo_height_spin.setRange(16, 2000)
        form.addRow("Maximum height (px):", self.logo_height_spin)
        return box

    def _build_font_tab(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        self.font_family_edit = QLineEdit()
        self.font_family_edit.setObjectName("reportFontFamilyEdit")
        self.font_family_edit.setPlaceholderText("Leave blank to keep the template's font")
        form.addRow("Font family:", self.font_family_edit)

        self.title_size_spin = QSpinBox()
        self.title_size_spin.setObjectName("titleFontSizeSpin")
        self.title_size_spin.setRange(6, 72)
        form.addRow("Title font size:", self.title_size_spin)

        self.header_size_spin = QSpinBox()
        self.header_size_spin.setObjectName("headerFontSizeSpin")
        self.header_size_spin.setRange(6, 48)
        form.addRow("Header font size:", self.header_size_spin)

        self.body_size_spin = QSpinBox()
        self.body_size_spin.setObjectName("bodyFontSizeSpin")
        self.body_size_spin.setRange(6, 36)
        form.addRow("Body font size:", self.body_size_spin)

        self.bold_headers_check = QCheckBox("Bold column headers")
        self.bold_headers_check.setObjectName("boldHeadersCheck")
        form.addRow("", self.bold_headers_check)
        return box

    def _build_page_tab(self) -> QWidget:
        box = QGroupBox()
        form = QFormLayout(box)

        self.page_size_combo = QComboBox()
        self.page_size_combo.setObjectName("pageSizeCombo")
        self.page_size_combo.addItems(["A4", "Letter"])
        form.addRow("Paper size:", self.page_size_combo)

        self.orientation_combo = QComboBox()
        self.orientation_combo.setObjectName("orientationCombo")
        self.orientation_combo.addItems(["Portrait", "Landscape"])
        form.addRow("Orientation:", self.orientation_combo)

        self.margin_spin = QSpinBox()
        self.margin_spin.setObjectName("marginSpin")
        self.margin_spin.setRange(0, 100)
        self.margin_spin.setSuffix(" mm")
        self.margin_spin.setToolTip("Applied to all four margins equally.")
        form.addRow("Margins:", self.margin_spin)

        self.fit_to_width_check = QCheckBox("Fit to one page wide")
        self.fit_to_width_check.setObjectName("fitToWidthCheck")
        form.addRow("", self.fit_to_width_check)

        self.scale_spin = QSpinBox()
        self.scale_spin.setObjectName("scalePercentSpin")
        self.scale_spin.setRange(10, 400)
        self.scale_spin.setSuffix(" %")
        form.addRow("Scale (when not fit to width):", self.scale_spin)

        self.center_check = QCheckBox("Centre horizontally on the page")
        self.center_check.setObjectName("centerHorizontallyCheck")
        form.addRow("", self.center_check)

        self.repeat_header_check = QCheckBox("Repeat the header row on every printed page")
        self.repeat_header_check.setObjectName("repeatHeaderRowCheck")
        form.addRow("", self.repeat_header_check)
        return box

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _prompt_logo(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose Logo", "", "Images (*.png *.jpg *.jpeg)"
        )
        if chosen:
            self.logo_edit.setText(chosen)

    def load(self, settings: LayoutSettings) -> None:
        """Show an existing configuration."""
        self.title_edit.setText(settings.title_text)
        self.subtitle_edit.setText(settings.subtitle_text)
        self.examination_edit.setText(settings.examination_name)
        self.footer_edit.setText(settings.footer_text)
        self.auto_header_check.setChecked(settings.auto_update_marks_header)
        self.logo_edit.setText(settings.logo_path)
        self.logo_width_spin.setValue(settings.logo_max_width_px)
        self.logo_height_spin.setValue(settings.logo_max_height_px)
        self.font_family_edit.setText(settings.font_family)
        self.title_size_spin.setValue(settings.title_font_size)
        self.header_size_spin.setValue(settings.header_font_size)
        self.body_size_spin.setValue(settings.body_font_size)
        self.bold_headers_check.setChecked(settings.bold_headers)
        self.page_size_combo.setCurrentText(settings.page_size)
        self.orientation_combo.setCurrentText(settings.orientation.capitalize())
        self.margin_spin.setValue(int(settings.margin_top_mm))
        self.fit_to_width_check.setChecked(settings.fit_to_width)
        self.scale_spin.setValue(settings.scale_percent)
        self.center_check.setChecked(settings.center_horizontally)
        self.repeat_header_check.setChecked(settings.repeat_header_row)

    def settings(self) -> LayoutSettings:
        """Build the settings this dialog currently describes."""
        margin = float(self.margin_spin.value())
        return LayoutSettings(
            title_text=self.title_edit.text().strip(),
            subtitle_text=self.subtitle_edit.text().strip(),
            examination_name=self.examination_edit.text().strip(),
            footer_text=self.footer_edit.text().strip(),
            auto_update_marks_header=self.auto_header_check.isChecked(),
            logo_path=self.logo_edit.text().strip(),
            logo_max_width_px=self.logo_width_spin.value(),
            logo_max_height_px=self.logo_height_spin.value(),
            font_family=self.font_family_edit.text().strip(),
            title_font_size=self.title_size_spin.value(),
            header_font_size=self.header_size_spin.value(),
            body_font_size=self.body_size_spin.value(),
            bold_headers=self.bold_headers_check.isChecked(),
            page_size=self.page_size_combo.currentText(),
            orientation=self.orientation_combo.currentText().lower(),
            margin_top_mm=margin, margin_bottom_mm=margin,
            margin_left_mm=margin, margin_right_mm=margin,
            fit_to_width=self.fit_to_width_check.isChecked(),
            scale_percent=self.scale_spin.value(),
            center_horizontally=self.center_check.isChecked(),
            repeat_header_row=self.repeat_header_check.isChecked(),
        )
