"""Choose how a paper is marked.

The dialog for brief §15: correct mark, blank mark, negative-marking mode and
amount, and whether a total may go below zero.

Two things worth keeping:

* **Penalties are entered as magnitudes.** The field says "deduction", the
  value is positive, and the scorer subtracts it. A field that accepted
  ``-0.25`` and also ``0.25`` would eventually be given one when the operator
  meant the other, and the paper would be marked generously by half.
* **Marks never travel through ``float``.** Every value is read as text and
  converted with :func:`~omr_scanner.domain.scoring.parse_mark`, which is
  exact. ``Fraction(0.1)`` is not one tenth.
"""

from __future__ import annotations

from decimal import InvalidOperation
from fractions import Fraction
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scoring import (
    NegativeMarking,
    ScoringPolicy,
    format_mark,
    parse_mark,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

MARK_DECIMALS = 4
"""Decimals offered for a mark.

Four rather than two so a thirds-based deduction can be typed by hand if an
operator wants to, without the *stored* policy being the rounded value - the
1-per-3 and 1-per-4 modes carry the exact fraction themselves.
"""


class ScoringPolicyDialog(QDialog):
    """Edit the marking rules, and preview them before saving."""

    def __init__(
        self, policy: ScoringPolicy, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("scoringPolicyDialog")
        self.setWindowTitle("Scoring Configuration")
        self.setModal(True)
        self.resize(560, 620)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_marks_box())
        layout.addWidget(self._build_negative_box())
        layout.addWidget(self._build_minimum_box())
        layout.addWidget(self._build_preview())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("scoringPolicyButtons")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.load(policy)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _mark_spin(self, name: str, *, minimum: float = 0.0) -> QDoubleSpinBox:
        """A spin box for one mark."""
        spin = QDoubleSpinBox()
        spin.setObjectName(name)
        spin.setDecimals(MARK_DECIMALS)
        spin.setRange(minimum, 1000.0)
        spin.setSingleStep(0.25)
        spin.valueChanged.connect(self._refresh_preview)
        return spin

    def _build_marks_box(self) -> QWidget:
        """Correct and blank marks."""
        box = QGroupBox("Marks")
        box.setObjectName("scoringMarksBox")
        layout = QFormLayout(box)

        self.correct_spin = self._mark_spin("correctMarkSpin")
        self.correct_spin.setToolTip(
            "Added for a correct answer, and for any question flagged as a "
            "wrong question."
        )
        layout.addRow("Marks for correct answer:", self.correct_spin)

        self.blank_spin = self._mark_spin("blankMarkSpin", minimum=-1000.0)
        self.blank_spin.setToolTip(
            "Added for an unanswered question. A blank is not a wrong answer: "
            "it never attracts the incorrect-answer deduction."
        )
        layout.addRow("Blank answer mark:", self.blank_spin)
        return box

    def _build_negative_box(self) -> QWidget:
        """The four negative-marking modes."""
        box = QGroupBox("Negative marking")
        box.setObjectName("negativeMarkingBox")
        layout = QVBoxLayout(box)

        self.mode_buttons: dict[NegativeMarking, QRadioButton] = {}
        for mode, name in (
            (NegativeMarking.NONE, "negativeNoneRadio"),
            (NegativeMarking.FIXED, "negativeFixedRadio"),
            (NegativeMarking.ONE_PER_THREE, "negativeOnePerThreeRadio"),
            (NegativeMarking.ONE_PER_FOUR, "negativeOnePerFourRadio"),
        ):
            button = QRadioButton(mode.label)
            button.setObjectName(name)
            button.toggled.connect(self._on_mode_changed)
            layout.addWidget(button)
            self.mode_buttons[mode] = button

        form = QFormLayout()
        self.incorrect_spin = self._mark_spin("incorrectPenaltySpin")
        self.incorrect_spin.setToolTip(
            "How much is taken off for one wrong answer. Enter it as a "
            "positive amount - 0.25 means minus a quarter mark."
        )
        form.addRow("Deduction per incorrect answer:", self.incorrect_spin)

        self.same_penalty_box = QCheckBox("Multiple answers: same as incorrect")
        self.same_penalty_box.setObjectName("multipleSamePenaltyCheck")
        self.same_penalty_box.setToolTip(
            "A question answered twice usually attracts the same deduction as "
            "one answered wrongly. Clear this to set it separately."
        )
        self.same_penalty_box.toggled.connect(self._on_mode_changed)
        form.addRow("", self.same_penalty_box)

        self.multiple_spin = self._mark_spin("multiplePenaltySpin")
        self.multiple_spin.setToolTip(
            "How much is taken off for one multiple answer, as a positive "
            "amount."
        )
        form.addRow("Deduction per multiple answer:", self.multiple_spin)
        layout.addLayout(form)
        return box

    def _build_minimum_box(self) -> QWidget:
        """Whether a total may go below zero."""
        box = QGroupBox("Minimum total")
        box.setObjectName("minimumScoreBox")
        layout = QFormLayout(box)

        self.clamp_box = QCheckBox("Do not allow a result below the minimum")
        self.clamp_box.setObjectName("clampMinimumCheck")
        self.clamp_box.setToolTip(
            "Recorded as part of the policy, so a result can say whether it "
            "was clamped rather than leaving it to be inferred."
        )
        self.clamp_box.toggled.connect(self._on_mode_changed)
        layout.addRow("", self.clamp_box)

        self.minimum_spin = self._mark_spin("minimumScoreSpin", minimum=-1000.0)
        layout.addRow("Minimum total:", self.minimum_spin)
        return box

    def _build_preview(self) -> QWidget:
        """The rules, in the words an operator is asked to confirm."""
        box = QGroupBox("Preview")
        box.setObjectName("scoringPreviewBox")
        layout = QVBoxLayout(box)
        self.preview_label = QLabel("")
        self.preview_label.setObjectName("scoringPreviewLabel")
        self.preview_label.setTextFormat(Qt.TextFormat.RichText)
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        return box

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def load(self, policy: ScoringPolicy) -> None:
        """Show an existing policy."""
        self.correct_spin.setValue(float(policy.correct_mark))
        self.blank_spin.setValue(float(policy.blank_mark))
        self.incorrect_spin.setValue(float(policy.incorrect_penalty))
        self.multiple_spin.setValue(
            float(
                policy.multiple_penalty
                if policy.multiple_penalty is not None
                else policy.incorrect_penalty
            )
        )
        self.same_penalty_box.setChecked(policy.multiple_penalty is None)
        self.clamp_box.setChecked(policy.clamp_minimum)
        self.minimum_spin.setValue(float(policy.minimum_score))
        self.mode_buttons[policy.mode].setChecked(True)
        self._on_mode_changed()

    def current_mode(self) -> NegativeMarking:
        """The mode currently selected."""
        for mode, button in self.mode_buttons.items():
            if button.isChecked():
                return mode
        return NegativeMarking.NONE

    def policy(self) -> ScoringPolicy:
        """Build the policy the dialog describes.

        Values are read from the spin boxes' *text* rather than their float
        value, so ``0.25`` is exactly one quarter rather than the nearest
        double to it.
        """
        mode = self.current_mode()
        return ScoringPolicy(
            correct_mark=_exact(self.correct_spin),
            blank_mark=_exact(self.blank_spin),
            incorrect_penalty=_exact(self.incorrect_spin),
            multiple_penalty=(
                None if self.same_penalty_box.isChecked() else _exact(self.multiple_spin)
            ),
            mode=mode,
            clamp_minimum=self.clamp_box.isChecked(),
            minimum_score=_exact(self.minimum_spin),
        )

    def _on_mode_changed(self) -> None:
        """Enable only the fields the chosen mode uses."""
        mode = self.current_mode()
        fixed = mode is NegativeMarking.FIXED
        self.incorrect_spin.setEnabled(fixed)
        self.same_penalty_box.setEnabled(fixed)
        self.multiple_spin.setEnabled(fixed and not self.same_penalty_box.isChecked())
        self.minimum_spin.setEnabled(self.clamp_box.isChecked())
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        """Show what the current settings will do."""
        policy = self.policy()
        lines = list(policy.describe())
        worked = (
            "<br><br><i>Worked example - 10 correct, 3 wrong, 2 blank: "
            f"<b>{format_mark(_example(policy))}</b></i>"
        )
        self.preview_label.setText("<br>".join(lines) + worked)


def _exact(spin: QDoubleSpinBox) -> Fraction:
    """Read a spin box's value exactly, from its text."""
    try:
        return parse_mark(spin.cleanText().replace(",", "."))
    except (InvalidOperation, ValueError):  # pragma: no cover - defensive
        return Fraction(0)


def _example(policy: ScoringPolicy) -> Fraction:
    """Mark a worked example: 10 correct, 3 wrong, 2 blank.

    Shown beside the rules so an operator can sanity-check a policy before
    applying it to a cohort - a deduction entered a decimal place out is much
    easier to see in a total than in a field.
    """
    total = (
        10 * policy.correct_mark
        + 2 * policy.blank_mark
        - 3 * policy.effective_incorrect_penalty
    )
    if policy.clamp_minimum and total < policy.minimum_score:
        return policy.minimum_score
    return total
