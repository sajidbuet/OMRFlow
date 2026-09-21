"""The heading every workflow page shares.

Purpose:
    Give each stage the same heading shape - icon, title, one-line summary,
    hairline rule - so that moving between stages does not move the point at
    which content starts.

Responsibilities:
    * :class:`PageHeader` - the heading row and its rule.
    * :class:`HeroTagline` - the optional right-aligned "Organize. Process.
      Get Results." block from the reference design.

What does NOT belong here:
    * Page content. This is the band above it.

Why the summary can be turned off:
    On a stage whose body is a full-size editor - the template designer's
    canvas, the scan preview - a permanent sentence of explanatory text is a
    row the operator reads once and then works around for the rest of the
    session. With ``show_summary=False`` the sentence survives as the title's
    tooltip and status tip, so it is still discoverable, without costing a row
    of the workspace those pages need most.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.theme import (
    Color,
    Dashboard,
    FontSize,
    FontWeight,
    IconSize,
    Spacing,
    Stroke,
)

HERO_LINES = ("Organize.", "Process.", "Get Results.")
"""The reference design's three-line tagline for the Project page.

Text, not an image: the brief requires that important information never live
only inside a picture, and this renders at any scale, in any font size, and
is readable to a screen reader.
"""


class _PageIcon(QWidget):
    """The stage's glyph, tinted with the brand accent."""

    def __init__(self, icon_name: str, extent: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeaderIcon")
        self.setFixedSize(extent, extent)
        self._icon = load_icon(icon_name)
        self._extent = extent

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ratio = self.devicePixelRatioF()
        pixmap = self._icon.pixmap(
            QSize(self._extent, self._extent) * ratio, ratio, QIcon.Mode.Normal
        )
        tint = QPainter(pixmap)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(pixmap.rect(), QColor(Color.PRIMARY))
        tint.end()
        painter.drawPixmap(0, 0, self._extent, self._extent, pixmap)
        painter.end()


class HeroTagline(QWidget):
    """The reference design's right-aligned three-line tagline.

    Decorative, and deliberately implemented as text with a short accent rule
    rather than as the reference's photographic artwork: the page must read
    correctly without it, no bundled asset has to be licensed for it, and it
    costs no vertical space beyond the heading it sits beside.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageHero")
        self.setMaximumWidth(Dashboard.HERO_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.LG, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        for line in HERO_LINES:
            label = QLabel(line, self)
            label.setObjectName("pageHeroLine")
            font = QFont(label.font())
            font.setItalic(True)
            font.setPointSizeF(
                max(font.pointSizeF() + FontSize.SECONDARY, FontSize.MIN_POINT_SIZE)
            )
            label.setFont(font)
            label.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
            label.setAlignment(Qt.AlignmentFlag.AlignRight)
            layout.addWidget(label)

        rule = QFrame(self)
        rule.setObjectName("pageHeroRule")
        rule.setFrameShape(QFrame.Shape.NoFrame)
        rule.setFixedHeight(Stroke.ACCENT_RULE)
        rule.setMaximumWidth(Spacing.XXL + Spacing.MD)
        rule.setStyleSheet(f"background: {Color.PRIMARY};")
        row = QHBoxLayout()
        row.setContentsMargins(0, Spacing.SM, 0, 0)
        row.addStretch(1)
        row.addWidget(rule)
        layout.addLayout(row)
        layout.addStretch(1)


class PageHeader(QWidget):
    """A stage's heading: icon, title, summary, and a hairline rule beneath.

    Args:
        title: The stage's name.
        summary: One line describing what the stage is for.
        icon_name: Bundled Lucide icon name.
        parent: Optional Qt parent.
        show_summary: Whether the summary gets its own row - see the module
            docstring.
        hero: Show the decorative :class:`HeroTagline` on the right.

    Attributes:
        title_label: The heading.
        summary_label: The summary row, or ``None`` when not shown.
    """

    def __init__(
        self,
        title: str,
        summary: str,
        icon_name: str,
        parent: QWidget | None = None,
        *,
        show_summary: bool = True,
        hero: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeader")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.SM)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Spacing.MD)

        self.icon = _PageIcon(icon_name, IconSize.PAGE_HEADER, self)
        row.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(Spacing.XXS)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("pageTitle")
        title_font = QFont(self.title_label.font())
        title_font.setPointSizeF(title_font.pointSizeF() + FontSize.PAGE_TITLE)
        title_font.setWeight(QFont.Weight(FontWeight.BOLD))
        self.title_label.setFont(title_font)
        text_column.addWidget(self.title_label)

        self.summary_label: QLabel | None = None
        if show_summary:
            self.summary_label = QLabel(summary, self)
            self.summary_label.setObjectName("pageSummary")
            self.summary_label.setWordWrap(True)
            text_column.addWidget(self.summary_label)
        else:
            self.title_label.setToolTip(summary)
            self.title_label.setStatusTip(summary)

        row.addLayout(text_column, 1)

        self.hero: HeroTagline | None = None
        if hero:
            self.hero = HeroTagline(self)
            row.addWidget(self.hero, alignment=Qt.AlignmentFlag.AlignVCenter)

        outer.addLayout(row)

        self.rule = QFrame(self)
        self.rule.setObjectName("pageHeaderRule")
        self.rule.setFrameShape(QFrame.Shape.NoFrame)
        self.rule.setFixedHeight(Stroke.HAIRLINE)
        outer.addWidget(self.rule)

    def set_hero_visible(self, visible: bool) -> None:
        """Show or hide the decorative tagline.

        Called by a responsive page that has stopped having room for
        decoration. Silently ignored when the header was built without one.
        """
        if self.hero is not None:
            self.hero.setVisible(visible)


__all__ = ["HERO_LINES", "HeroTagline", "PageHeader"]
