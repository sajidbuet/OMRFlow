"""The workflow pages' container: a stack that scrolls instead of clipping.

Purpose:
    Hold the nine workflow pages, show one at a time, and - when the page on
    screen needs more room than the window has - scroll it rather than
    squeezing its controls on top of each other.

Responsibilities:
    * :class:`PageStack` - the `QStackedWidget` interface the main window and
      the tests use (``addWidget``, ``widget``, ``currentWidget``, ...), on a
      scroll area whose content is as large as the *current* page needs and
      no larger.

What does NOT belong here:
    * Which page is current, and when. The main window decides; this only
      shows what it is told to.
    * Any page's own responsive behaviour. A page that reflows - the Project
      dashboard stacking its columns, the ribbon changing layout - still
      does; the scroll bars only appear once a page has nothing left to give.

Why the stack scrolls:
    The interface zoom makes every control larger, and at 200% a page built
    for a 900-pixel-wide window needs 1800 pixels - more than a 1366x768
    display has. Before this container, a page below its minimum size was
    laid out anyway, and its controls overlapped. Raising the window's
    minimum instead would make the application impossible to fit on the
    displays the zoom is most useful on. Scrolling is the remaining honest
    answer: every control at its full size, all of it reachable.

Why the size is the current page's and not the largest page's:
    A plain `QStackedWidget` reports the largest minimum of *all* its pages,
    so the widest page - Reports - would put a horizontal scroll bar under
    the Project dashboard too. :class:`_CurrentPageStack` answers for the
    page on screen only. At a window size every page fits, nothing scrolls
    and nothing has moved, so at 100% the shell looks exactly as it did.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QLayout, QScrollArea, QStackedWidget, QWidget

from omr_scanner.gui.ui_scale import UiScaleManager


class _CurrentPageStack(QStackedWidget):
    """A `QStackedWidget` whose size hints are its current page's."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.layout()
        if layout is not None:
            # Without this the stacked layout pins the widget's minimum to the
            # largest page's, which is exactly what this class exists to stop.
            layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def minimumSizeHint(self) -> QSize:
        """The current page's floor, honouring an explicit minimum it set."""
        page = self.currentWidget()
        if page is None:
            return QSize(0, 0)
        return page.minimumSizeHint().expandedTo(page.minimumSize())

    def sizeHint(self) -> QSize:
        """The current page's preferred size."""
        page = self.currentWidget()
        return page.sizeHint() if page is not None else QSize(0, 0)


class PageStack(QScrollArea):
    """The workflow pages, one at a time, scrolled when one cannot fit.

    Args:
        parent: Optional Qt parent.

    Signals:
        currentChanged: The current page changed. Carries its index - the
            same signal, with the same meaning, as `QStackedWidget`'s.
    """

    currentChanged = Signal(int)  # noqa: N815 - QStackedWidget's own name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workflowPageStack")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._stack = _CurrentPageStack()
        self._stack.setObjectName("workflowPages")
        self._stack.currentChanged.connect(self.currentChanged.emit)
        self.setWidget(self._stack)
        viewport = self.viewport()
        if viewport is not None:
            viewport.setAutoFillBackground(False)
        # After the zoom has finished - fonts, metrics and pane floors - the
        # current page may need more (or less) room than the scroll area last
        # gave it, with no resize of this widget to make it look again.
        manager = UiScaleManager.find()
        if manager is not None:
            manager.scale_changed.connect(self._refit)

    def _refit(self, _percent: int = 0) -> None:
        """Size the content to the viewport, or to the current page's floor."""
        self._stack.updateGeometry()
        self._stack.resize(self.viewport().size().expandedTo(self._stack.minimumSizeHint()))

    # ------------------------------------------------------------------
    # The QStackedWidget interface
    # ------------------------------------------------------------------
    def addWidget(self, page: QWidget) -> int:  # noqa: N802 - Qt's name
        """Append ``page``; returns its index."""
        return self._stack.addWidget(page)

    def count(self) -> int:
        """How many pages the stack holds."""
        return self._stack.count()

    def widget(self, index: int) -> QWidget | None:  # type: ignore[override]
        """The page at ``index``, or ``None``.

        Deliberately the *page*, as `QStackedWidget.widget` returns - not the
        scroll area's content widget, which is what `QScrollArea.widget`
        would return and which no caller of a page stack wants.
        """
        return self._stack.widget(index)

    def indexOf(self, page: QWidget) -> int:  # noqa: N802 - Qt's name
        """``page``'s index, or ``-1``."""
        return self._stack.indexOf(page)

    def currentIndex(self) -> int:  # noqa: N802 - Qt's name
        """The index of the page on screen."""
        return self._stack.currentIndex()

    def currentWidget(self) -> QWidget | None:  # noqa: N802 - Qt's name
        """The page on screen."""
        return self._stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt's name
        """Show the page at ``index``, scrolled to its top-left."""
        self._stack.setCurrentIndex(index)
        self._refit()
        self._reset_scroll()

    def setCurrentWidget(self, page: QWidget) -> None:  # noqa: N802 - Qt's name
        """Show ``page``, scrolled to its top-left."""
        self._stack.setCurrentWidget(page)
        self._refit()
        self._reset_scroll()

    @property
    def pages_widget(self) -> QStackedWidget:
        """The stacked widget inside the scroll area."""
        return self._stack

    def _reset_scroll(self) -> None:
        """A newly shown page starts at its origin, not at the last one's offset."""
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            if bar is not None:
                bar.setValue(0)


__all__ = ["PageStack"]
