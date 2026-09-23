"""The design system: every colour, dimension and type size, named once.

Purpose:
    Be the single place a visual decision is recorded, so that changing the
    accent colour, the navigator's height or the page margin is one edit here
    rather than a search for hex literals across forty widget modules.

Responsibilities:
    * :class:`Color` - the palette, built around one accent
      (:data:`Color.PRIMARY`) on a predominantly white/neutral ground.
    * :class:`Spacing`, :class:`Radius`, :class:`IconSize`, :class:`Stroke` -
      the dimensional scale.
    * :class:`FontSize`, :class:`FontWeight` - type.
    * :class:`Navigator`, :class:`Chrome`, :class:`Footer` - metrics for the
      three shell components that need fixed relationships between parts.
    * :class:`Density` - the workflow ribbon's five density levels, which the
      chrome's ``-``/``+`` buttons step between.

What does NOT belong here:
    * Widget construction, and any Qt import at all. This module is plain
      data, which is what lets a test assert on contrast ratios and scale
      relationships without a display.
    * Qt stylesheet text. That is composed from these tokens in
      :mod:`omr_scanner.gui.theme.stylesheet`.

Why a class-per-group rather than an enum or a dict:
    A use site reads ``Color.TEXT_SECONDARY`` or ``Spacing.MD`` - which needs
    no comment - while a dict lookup reads ``tokens["text_secondary"]``, which
    a typo turns into a runtime error instead of a type error.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class Color:
    """The palette.

    Predominantly white, near-white and neutral grey, with a dark charcoal for
    text and exactly one accent. The accent is reserved for the active
    workflow step, the single primary action on a page, and meaningful focus -
    never as a decorative fill, because an interface that is mostly red gives
    the operator nothing to notice.
    """

    PRIMARY: Final = "#AC1F24"
    """The brand accent. Active workflow step, primary buttons, focus accents."""

    PRIMARY_HOVER: Final = "#C02830"
    """Lighter than :data:`PRIMARY`, so hovering an accented control reads as a
    response rather than as a different control."""

    PRIMARY_PRESSED: Final = "#8E1A1E"
    """Darker than :data:`PRIMARY` - the conventional "pushed in" direction."""

    PRIMARY_DISABLED: Final = "#D9A2A5"
    """A desaturated accent, for a primary button that is not currently
    available. Paired with :data:`TEXT_ON_PRIMARY_DISABLED`, never used as the
    only signal - the control is also genuinely disabled, so it does not
    respond to hover and is skipped by the focus chain."""

    PRIMARY_SOFT: Final = "#F7E7E8"
    """A very pale tint of the accent, for the circular icon plate behind a
    Getting Started entry's icon. Light enough that charcoal text on top of it
    stays comfortably readable."""

    SURFACE: Final = "#FFFFFF"
    """Cards, input backgrounds, the page ground."""

    SURFACE_SUNKEN: Final = "#FAFAFA"
    """The window behind the cards, and the resting fill of an inactive
    workflow step. Distinguishable from :data:`SURFACE` without a border."""

    SURFACE_MUTED: Final = "#F2F2F3"
    """The header and footer bands, and a hovered inactive workflow step."""

    SURFACE_HOVER: Final = "#EAEAEB"
    """Hover fill for a neutral control."""

    SURFACE_PRESSED: Final = "#DEDEDF"

    SURFACE_DISABLED: Final = "#F5F5F5"

    BORDER: Final = "#E2E2E4"
    """The default hairline: card edges, dividers, the header's bottom rule."""

    BORDER_STRONG: Final = "#CBCBCE"
    """Input and neutral-button outlines, which need to read as an edge rather
    than as a change of surface."""

    BORDER_DASHED: Final = "#D4D4D7"
    """The dashed outline of an empty state, which is a placeholder rather than
    a container and should not look like a solid card."""

    TEXT_PRIMARY: Final = "#1A1A1A"
    """Dark charcoal. Headings and body text. Also what SVG icons resolve
    ``stroke="currentColor"`` to, for an icon beside primary text."""

    TEXT_SECONDARY: Final = "#5C5C60"
    """Subtitles, supporting sentences, the footer's credit line.

    6.66:1 on :data:`SURFACE` and 5.95:1 on :data:`SURFACE_MUTED` - both
    comfortably past WCAG AA for body text, because "secondary" must not mean
    "hard to read". The ratios are verified in
    ``tests/unit/test_theme_tokens.py``, not asserted here."""

    TEXT_TERTIARY: Final = "#6E6E73"
    """The least prominent readable text: an empty state's explanatory line.

    5.07:1 on :data:`SURFACE` and 4.86:1 on :data:`SURFACE_SUNKEN`, so it
    passes WCAG AA for normal text on both grounds it is used against. The
    first value tried here was ``#77777C``, which reads as a perfectly
    ordinary grey and fails at 4.45:1 and 4.27:1 - caught by the contrast
    test, which is the reason that test computes the ratios rather than
    trusting a comment."""

    TEXT_DISABLED: Final = "#8A8A8E"
    """A disabled control's text. Deliberately still legible - a disabled
    workflow step has to be readable to be understood as a step - and never
    the only signal that a control is unavailable."""

    TEXT_ON_PRIMARY: Final = "#FFFFFF"
    """White on :data:`PRIMARY`: 7.06:1, past AA for body text."""

    TEXT_ON_PRIMARY_DISABLED: Final = "#FBEFEF"

    FOCUS: Final = "#AC1F24"
    """The focus ring colour. The same accent, at
    :data:`Stroke.FOCUS_RING` width with an inner light halo, so it stays
    visible on both light and accented backgrounds."""

    FOCUS_HALO: Final = "#FFFFFF"
    """Drawn just inside :data:`FOCUS`, so the ring remains visible when the
    control it outlines is itself accent-coloured."""

    STATUS_READY: Final = "#1F8A4C"
    STATUS_BUSY: Final = "#B26A00"
    STATUS_ERROR: Final = "#AC1F24"
    STATUS_IDLE: Final = "#77777C"
    """Footer status dots. Always accompanied by the status *word*, never the
    only carrier of the state - see :mod:`omr_scanner.gui.widgets.status_footer`."""

    WINDOW_BUTTON_HOVER: Final = "#E4E4E6"
    """The hover fill of the minimise and maximise buttons, matching the
    neutral hover the rest of the chrome row uses."""

    WINDOW_CLOSE_HOVER: Final = "#C42B1C"
    """Windows' own close-button hover red. Deliberately *not*
    :data:`PRIMARY`: the brand accent means "the active step" everywhere else
    in this row, and reusing it on Close would make the one irreversible
    control look like a selection."""

    WINDOW_CLOSE_HOVER_GLYPH: Final = "#FFFFFF"

    DESTRUCTIVE: Final = "#B3261E"
    """For a genuinely destructive confirmation, distinguished from
    :data:`PRIMARY` so that "delete" and "proceed" are not the same colour."""


class Spacing:
    """The spacing scale, in logical (DPI-independent) pixels.

    A four-step geometric-ish scale rather than arbitrary numbers, so that
    gaps in unrelated parts of the interface line up without coordination.
    """

    XXS: Final = 2
    XS: Final = 4
    SM: Final = 8
    MD: Final = 12
    LG: Final = 16
    XL: Final = 24
    XXL: Final = 32

    PAGE_MARGIN: Final = 16
    """Around a page's content. Smaller than the 24px the pages used before
    this shell: the horizontal room the removed sidebar gave back is worth
    more as content than as margin."""

    PAGE_MARGIN_COMPACT: Final = 8
    """For a page whose body is a full-size editor (the template designer's
    canvas, the scan preview), where every pixel of margin is a pixel the
    operator cannot zoom into."""

    CARD_PADDING: Final = 16
    CARD_GAP: Final = 12


class Radius:
    """Corner radii.

    Restrained by intent - this is examination software, not a consumer
    dashboard.
    """

    NONE: Final = 0
    SM: Final = 3
    MD: Final = 6
    LG: Final = 8
    PILL: Final = 999


class Stroke:
    """Line widths, in logical pixels."""

    HAIRLINE: Final = 1
    BORDER: Final = 1
    FOCUS_RING: Final = 2
    ACCENT_RULE: Final = 3
    """The short accent underline beneath the Project page's hero text."""


class IconSize:
    """Square icon edge lengths, in logical pixels."""

    SM: Final = 14
    MD: Final = 16
    LG: Final = 20
    XL: Final = 24
    CHROME_MENU: Final = 18
    """The hamburger button's glyph, in the chrome row."""

    CHROME_CONTROL: Final = 14
    """The density and previous/next glyphs, which sit beside the ribbon and
    must read as secondary to both it and the menu button."""

    PAGE_HEADER: Final = 32
    EMPTY_STATE: Final = 64
    GETTING_STARTED: Final = 20
    GETTING_STARTED_PLATE: Final = 40
    """Diameter of the pale circular plate the icon sits on."""


class FontSize:
    """Point-size *deltas* from the platform's default UI font.

    Deltas, not absolute sizes. An absolute size would ignore both the
    platform default and the user's
    Windows text-scaling preference; a delta respects both, which is what
    keeps the interface usable at 125% and 150% scaling.
    """

    PAGE_TITLE: Final = 6
    SECTION_TITLE: Final = 1
    CARD_TITLE: Final = 0
    BODY: Final = 0
    SECONDARY: Final = -1
    SMALL: Final = -1
    NAV_STEP: Final = 0
    FOOTER: Final = -1

    MIN_POINT_SIZE: Final = 7.5
    """Floor applied after any negative delta. A "secondary" size that lands
    below this stops being secondary and starts being unreadable, which is
    the specific failure this floor exists to prevent on small default
    fonts."""


class FontWeight:
    """Qt font weights, named."""

    NORMAL: Final = 400
    MEDIUM: Final = 500
    SEMIBOLD: Final = 600
    BOLD: Final = 700


class DensityLevel(NamedTuple):
    """The horizontal metrics of one workflow-ribbon density level.

    Attributes:
        h_padding: Inside each end of a step, clear of the arrow geometry.
        icon_text_gap: Between a step's icon and its label.
        arrow_depth: How far the chevron's point projects, and equally how
            deep the notch on its left is.
        icon_extent: Edge length of a step's icon.
        min_label_width: The narrowest a label may be drawn before the step
            is considered unrenderable at this density.
    """

    h_padding: int
    icon_text_gap: int
    arrow_depth: int
    icon_extent: int
    min_label_width: int


class Density:
    """The workflow ribbon's density levels, tightest to roomiest.

    Every level changes *horizontal* measurements only - padding, the icon/
    label gap, the arrow depth, the icon and the label allowance. Nothing here
    touches the step's height or its font, for two separate reasons. The
    height is shared with the window controls and the logo in one chrome row,
    so varying it would make the whole application shell jump between two
    sizes when someone pressed ``-``. And the font is what a user reads: the
    brief's own line is that density must stay "within reasonable accessible
    limits", which a control that shrank the type would not be.

    :data:`DEFAULT` is what a fresh installation uses;
    :data:`MINIMUM`/:data:`MAXIMUM` are the bounds the ``-``/``+`` buttons
    clamp to.
    """

    LEVELS: Final = (
        DensityLevel(h_padding=5, icon_text_gap=4, arrow_depth=8, icon_extent=14,
                     min_label_width=24),
        DensityLevel(h_padding=7, icon_text_gap=5, arrow_depth=9, icon_extent=16,
                     min_label_width=26),
        DensityLevel(h_padding=10, icon_text_gap=6, arrow_depth=11, icon_extent=18,
                     min_label_width=28),
        DensityLevel(h_padding=13, icon_text_gap=8, arrow_depth=12, icon_extent=18,
                     min_label_width=30),
        DensityLevel(h_padding=17, icon_text_gap=10, arrow_depth=13, icon_extent=20,
                     min_label_width=32),
    )

    MINIMUM: Final = 0
    MAXIMUM: Final = len(LEVELS) - 1

    DEFAULT: Final = 1
    """Level 1, not the middle of the range.

    Chosen by measurement against the display the brief singles out: at 1366
    pixels and 100% scaling, the chrome row leaves the ribbon roughly 950
    logical pixels, and the nine stages want about 940 at this level and about
    1040 at the next one up. Defaulting to the roomier level would put the
    most constrained target display into the scrolling layout out of the box,
    for the sake of three pixels of padding per step. The two roomier levels
    are still there for anyone with the width to spend on them.
    """

    @classmethod
    def clamp(cls, level: int) -> int:
        """Return ``level`` brought inside :data:`MINIMUM`..:data:`MAXIMUM`."""
        return max(cls.MINIMUM, min(cls.MAXIMUM, level))

    @classmethod
    def level(cls, level: int) -> DensityLevel:
        """The metrics for ``level``, clamped to the supported range."""
        return cls.LEVELS[cls.clamp(level)]


class Navigator:
    """Metrics shared by every workflow-ribbon density level.

    The one relationship that matters in the chevron's geometry is between
    the arrow depth and the horizontal padding: the arrow eats into the step's
    box at both ends, so the padding has to clear it or the label collides
    with the chevron's point. Both vary per density level, so that invariant
    is asserted across all of them in ``tests/unit/test_theme_tokens.py``.
    """

    STEP_HEIGHT: Final = 34
    """A step's height, at every density.

    Constant on purpose - see :class:`Density`. Comfortably past the 32px
    a pointer target wants, and short enough that the whole application
    chrome - logo, menu, ribbon and window controls - fits in one
    :data:`Chrome.HEIGHT` row.
    """

    STEP_GAP: Final = 3
    """The visible seam between two chevrons. Small, and deliberately not
    zero: two accent-adjacent steps with no seam read as one wide shape."""

    STRIP_V_PADDING: Final = 0
    STRIP_H_PADDING: Final = 2
    """Around the scrolled strip, inside the chrome row."""

    SCROLL_MARGIN: Final = 24
    """How much of the neighbouring step to keep visible when the ribbon
    scrolls the active one into view, so the strip reads as continuing rather
    than as ending at the viewport edge."""

    WHEEL_STEP: Final = 60
    """Logical pixels scrolled per wheel notch in the scrolling layout."""


class Chrome:
    """Metrics for the single application chrome row.

    One row carries everything the shell used to spread over two bands and a
    native title bar: the application menu, the wordmark, the density and
    previous/next controls, the workflow ribbon, and the window buttons.
    """

    HEIGHT: Final = 46
    """Total row height. It has to clear :data:`Navigator.STEP_HEIGHT` plus
    the row's vertical padding, and it is the whole vertical cost of the
    application's chrome - there is no second band beneath it."""

    H_PADDING: Final = 8
    V_PADDING: Final = 6
    GROUP_GAP: Final = 5
    """Between the logical groups in the row: branding, controls, ribbon,
    window buttons.

    Tight, because there are eight of these gaps and the ribbon is what they
    are competing with. Every pixel here is a pixel the workflow does not get,
    and at 1366 - the display the brief singles out - the nine stages fit with
    only a few to spare."""

    MENU_BUTTON_SIZE: Final = 32
    SMALL_BUTTON_SIZE: Final = 26
    """The density and previous/next buttons. Smaller than the menu button
    and still a comfortable target at 100% scaling."""

    LOGO_HEIGHT: Final = 24
    """The wordmark's height; its width follows from the artwork's own aspect
    ratio, so it is never stretched."""

    LOGO_MARGIN: Final = 6

    DRAG_HANDLE_WIDTH: Final = 24
    """Empty row between the workflow's next-stage arrow and the window
    buttons. Not decoration and not an accident of the layout: once the ribbon
    fills its viewport this is the one part of the row that is guaranteed to
    be empty, and therefore the one part guaranteed to be a drag handle."""

    WINDOW_BUTTON_WIDTH: Final = 44
    """Windows' own title-bar buttons are 45 logical pixels wide; matching
    them is what makes the custom row feel like a title bar rather than a
    toolbar pretending to be one."""

    WINDOW_BUTTON_GLYPH: Final = 10
    """Edge length of the painted minimise/maximise/close glyph."""

    RESIZE_BORDER: Final = 5
    """The frame around the central widget that belongs to the window itself,
    so a press there can start a native resize. Zeroed when maximised, where
    there is no edge to drag."""


class Footer:
    """Metrics for the status footer."""

    HEIGHT: Final = 30
    H_PADDING: Final = 12
    V_PADDING: Final = 4
    STATUS_DOT_SIZE: Final = 8
    STATUS_DOT_GAP: Final = 6
    SEPARATOR_MARGIN: Final = 8


class Card:
    """Metrics for the reusable content card."""

    PADDING: Final = 16
    GAP: Final = 12
    TITLE_GAP: Final = 10
    ROW_HEIGHT: Final = 52
    """A Getting Started entry's minimum height - enough for a heading and a
    supporting line without the row feeling cramped."""


class Dashboard:
    """Proportions for the Project page's two-column dashboard.

    Expressed as stretch factors and a width band rather than as pixel
    columns, so the split adapts to the window instead of reproducing the
    reference screenshot's exact dimensions.
    """

    MAIN_STRETCH: Final = 5
    SIDE_STRETCH: Final = 2

    SIDE_MIN_WIDTH: Final = 268
    """Below this the right-hand column stops being readable, and the page
    stacks instead of squeezing it into an unusable strip."""

    SIDE_MAX_WIDTH: Final = 400
    """Above this the column is just wasting width the main content would use
    better."""

    MAIN_MIN_WIDTH: Final = 420
    """The main column's own floor. Side-by-side needs this *plus*
    :data:`SIDE_MIN_WIDTH` plus the gap, which is what decides the stacking
    point - a measurement, not a screen-resolution breakpoint."""

    COLUMN_GAP: Final = 16


__all__ = [
    "Card",
    "Chrome",
    "Color",
    "Dashboard",
    "Density",
    "DensityLevel",
    "FontSize",
    "FontWeight",
    "Footer",
    "IconSize",
    "Navigator",
    "Radius",
    "Spacing",
    "Stroke",
]
