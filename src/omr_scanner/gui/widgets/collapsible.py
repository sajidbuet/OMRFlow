"""A section of a form that can be folded away.

Purpose:
    Let a long configuration form show the settings most people change and
    fold the rest, without hiding that the folded part exists or what it is
    currently set to.

Responsibilities:
    * :class:`CollapsibleSection` - a titled header button and the widget it
      shows or hides.

What does NOT belong here:
    * Any knowledge of what is inside. A section is handed a widget and a
      title; it never inspects, validates or rebuilds its contents.

Why hiding rather than removing:
    Collapsing calls ``setVisible(False)`` on the content widget and nothing
    else. Every control inside keeps its value, its enabled state and its
    signal connections, so a form can be folded and unfolded without a single
    setting changing - which is the whole reason this is safe to apply to a
    dialog whose defaults matter.

Why the header is a `QToolButton`:
    It is focusable, it toggles on Space and Enter without any extra handling,
    and it reports itself to a screen reader as a button with a checked state.
    A `QLabel` with a mouse handler would look the same and be unusable from
    the keyboard.

Why a summary line:
    A folded section that says only "Scanner simulation" forces the reader to
    unfold it to discover whether anything unusual is set. :meth:`set_summary`
    puts a short description of the current values in the header, so the
    common case - "is anything non-default here?" - is answered without
    expanding anything.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.theme import Color, FontSize, FontWeight, Spacing, Stroke

_HEADER_STYLE = f"""
QToolButton#{{name}} {{{{
    background: transparent;
    border: none;
    border-bottom: {Stroke.HAIRLINE}px solid {Color.BORDER};
    border-radius: 0px;
    color: {Color.TEXT_PRIMARY};
    padding: {Spacing.XS}px {Spacing.XXS}px;
    text-align: left;
}}}}

QToolButton#{{name}}:hover {{{{
    background: {Color.SURFACE_HOVER};
}}}}

QToolButton#{{name}}:checked {{{{
    background: transparent;
}}}}

QToolButton#{{name}}:focus {{{{
    border: {Stroke.FOCUS_RING}px solid {Color.FOCUS};
}}}}
"""
"""Why the header carries its own style rather than inheriting one.

A checkable `QToolButton` is painted as an *active toggle* by every style
worth having - the application's own sheet tints it, and the native Windows
style fills it with the desktop accent colour and centres white text across
its full width. That is the right look for a toolbar button that is currently
engaged and entirely the wrong one for "this section is open": it turns a
quiet form into four saturated bands, and it reads as four things being
switched on.

Scoping the rules to the header's object name keeps them off every other tool
button in the application, and stating them here rather than in the theme
means a section looks right in a dialog that was built before the application
stylesheet was applied.
"""


class CollapsibleSection(QWidget):
    """A titled section whose contents can be folded away.

    Args:
        title: The section's name, shown on the header button.
        content: The widget to show or hide. Re-parented into this section.
        expanded: Whether it starts open.
        parent: Optional Qt parent.

    Signals:
        toggled: The section was expanded (``True``) or collapsed (``False``).

    Attributes:
        header: The toggle button. Exposed so a test can activate it from the
            keyboard without synthesising a click at a coordinate.
        content: As passed in.
    """

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        content: QWidget,
        *,
        expanded: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"collapsible_{_slug(title)}")
        self._title = title

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XXS)

        self.header = QToolButton(self)
        self.header.setObjectName(f"collapsibleHeader_{_slug(title)}")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.header.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        font = QFont(self.header.font())
        font.setWeight(QFont.Weight(FontWeight.SEMIBOLD))
        self.header.setFont(font)
        self.header.setAccessibleName(title)
        self.header.setStyleSheet(_HEADER_STYLE.format(name=self.header.objectName()))
        self.header.toggled.connect(self._on_toggled)
        layout.addWidget(self.header)

        self.summary_label = QLabel("", self)
        self.summary_label.setObjectName(f"collapsibleSummary_{_slug(title)}")
        self.summary_label.setWordWrap(True)
        summary_font = self.summary_label.font()
        summary_font.setPointSizeF(
            max(summary_font.pointSizeF() + FontSize.SECONDARY, FontSize.MIN_POINT_SIZE)
        )
        self.summary_label.setFont(summary_font)
        self.summary_label.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
        self.summary_label.setContentsMargins(Spacing.LG, 0, 0, 0)
        self.summary_label.setVisible(False)
        layout.addWidget(self.summary_label)

        self.content = content
        content.setParent(self)
        content.setVisible(expanded)
        layout.addWidget(content)

        self._summary = ""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def is_expanded(self) -> bool:
        """Whether the contents are currently shown."""
        return self.header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        """Fold or unfold the section.

        Idempotent, and safe to call on a section that is already in the
        requested state - which is what lets a validation failure say "make
        sure this is visible" without first checking whether it is.
        """
        self.header.setChecked(expanded)

    def set_summary(self, text: str) -> None:
        """Describe the current settings, for when the section is folded.

        Shown only while collapsed: an expanded section already shows its
        values, and repeating them above would be noise.
        """
        self._summary = text
        self.summary_label.setText(text)
        self.summary_label.setVisible(bool(text) and not self.is_expanded)

    def _on_toggled(self, expanded: bool) -> None:
        self.header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.content.setVisible(expanded)
        self.summary_label.setVisible(bool(self._summary) and not expanded)
        self.toggled.emit(expanded)


def _slug(title: str) -> str:
    """A stable object-name fragment, so tests can find a section by title."""
    return "".join(
        character.lower() if character.isalnum() else "_" for character in title
    ).strip("_")


__all__ = ["CollapsibleSection"]
