"""The compact branded application header.

Purpose:
    Replace the permanently visible File / Tools / Help menu row with one
    short branded band: an application-menu button, the OMRFlow wordmark, and
    the tagline.

Responsibilities:
    * :class:`AppHeader` - the band, its menu button and its branding.
    * Hide the tagline, and then the separator, when the window is too narrow
      for them - they are branding, and the menu button is not.

What does NOT belong here:
    * The menu's *contents*. The header is handed a `QMenu` that the main
      window already built and still owns, so there is exactly one File menu
      in the application and this widget does not know what is in it.
    * Window controls. The native title bar keeps them.

Why the wordmark is a `QSvgWidget` and not a `QPixmap`:
    The logo has to be crisp at 100%, 125%, 150% and 200% display scaling. A
    raster asset needs one file per scale and still blurs between them; Qt
    renders the SVG through the widget's own transform, so one asset is exact
    at every scale. Its height is fixed and its width computed from
    :data:`~omr_scanner.gui.branding.LOGO_ASPECT_RATIO`, which is the artwork's
    own measured ratio - so the wordmark is never stretched in either axis.

Why the menu button's icon is an SVG and not "☰":
    A Unicode glyph is a *font* dependency: it renders at a different weight
    and vertical offset in every UI font, is missing outright from some, and
    cannot take the application's own colour. The bundled Lucide `menu` icon
    is a vector that scales with the button and inherits its foreground.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, logo_svg_path
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.theme import Color, FontSize, FontWeight, Header, IconSize

MENU_ACCESSIBLE_NAME = "Application menu"
"""What a screen reader calls the header's menu button.

Spelled out because the button shows an icon and no text: without this a
screen reader would announce it as an unnamed button, which is the one thing
an icon-only control must never be.
"""

TAGLINE_WORDS = ("SCAN", "GRADE", "SIMPLIFY")

_TAGLINE_BREAKPOINT_SLACK = 24
"""Extra pixels of room the header wants before it shows the tagline again.

Without a margin between "hide it" and "show it" the tagline flickers on and
off while the window is dragged across the exact width at which it fits.
"""


class AppHeader(QWidget):
    """The application's top band: menu button, wordmark, tagline.

    Args:
        menu: The application menu to pop up. Built and owned by the main
            window; the header only shows it.
        parent: Optional Qt parent.

    Attributes:
        menu_button: The hamburger button. Exposed so a test can activate it
            from the keyboard without a modal appearing.
        logo: The wordmark widget.
        tagline: The "SCAN . GRADE . SIMPLIFY" label.
    """

    def __init__(self, menu: QMenu | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appHeader")
        self.setFixedHeight(Header.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            Header.H_PADDING, Header.V_PADDING, Header.H_PADDING, Header.V_PADDING
        )
        layout.setSpacing(Header.SEPARATOR_MARGIN)

        self.menu_button = self._build_menu_button(menu)
        layout.addWidget(self.menu_button)

        self.logo = self._build_logo()
        layout.addWidget(self.logo)

        self.separator = self._build_separator()
        layout.addWidget(self.separator)

        self.tagline = self._build_tagline()
        layout.addWidget(self.tagline)

        layout.addStretch(1)
        self._natural_branding_width = 0

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_menu_button(self, menu: QMenu | None) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("appMenuButton")
        button.setIcon(load_icon("menu"))
        button.setIconSize(QSize(IconSize.HEADER, IconSize.HEADER))
        button.setFixedSize(Header.MENU_BUTTON_SIZE, Header.MENU_BUTTON_SIZE)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Reachable by Tab, and it is the first thing in the tab order, so the
        # whole menu hierarchy is available from the keyboard without a mouse.
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setAccessibleName(MENU_ACCESSIBLE_NAME)
        button.setAccessibleDescription(
            "Open the File, Tools and Help menus"
        )
        button.setToolTip("Application menu (File, Tools, Help)")
        button.setStatusTip("File, Tools and Help")
        if menu is not None:
            button.setMenu(menu)
        return button

    def set_menu(self, menu: QMenu) -> None:
        """Attach the application menu after construction."""
        self.menu_button.setMenu(menu)

    def _build_logo(self) -> QSvgWidget:
        logo = QSvgWidget(str(logo_svg_path()), self)
        logo.setObjectName("appLogo")
        width = round(Header.LOGO_HEIGHT * LOGO_ASPECT_RATIO)
        logo.setFixedSize(width, Header.LOGO_HEIGHT)
        logo.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        logo.setAccessibleName("OMRFlow")
        return logo

    def _build_separator(self) -> QFrame:
        separator = QFrame(self)
        separator.setObjectName("appHeaderSeparator")
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedHeight(Header.SEPARATOR_HEIGHT)
        separator.setFixedWidth(1)
        return separator

    def _build_tagline(self) -> QLabel:
        """The tagline, with accent-coloured separator dots.

        Rich text rather than three labels and two painted dots: it is one
        line of branding, and one label keeps it on one baseline at every font
        size. Purely decorative, which is what makes it the first thing the
        header drops when space runs short - no information is lost.
        """
        dot = (
            f'<span style="color:{Color.PRIMARY};">'
            f"&nbsp;&nbsp;&#8226;&nbsp;&nbsp;</span>"
        )
        label = QLabel(dot.join(TAGLINE_WORDS), self)
        label.setObjectName("appTagline")
        label.setTextFormat(Qt.TextFormat.RichText)
        font = QFont(label.font())
        font.setPointSizeF(
            max(font.pointSizeF() + FontSize.TAGLINE, FontSize.MIN_POINT_SIZE)
        )
        font.setWeight(QFont.Weight(FontWeight.MEDIUM))
        font.setLetterSpacing(
            QFont.SpacingType.AbsoluteSpacing, Header.TAGLINE_LETTER_SPACING
        )
        label.setFont(font)
        label.setAccessibleName("SCAN, GRADE, SIMPLIFY")
        return label

    # ------------------------------------------------------------------
    # Responsive behaviour
    # ------------------------------------------------------------------
    def _branding_width(self) -> int:
        """Width the menu button, wordmark, separator and tagline want."""
        return (
            Header.MENU_BUTTON_SIZE
            + self.logo.width()
            + self.separator.width()
            + self.tagline.sizeHint().width()
            + 3 * Header.SEPARATOR_MARGIN
            + 2 * Header.H_PADDING
        )

    def resizeEvent(self, event: object) -> None:
        """Drop the tagline, then the separator, as the window narrows.

        Measured against what the branding actually wants at the current font
        rather than against a resolution, so a larger Windows text size drops
        the tagline at a wider window - which is correct, because that is when
        it stops fitting.
        """
        super().resizeEvent(event)  # type: ignore[arg-type]
        if self._natural_branding_width == 0:
            self._natural_branding_width = self._branding_width()

        showing = self.tagline.isVisible()
        needed = self._natural_branding_width + (
            0 if showing else _TAGLINE_BREAKPOINT_SLACK
        )
        wanted = self.width() >= needed
        if wanted != showing:
            self.tagline.setVisible(wanted)
            self.separator.setVisible(wanted)

    def sizeHint(self) -> QSize:
        """Room for the menu button and the whole branding, at the band's height."""
        return QSize(self._branding_width(), Header.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        """Only the menu button is mandatory, so that is the whole minimum.

        A minimum that included the branding would become part of the
        window's minimum width, making the branding - decoration - dictate how
        small the application can be.
        """
        return QSize(
            Header.MENU_BUTTON_SIZE + 2 * Header.H_PADDING,
            Header.HEIGHT,
        )


__all__ = ["MENU_ACCESSIBLE_NAME", "TAGLINE_WORDS", "AppHeader"]
