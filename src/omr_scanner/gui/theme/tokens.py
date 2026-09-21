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
    * :class:`Navigator`, :class:`Header`, :class:`Footer` - metrics for the
      three shell components that need fixed relationships between parts.

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

from typing import Final


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
    HEADER: Final = 20
    """The hamburger button's glyph."""

    NAV_STEP: Final = 20
    """A workflow step's icon, in the wide and medium layouts."""

    NAV_STEP_COMPACT: Final = 16
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
    TAGLINE: Final = -1
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


class Navigator:
    """Metrics for the horizontal workflow navigator.

    The one relationship that matters here is between :data:`ARROW_DEPTH` and
    :data:`STEP_H_PADDING`: the arrow eats into the step's box at both ends,
    so the padding has to clear it or the label collides with the chevron's
    point.
    """

    STEP_HEIGHT: Final = 44
    STEP_HEIGHT_COMPACT: Final = 38

    ARROW_DEPTH: Final = 14
    """How far the chevron's point projects beyond its step's own box, and
    equally how deep the notch on its left is. Successive steps overlap by
    exactly this much, which is what makes the row read as one connected
    process rather than as nine separate tiles."""

    ARROW_DEPTH_COMPACT: Final = 9

    STEP_H_PADDING: Final = 14
    """Inside the step, clear of the arrow geometry at both ends."""

    STEP_GAP: Final = 3
    """The visible seam between two chevrons. Small, and deliberately not
    zero: two accent-adjacent steps with no seam read as one wide shape."""

    ROW_GAP: Final = 4
    """Between the two rows of the medium layout."""

    MIN_LABEL_WIDTH: Final = 34
    """Below this much room for the label itself, a step is not worth
    rendering in the current mode and the navigator drops to the next one
    down. Not a font size floor - the font never shrinks; the *layout*
    changes."""

    BAND_V_PADDING: Final = 6
    BAND_H_PADDING: Final = 8

    SCROLL_STEP_MIN_WIDTH: Final = 108
    """A step's width in the scrollable last-resort layout, where steps no
    longer share out the available width but keep a readable size and the
    strip scrolls instead."""


class Header:
    """Metrics for the compact application header."""

    HEIGHT: Final = 48
    """Total band height. Deliberately close to a conventional menu bar's:
    the header replaces that row rather than adding to it, so the workflow
    navigator sits where the old menu bar's underside was."""

    H_PADDING: Final = 10
    V_PADDING: Final = 4
    MENU_BUTTON_SIZE: Final = 34
    LOGO_HEIGHT: Final = 26
    """The wordmark's height; its width follows from the artwork's own aspect
    ratio, so it is never stretched."""

    SEPARATOR_HEIGHT: Final = 22
    SEPARATOR_MARGIN: Final = 12
    TAGLINE_DOT_SIZE: Final = 4
    TAGLINE_LETTER_SPACING: Final = 1.4
    TAGLINE_GAP: Final = 7


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
    HERO_MAX_WIDTH: Final = 300


__all__ = [
    "Card",
    "Color",
    "Dashboard",
    "FontSize",
    "FontWeight",
    "Footer",
    "Header",
    "IconSize",
    "Navigator",
    "Radius",
    "Spacing",
    "Stroke",
]
