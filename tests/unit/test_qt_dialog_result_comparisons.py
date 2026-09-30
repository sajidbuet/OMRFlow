"""No Qt dialog result is compared by identity.

PySide6 (6.11) returns the button clicked in a real ``QMessageBox.question``
as a plain ``int``, equal to - but not the same object as -
``QMessageBox.StandardButton.Yes``. ``answer is StandardButton.Yes`` is
therefore always false for a real click, and that silently disabled Answer
Key verification, replacing an attendance list, and three stress-campaign
confirmations, while tests that mocked the dialog to return the enum member
passed. This test keeps the pattern out of the source.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "omr_scanner"

IDENTITY_COMPARISON = re.compile(
    r"\bis\s+(?:not\s+)?(?:QMessageBox\.StandardButton|QDialog\.DialogCode|"
    r"QDialogButtonBox\.StandardButton)\."
)


def test_the_real_return_type_is_what_this_guards_against():
    from PySide6.QtWidgets import QMessageBox

    yes = QMessageBox.StandardButton.Yes
    returned = int(yes.value)  # what the real dialog hands back
    assert returned == yes
    assert returned is not yes


def test_no_dialog_result_is_compared_by_identity():
    offenders = [
        f"{path.relative_to(SOURCE)}:{number}: {line.strip()}"
        for path in sorted(SOURCE.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if IDENTITY_COMPARISON.search(line)
    ]
    assert offenders == []
