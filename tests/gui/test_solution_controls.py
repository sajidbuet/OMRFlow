"""The dialog's answer-key and candidate-performance controls.

Checked at the level the rest of the dialog is: the controls exist with the
right defaults, reach :class:`GenerationRequest` unchanged, refuse impossible
figures with a message on the right control, stay reachable on a small laptop
screen, and the completion summary says what was written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QWidget
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.ground_truth import DatasetManifest
from omr_scanner.evaluation.performance import (
    DEFAULT_PERFORMANCE,
    PerformanceDistribution,
)
from omr_scanner.gui.devtools import GenerateDatasetDialog, GenerationSummaryDialog
from omr_scanner.gui.devtools.generate_dialog import GenerationRequest
from omr_scanner.services import save_template

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.gui


@pytest.fixture
def template_path(tmp_path: Path) -> Path:
    path = tmp_path / "templates" / "synthetic.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(build_answer_sheet_template(), path)


@pytest.fixture
def dialog(qtbot, tmp_path: Path, template_path: Path) -> GenerateDatasetDialog:
    widget = GenerateDatasetDialog(template_path=template_path, output_dir=tmp_path)
    qtbot.addWidget(widget)
    return widget


@pytest.fixture
def complaints(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []

    def capture(_parent: object, _title: str, text: str, *_args: object) -> None:
        shown.append(text)

    monkeypatch.setattr(
        "omr_scanner.gui.devtools.generate_dialog.QMessageBox.information",
        staticmethod(capture),
    )
    return shown


def _in_view(area: QScrollArea, widget: QWidget) -> bool:
    viewport = area.viewport()
    top_left = widget.mapTo(viewport, widget.rect().topLeft())
    return viewport.rect().intersects(QRect(top_left, widget.size()))


class TestTheDefaults:
    def test_solutions_are_on_and_the_model_is_the_brief(self, dialog):
        assert dialog.solutions_checkbox.isChecked()
        request = dialog.request()
        assert request is not None
        assert request.generate_solutions is True
        assert request.performance == DEFAULT_PERFORMANCE

    def test_the_dialog_and_the_api_share_one_default(self, tmp_path: Path):
        request = GenerationRequest(template_path=tmp_path, output_dir=tmp_path)
        assert request.generate_solutions is True
        assert request.performance == DEFAULT_PERFORMANCE

    def test_the_figures_are_shown_as_percentages(self, dialog):
        assert dialog.mean_spin.value() == 65.0
        assert dialog.sd_spin.value() == 15.0
        assert dialog.min_spin.value() == 0.0
        assert dialog.max_spin.value() == 100.0

    def test_the_section_starts_folded_and_summarises_itself(self, dialog):
        assert dialog.answer_key_section.is_expanded is False
        summary = dialog.answer_key_section.findChildren(QLabel)
        text = " ".join(label.text() for label in summary)
        assert "solution sheets" in text
        assert "65%" in text


class TestTheRequest:
    def test_the_figures_reach_the_request_as_fractions(self, dialog):
        dialog.mean_spin.setValue(72.5)
        dialog.sd_spin.setValue(10.0)
        dialog.min_spin.setValue(20.0)
        dialog.max_spin.setValue(95.0)
        policy = dialog.request().performance
        assert policy.mean == pytest.approx(0.725)
        assert policy.stddev == pytest.approx(0.10)
        assert (policy.minimum, policy.maximum) == (pytest.approx(0.2), pytest.approx(0.95))

    def test_solutions_can_be_switched_off(self, dialog):
        dialog.solutions_checkbox.setChecked(False)
        assert dialog.request().generate_solutions is False

    def test_the_legacy_mode_disables_the_figures(self, dialog):
        dialog.distribution_combo.setCurrentIndex(
            dialog.distribution_combo.findData(PerformanceDistribution.RANDOM)
        )
        assert dialog.request().performance.distribution is PerformanceDistribution.RANDOM
        assert not dialog.mean_spin.isEnabled()
        assert not dialog.max_spin.isEnabled()


class TestValidation:
    @pytest.mark.parametrize(
        ("field", "value"), [("min_spin", 70.0), ("max_spin", 50.0)]
    )
    def test_an_impossible_combination_is_refused_on_the_right_section(
        self, dialog, complaints, field, value
    ):
        getattr(dialog, field).setValue(value)
        dialog._on_accept()
        assert dialog.result() != dialog.DialogCode.Accepted
        assert complaints and "minimum <= mean <= maximum" in complaints[0]
        assert dialog.answer_key_section.is_expanded is True

    def test_valid_figures_are_accepted(self, dialog, complaints):
        dialog.mean_spin.setValue(50.0)
        dialog._on_accept()
        assert complaints == []
        assert dialog.result() == dialog.DialogCode.Accepted


class TestItFitsASmallLaptop:
    def test_every_new_control_scrolls_into_view_at_1366x768(self, qtbot, dialog):
        dialog.resize(1366, 768)
        dialog.show()
        qtbot.waitExposed(dialog)
        for section in dialog._sections():
            section.set_expanded(True)
        QApplication.processEvents()
        for control in (
            dialog.solutions_checkbox,
            dialog.distribution_combo,
            dialog.mean_spin,
            dialog.max_spin,
        ):
            dialog.scroll_area.ensureWidgetVisible(control)
            QApplication.processEvents()
            assert _in_view(dialog.scroll_area, control), control.objectName()
        assert dialog.height() <= 768
        assert dialog.width() <= 1366


class TestTheCompletionSummary:
    def _details(self, qtbot, tmp_path: Path, generator: dict) -> str:
        manifest = DatasetManifest(name="x", generator=generator, entries=("a.json",))
        summary = GenerationSummaryDialog(manifest, tmp_path)
        qtbot.addWidget(summary)
        label = summary.findChild(QLabel, "generationSummaryDetails")
        assert label is not None
        return label.text()

    def test_it_names_the_solution_files_and_the_scores(self, qtbot, tmp_path: Path):
        text = self._details(
            qtbot,
            tmp_path,
            {
                "solutions": {"directory": "solution", "sets": [{}, {}, {}]},
                "performance": {
                    "observed": {"mean": 0.651, "stddev": 0.149, "candidates": 250}
                },
            },
        )
        assert "Solution sheets: 3" in text
        assert "Answer-key files: 3" in text
        assert "mean 65%" in text

    def test_a_manifest_from_before_solutions_existed_still_opens(
        self, qtbot, tmp_path: Path
    ):
        text = self._details(qtbot, tmp_path, {"template_name": "Old"})
        assert "Solution sheets" not in text
        assert "Old" in text
